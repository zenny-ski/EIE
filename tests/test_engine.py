import sqlite3

import pytest

from eie.app import create_app
from eie.classification.rules import RuleClassifier, extract_booking_ids
from eie.config import Settings
from eie.ingestion.file import JsonFileSource
from eie.models import Action, Category, ClassificationResult, Destination, Email, ExtractedInfo
from eie.routing import DispatchValidationError, decide, field_gaps

HIGH, MEDIUM = 85, 60
ESSENTIALS = {"pickup_location": "A", "pickup_datetime": "2026-01-01T09:00"}
FULL_TRIP = ESSENTIALS | {"drop_location": "B", "vehicle_type": "Sedan", "cost_centre": "CC1", "concur_id": "C9"}


def result(category: Category, confidence: int) -> ClassificationResult:
    return ClassificationResult(category=category, confidence=confidence, reasoning="test",
                                extracted=ExtractedInfo(booking_ids=["BK-1234"]), suggested_reply="Hi")


@pytest.mark.parametrize("category, confidence, action, destination", [
    (Category.FEEDBACK, 90, Action.AUTO_CREATE, Destination.WHATSAPP_FEEDBACK),
    (Category.FEEDBACK, 70, Action.REVIEW_FORM, Destination.WHATSAPP_FEEDBACK),
    (Category.FEEDBACK, 40, Action.CCP_REVIEW, Destination.CCP_QUEUE),
    (Category.ESCALATION, 85, Action.AUTO_CREATE, Destination.ESCALATION_SYSTEM),
    (Category.ESCALATION, 60, Action.REVIEW_FORM, Destination.ESCALATION_SYSTEM),
    (Category.ESCALATION, 59, Action.CCP_REVIEW, Destination.CCP_QUEUE),
    (Category.NEW_BOOKING, 99, Action.BOOKING_APPROVAL, Destination.INDECAB),
    (Category.NEW_BOOKING, 65, Action.BOOKING_APPROVAL, Destination.INDECAB),
    (Category.NEW_BOOKING, 30, Action.CCP_REVIEW, Destination.CCP_QUEUE),
    (Category.UNCLASSIFIED, 95, Action.CCP_REVIEW, Destination.CCP_QUEUE),
])
def test_routing_matrix(category, confidence, action, destination):
    d = decide(result(category, confidence), HIGH, MEDIUM)
    assert (d.action, d.destination) == (action, destination)


class FixedClassifier:
    def __init__(self, by_subject):
        self.by_subject = by_subject

    def classify(self, email):
        return self.by_subject[email.subject]


class ListSource:
    name = "file"

    def __init__(self, emails):
        self.emails = emails

    def fetch_unread(self, limit):
        return self.emails

    def mark_processed(self, email):
        pass


def email(n: int, subject: str) -> Email:
    return Email(message_id=f"<m{n}@x>", subject=subject, sender="a@b.com", received_at="2026-09-22",
                 body="body", source="file", source_ref=str(n))


@pytest.fixture
def app(tmp_path):
    a = create_app(Settings(db_path=str(tmp_path / "t.db"), use_llm=False, dry_run=True))
    yield a
    a.store.close()


def make_pipeline(app, emails, results):
    p = app.pipeline(ListSource(emails))
    p.classifier = FixedClassifier(results)
    return p


def test_pipeline_routes_audits_and_is_idempotent(app):
    emails = [email(1, "fb-high"), email(2, "esc-med"), email(3, "booking"), email(4, "junk")]
    results = {
        "fb-high": result(Category.FEEDBACK, 95),
        "esc-med": result(Category.ESCALATION, 70),
        "booking": result(Category.NEW_BOOKING, 92),
        "junk": result(Category.UNCLASSIFIED, 50),
    }
    pipeline = make_pipeline(app, emails, results)
    out = {r.subject: r for r in pipeline.run_once()}

    assert out["fb-high"].status == "auto_created"
    assert out["fb-high"].external_ref.startswith("dryrun-whatsapp_feedback")
    assert out["esc-med"].action == "review_form"
    assert out["booking"].action == "booking_approval"
    assert out["junk"].action == "ccp_review"
    assert len(app.store.list_reviews()) == 3

    events = [a["event"] for a in app.store.audit_entries(out["fb-high"].email_id)]
    assert events == ["email.ingested", "email.classified", "requester.checked", "routing.decided",
                      "destination.created", "reply.drafted"]

    assert pipeline.run_once() == []  # already processed


def test_review_approve_reject_and_ccp_routing(app):
    emails = [email(1, "booking"), email(2, "junk"), email(3, "esc-med")]
    results = {"booking": result(Category.NEW_BOOKING, 90), "junk": result(Category.UNCLASSIFIED, 20),
               "esc-med": result(Category.ESCALATION, 70)}
    out = {r.subject: r for r in make_pipeline(app, emails, results).run_once()}

    with pytest.raises(DispatchValidationError) as blocked:  # Essential fields (pickup, date) are empty
        app.reviews.approve(out["booking"].review_id, "alice")
    assert blocked.value.tier == "essential" and "Pickup location" in blocked.value.missing
    assert app.store.get_review(out["booking"].review_id)["status"] == "pending"
    assert "destination.blocked" in [a["event"] for a in app.store.audit_entries(out["booking"].email_id)]

    essentials_only = {"trip": ESSENTIALS}  # Mandatory fields still missing: needs an acknowledged gap
    with pytest.raises(DispatchValidationError) as gap:
        app.reviews.approve(out["booking"].review_id, "alice", payload=essentials_only)
    assert gap.value.tier == "mandatory" and "Cost centre" in gap.value.missing

    complete = {"company": "Initech", "passengers": [{"name": "A", "phone": "1"}], "trip": FULL_TRIP}
    approved = app.reviews.approve(out["booking"].review_id, "alice", payload=complete)
    assert approved["destination"] == "indecab"
    assert app.store.get_review(out["booking"].review_id)["payload"] == complete
    assert app.store.get_email(out["booking"].email_id)["status"] == "approved"

    routed = app.reviews.approve(out["junk"].review_id, "bob", category=Category.FEEDBACK)
    assert routed["destination"] == "whatsapp_feedback"

    app.reviews.reject(out["esc-med"].review_id, "carol", "not an escalation")
    assert app.store.get_email(out["esc-med"].email_id)["status"] == "rejected"
    with pytest.raises(Exception):
        app.reviews.approve(out["esc-med"].review_id, "carol")


def test_audit_log_is_append_only(app):
    app.store.audit("test.event", note="x")
    with pytest.raises(sqlite3.DatabaseError):
        app.store.conn.execute("DELETE FROM audit_log")
    with pytest.raises(sqlite3.DatabaseError):
        app.store.conn.execute("UPDATE audit_log SET event = 'x'")


def test_rules_classifier_never_auto_creates():
    clf = RuleClassifier(Settings().booking_id_regex)
    for e in JsonFileSource("samples/sample_emails.json").fetch_unread(10):
        r = clf.classify(e)
        assert decide(r, HIGH, MEDIUM).action != Action.AUTO_CREATE


def test_booking_id_extraction():
    assert extract_booking_ids("ref bk-482913 and BK482913, BK-482913", Settings().booking_id_regex) == [
        "BK-482913", "BK482913"]


class Overloaded(Exception):
    code = 503


class FlakyLLM:
    """Raises a 503 for the first `failures` calls, then classifies."""

    def __init__(self, failures):
        self.failures = failures

    def classify(self, email):
        if self.failures:
            self.failures -= 1
            raise Overloaded("503 UNAVAILABLE: high demand")
        return result(Category.FEEDBACK, 95)


def test_llm_overload_defers_email_until_llm_recovers(app):
    from eie.classification import FallbackClassifier

    p = app.pipeline(ListSource([email(1, "fb")]))
    p.classifier = FallbackClassifier(FlakyLLM(failures=2), RuleClassifier(app.settings.booking_id_regex),
                                      base_retry_seconds=0)
    assert p.run_once() == [] and p.deferred == 1
    assert not app.store.email_exists("<m1@x>")  # left unread, nothing stored
    assert p.run_once() == [] and p.deferred == 1
    [done] = p.run_once()
    assert (done.category, done.confidence, p.deferred) == ("feedback", 95, 0)
    events = [a["event"] for a in app.store.audit_entries(limit=100)]
    assert events.count("classifier.deferred") == 2


def test_long_llm_outage_goes_to_the_queue_as_a_system_error(app):
    from eie.classification import FallbackClassifier

    e = email(1, "fb")
    e.body = "Hello team, about BK-482913. Please call me on +91 98765 43210"
    p = app.pipeline(ListSource([e]))
    p.classifier = FallbackClassifier(FlakyLLM(failures=99), RuleClassifier(app.settings.booking_id_regex),
                                      max_deferrals=2, base_retry_seconds=0)
    assert p.run_once() == [] and p.run_once() == []
    [done] = p.run_once()
    assert done.status == "pending_review" and done.action == "ccp_review" and done.destination == "ccp_queue"
    row = app.store.get_email(done.email_id)
    assert row["classifier"] == "error" and row["confidence"] == 0 and row["category"] == "unclassified"
    item = app.store.get_review(done.review_id)
    assert item["kind"] == "ccp_review" and item["system_error"] == 1  # flagged, unlike a low-confidence item
    assert item["reason"].startswith("System error: classification failed")
    assert row["extracted"]["booking_ids"] == ["BK-482913"]  # pre-filled from the keyword rules
    assert row["suggested_reply"]  # FR-026: there is still a draft
    events = [a["event"] for a in app.store.audit_entries(done.email_id)]
    assert "classifier.failed" in events and "email.classified" in events


def test_deferred_email_waits_before_calling_llm_again(app):
    from eie.classification import FallbackClassifier

    now = [0.0]
    llm = FlakyLLM(failures=1)
    p = app.pipeline(ListSource([email(1, "fb")]))
    p.classifier = FallbackClassifier(llm, RuleClassifier(app.settings.booking_id_regex),
                                      base_retry_seconds=15, clock=lambda: now[0])
    assert p.run_once() == []          # 503 -> deferred for 15s
    now[0] = 8
    assert p.run_once() == [] and p.deferred == 1  # still waiting: LLM not called
    now[0] = 16
    [done] = p.run_once()
    assert done.confidence == 95
    events = [a["event"] for a in app.store.audit_entries(limit=100)]
    assert events.count("classifier.deferred") == 1


def failing_pipeline(app, error, emails):
    from eie.classification import FallbackClassifier

    class Broken:
        def classify(self, email):
            raise error

    p = app.pipeline(ListSource(emails))
    p.classifier = FallbackClassifier(Broken(), RuleClassifier(app.settings.booking_id_regex))
    return p


def test_daily_quota_is_a_system_error_immediately(app):
    err = Exception("429 RESOURCE_EXHAUSTED quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier")
    err.code = 429
    [done] = failing_pipeline(app, err, [email(1, "fb")]).run_once()
    assert app.store.get_email(done.email_id)["classifier"] == "error"
    assert app.store.get_review(done.review_id)["system_error"] == 1


def test_any_other_model_error_is_a_system_error_and_is_not_retried(app):
    p = failing_pipeline(app, ValueError("model returned garbage"), [email(1, "a"), email(2, "b")])
    out = p.run_once()
    assert [o.status for o in out] == ["pending_review", "pending_review"] and p.deferred == 0
    assert all(app.store.get_review(o.review_id)["system_error"] == 1 for o in out)


def test_system_error_items_can_still_be_assigned_a_bucket(app):
    [done] = failing_pipeline(app, ValueError("boom"), [email(1, "fb")]).run_once()
    assert app.reviews.approve(done.review_id, "carol", category=Category.FEEDBACK)["destination"] == "whatsapp_feedback"
    row = app.store.get_email(done.email_id)
    assert row["status"] == "approved" and "Thank you for sharing your feedback" in row["suggested_reply"]


def test_a_failure_wave_sends_one_alert_not_one_per_email(tmp_path):
    from eie.config import Settings

    a = create_app(Settings(db_path=str(tmp_path / "t.db"), use_llm=False, dry_run=True, notify_ccp="mgr@x.com"))
    out = failing_pipeline(a, ValueError("model returned garbage"), [email(i, f"m{i}") for i in range(1, 5)]).run_once()
    assert len(out) == 4
    events = [x["event"] for x in a.store.audit_entries(limit=200)]
    assert events.count("classification.failing") == 4  # every one is on the record...
    sent = [x for x in a.store.audit_entries(limit=200) if x["event"] == "notification.sent"]
    assert len(sent) == 1 and sent[0]["details"]["kind"] == "alert"  # ...but staff get a single email
    a.store.close()


def test_configured_rules_classifier_is_not_a_failure(app):
    out = make_pipeline(app, [email(1, "x")], {"x": result(Category.FEEDBACK, 60)}).run_once()
    assert app.store.get_review(out[0].review_id)["system_error"] == 0


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.headers = status, body or {}, {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}", response=self)


def live_dispatcher(monkeypatch, responses):
    """A non-dry-run dispatcher whose HTTP calls return `responses` in order; records sleeps."""
    from eie.routing import Dispatcher
    from eie.routing import destinations

    calls, sleeps = [], []

    def post(url, **kw):
        calls.append(url)
        r = responses[len(calls) - 1]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(destinations.requests, "post", post)
    d = Dispatcher(Settings(dry_run=False, whatsapp_feedback_url="http://x/fb"), sleep=sleeps.append)
    return d, calls, sleeps


def test_dispatch_retries_with_exponential_backoff(monkeypatch):
    import requests
    d, calls, sleeps = live_dispatcher(
        monkeypatch, [requests.ConnectionError("down"), FakeResponse(503), FakeResponse(200, {"id": "FB-1"})])
    retries = []
    ref = d.create(Destination.WHATSAPP_FEEDBACK, {}, lambda attempt, exc: retries.append(attempt))
    assert ref == "FB-1" and len(calls) == 3
    assert sleeps == [2.0, 4.0] and retries == [1, 2]


def test_dispatch_gives_up_after_three_attempts(monkeypatch):
    import requests
    d, calls, _ = live_dispatcher(monkeypatch, [FakeResponse(500)] * 3)
    with pytest.raises(requests.HTTPError):
        d.create(Destination.WHATSAPP_FEEDBACK, {})
    assert len(calls) == 3


def test_dispatch_does_not_retry_client_errors(monkeypatch):
    import requests
    d, calls, sleeps = live_dispatcher(monkeypatch, [FakeResponse(400)])
    with pytest.raises(requests.HTTPError):
        d.create(Destination.WHATSAPP_FEEDBACK, {})
    assert len(calls) == 1 and sleeps == []


def test_indecab_blocks_until_mandatory_fields_filled():
    from eie.routing import Dispatcher
    d = Dispatcher(Settings(dry_run=True))
    with pytest.raises(DispatchValidationError) as e:  # Essential gaps always block
        d.create(Destination.INDECAB, {"company": " ", "passengers": [], "trip": {}}, acknowledge_gaps=True)
    assert e.value.missing == ["Pickup location", "Pickup date"]
    only_essentials = {"trip": ESSENTIALS}
    with pytest.raises(DispatchValidationError) as m:  # Mandatory gaps need an acknowledgement
        d.create(Destination.INDECAB, only_essentials)
    assert m.value.tier == "mandatory"
    assert d.create(Destination.INDECAB, only_essentials, acknowledge_gaps=True).startswith("dryrun-indecab")
    ok = {"company": "C", "passengers": [{"name": "A", "phone": "1"}], "trip": FULL_TRIP}
    gaps = field_gaps(Destination.INDECAB, ok)
    assert gaps["essential"] == gaps["mandatory"] == [] and gaps["semi_mandatory"] == ["Flight or train number"]
    assert d.create(Destination.INDECAB, ok).startswith("dryrun-indecab")  # Semi-Mandatory never blocks
    ok["trip"] = ok["trip"] | {"train_number": "12951"}
    assert field_gaps(Destination.INDECAB, ok)["semi_mandatory"] == []
    assert d.create(Destination.ESCALATION_SYSTEM, {}).startswith("dryrun-")  # no field rules elsewhere


def test_payload_carries_confidence_and_source_link(app):
    from eie.routing import build_payload
    row = {"id": 7, "message_id": "m", "sender": "s", "subject": "x", "received_at": ""}
    p = build_payload(Destination.WHATSAPP_FEEDBACK, row, result(Category.FEEDBACK, 91), "http://eie.local/")
    assert p["confidence"] == 91 and p["source_email_link"] == "http://eie.local/#/emails/7"


def test_reroute_from_ccp_rebuilds_payload_and_shows_gaps(app):
    out = make_pipeline(app, [email(1, "junk")], {"junk": result(Category.UNCLASSIFIED, 20)}).run_once()[0]
    with pytest.raises(DispatchValidationError):
        app.reviews.approve(out.review_id, "bob", category=Category.NEW_BOOKING)
    item = app.store.get_review(out.review_id)
    assert item["status"] == "pending" and item["destination"] == "indecab"
    assert "trip" in item["payload"] and "company" in item["payload"]  # the reviewer can now fill these in


def threaded(n: int, subject: str, conv: str = "conv-1") -> Email:
    e = email(n, subject)
    e.conversation_id = conv
    return e


def test_thread_same_bucket_is_appended(app):
    out = make_pipeline(app, [threaded(1, "first"), threaded(2, "more")],
                        {"first": result(Category.FEEDBACK, 95), "more": result(Category.FEEDBACK, 90)}).run_once()
    first, second = out
    assert first.status == "auto_created" and second.status == "appended" and second.review_id is None
    row = app.store.get_email(second.email_id)
    assert row["parent_email_id"] == first.email_id and row["thread_role"] == "appended"
    assert row["external_ref"] == first.external_ref  # points at the existing record
    assert "thread.message_appended" in [a["event"] for a in app.store.audit_entries(first.email_id)]


def test_thread_clean_split_inherits_context_and_links_both_ways(app):
    first = result(Category.FEEDBACK, 95)
    first.extracted.booking_ids = ["BK-9999"]
    booking = result(Category.NEW_BOOKING, 93)
    booking.extracted.booking_ids = []
    out = make_pipeline(app, [threaded(1, "first"), threaded(2, "book")],
                        {"first": first, "book": booking}).run_once()
    parent, child = out
    assert child.action == "booking_approval"  # a New Booking split is always HITL (FR-033)
    payload = app.store.get_review(child.review_id)["payload"]
    assert payload["booking_ids"] == ["BK-9999"]  # FR-034
    assert payload["parent_email_id"] == parent.email_id and payload["parent_ref"] == parent.external_ref
    row = app.store.get_email(child.email_id)
    assert row["parent_email_id"] == parent.email_id and row["thread_role"] == "split"
    assert "thread.split_from" in [a["event"] for a in app.store.audit_entries(child.email_id)]
    assert "thread.split_off" in [a["event"] for a in app.store.audit_entries(parent.email_id)]  # FR-036


def test_thread_clean_feedback_split_auto_creates(app):
    out = make_pipeline(app, [threaded(1, "esc"), threaded(2, "fb")],
                        {"esc": result(Category.ESCALATION, 90), "fb": result(Category.FEEDBACK, 92)}).run_once()
    assert out[1].status == "auto_created"
    assert app.store.get_email(out[1].email_id)["thread_role"] == "split"


def possible_split(app, conv, n):
    e1, e2 = threaded(n, f"first{n}", conv), threaded(n + 1, f"maybe{n}", conv)
    results = {e1.subject: result(Category.FEEDBACK, 95), e2.subject: result(Category.NEW_BOOKING, 70)}
    return make_pipeline(app, [e1, e2], results).run_once()[1]


def test_possible_split_keep_with_existing_record(app):
    r = possible_split(app, "c-keep", 1)
    assert r.action == "possible_split"
    item = app.store.get_review(r.review_id)
    assert item["kind"] == "possible_split" and item["destination"] == "ccp_queue"
    assert app.reviews.approve(r.review_id, "carol", resolution="keep")["status"] == "resolved"
    row = app.store.get_email(r.email_id)
    assert row["status"] == "appended" and row["thread_role"] == "appended"


def test_possible_split_link_to_another_ticket(app):
    r = possible_split(app, "c-link", 3)
    with pytest.raises(Exception):  # a ticket reference is required
        app.reviews.approve(r.review_id, "carol", resolution="link")
    assert app.reviews.approve(r.review_id, "carol", resolution="link", link_ref="ESC-77")["external_ref"] == "ESC-77"
    assert app.store.get_email(r.email_id)["thread_role"] == "linked"


def test_possible_split_to_new_record(app):
    r = possible_split(app, "c-new", 5)
    with pytest.raises(DispatchValidationError):  # becomes an IndeCab booking: Essential gaps block
        app.reviews.approve(r.review_id, "carol", category=Category.NEW_BOOKING)
    item = app.store.get_review(r.review_id)
    assert item["destination"] == "indecab" and item["payload"]["parent_email_id"]
    done = {"company": "C", "passengers": [{"name": "A", "phone": "1"}], "trip": FULL_TRIP}
    assert app.reviews.approve(r.review_id, "carol", payload=done, category=Category.NEW_BOOKING)["destination"] == "indecab"
    assert app.store.get_email(r.email_id)["thread_role"] == "split"


def test_thread_unclassified_message_never_joins_a_record(app):
    out = make_pipeline(app, [threaded(1, "first"), threaded(2, "invoice")],
                        {"first": result(Category.FEEDBACK, 95), "invoice": result(Category.UNCLASSIFIED, 40)}).run_once()
    assert out[1].action == "ccp_review"
    row = app.store.get_email(out[1].email_id)
    assert row["thread_role"] == "related" and row["parent_email_id"] == out[0].email_id


def test_rejected_parent_does_not_anchor_the_thread(app):
    first = make_pipeline(app, [threaded(1, "first")], {"first": result(Category.ESCALATION, 70)}).run_once()[0]
    app.reviews.reject(first.review_id, "carol")
    out = make_pipeline(app, [threaded(2, "next")], {"next": result(Category.FEEDBACK, 95)}).run_once()
    assert out[0].status == "auto_created"


def test_per_bucket_thresholds_and_default_floor():
    s = Settings(confidence_high_feedback=70)
    assert s.thresholds(Category.FEEDBACK) == (70, 50)  # FRD default floor is 50
    assert s.thresholds(Category.ESCALATION) == (85, 50)
    assert Settings(confidence_medium_escalation=65).thresholds("escalation") == (85, 65)


def test_pipeline_uses_per_bucket_thresholds(tmp_path):
    a = create_app(Settings(db_path=str(tmp_path / "t.db"), use_llm=False, dry_run=True,
                            confidence_high_feedback=70, confidence_medium_escalation=75))
    out = {r.subject: r for r in make_pipeline(
        a, [email(1, "fb"), email(2, "esc")],
        {"fb": result(Category.FEEDBACK, 72), "esc": result(Category.ESCALATION, 72)}).run_once()}
    assert out["fb"].status == "auto_created"       # above this bucket's lower high threshold
    assert out["esc"].action == "ccp_review"        # below this bucket's higher medium floor
    a.store.close()


def test_failed_auto_create_goes_to_ccp_queue(app, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("downstream is down")

    monkeypatch.setattr(app.dispatcher, "create", boom)
    out = make_pipeline(app, [email(1, "fb")], {"fb": result(Category.FEEDBACK, 95)}).run_once()[0]
    assert out.status == "pending_review"
    item = app.store.get_review(out.review_id)
    assert item["kind"] == "ccp_review" and item["destination"] == "ccp_queue"  # FRD 13.3
    assert "failed" in item["reason"]


def test_missing_details_reply_wording_follows_tier():
    from eie.templates import missing_details_draft
    gaps = {"essential": ["Pickup location"], "mandatory": ["Cost centre"], "semi_mandatory": []}
    essential = missing_details_draft("Asha Rao <a@b.com>", gaps)
    assert essential.startswith("Dear Asha") and "cannot be created without" in essential
    assert "- Pickup location" in essential and "- Cost centre" in essential
    mandatory = missing_details_draft("a@b.com", {"essential": [], "mandatory": ["Cost centre"], "semi_mandatory": []})
    assert mandatory.startswith("Dear Customer") and "finalise" in mandatory
    semi = missing_details_draft("A", {"essential": [], "mandatory": [], "semi_mandatory": ["Flight or train number"]})
    assert "cab details will follow" in semi
    assert missing_details_draft("A", {"essential": [], "mandatory": [], "semi_mandatory": []}) == ""
