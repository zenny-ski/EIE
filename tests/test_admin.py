"""Admin settings, configurable replies, pickup parsing, timezone dates and webhook renewal."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from eie.adminconfig import SettingsError
from eie.app import create_app
from eie.config import Settings
from eie.models import Category, Destination
from eie.routing import INDECAB_TIERS, derive_pickup, field_gaps
from eie.subscription import SubscriptionManager
from eie.web import create_api
from test_engine import email, make_pipeline, result


def make_app(tmp_path, name="t.db", **kw):
    return create_app(Settings(db_path=str(tmp_path / name), use_llm=False, dry_run=True, **kw))


# ---- thresholds: live, persisted, audited ----

def test_threshold_change_takes_effect_immediately_and_survives_restart(tmp_path):
    app = make_app(tmp_path)
    r = make_pipeline(app, [email(1, "fb")], {"fb": result(Category.FEEDBACK, 72)}).run_once()[0]
    assert r.status == "pending_review"  # 72 is below the default high threshold of 85

    assert app.admin.update({"confidence_high_feedback": 70}, "ada") == ["confidence_high_feedback"]
    r = make_pipeline(app, [email(2, "fb2")], {"fb2": result(Category.FEEDBACK, 72)}).run_once()[0]
    assert r.status == "auto_created"

    app.store.close()
    again = make_app(tmp_path)  # a new process: the saved value is applied on startup
    assert again.settings.confidence_high_feedback == 70
    assert again.admin.snapshot()["overridden"] == ["confidence_high_feedback"]
    again.admin.reset(["confidence_high_feedback"], "ada")
    assert again.settings.confidence_high_feedback == -1 and again.admin.snapshot()["overridden"] == []
    again.store.close()


def test_changes_are_audited_with_before_and_after(tmp_path):
    app = make_app(tmp_path)
    app.admin.update({"confidence_medium": 40, "notify_ccp": "a@x.com"}, "ada")
    app.admin.update({"confidence_medium": 40}, "ada")  # unchanged: no new entry
    entries = [a for a in app.store.audit_entries(limit=50) if a["event"] == "settings.changed"]
    assert {e["details"]["key"] for e in entries} == {"confidence_medium", "notify_ccp"}
    medium = next(e for e in entries if e["details"]["key"] == "confidence_medium")
    assert medium["actor"] == "ada" and medium["details"]["before"] == 50 and medium["details"]["after"] == 40
    app.store.close()


@pytest.mark.parametrize("changes, message", [
    ({"confidence_high": 101}, "between 0 and 100"),
    ({"confidence_high": "abc"}, "whole number"),
    ({"confidence_high": None}, "required"),
    ({"confidence_medium": 90}, "can't be above"),
    ({"confidence_high_escalation": 40}, "Escalation"),
    ({"notify_ccp": "good@x.com, not-an-email"}, "not a valid email"),
    ({"forward_teams": [{"name": "IT", "address": "nope"}]}, "valid email"),
    ({"forward_teams": [{"name": "A=B", "address": "a@x.com"}]}, "team name"),
    ({"forward_teams": [{"name": "IT", "address": "a@x.com"}, {"name": "it", "address": "b@x.com"}]}, "twice"),
    ({"reply_templates": {"feedback": "Hi {name.__class__}"}}, "unsupported placeholder"),
    ({"reply_templates": {"feedback": "Hi {name!r} {0} {}"}}, "unsupported placeholder"),
    ({"reply_templates": {"sales": "x"}}, "unknown entry"),
    ({"missing_templates": {"essential": "We need things."}}, "must contain {items}"),
    ({"indecab_tiers": [{"path": "trip.cost_center", "label": "Cost", "tier": "mandatory"}]}, "not a trip field"),
    ({"indecab_tiers": [{"path": "sender", "label": "Sender", "tier": "mandatory"}]}, "must start with"),
    ({"indecab_tiers": [{"path": "trip.concur_id", "label": "Concur", "tier": "optional"}]}, "must be one of"),
    ({"indecab_tiers": [{"path": "trip.concur_id", "label": "", "tier": "mandatory"}]}, "needs a label"),
    ({"nonsense": 1}, "Unknown setting"),
])
def test_invalid_settings_are_rejected_and_nothing_is_applied(tmp_path, changes, message):
    app = make_app(tmp_path)
    before = app.admin.snapshot()["values"]
    with pytest.raises(SettingsError, match=message.replace("{", r"\{").replace("}", r"\}")):
        app.admin.update({"notify_booking": "ok@x.com", **changes}, "ada")  # a valid change rides along
    assert app.admin.snapshot()["values"] == before  # all or nothing
    assert app.store.audit_entries(limit=20) == []
    app.store.close()


def test_blank_per_bucket_threshold_means_use_global(tmp_path):
    app = make_app(tmp_path, confidence_high_feedback=70)
    app.admin.update({"confidence_high_feedback": None}, "ada")
    assert app.settings.thresholds(Category.FEEDBACK) == (85, 50)
    app.store.close()


# ---- reply wording ----

def test_bucket_template_and_signature_shape_the_draft(tmp_path):
    app = make_app(tmp_path)
    app.admin.update({"reply_templates": {"feedback": "Hi {name}, thanks for {booking_id}! {unknown}\n[Agent Name]"},
                      "reply_signature": "Cabi Support Team"}, "ada")
    e = email(1, "fb")
    e.sender = "Asha Rao <asha@x.com>"
    out = make_pipeline(app, [e, email(2, "esc")],
                        {"fb": result(Category.FEEDBACK, 95), "esc": result(Category.ESCALATION, 70)}).run_once()
    fb = app.store.get_email(out[0].email_id)["suggested_reply"]
    assert fb == "Hi Asha, thanks for BK-1234! {unknown}\nCabi Support Team"  # unknown placeholders stay as written
    esc = app.store.get_email(out[1].email_id)["suggested_reply"]
    assert esc == "Hi"  # no template for escalation: the classifier's own draft ("Hi" in the test fixture)
    app.admin.update({"reply_templates": {"feedback": ""}, "reply_signature": ""}, "ada")
    again = make_pipeline(app, [email(3, "fb3")], {"fb3": result(Category.FEEDBACK, 95)}).run_once()[0]
    assert app.store.get_email(again.email_id)["suggested_reply"] == "Hi"
    app.store.close()


def test_assigning_a_bucket_regenerates_the_draft(tmp_path):
    app = make_app(tmp_path)
    junk = make_pipeline(app, [email(1, "junk")], {"junk": result(Category.UNCLASSIFIED, 20)}).run_once()[0]
    assert app.store.get_email(junk.email_id)["suggested_reply"] == "Hi"
    app.reviews.approve(junk.review_id, "carol", category=Category.FEEDBACK)  # FR-026
    draft = app.store.get_email(junk.email_id)["suggested_reply"]
    assert "Thank you for sharing your feedback" in draft  # the built-in feedback wording
    assert "reply.drafted" in [a["event"] for a in app.store.audit_entries(junk.email_id)]

    other = make_pipeline(app, [email(2, "junk2")], {"junk2": result(Category.UNCLASSIFIED, 20)}).run_once()[0]
    app.admin.update({"reply_templates": {"feedback": "Dear {name}, noted. [Agent Name]"},
                      "reply_signature": "Desk"}, "ada")
    app.reviews.approve(other.review_id, "carol", category=Category.FEEDBACK)
    assert app.store.get_email(other.email_id)["suggested_reply"] == "Dear Customer, noted. Desk"
    app.store.close()


def test_missing_details_wording_is_editable(tmp_path):
    app = make_app(tmp_path)
    api = TestClient(create_api(app))
    book = make_pipeline(app, [email(1, "book")], {"book": result(Category.NEW_BOOKING, 90)}).run_once()[0]
    app.admin.update({"missing_templates": {"essential": "Hello {name}, please send:\n{items}"}}, "ada")
    draft = api.post(f"/api/reviews/{book.review_id}/missing-details", json={}).json()["body"]
    assert draft.startswith("Hello Customer, please send:") and "- Pickup location" in draft
    app.store.close()


# ---- IndeCab tiers are editable ----

def test_tiers_can_be_edited_and_reset(tmp_path):
    app = make_app(tmp_path)
    payload = {"company": "C", "passengers": [{"name": "A", "phone": "1"}],
               "trip": {"pickup_location": "A", "pickup_date": "2026-10-05", "pickup_time": "09:00",
                        "drop_location": "B", "vehicle_type": "Sedan", "concur_id": "X"}}
    assert field_gaps(Destination.INDECAB, payload)["mandatory"] == ["Cost centre"]

    tiers = app.admin.snapshot()["values"]["indecab_tiers"]
    for row in tiers:
        if row["path"] == "trip.cost_centre":
            row["tier"] = "essential"  # this customer can't live without it
    tiers.append({"path": "trip.passenger_count", "label": "Passengers", "tier": "semi_mandatory"})
    app.admin.update({"indecab_tiers": tiers}, "ada")
    gaps = field_gaps(Destination.INDECAB, payload)
    assert gaps["essential"] == ["Cost centre"] and gaps["mandatory"] == []
    assert gaps["semi_mandatory"] == ["Flight or train number", "Passengers"]

    other = make_app(tmp_path, name="other.db")  # a different database starts from the defaults again
    assert field_gaps(Destination.INDECAB, payload)["essential"] == []
    other.store.close()
    app.store.close()

    reopened = make_app(tmp_path)  # and this one's saved tiers come back on restart
    assert field_gaps(Destination.INDECAB, payload)["essential"] == ["Cost centre"]
    reopened.admin.reset(["indecab_tiers"], "ada")
    assert field_gaps(Destination.INDECAB, payload)["essential"] == []
    reopened.store.close()


# ---- the API ----

def test_settings_api_is_admin_only(tmp_path):
    app = make_app(tmp_path, users="maya:module_manager:tm,ada:admin:ta")
    api = TestClient(create_api(app))
    m, a = {"X-EIE-Token": "tm"}, {"X-EIE-Token": "ta"}
    assert api.get("/api/admin/settings", headers=m).status_code == 403
    assert api.put("/api/admin/settings", json={"changes": {"notify_ccp": "a@x.com"}}, headers=m).status_code == 403
    snap = api.get("/api/admin/settings", headers=a).json()
    assert snap["values"]["confidence_medium"] == 50 and snap["buckets"] and "name" in snap["placeholders"]
    saved = api.put("/api/admin/settings", json={"changes": {"notify_ccp": "a@x.com", "confidence_medium": 45}},
                    headers=a).json()
    assert set(saved["changed"]) == {"notify_ccp", "confidence_medium"} and saved["values"]["notify_ccp"] == "a@x.com"
    bad = api.put("/api/admin/settings", json={"changes": {"confidence_medium": 999}}, headers=a)
    assert bad.status_code == 400 and "between 0 and 100" in bad.json()["detail"]
    reset = api.put("/api/admin/settings", json={"reset": ["notify_ccp"]}, headers=a).json()
    assert reset["changed"] == ["notify_ccp"] and reset["values"]["notify_ccp"] == ""
    assert any(x["actor"] == "ada" for x in app.store.audit_entries(limit=50) if x["event"].startswith("settings."))
    app.store.close()


def test_settings_change_alters_notifications_live(tmp_path):
    app = make_app(tmp_path)
    junk = make_pipeline(app, [email(1, "j1")], {"j1": result(Category.UNCLASSIFIED, 10)}).run_once()[0]
    assert not [a for a in app.store.audit_entries(junk.email_id) if a["event"].startswith("notification.")]
    app.admin.update({"notify_ccp": "mgr@x.com", "forward_teams": [{"name": "IT", "address": "it@x.com"}]}, "ada")
    junk2 = make_pipeline(app, [email(2, "j2")], {"j2": result(Category.UNCLASSIFIED, 10)}).run_once()[0]
    sent = [a for a in app.store.audit_entries(junk2.email_id) if a["event"] == "notification.sent"]
    assert sent and sent[0]["details"]["recipients"] == ["mgr@x.com"]
    assert app.reviews.forward(junk2.review_id, "carol", "IT")["to"] == "it@x.com"  # teams update live too
    app.store.close()


# ---- pickup date and time ----

@pytest.mark.parametrize("combined, date, time", [
    ("2026-10-05T09:30", "2026-10-05", "09:30"),
    ("2026-10-05 18:45:00", "2026-10-05", "18:45"),
    ("25 Sept at 7am", "25 Sept", "7am"),
    ("5 Oct 2026 10:30 AM", "5 Oct 2026", "10:30 AM"),
    ("tomorrow, 6.30 pm", "tomorrow", "6.30 pm"),
    ("25.09.2026", "25.09.2026", None),      # a date with dots is not a time
    ("Friday", "Friday", None),
])
def test_pickup_datetime_is_split_into_date_and_time(combined, date, time):
    trip = derive_pickup({"pickup_datetime": combined})
    assert trip.get("pickup_date") == date and trip.get("pickup_time") == time


def test_explicit_pickup_fields_are_not_overwritten():
    trip = derive_pickup({"pickup_datetime": "2026-10-05T09:30", "pickup_date": "6 Oct", "pickup_time": "noon"})
    assert trip["pickup_date"] == "6 Oct" and trip["pickup_time"] == "noon"
    assert derive_pickup(None) == {}


def test_date_without_time_is_a_mandatory_gap_not_a_block(tmp_path):
    app = make_app(tmp_path)
    api = TestClient(create_api(app))
    only_date = {"company": "C", "trip": {"pickup_location": "A", "pickup_datetime": "5 Oct 2026"}}
    gaps = api.post("/api/gaps", json={"payload": only_date}).json()
    assert gaps["essential"] == [] and "Pickup time" in gaps["mandatory"]
    nothing = api.post("/api/gaps", json={"payload": {"trip": {}}}).json()
    assert nothing["essential"] == ["Pickup location", "Pickup date"]
    app.store.close()


def test_booking_payload_carries_split_pickup_fields(tmp_path):
    app = make_app(tmp_path)
    r = result(Category.NEW_BOOKING, 90)
    from eie.models import TripDetails
    r.extracted.trip = TripDetails(pickup_location="BOM T2", pickup_datetime="2026-10-05T09:30")
    out = make_pipeline(app, [email(1, "book")], {"book": r}).run_once()[0]
    trip = app.store.get_review(out.review_id)["payload"]["trip"]
    assert trip["pickup_date"] == "2026-10-05" and trip["pickup_time"] == "09:30"
    app.store.close()


# ---- local-time dates ----

def set_created(app, email_id, iso):
    app.store.conn.execute("UPDATE emails SET created_at = ? WHERE id = ?", (iso, email_id))
    app.store.conn.commit()


def test_date_filters_use_the_configured_timezone(tmp_path):
    app = make_app(tmp_path, timezone="Asia/Kolkata")
    a = make_pipeline(app, [email(1, "late")], {"late": result(Category.FEEDBACK, 60)}).run_once()[0]
    b = make_pipeline(app, [email(2, "early")], {"early": result(Category.FEEDBACK, 60)}).run_once()[0]
    set_created(app, a.email_id, "2026-10-01T20:00:00+00:00")  # 01:30 on 2 Oct in India
    set_created(app, b.email_id, "2026-10-01T10:00:00+00:00")  # 15:30 on 1 Oct in India

    def ids(**kw):
        return {e["id"] for e in app.store.list_emails(**kw)}

    assert ids(date_from="2026-10-02", date_to="2026-10-02") == {a.email_id}
    assert ids(date_from="2026-10-01", date_to="2026-10-01") == {b.email_id}
    assert ids(date_from="2026-10-01", date_to="2026-10-02") == {a.email_id, b.email_id}
    assert app.store.kpis("2026-10-02", "2026-10-02")["total_ingested"] == 1
    app.store.close()

    utc = make_app(tmp_path, name="utc.db", timezone="UTC")
    c = make_pipeline(utc, [email(3, "x")], {"x": result(Category.FEEDBACK, 60)}).run_once()[0]
    set_created(utc, c.email_id, "2026-10-01T20:00:00+00:00")
    assert {e["id"] for e in utc.store.list_emails(date_from="2026-10-01", date_to="2026-10-01")} == {c.email_id}
    utc.store.close()


def test_periods_and_bad_dates(tmp_path):
    app = make_app(tmp_path)
    api = TestClient(create_api(app))
    make_pipeline(app, [email(1, "now")], {"now": result(Category.FEEDBACK, 60)}).run_once()
    for period in ("today", "7", "30", "all"):
        assert api.get("/api/kpis", params={"period": period}).json()["total_ingested"] == 1
    old = make_pipeline(app, [email(2, "old")], {"old": result(Category.FEEDBACK, 60)}).run_once()[0]
    set_created(app, old.email_id, "2020-01-01T00:00:00+00:00")
    assert api.get("/api/kpis", params={"period": "today"}).json()["total_ingested"] == 1
    assert api.get("/api/kpis", params={"period": "all"}).json()["total_ingested"] == 2
    assert api.get("/api/kpis", params={"period": "century"}).status_code == 400
    assert api.get("/api/emails", params={"date_from": "yesterday"}).status_code == 400
    app.store.close()


def test_bad_timezone_fails_clearly(tmp_path):
    from zoneinfo import ZoneInfoNotFoundError

    with pytest.raises(ZoneInfoNotFoundError):
        make_app(tmp_path, timezone="Mars/Olympus")


# ---- webhook subscription renewal (FR-002) ----

class FakeGraph:
    def __init__(self, fail_renew=False):
        self.calls, self.fail_renew = [], fail_renew

    @staticmethod
    def _expiry(hours):
        return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")

    def subscribe(self, url, secret):
        self.calls.append(("subscribe", url, secret))
        return {"id": f"sub-{len(self.calls)}", "expirationDateTime": self._expiry(70)}

    def renew(self, sub_id):
        self.calls.append(("renew", sub_id))
        if self.fail_renew:
            raise RuntimeError("404 subscription not found")
        return {"expirationDateTime": self._expiry(70)}


def sub_app(tmp_path, **kw):
    settings = dict(graph_tenant_id="t", graph_client_id="c", graph_client_secret="s", graph_mailbox="m@x.com",
                    graph_webhook_secret="hook", public_base_url="https://eie.example.com")
    return make_app(tmp_path, **{**settings, **kw})


def test_subscription_lifecycle(tmp_path):
    app = sub_app(tmp_path)
    fake = FakeGraph()
    mgr = SubscriptionManager(app.settings, app.store, graph_factory=lambda: fake)
    assert mgr.enabled
    info = mgr.ensure()
    assert info["id"] == "sub-1" and fake.calls == [("subscribe", "https://eie.example.com/api/graph/webhook", "hook")]
    assert mgr.ensure() == info and len(fake.calls) == 1  # still fresh: nothing to do

    app.store.kv_set("graph_subscription", {**info, "expires": FakeGraph._expiry(3)})  # about to lapse
    renewed = mgr.ensure()
    assert fake.calls[-1] == ("renew", "sub-1") and renewed["id"] == "sub-1"
    assert "webhook.renewed" in [a["event"] for a in app.store.audit_entries(limit=20)]

    app.store.kv_set("graph_subscription", {**info, "expires": FakeGraph._expiry(3)})
    broken = SubscriptionManager(app.settings, app.store, graph_factory=lambda: FakeGraph(fail_renew=True))
    broken_graph = broken._graph()
    broken._graph = lambda: broken_graph
    again = broken.ensure()  # renewing fails (Graph dropped it): a new subscription is created
    assert [c[0] for c in broken_graph.calls] == ["renew", "subscribe"] and again["id"] == "sub-2"

    app.settings.public_base_url = "https://new.example.com"  # moved: the old subscription points nowhere
    moved = mgr.ensure()
    assert fake.calls[-1][1] == "https://new.example.com/api/graph/webhook" and moved["url"].startswith("https://new")
    app.store.close()


@pytest.mark.parametrize("override", [
    {"graph_webhook_secret": ""}, {"public_base_url": "http://127.0.0.1:8000"}, {"graph_tenant_id": ""}])
def test_subscription_stays_off_when_not_configured(tmp_path, override):
    app = sub_app(tmp_path, **override)
    mgr = SubscriptionManager(app.settings, app.store, graph_factory=lambda: pytest.fail("must not call Graph"))
    assert not mgr.enabled and mgr.ensure() is None
    mgr.start()  # no thread, no error
    app.store.close()


def test_subscription_failure_is_audited_and_alerted_not_raised(tmp_path):
    app = sub_app(tmp_path, notify_ccp="mgr@x.com")

    class Down:
        def subscribe(self, url, secret):
            raise RuntimeError("Graph is down")

    mgr = SubscriptionManager(app.settings, app.store, app.notifier, graph_factory=Down)
    mgr._safe_ensure()
    events = [a["event"] for a in app.store.audit_entries(limit=20)]
    assert "webhook.subscription_failed" in events and "ingestion.failed" in events
    app.store.close()
