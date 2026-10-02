"""Orchestrates one pass: read -> classify -> decide -> dispatch or queue for review -> audit."""
import logging
from dataclasses import dataclass, replace
from typing import Optional

from .classification import ClassificationDeferred, ClassificationFailed
from .classification.rules import REPLY_TEMPLATES
from .config import Settings
from .models import (DESTINATION_FOR_CATEGORY, Action, Category, ClassificationResult, ConfidenceTier,
                     Destination, Email, RoutingDecision)
from .routing import Dispatcher, build_payload, decide
from .storage import Store
from .templates import render_draft
from .verification import verify_requester

log = logging.getLogger("eie")

BUCKETS = (Category.FEEDBACK, Category.ESCALATION, Category.NEW_BOOKING)


@dataclass
class ProcessedEmail:
    email_id: int
    subject: str
    category: str
    confidence: int
    action: str
    destination: str
    status: str
    review_id: Optional[int] = None
    external_ref: Optional[str] = None


@dataclass
class ThreadLink:
    kind: str       # append | split | related
    parent: dict    # the earlier email whose record this message joins or splits from


class Pipeline:
    def __init__(self, settings: Settings, store: Store, source, classifier, dispatcher: Dispatcher,
                 notifier=None):
        self.notifier = notifier
        self.settings = settings
        self.store = store
        self.source = source
        self.classifier = classifier
        self.dispatcher = dispatcher
        self.deferred = 0  # emails left unread by the last run because the LLM was unavailable

    def run_once(self, audit_empty: bool = True) -> list[ProcessedEmail]:
        self.deferred = 0
        emails = self.source.fetch_unread(self.settings.fetch_limit)
        if emails or audit_empty:
            self.store.audit("fetch.completed", count=len(emails))
        results = []
        for email in emails:
            try:
                processed = self.process(email)
            except ClassificationDeferred as exc:  # stays unread; retried on the next fetch
                self.deferred += 1
                if not exc.waiting:  # only audit real LLM attempts, not fetches that skip a waiting email
                    self.store.audit("classifier.deferred", message_id=email.message_id,
                                     subject=email.subject, error=str(exc))
                continue
            except Exception as exc:  # one bad email must not stop the batch
                log.exception("Failed to process %s", email.message_id)
                self.store.audit("email.failed", message_id=email.message_id, error=repr(exc))
                continue
            if processed:
                results.append(processed)
        return results

    def process(self, email: Email) -> Optional[ProcessedEmail]:
        if self.store.email_exists(email.message_id):
            return None

        # FR-030: every message is classified on its own merits, whatever its thread's existing bucket.
        system_error = None
        try:
            result = self.classifier.classify(email)
        except ClassificationFailed as exc:  # FRD 13.2: queue it, flagged, proposing no bucket
            system_error = f"{type(exc.error).__name__}: {exc.error}"[:300]
            result = self._failed_result(exc, system_error)
        email_id = self.store.insert_email(email)
        self.store.audit("email.ingested", email_id=email_id, source=email.source,
                         message_id=email.message_id, sender=email.sender, subject=email.subject)

        if system_error:
            self.store.audit("classifier.failed", email_id=email_id, error=system_error)

        link = self._thread_link(email, email_id, result)
        if link and link.kind == "split":
            self._inherit_context(result, link.parent)  # FR-034

        result.suggested_reply = render_draft(self.settings, result.category.value, email.sender, email.subject,
                                              result.extracted, result.suggested_reply)
        self.store.audit("email.classified", result.classifier, email_id=email_id,
                         category=result.category.value, confidence=result.confidence,
                         reasoning=result.reasoning, extracted=result.extracted.model_dump())

        verified, detail = verify_requester(self.store, email.sender, result.extracted.booking_ids)  # FR-007
        self.store.set_requester(email_id, verified, detail)
        self.store.audit("requester.checked", email_id=email_id, verified=verified, **detail)

        decision = self._decide(result, link)
        if system_error:
            decision = replace(decision, reason=f"System error: classification failed ({system_error})")
        self.store.save_classification(email_id, result, decision)
        self.store.audit("routing.decided", email_id=email_id, tier=decision.tier.value,
                         action=decision.action.value, destination=decision.destination.value,
                         reason=decision.reason)
        if link:
            self._record_link(email_id, link, decision)

        row = self.store.get_email(email_id)
        base_url = self.settings.public_base_url
        parent = link.parent if link else None
        payload = build_payload(decision.destination, row, result, base_url,
                                parent if link and link.kind == "split" else None)
        out = ProcessedEmail(email_id, email.subject, result.category.value, result.confidence,
                             decision.action.value, decision.destination.value, status="")

        if decision.action == Action.APPEND_TO_THREAD:
            self.store.set_email_status(email_id, "appended", parent.get("external_ref"))
            out.status = "appended"
        elif decision.action == Action.AUTO_CREATE:
            def on_retry(attempt: int, exc: Exception) -> None:
                self.store.audit("destination.retry", email_id=email_id, destination=decision.destination.value,
                                 attempt=attempt, error=repr(exc))

            try:
                ref = self.dispatcher.create(decision.destination, payload, on_retry)
            except Exception as exc:
                # FRD 13.3: persistent failure routes the email to the CCP queue. Nothing is lost.
                self.store.audit("destination.failed", email_id=email_id,
                                 destination=decision.destination.value, error=repr(exc))
                ccp_payload = build_payload(Destination.CCP_QUEUE, row, result, base_url, parent)
                out.review_id = self._queue(email_id, Action.CCP_REVIEW.value, Destination.CCP_QUEUE.value,
                                            ccp_payload,
                                            f"Auto-create in {decision.destination.value} failed: {exc!r}")
                out.status = "pending_review"
            else:
                self.store.audit("destination.created", email_id=email_id,
                                 destination=decision.destination.value, external_ref=ref,
                                 dry_run=ref.startswith("dryrun-"), payload=payload)
                self.store.set_email_status(email_id, "auto_created", ref)
                out.status, out.external_ref = "auto_created", ref
        else:
            out.review_id = self._queue(email_id, decision.action.value, decision.destination.value,
                                        payload, decision.reason, system_error=bool(system_error))
            out.status = "pending_review"

        self.store.audit("reply.drafted", email_id=email_id, suggested_reply=result.suggested_reply)

        if self.settings.mark_as_read:
            try:
                self.source.mark_processed(email)
            except Exception as exc:
                self.store.audit("source.mark_read_failed", email_id=email_id, error=repr(exc))
        return out

    # ---- threads (FRD 7.4) ----

    def _thread_link(self, email: Email, email_id: int, result: ClassificationResult) -> Optional[ThreadLink]:
        records = self.store.thread_records(email.conversation_id, email_id)
        if not records:
            return None
        if result.category not in BUCKETS:
            # e.g. a standalone invoice request: never joins or splits a record, goes to the queue
            return ThreadLink("related", records[0])
        same_bucket = next((r for r in records if r["category"] == result.category.value), None)
        if same_bucket:
            return ThreadLink("append", same_bucket)  # FR-031
        return ThreadLink("split", records[0])        # FR-032

    def _inherit_context(self, result: ClassificationResult, parent: dict) -> None:
        inherited = (parent.get("extracted") or {}).get("booking_ids") or []
        if not result.extracted.booking_ids:
            result.extracted.booking_ids = list(inherited)

    def _decide(self, result: ClassificationResult, link: Optional[ThreadLink]) -> RoutingDecision:
        high, medium = self.settings.thresholds(result.category)
        decision = decide(result, high, medium)
        if link is None or link.kind == "related":
            return decision
        parent_id = link.parent["id"]
        if link.kind == "append":
            return RoutingDecision(
                result.category, result.confidence, decision.tier, Action.APPEND_TO_THREAD,
                DESTINATION_FOR_CATEGORY[result.category],
                f"Same topic as the thread's existing {result.category.value.replace('_', ' ')} "
                f"record (email #{parent_id}): appended, no new record")
        if decision.tier != ConfidenceTier.HIGH:  # unclear whether this is a genuinely new intent
            return RoutingDecision(
                result.category, result.confidence, decision.tier, Action.POSSIBLE_SPLIT, Destination.CCP_QUEUE,
                f"Possible split: the thread's record (email #{parent_id}) is "
                f"{link.parent['category'].replace('_', ' ')} but this reads as "
                f"{result.category.value.replace('_', ' ')} at confidence {result.confidence}")
        return decision  # a clean split follows the normal tier rules (FR-033)

    def _record_link(self, email_id: int, link: ThreadLink, decision: RoutingDecision) -> None:
        parent_id = link.parent["id"]
        role = {"append": "appended", "related": "related",
                "split": "possible_split" if decision.action == Action.POSSIBLE_SPLIT else "split"}[link.kind]
        self.store.set_thread_link(email_id, parent_id, role)
        # FR-036: both sides of the link are in the audit trail
        if link.kind == "append":
            self.store.audit("thread.appended", email_id=email_id, parent_email_id=parent_id)
            self.store.audit("thread.message_appended", email_id=parent_id, child_email_id=email_id)
        elif link.kind == "split":
            self.store.audit("thread.split_from", email_id=email_id, parent_email_id=parent_id,
                             possible=role == "possible_split")
            self.store.audit("thread.split_off", email_id=parent_id, child_email_id=email_id,
                             possible=role == "possible_split")

    @staticmethod
    def _failed_result(exc: ClassificationFailed, error: str) -> ClassificationResult:
        """What gets stored when the model failed: no bucket and no confidence, but whatever the keyword rules
        could read (booking IDs, phone numbers) so the reviewer's form is pre-filled (FRD 7.3)."""
        return ClassificationResult(
            category=Category.UNCLASSIFIED, confidence=0, classifier="error",
            reasoning=f"System error: the classifier failed ({error}). No bucket was proposed.",
            extracted=exc.hint.extracted, suggested_reply=REPLY_TEMPLATES[Category.UNCLASSIFIED])

    def _queue(self, email_id: int, kind: str, destination: str, payload: dict, reason: str,
               system_error: bool = False) -> int:
        review_id = self.store.add_review(email_id, kind, destination, payload, reason, system_error)
        self.store.set_email_status(email_id, "pending_review")
        self.store.audit("review.queued", email_id=email_id, review_id=review_id, kind=kind,
                         destination=destination, reason=reason)
        if self.notifier:
            email = self.store.get_email(email_id)
            self.notifier.review_queued(email_id, review_id, kind, email["subject"], reason, system_error)
        return review_id
