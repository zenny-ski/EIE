"""Queue actions, notifications, verification, KPIs, roles, webhook and ingestion details."""
import time
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from eie.app import create_app
from eie.config import Settings
from eie.models import Category
from eie.review import ReviewError
from eie.web import create_api
from test_engine import FULL_TRIP, email, make_pipeline, result


def make_app(tmp_path, **kw):
    return create_app(Settings(db_path=str(tmp_path / "t.db"), use_llm=False, dry_run=True, **kw))


def seed(app):
    """feedback (auto), escalation (mid -> form), booking (HITL), junk (CCP queue)."""
    emails = [email(1, "fb"), email(2, "esc"), email(3, "book"), email(4, "junk")]
    results = {"fb": result(Category.FEEDBACK, 95), "esc": result(Category.ESCALATION, 70),
               "book": result(Category.NEW_BOOKING, 90), "junk": result(Category.UNCLASSIFIED, 20)}
    return {r.subject: r for r in make_pipeline(app, emails, results).run_once()}


# ---- requester verification (FR-007) ----

def test_requester_verification(tmp_path):
    app = make_app(tmp_path)
    app.store.replace_contacts([{"booking_id": "bk-1234", "role": "booker", "name": "Asha", "email": "A@B.com"}])
    ok = make_pipeline(app, [email(1, "mine")], {"mine": result(Category.FEEDBACK, 95)}).run_once()[0]
    row = app.store.get_email(ok.email_id)
    assert row["requester_verified"] == 1 and "booker" in row["requester_detail"]

    stranger = email(2, "other")
    stranger.sender = "someone@else.com"
    out = make_pipeline(app, [stranger], {"other": result(Category.FEEDBACK, 95)}).run_once()[0]
    assert app.store.get_email(out.email_id)["requester_verified"] == 0

    nobooking = result(Category.FEEDBACK, 95)
    nobooking.extracted.booking_ids = []
    out = make_pipeline(app, [email(3, "none")], {"none": nobooking}).run_once()[0]
    assert app.store.get_email(out.email_id)["requester_verified"] is None  # not checkable, not a failure
    events = [a["event"] for a in app.store.audit_entries(ok.email_id)]
    assert "requester.checked" in events
    app.store.close()


def test_booking_contacts_endpoint(tmp_path):
    app = make_app(tmp_path)
    api = TestClient(create_api(app))
    res = api.post("/api/booking-contacts", json=[{"booking_id": "BK-1234", "email": "a@b.com"}])
    assert res.json() == {"stored": 1}
    assert app.store.contacts_for(["BK-1234"])[0]["email"] == "a@b.com"
    app.store.close()


# ---- queue actions (FR-035, 8.3) ----

def test_claim_dismiss_and_forward(tmp_path):
    app = make_app(tmp_path, forward_teams="Invoicing=inv@x.com, IT=it@x.com")
    out = seed(app)
    junk = out["junk"].review_id

    app.reviews.claim(junk, "carol")
    assert app.store.get_review(junk)["assigned_to"] == "carol"
    assert app.store.list_emails(assignee="carol")[0]["id"] == out["junk"].email_id

    with pytest.raises(ReviewError):
        app.reviews.forward(junk, "carol", "Legal")  # not a configured team
    with pytest.raises(ReviewError):
        app.reviews.forward(out["esc"].review_id, "carol", "Invoicing")  # only queue items can be forwarded
    sent = app.reviews.forward(junk, "carol", "Invoicing", "invoice request")
    assert sent["to"] == "inv@x.com" and sent["transport"] == "dry_run"
    email_row = app.store.get_email(out["junk"].email_id)
    assert email_row["status"] == "forwarded"
    assert app.store.get_review(junk)["status"] == "resolved"
    events = [a["event"] for a in app.store.audit_entries(out["junk"].email_id)]
    assert "review.forwarded" in events and "review.claimed" in events

    with pytest.raises(ReviewError):
        app.reviews.dismiss(out["esc"].review_id, "carol", "  ")  # a reason is required
    app.reviews.dismiss(out["esc"].review_id, "carol", "duplicate")
    assert app.store.get_email(out["esc"].email_id)["status"] == "dismissed"
    with pytest.raises(ReviewError):
        app.reviews.dismiss(out["esc"].review_id, "carol", "again")  # already closed
    app.store.close()


def test_failed_forward_keeps_item_pending(tmp_path, monkeypatch):
    app = make_app(tmp_path, forward_teams="IT=it@x.com")
    junk = seed(app)["junk"]

    def boom(*a, **k):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(app.reviews.mailer, "send_message", boom)
    with pytest.raises(RuntimeError):
        app.reviews.forward(junk.review_id, "carol", "IT")
    assert app.store.get_review(junk.review_id)["status"] == "pending"
    assert "forward.failed" in [a["event"] for a in app.store.audit_entries(junk.email_id)]
    app.store.close()


# ---- notifications (FR-019, FR-020, 13.1) ----

def notifications(app, email_id):
    return [a for a in app.store.audit_entries(email_id) if a["event"].startswith("notification.")]


def test_queue_notifications(tmp_path):
    app = make_app(tmp_path, notify_ccp="mgr@x.com, exec@x.com", notify_booking="ops@x.com")
    out = seed(app)
    junk = notifications(app, out["junk"].email_id)
    assert junk[0]["event"] == "notification.sent"
    assert junk[0]["details"]["recipients"] == ["mgr@x.com", "exec@x.com"]
    assert notifications(app, out["book"].email_id)[0]["details"]["recipients"] == ["ops@x.com"]
    assert notifications(app, out["esc"].email_id) == []  # a quick-create form needs no notification
    assert notifications(app, out["fb"].email_id) == []
    app.store.close()


def test_booking_notifications_default_to_ccp_list(tmp_path):
    app = make_app(tmp_path, notify_ccp="mgr@x.com")
    book = seed(app)["book"]
    assert notifications(app, book.email_id)[0]["details"]["recipients"] == ["mgr@x.com"]
    app.store.close()


def test_failed_notification_does_not_stop_processing(tmp_path, monkeypatch):
    app = make_app(tmp_path, notify_ccp="mgr@x.com")

    def boom(*a, **k):
        raise RuntimeError("mail server down")

    monkeypatch.setattr(app.notifier.mailer, "send_message", boom)
    junk = seed(app)["junk"]
    assert junk.status == "pending_review"
    assert notifications(app, junk.email_id)[0]["event"] == "notification.failed"
    app.store.close()


def test_ingestion_alert_is_audited_always_and_emailed_once(tmp_path):
    app = make_app(tmp_path, notify_ccp="mgr@x.com")
    for _ in range(3):
        app.notifier.alert("Mailbox ingestion is failing", "Graph and IMAP both down")
    events = [a["event"] for a in app.store.audit_entries(limit=50)]
    assert events.count("ingestion.failed") == 3 and events.count("notification.sent") == 1
    app.store.close()


def test_autofetcher_reports_errors():
    from eie.autofetch import AutoFetcher

    seen = []

    def build():
        raise RuntimeError("no connection")

    fetcher = AutoFetcher(build, interval=1, on_error=seen.append)
    fetcher.start()
    deadline = time.time() + 4
    while not seen and time.time() < deadline:
        time.sleep(0.1)
    fetcher.stop()
    assert seen and "no connection" in str(seen[0]) and fetcher.last_error


# ---- dashboard KPIs and filters (FR-017, 11.1) ----

def test_kpis_and_filters(tmp_path):
    app = make_app(tmp_path)
    api = TestClient(create_api(app))
    out = seed(app)
    api.post(f"/api/emails/{out['fb'].email_id}/reply", json={"user": "a", "body": "thanks"})

    k = api.get("/api/kpis").json()
    assert k["total_ingested"] == 4 and k["auto_routed"] == 1
    assert k["pending_confirmation"] == 2          # the escalation form and the booking
    assert k["unclassified_unreviewed"] == 1       # the junk email
    assert k["reply_rate"] == 25 and k["avg_time_to_action_seconds"] is not None

    api.post(f"/api/reviews/{out['junk'].review_id}/dismiss", json={"reviewer": "a", "reason": "spam"})
    assert api.get("/api/kpis").json()["unclassified_unreviewed"] == 0

    def ids(**params):
        return {e["id"] for e in api.get("/api/emails", params=params).json()}

    assert ids(category="feedback") == {out["fb"].email_id}
    assert ids(status_group="auto_routed") == {out["fb"].email_id}
    assert ids(status_group="pending") == {out["esc"].email_id, out["book"].email_id}
    assert ids(status_group="actioned") == {out["junk"].email_id}
    assert ids(tier="high") >= {out["fb"].email_id}
    assert ids(tier="medium") == {out["esc"].email_id}
    assert ids(date_from="2999-01-01") == set() and len(ids(date_from="2000-01-01", date_to="2999-01-01")) == 4
    api.post(f"/api/reviews/{out['esc'].review_id}/claim", json={"reviewer": "carol"})
    assert ids(assignee="carol") == {out["esc"].email_id}
    assert api.get("/api/emails", params={"status_group": "nope"}).status_code == 400
    assert api.get("/api/kpis", params={"date_from": "2999-01-01"}).json()["total_ingested"] == 0
    app.store.close()


# ---- roles (FRD 14) ----

USERS = "maya:module_manager:tok-m,esha:escalation_manager:tok-e,ada:admin:tok-a"


def as_user(api, token):
    return {"headers": {"X-EIE-Token": token}}


def test_login_required_and_identity_comes_from_token(tmp_path):
    app = make_app(tmp_path, users=USERS)
    api = TestClient(create_api(app))
    seed(app)
    assert api.get("/api/summary").status_code == 401
    assert api.get("/api/summary", headers={"X-EIE-Token": "wrong"}).status_code == 401
    me = api.get("/api/me", headers={"X-EIE-Token": "tok-m"}).json()
    assert me["name"] == "maya" and me["role"] == "module_manager" and me["can"]["reply"]
    assert api.get("/api/config").status_code == 200  # nothing sensitive: the login screen can use it

    junk = next(r for r in api.get("/api/reviews", headers={"X-EIE-Token": "tok-m"}).json()
                if r["kind"] == "ccp_review")
    api.post(f"/api/reviews/{junk['id']}/claim", json={"reviewer": "someone-else"}, headers={"X-EIE-Token": "tok-m"})
    assert app.store.get_review(junk["id"])["assigned_to"] == "maya"  # the typed name is ignored
    app.store.close()


def test_escalation_manager_is_restricted(tmp_path):
    app = make_app(tmp_path, users=USERS)
    api = TestClient(create_api(app))
    out = seed(app)
    h = {"X-EIE-Token": "tok-e"}

    queue = api.get("/api/reviews", headers=h).json()
    assert [r["id"] for r in queue] == [out["esc"].review_id]  # no booking approval, no CCP queue
    assert api.get(f"/api/reviews/{out['book'].review_id}", headers=h).status_code == 403
    assert api.get(f"/api/reviews/{out['junk'].review_id}", headers=h).status_code == 403
    assert api.post(f"/api/reviews/{out['book'].review_id}/approve", json={}, headers=h).status_code == 403
    assert api.post(f"/api/reviews/{out['esc'].review_id}/approve", json={"category": "new_booking"},
                    headers=h).status_code == 403  # can't re-route into a booking

    assert {e["id"] for e in api.get("/api/emails", headers=h).json()} == {out["esc"].email_id}
    assert api.get(f"/api/emails/{out['esc'].email_id}", headers=h).status_code == 200
    assert api.get(f"/api/emails/{out['book'].email_id}", headers=h).status_code == 403
    assert {a["email_id"] for a in api.get("/api/audit", headers=h).json() if a["email_id"]} == {out["esc"].email_id}

    body = {"user": "x", "body": "hello"}
    assert api.post(f"/api/emails/{out['esc'].email_id}/reply", json=body, headers=h).status_code == 403
    assert api.post("/api/demo", headers=h).status_code == 403
    assert api.post(f"/api/reviews/{out['esc'].review_id}/dismiss", json={"reason": "x"}, headers=h).status_code == 403

    approved = api.post(f"/api/reviews/{out['esc'].review_id}/approve", json={}, headers=h)
    assert approved.status_code == 200 and approved.json()["destination"] == "escalation_system"
    assert any(a["actor"] == "esha" for a in app.store.audit_entries(out["esc"].email_id))
    app.store.close()


def test_module_manager_and_admin_rights(tmp_path):
    app = make_app(tmp_path, users=USERS)
    api = TestClient(create_api(app))
    out = seed(app)
    m, a = {"X-EIE-Token": "tok-m"}, {"X-EIE-Token": "tok-a"}
    assert len(api.get("/api/reviews", headers=m).json()) == 3
    assert api.post(f"/api/emails/{out['fb'].email_id}/reply", json={"body": "hi"}, headers=m).status_code == 200
    contacts = [{"booking_id": "BK-1", "email": "a@b.com"}]
    assert api.post("/api/booking-contacts", json=contacts, headers=m).status_code == 403
    assert api.post("/api/booking-contacts", json=contacts, headers=a).status_code == 200
    app.store.close()


def test_bad_user_spec_is_rejected():
    from eie.auth import Auth

    with pytest.raises(ValueError):
        Auth("bob:wizard:tok")
    with pytest.raises(ValueError):
        Auth("just-a-name")
    assert not Auth("").enabled


# ---- Graph webhook (FR-002) ----

def test_graph_webhook(tmp_path):
    app = make_app(tmp_path, graph_webhook_secret="s3cret")
    api = TestClient(create_api(app))
    handshake = api.post("/api/graph/webhook", params={"validationToken": "abc 123"})
    assert handshake.status_code == 200 and handshake.text == "abc 123"
    assert api.post("/api/graph/webhook", json={"value": [{"clientState": "nope"}]}).status_code == 401
    assert api.post("/api/graph/webhook", json={"value": [{"clientState": "s3cret"}]}).status_code == 202
    deadline = time.time() + 3  # the fetch runs in the background; with no mailbox it raises an alert
    while time.time() < deadline and "ingestion.failed" not in [a["event"] for a in app.store.audit_entries()]:
        time.sleep(0.05)
    assert "ingestion.failed" in [a["event"] for a in app.store.audit_entries()]
    app.store.close()


# ---- ingestion details (FR-003, threads) ----

def test_imap_extracts_attachments_and_thread_root():
    from eie.ingestion.imap import ImapSource

    msg = EmailMessage()
    msg["Message-ID"] = "<r2@x>"
    msg["From"] = "A <a@b.com>"
    msg["Subject"] = "Re: trip"
    msg["References"] = "<root@x> <r1@x>"
    msg["In-Reply-To"] = "<r1@x>"
    msg.set_content("see attached")
    msg.add_attachment(b"%PDF-1.4 data", maintype="application", subtype="pdf", filename="invoice.pdf")
    src = ImapSource(Settings(imap_host="h", imap_user="u", imap_password="p"))
    parsed = src._to_email(msg, "7")
    assert parsed.conversation_id == "<root@x>"
    assert parsed.attachments == [{"name": "invoice.pdf", "content_type": "application/pdf", "size": 13,
                                   "inline": False, "text": ""}]  # a PDF with no readable text layer


def test_graph_message_parsing_keeps_attachments():
    from eie.ingestion.graph import GraphSource

    src = GraphSource.__new__(GraphSource)  # skip the MSAL client: no network in tests
    parsed = src._to_email({
        "id": "g1", "internetMessageId": "<m@x>", "conversationId": "conv", "subject": "S",
        "from": {"emailAddress": {"name": "A", "address": "a@b.com"}}, "receivedDateTime": "2026-01-01T00:00:00Z",
        "body": {"contentType": "text", "content": "hi"},
        "attachments": [{"name": "doc.pdf", "contentType": "application/pdf", "size": 10, "isInline": False}],
    })
    assert parsed.attachments == [{"id": None, "name": "doc.pdf", "content_type": "application/pdf", "size": 10,
                                   "inline": False}]  # the id is only used to download the text, then dropped


def test_attachments_are_stored_and_shown(tmp_path):
    import json

    app = make_app(tmp_path)
    e = email(1, "fb")
    e.attachments = [{"name": "photo.jpg", "content_type": "image/jpeg", "size": 5, "inline": False}]
    out = make_pipeline(app, [e], {"fb": result(Category.FEEDBACK, 95)}).run_once()[0]
    row = app.store.get_email(out.email_id)
    assert json.loads(row["attachments_json"])[0]["name"] == "photo.jpg"
    app.store.close()
