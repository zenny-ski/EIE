"""Sending replies to customers: the editable AI draft goes out via Graph (in-thread) or SMTP."""
import smtplib
import uuid
from typing import Optional
from email.message import EmailMessage
from email.utils import parseaddr

from .config import Settings
from .ingestion.graph import GraphSource
from .storage import Store


class ReplyError(Exception):
    pass


class Mailer:
    """Transport for outgoing replies. Returns (transport, external_ref)."""

    def __init__(self, settings: Settings):
        self.s = settings

    def send(self, email: dict, to: str, subject: str, body: str) -> tuple[str, str]:
        if self.s.dry_run:
            return "dry_run", f"dryrun-reply-{uuid.uuid4().hex[:8]}"
        if email["source"] == "graph" and self.s.graph_configured and email.get("source_ref"):
            try:
                GraphSource(self.s).reply(email["source_ref"], body)
                return "graph", "graph-reply"
            except Exception:
                if not self.s.smtp_configured:
                    raise
                # Graph can't send (e.g. no Mail.Send permission): fall through to SMTP
        if self.s.smtp_configured:
            return "smtp", self._smtp(to, subject, body, email)
        raise ReplyError("No way to send: configure Graph with Mail.Send or SMTP_HOST/SMTP_USER/SMTP_PASSWORD")

    def send_message(self, to: list[str], subject: str, body: str) -> tuple[str, str]:
        """A new outgoing message (forwards, notifications): Graph sendMail, else SMTP, else dry run."""
        if self.s.dry_run:
            return "dry_run", f"dryrun-mail-{uuid.uuid4().hex[:8]}"
        if self.s.graph_configured:
            try:
                GraphSource(self.s).send_mail(to, subject, body)
                return "graph", "graph-sendmail"
            except Exception:
                if not self.s.smtp_configured:
                    raise
        if self.s.smtp_configured:
            return "smtp", self._smtp(", ".join(to), subject, body, None)
        raise ReplyError("No way to send: configure Graph with Mail.Send or SMTP_HOST/SMTP_USER/SMTP_PASSWORD")

    def _smtp(self, to: str, subject: str, body: str, email: Optional[dict]) -> str:
        msg = EmailMessage()
        msg["From"] = self.s.smtp_from or self.s.smtp_user
        msg["To"] = to
        msg["Subject"] = subject
        msg["Message-ID"] = f"<{uuid.uuid4().hex}@eie>"
        if email and email["message_id"].startswith("<"):  # keep it in the customer's thread
            msg["In-Reply-To"] = msg["References"] = email["message_id"]
        msg.set_content(body)
        with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(self.s.smtp_user, self.s.smtp_password)
            smtp.send_message(msg)
        return msg["Message-ID"]


class ReplyService:
    def __init__(self, store: Store, mailer: Mailer):
        self.store = store
        self.mailer = mailer

    def send(self, email_id: int, user: str, body: str, *, to: str = "", subject: str = "",
             resend: bool = False) -> dict:
        email = self.store.get_email(email_id)
        if email is None:
            raise ReplyError(f"Email {email_id} not found")
        body = body.strip()
        if not body:
            raise ReplyError("The reply is empty")
        to = (to or parseaddr(email["sender"])[1]).strip()
        if "@" not in to or any(c in to for c in "\r\n"):
            raise ReplyError(f"'{to}' is not a valid recipient address")
        subject = " ".join((subject or "").split()) or _reply_subject(email["subject"])

        sent = [r for r in self.store.list_replies(email_id) if r["status"] in ("sent", "dry_run")]
        if sent and not resend:
            raise ReplyError("A reply was already sent for this email; confirm to send another")

        edited = body != (email["suggested_reply"] or "").strip()
        try:
            transport, ref = self.mailer.send(email, to, subject, body)
        except Exception as exc:
            reply_id = self.store.add_reply(email_id, to, subject, body, edited=edited, status="failed",
                                            sent_by=user, error=repr(exc))
            self.store.audit("reply.failed", user, email_id=email_id, reply_id=reply_id, to=to, error=repr(exc))
            raise ReplyError(f"Sending failed: {exc}") from exc

        status = "dry_run" if transport == "dry_run" else "sent"
        reply_id = self.store.add_reply(email_id, to, subject, body, edited=edited, status=status, sent_by=user,
                                        transport=transport, external_ref=ref)
        self.store.audit("reply.sent", user, email_id=email_id, reply_id=reply_id, to=to, subject=subject,
                         transport=transport, external_ref=ref, edited=edited, dry_run=status == "dry_run")
        return {"id": reply_id, "status": status, "transport": transport, "to": to, "external_ref": ref}


def _reply_subject(subject: str) -> str:
    subject = " ".join((subject or "").split())
    return subject if subject.lower().startswith("re:") else f"Re: {subject}".strip()
