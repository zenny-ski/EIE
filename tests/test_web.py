import pytest
from fastapi.testclient import TestClient

from eie.app import create_app
from eie.config import Settings
from eie.web import create_api


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "t.db"), use_llm=False, dry_run=True))
    yield TestClient(create_api(app))
    app.store.close()


def test_gui_flow(client):
    assert "Email Intelligence Engine" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200

    processed = client.post("/api/demo").json()["processed"]
    assert len(processed) == 5
    assert client.post("/api/demo").json()["processed"] == []  # idempotent

    summary = client.get("/api/summary").json()
    assert summary["total_emails"] == 5

    reviews = client.get("/api/reviews").json()
    booking = next(r for r in reviews if r["kind"] == "booking_approval")
    assert booking["subject"]

    detail = client.get(f"/api/reviews/{booking['id']}").json()
    blocked = client.post(f"/api/reviews/{booking['id']}/approve", json={"reviewer": "alice"})
    assert blocked.status_code == 422 and "Pickup location" in blocked.json()["detail"]
    assert "Pickup location" in detail["tiers"]["essential"]
    draft = client.post(f"/api/reviews/{booking['id']}/missing-details", json={}).json()
    assert "cannot be created without" in draft["body"] and "Pickup location" in draft["gaps"]["essential"]

    payload = detail["review"]["payload"] | {
        "company": "Initech India", "passengers": [{"name": "A. Rao", "phone": "9800000000"}],
        "trip": {"pickup_location": "BOM T2", "pickup_datetime": "2026-01-01T09:00"}}
    gap = client.post(f"/api/reviews/{booking['id']}/approve", json={"reviewer": "alice", "payload": payload})
    assert gap.status_code == 422 and "Cost centre" in gap.json()["detail"]  # Mandatory gap: needs acknowledging
    res = client.post(f"/api/reviews/{booking['id']}/approve",
                      json={"reviewer": "alice", "payload": payload, "acknowledge_gaps": True}).json()
    assert res["destination"] == "indecab"
    assert client.post(f"/api/reviews/{booking['id']}/approve", json={"reviewer": "alice"}).status_code == 409

    audit = client.get(f"/api/emails/{booking['email_id']}").json()["audit"]
    approved = next(a for a in audit if a["event"] == "review.approved")
    assert "Cost centre" in approved["details"]["acknowledged_gaps"]

    ccp = next(r for r in reviews if r["kind"] == "ccp_review")
    assert client.post(f"/api/reviews/{ccp['id']}/reject", json={"reviewer": " "}).status_code == 400
    assert client.post(f"/api/reviews/{ccp['id']}/approve",
                       json={"reviewer": "bob", "category": "escalation"}).json()["destination"] == "escalation_system"

    email = client.get(f"/api/emails/{booking['email_id']}").json()
    assert email["email"]["status"] == "approved"
    assert [a["event"] for a in email["audit"]][-1] == "review.approved"


def test_run_without_mailbox_reports_error(client):
    res = client.post("/api/run")
    assert res.status_code == 400
    assert "No email source configured" in res.json()["detail"]


def test_autofetch_toggle_requires_mailbox(client):
    status = client.get("/api/autofetch").json()
    assert status["enabled"] is False  # no mailbox configured in tests
    assert client.post("/api/autofetch", json={"enabled": True}).status_code == 400
    assert client.post("/api/autofetch", json={"enabled": False}).json()["enabled"] is False


def test_autofetcher_runs_pipeline(tmp_path):
    from eie.autofetch import AutoFetcher
    from eie.ingestion.file import JsonFileSource

    class OneShotSource(JsonFileSource):
        """Returns the samples once, then behaves like an empty inbox."""
        served = False

        def fetch_unread(self, limit):
            if self.served:
                return []
            self.served = True
            return super().fetch_unread(limit)

    app = create_app(Settings(db_path=str(tmp_path / "t.db"), use_llm=False, dry_run=True))
    source = OneShotSource("samples/sample_emails.json")
    fetcher = AutoFetcher(lambda: app.pipeline(source))
    assert len(fetcher.run_once()) == 5
    assert fetcher.run_once() == []
    assert fetcher.status()["processed_total"] == 5
    events = [a["event"] for a in app.store.audit_entries(limit=500)]
    assert events.count("fetch.completed") == 1  # the empty poll is not audited
    app.store.close()


def test_reply_flow(client, monkeypatch):
    client.post("/api/demo")
    email = client.get("/api/emails").json()[0]
    url = f"/api/emails/{email['id']}/reply"

    assert client.post(url, json={"user": " ", "body": "hi"}).status_code == 400
    assert client.post(url, json={"user": "alice", "body": "  "}).status_code == 400
    assert client.post(url, json={"user": "alice", "body": "hi", "to": "not-an-address"}).status_code == 400

    res = client.post(url, json={"user": "alice", "body": "Edited reply"}).json()
    assert res["status"] == "dry_run" and res["transport"] == "dry_run"

    assert client.post(url, json={"user": "alice", "body": "again"}).status_code == 409
    assert client.post(url, json={"user": "alice", "body": "again", "resend": True}).status_code == 200

    detail = client.get(f"/api/emails/{email['id']}").json()
    assert [r["status"] for r in detail["replies"]] == ["dry_run", "dry_run"]
    assert detail["replies"][0]["edited"] == 1
    assert detail["replies"][0]["subject"].startswith("Re: ")
    assert [a["event"] for a in detail["audit"]].count("reply.sent") == 2


def test_reply_failure_is_recorded(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "t.db"), use_llm=False, dry_run=False))  # live, no transport
    api = TestClient(create_api(app))
    api.post("/api/demo")
    eid = api.get("/api/emails").json()[0]["id"]
    res = api.post(f"/api/emails/{eid}/reply", json={"user": "alice", "body": "hello"})
    assert res.status_code == 400 and "Sending failed" in res.json()["detail"]
    detail = api.get(f"/api/emails/{eid}").json()
    assert detail["replies"][0]["status"] == "failed"
    assert "reply.failed" in [a["event"] for a in detail["audit"]]
    # a failed attempt does not block a later send
    app.replies.mailer.s = Settings(dry_run=True)
    assert api.post(f"/api/emails/{eid}/reply", json={"user": "alice", "body": "hello"}).status_code == 200
    app.store.close()


def test_preview_shows_rerouted_form_without_saving(client):
    client.post("/api/demo")
    ccp = next(r for r in client.get("/api/reviews").json() if r["kind"] == "ccp_review")
    preview = client.get(f"/api/reviews/{ccp['id']}/preview", params={"category": "new_booking"}).json()["payload"]
    assert "trip" in preview and "company" in preview
    stored = client.get(f"/api/reviews/{ccp['id']}").json()["review"]
    assert stored["destination"] == "ccp_queue" and "trip" not in stored["payload"]  # nothing was saved
    assert client.get(f"/api/reviews/{ccp['id']}/preview", params={"category": "unclassified"}).status_code == 409


def test_existing_database_is_migrated(tmp_path):
    import sqlite3
    from eie.storage import Store

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)  # the schema before thread columns existed
    conn.execute("CREATE TABLE emails (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL UNIQUE, "
                 "source TEXT NOT NULL, source_ref TEXT, conversation_id TEXT, sender TEXT, subject TEXT, "
                 "received_at TEXT, body TEXT, category TEXT, confidence INTEGER, tier TEXT, action TEXT, "
                 "destination TEXT, classifier TEXT, extracted_json TEXT, suggested_reply TEXT, external_ref TEXT, "
                 "status TEXT NOT NULL DEFAULT 'ingested', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.commit()
    conn.close()
    store = Store(str(db))
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(emails)")}
    assert {"parent_email_id", "thread_role"} <= cols
    store.close()
