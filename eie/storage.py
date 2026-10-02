"""SQLite persistence: processed emails, the human review queue and the audit trail."""
import functools
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, Optional

from .models import ClassificationResult, Email, RoutingDecision

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      TEXT NOT NULL UNIQUE,
    source          TEXT NOT NULL,
    source_ref      TEXT,
    conversation_id TEXT,
    sender          TEXT,
    subject         TEXT,
    received_at     TEXT,
    body            TEXT,
    category        TEXT,
    confidence      INTEGER,
    tier            TEXT,
    action          TEXT,
    destination     TEXT,
    classifier      TEXT,
    extracted_json  TEXT,
    suggested_reply TEXT,
    external_ref    TEXT,
    status          TEXT NOT NULL DEFAULT 'ingested',
    parent_email_id INTEGER REFERENCES emails(id),  -- the thread's earlier email this one was appended to / split from
    thread_role     TEXT,                           -- appended | split | possible_split
    requester_verified INTEGER,                     -- NULL = could not be checked, 1 = matched, 0 = no match
    requester_detail   TEXT,
    attachments_json   TEXT,
    body_html          TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id     INTEGER NOT NULL REFERENCES emails(id),
    kind         TEXT NOT NULL,              -- review_form | booking_approval | ccp_review
    destination  TEXT NOT NULL,
    payload_json TEXT NOT NULL,              -- pre-filled form data
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected | resolved | dismissed
    reason       TEXT,
    assigned_to  TEXT,                       -- who claimed it ("In Review")
    system_error INTEGER NOT NULL DEFAULT 0, -- 1 = the classifier failed (distinct from low confidence)
    reviewer     TEXT,
    notes        TEXT,
    external_ref TEXT,
    created_at   TEXT NOT NULL,
    resolved_at  TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    email_id     INTEGER,
    review_id    INTEGER,
    event        TEXT NOT NULL,
    actor        TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id     INTEGER NOT NULL REFERENCES emails(id),
    to_addr      TEXT NOT NULL,
    subject      TEXT NOT NULL,
    body         TEXT NOT NULL,
    edited       INTEGER NOT NULL DEFAULT 0,      -- 1 when the sender changed the AI draft
    status       TEXT NOT NULL,                   -- sent | dry_run | failed
    sent_by      TEXT NOT NULL,
    transport    TEXT,                            -- graph | smtp | dry_run
    external_ref TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL
);

-- Known passenger / booker / additional contacts per booking, for requester verification (FR-007)
CREATE TABLE IF NOT EXISTS booking_contacts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id TEXT NOT NULL,
    role       TEXT NOT NULL DEFAULT 'passenger',   -- passenger | booker | contact
    name       TEXT,
    email      TEXT,
    phone      TEXT
);

-- Admin settings overrides ("setting:<key>") and small bits of state such as the Graph subscription
CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_by TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_contacts_booking ON booking_contacts(booking_id);
CREATE INDEX IF NOT EXISTS idx_review_status ON review_items(status);
CREATE INDEX IF NOT EXISTS idx_reply_email ON replies(email_id);
CREATE INDEX IF NOT EXISTS idx_audit_email ON audit_log(email_id);

-- The audit trail is append-only.
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


# Dashboard status groups (FR-017) as SQL conditions on `emails e`.
_PENDING = "EXISTS (SELECT 1 FROM review_items r WHERE r.email_id = e.id AND r.status = 'pending' AND r.kind {})"
STATUS_GROUPS = {
    "auto_routed": "e.status = 'auto_created'",
    "pending": _PENDING.format("IN ('review_form', 'booking_approval')"),
    "unclassified": _PENDING.format("IN ('ccp_review', 'possible_split')"),
    "actioned": "e.status IN ('approved', 'rejected', 'resolved', 'forwarded', 'dismissed', 'appended')",
}


MAX_HTML_CHARS = 500_000


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: str, tz: str = "UTC"):
        self.tz = ZoneInfo(tz)  # the calendar used for "today", date filters and KPI periods
        # check_same_thread=False lets the web server's worker threads and the auto-fetch thread
        # share the connection; every public method holds self._lock (see _make_thread_safe below).
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Adds columns introduced after a database was first created."""
        added = {
            "emails": (("parent_email_id", "INTEGER REFERENCES emails(id)"), ("thread_role", "TEXT"),
                       ("requester_verified", "INTEGER"), ("requester_detail", "TEXT"),
                       ("attachments_json", "TEXT"), ("body_html", "TEXT")),
            "review_items": (("assigned_to", "TEXT"), ("system_error", "INTEGER NOT NULL DEFAULT 0")),
        }
        for table, columns in added.items():
            have = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for column, ddl in columns:
                if column not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def close(self) -> None:
        self.conn.close()

    # ---- audit ----

    def audit(self, event: str, actor: str = "system", *, email_id: Optional[int] = None,
              review_id: Optional[int] = None, **details: Any) -> None:
        self.conn.execute(
            "INSERT INTO audit_log (ts, email_id, review_id, event, actor, details_json) VALUES (?, ?, ?, ?, ?, ?)",
            (now(), email_id, review_id, event, actor, json.dumps(details, default=str)),
        )
        self.conn.commit()

    def audit_entries(self, email_id: Optional[int] = None, limit: int = 100) -> list[dict]:
        """The most recent `limit` entries (all entries for one email), oldest first."""
        if email_id is None:
            rows = self.conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            rows.reverse()
        else:
            rows = self.conn.execute("SELECT * FROM audit_log WHERE email_id = ? ORDER BY id", (email_id,))
        return [_row(r, "details_json") for r in rows]

    # ---- emails ----

    # ---- key/value state ----

    def kv_get(self, key: str):
        row = self.conn.execute("SELECT value_json FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def kv_set(self, key: str, value, user: str = "system") -> None:
        self.conn.execute(
            "INSERT INTO kv (key, value_json, updated_by, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_by = excluded.updated_by, "
            "updated_at = excluded.updated_at", (key, json.dumps(value), user, now()))
        self.conn.commit()

    def kv_delete(self, key: str) -> None:
        self.conn.execute("DELETE FROM kv WHERE key = ?", (key,))
        self.conn.commit()

    def kv_prefix(self, prefix: str) -> dict:
        rows = self.conn.execute("SELECT key, value_json FROM kv WHERE key LIKE ?", (prefix + "%",))
        return {k[len(prefix):]: json.loads(v) for k, v in rows}

    # ---- dates in the configured timezone ----

    def period_start(self, period: str) -> Optional[str]:
        """First local day (YYYY-MM-DD) of a dashboard period: today | 7 | 30 days; None for all time."""
        if period in (None, "", "all"):
            return None
        days = {"today": 1, "7": 7, "30": 30}.get(period)
        if days is None:
            raise ValueError(f"Unknown period {period!r}")
        return (datetime.now(self.tz).date() - timedelta(days=days - 1)).isoformat()

    def _created_between(self, date_from: Optional[str], date_to: Optional[str]) -> tuple[list, list]:
        """SQL conditions for emails ingested from the start of `date_from` to the end of `date_to`, both
        inclusive local days. created_at is stored in UTC, so the local days are converted to UTC bounds."""
        def utc(day: str, plus_days: int = 0) -> str:
            local = datetime.fromisoformat(day) + timedelta(days=plus_days)
            return local.replace(tzinfo=self.tz).astimezone(timezone.utc).isoformat(timespec="seconds")

        conditions, args = [], []
        if date_from:
            conditions.append("e.created_at >= ?")
            args.append(utc(date_from))
        if date_to:
            conditions.append("e.created_at < ?")
            args.append(utc(date_to, 1))
        return conditions, args

    def email_exists(self, message_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM emails WHERE message_id = ?", (message_id,)).fetchone() is not None

    def insert_email(self, email: Email) -> int:
        ts = now()
        cur = self.conn.execute(
            """INSERT INTO emails (message_id, source, source_ref, conversation_id, sender, subject,
                                   received_at, body, attachments_json, body_html, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (email.message_id, email.source, email.source_ref, email.conversation_id, email.sender,
             email.subject, email.received_at, email.body,
             json.dumps(email.attachments) if email.attachments else None,
             (email.body_html or None) and email.body_html[:MAX_HTML_CHARS], ts, ts),
        )
        self.conn.commit()
        return cur.lastrowid

    def save_classification(self, email_id: int, result: ClassificationResult, decision: RoutingDecision) -> None:
        self.conn.execute(
            """UPDATE emails SET category = ?, confidence = ?, tier = ?, action = ?, destination = ?,
                   classifier = ?, extracted_json = ?, suggested_reply = ?, status = 'classified', updated_at = ?
               WHERE id = ?""",
            (result.category.value, result.confidence, decision.tier.value, decision.action.value,
             decision.destination.value, result.classifier, result.extracted.model_dump_json(),
             result.suggested_reply, now(), email_id),
        )
        self.conn.commit()

    def set_email_status(self, email_id: int, status: str, external_ref: Optional[str] = None) -> None:
        self.conn.execute(
            "UPDATE emails SET status = ?, external_ref = COALESCE(?, external_ref), updated_at = ? WHERE id = ?",
            (status, external_ref, now(), email_id),
        )
        self.conn.commit()

    # ---- threads ----

    def thread_records(self, conversation_id: str, exclude_id: int) -> list[dict]:
        """Earlier emails in the thread that have (or will have) their own downstream record, oldest first:
        classified into a real bucket, not appended to another record, not rejected or closed."""
        if not conversation_id:
            return []
        rows = self.conn.execute(
            """SELECT * FROM emails WHERE conversation_id = ? AND id != ?
                 AND category IN ('feedback', 'escalation', 'new_booking')
                 AND COALESCE(thread_role, '') NOT IN ('appended', 'linked', 'possible_split')
                 AND status NOT IN ('rejected', 'resolved') ORDER BY id""", (conversation_id, exclude_id))
        return [_row(r, "extracted_json") for r in rows]

    def set_thread_link(self, email_id: int, parent_email_id: Optional[int], role: Optional[str]) -> None:
        self.conn.execute("UPDATE emails SET parent_email_id = ?, thread_role = ?, updated_at = ? WHERE id = ?",
                          (parent_email_id, role, now(), email_id))
        self.conn.commit()

    def thread_children(self, email_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, subject, category, status, thread_role FROM emails WHERE parent_email_id = ? ORDER BY id",
            (email_id,))
        return [dict(r) for r in rows]

    def get_email(self, email_id: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
        return _row(row, "extracted_json") if row else None

    def list_emails(self, limit: int = 50, *, category: Optional[str] = None, tier: Optional[str] = None,
                    status_group: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, assignee: Optional[str] = None) -> list[dict]:
        """Newest first. `status_group` is one of STATUS_GROUPS (FR-017); dates are YYYY-MM-DD (ingestion day)."""
        where, args = ["1=1"], []
        for column, value in (("e.category", category), ("e.tier", tier)):
            if value:
                where.append(f"{column} = ?")
                args.append(value)
        if status_group:
            if status_group not in STATUS_GROUPS:
                raise ValueError(f"Unknown status group {status_group!r}")
            where.append(STATUS_GROUPS[status_group])
        date_conditions, date_args = self._created_between(date_from, date_to)
        where += date_conditions
        args += date_args
        if assignee:
            where.append("EXISTS (SELECT 1 FROM review_items r WHERE r.email_id = e.id "
                         "AND (r.assigned_to = ? OR r.reviewer = ?))")
            args += [assignee, assignee]
        sql = f"""SELECT e.*, (SELECT r.assigned_to FROM review_items r WHERE r.email_id = e.id
                              ORDER BY r.id DESC LIMIT 1) AS assigned_to
                  FROM emails e WHERE {' AND '.join(where)} ORDER BY e.id DESC LIMIT ?"""
        rows = [_row(r, "extracted_json") for r in self.conn.execute(sql, args + [limit])]
        for row in rows:
            row.pop("body_html", None)  # large, and only the detail page shows it
        return rows

    def set_suggested_reply(self, email_id: int, draft: str) -> None:
        self.conn.execute("UPDATE emails SET suggested_reply = ? WHERE id = ?", (draft, email_id))
        self.conn.commit()

    def set_requester(self, email_id: int, verified: Optional[bool], detail: dict) -> None:
        self.conn.execute("UPDATE emails SET requester_verified = ?, requester_detail = ? WHERE id = ?",
                          (None if verified is None else int(verified), json.dumps(detail), email_id))
        self.conn.commit()

    # ---- booking contacts (requester verification) ----

    def replace_contacts(self, contacts: list[dict]) -> int:
        """Replaces the contacts of every booking mentioned in `contacts`; returns how many were stored."""
        for booking_id in {c["booking_id"].strip().upper() for c in contacts}:
            self.conn.execute("DELETE FROM booking_contacts WHERE booking_id = ?", (booking_id,))
        self.conn.executemany(
            "INSERT INTO booking_contacts (booking_id, role, name, email, phone) VALUES (?, ?, ?, ?, ?)",
            [(c["booking_id"].strip().upper(), c.get("role") or "passenger", c.get("name"),
              (c.get("email") or "").strip().lower() or None, c.get("phone")) for c in contacts])
        self.conn.commit()
        return len(contacts)

    def contacts_for(self, booking_ids: list[str]) -> list[dict]:
        if not booking_ids:
            return []
        marks = ",".join("?" * len(booking_ids))
        rows = self.conn.execute(f"SELECT * FROM booking_contacts WHERE booking_id IN ({marks})",
                                 [b.upper() for b in booking_ids])
        return [dict(r) for r in rows]

    # ---- replies ----

    def add_reply(self, email_id: int, to_addr: str, subject: str, body: str, *, edited: bool, status: str,
                  sent_by: str, transport: Optional[str] = None, external_ref: Optional[str] = None,
                  error: Optional[str] = None) -> int:
        cur = self.conn.execute(
            """INSERT INTO replies (email_id, to_addr, subject, body, edited, status, sent_by, transport,
                                    external_ref, error, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (email_id, to_addr, subject, body, int(edited), status, sent_by, transport, external_ref, error, now()),
        )
        self.conn.commit()
        return cur.lastrowid

    def list_replies(self, email_id: int) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM replies WHERE email_id = ? ORDER BY id", (email_id,))
        return [dict(r) for r in rows]

    # ---- review queue ----

    def add_review(self, email_id: int, kind: str, destination: str, payload: dict, reason: str,
                   system_error: bool = False) -> int:
        cur = self.conn.execute(
            """INSERT INTO review_items (email_id, kind, destination, payload_json, reason, system_error, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (email_id, kind, destination, json.dumps(payload, default=str), reason, int(system_error), now()),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_review(self, review_id: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM review_items WHERE id = ?", (review_id,)).fetchone()
        return _row(row, "payload_json") if row else None

    def list_reviews(self, status: Optional[str] = "pending", kind: Optional[str] = None,
                     email_id: Optional[int] = None) -> list[dict]:
        sql = """SELECT r.*, e.subject, e.sender, e.category, e.confidence, e.requester_verified, e.classifier
                 FROM review_items r JOIN emails e ON e.id = r.email_id WHERE 1=1"""
        args: list = []
        for column, value in (("r.status", status), ("r.kind", kind), ("r.email_id", email_id)):
            if value is not None:
                sql += f" AND {column} = ?"
                args.append(value)
        rows = self.conn.execute(sql + " ORDER BY r.id", args)
        return [_row(r, "payload_json") for r in rows]

    def summary(self) -> dict:
        def counts(sql: str) -> dict:
            return {k or "none": v for k, v in self.conn.execute(sql)}

        return {
            "total_emails": self.conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0],
            "by_status": counts("SELECT status, COUNT(*) FROM emails GROUP BY status"),
            "by_category": counts("SELECT category, COUNT(*) FROM emails GROUP BY category"),
            "by_action": counts("SELECT action, COUNT(*) FROM emails GROUP BY action"),
            "pending_reviews": counts("SELECT kind, COUNT(*) FROM review_items WHERE status = 'pending' GROUP BY kind"),
        }

    def kpis(self, date_from: Optional[str] = None, date_to: Optional[str] = None,
             period: Optional[str] = None) -> dict:
        """The dashboard cards (FRD 11.1) for emails ingested in the period: either `period` (today, 7, 30,
        all) or inclusive local YYYY-MM-DD dates."""
        if period:
            date_from, date_to = self.period_start(period), None
        conditions, args = self._created_between(date_from, date_to)
        where = " AND ".join(["1=1"] + conditions)

        def count(extra: str) -> int:
            return self.conn.execute(f"SELECT COUNT(*) FROM emails e WHERE {where} AND {extra}", args).fetchone()[0]

        total = count("1=1")
        done = self.conn.execute(
            f"""SELECT AVG((julianday(COALESCE((SELECT MAX(r.resolved_at) FROM review_items r
                                                WHERE r.email_id = e.id AND r.status != 'pending'), e.updated_at))
                            - julianday(e.created_at)) * 86400)
                FROM emails e WHERE {where} AND ({STATUS_GROUPS['actioned']} OR e.status = 'auto_created')""",
            args).fetchone()[0]
        replied = count("EXISTS (SELECT 1 FROM replies p WHERE p.email_id = e.id AND p.status != 'failed')")
        return {
            "total_ingested": total,
            "auto_routed": count(STATUS_GROUPS["auto_routed"]),
            "pending_confirmation": count(STATUS_GROUPS["pending"]),
            "unclassified_unreviewed": count(STATUS_GROUPS["unclassified"]),
            "avg_time_to_action_seconds": round(done) if done is not None else None,
            "reply_rate": round(100 * replied / total) if total else None,
        }

    def claim_review(self, review_id: int, user: str) -> None:
        self.conn.execute("UPDATE review_items SET assigned_to = ? WHERE id = ?", (user, review_id))
        self.conn.commit()

    def retarget_review(self, review_id: int, destination: str, payload: dict) -> None:
        self.conn.execute("UPDATE review_items SET destination = ?, payload_json = ? WHERE id = ?",
                          (destination, json.dumps(payload, default=str), review_id))
        self.conn.commit()

    def resolve_review(self, review_id: int, status: str, reviewer: str, notes: str = "",
                       payload: Optional[dict] = None, external_ref: Optional[str] = None,
                       destination: Optional[str] = None) -> None:
        self.conn.execute(
            """UPDATE review_items SET status = ?, reviewer = ?, notes = ?, resolved_at = ?,
                   payload_json = COALESCE(?, payload_json), external_ref = ?,
                   destination = COALESCE(?, destination)
               WHERE id = ?""",
            (status, reviewer, notes, now(), json.dumps(payload, default=str) if payload is not None else None,
             external_ref, destination, review_id),
        )
        self.conn.commit()


def _make_thread_safe(cls):
    def locked(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            with self._lock:
                return fn(self, *args, **kwargs)
        return wrapper

    for name, fn in list(vars(cls).items()):
        if callable(fn) and not name.startswith("_"):
            setattr(cls, name, locked(fn))


_make_thread_safe(Store)


def _row(row: sqlite3.Row, json_field: str) -> dict:
    d = dict(row)
    raw = d.pop(json_field, None)
    d[json_field.removesuffix("_json")] = json.loads(raw) if raw else None
    return d
