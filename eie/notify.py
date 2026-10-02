"""Email notifications to staff: queue items (FR-019, FR-020) and ingestion failures (13.1).

Never raises: a failed notification is audited, it must not stop an email being processed.
"""
import logging
import time
from typing import Optional

from .config import Settings
from .reply import Mailer
from .storage import Store

log = logging.getLogger("eie")
ALERT_COOLDOWN_SECONDS = 1800  # one alert per distinct failure per half hour


class Notifier:
    def __init__(self, settings: Settings, mailer: Mailer, store: Store):
        self.s = settings
        self.mailer = mailer
        self.store = store
        self._last_alert: dict[str, float] = {}

    def review_queued(self, email_id: int, review_id: int, kind: str, subject: str, reason: str,
                      system_error: bool = False) -> None:
        if system_error:
            # An outage can fail dozens of emails in a row: alert once per distinct error, not once per email
            self.alert("Classification is failing: emails need manual triage", reason, event="classification.failing",
                       detail="These emails are in the CCP review queue flagged as system errors: open the queue "
                              "and assign a bucket to each.")
            return
        if kind in ("ccp_review", "possible_split"):
            recipients, what = self.s.notify_ccp_list, "needs triage in the CCP review queue"
        elif kind == "booking_approval":
            recipients, what = self.s.notify_booking_list, "is a New Booking waiting for confirmation"
        else:
            return
        link = f"{self.s.public_base_url.rstrip('/')}/#/reviews/{review_id}"
        self._send(recipients, f"[EIE] Email {what}: {subject or '(no subject)'}",
                   f"An email {what}.\n\nSubject: {subject}\nWhy: {reason}\n\nOpen it: {link}\n",
                   email_id=email_id, review_id=review_id, kind=kind)

    def alert(self, title: str, error: str, event: str = "ingestion.failed", detail: str = "") -> None:
        """A failure staff should know about: audited every time, emailed at most once per cooldown."""
        self.store.audit(event, title=title, error=error)
        now = time.monotonic()
        if now - self._last_alert.get(error, -ALERT_COOLDOWN_SECONDS) < ALERT_COOLDOWN_SECONDS:
            return
        self._last_alert[error] = now
        self._send(self.s.notify_alerts_list, f"[EIE] ALERT: {title}",
                   f"{title}\n\n{error}\n\n" + (detail or "No email is lost: unread mail stays in the mailbox and "
                                                    "is retried on the next check.") + "\n", kind="alert")

    def _send(self, recipients: list[str], subject: str, body: str, *, email_id: Optional[int] = None,
              review_id: Optional[int] = None, kind: str = "") -> None:
        if not recipients:
            log.info("No notification recipients configured; skipped: %s", subject)
            return
        try:
            transport, ref = self.mailer.send_message(recipients, subject, body)
            self.store.audit("notification.sent", email_id=email_id, review_id=review_id, kind=kind,
                             recipients=recipients, transport=transport, dry_run=transport == "dry_run")
        except Exception as exc:
            log.warning("Notification failed: %s", exc)
            self.store.audit("notification.failed", email_id=email_id, review_id=review_id, kind=kind,
                             recipients=recipients, error=repr(exc))
