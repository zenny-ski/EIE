"""IMAP mailbox reader, used as a fallback when Graph is unavailable."""
import email as email_lib
import imaplib
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import parsedate_to_datetime

from .. import attachments as attachment_reader
from ..config import Settings
from ..models import Email
from ..textutil import html_to_text, normalize


def _thread_id(msg: EmailMessage) -> str:
    """The thread root's Message-ID: first entry of References, else In-Reply-To, else empty (a new thread)."""
    refs = str(msg["References"] or "").split()
    return refs[0] if refs else str(msg["In-Reply-To"] or "").strip()


class ImapSource:
    name = "imap"

    def __init__(self, settings: Settings):
        if not settings.imap_configured:
            raise ValueError("IMAP is not configured (IMAP_HOST/USER/PASSWORD)")
        self.s = settings

    def _connect(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(self.s.imap_host, self.s.imap_port)
        conn.login(self.s.imap_user, self.s.imap_password)
        conn.select(self.s.imap_folder)
        return conn

    def fetch_unread(self, limit: int) -> list[Email]:
        conn = self._connect()
        try:
            typ, data = conn.uid("SEARCH", None, "UNSEEN")
            if typ != "OK":
                raise RuntimeError(f"IMAP search failed: {data}")
            emails = []
            for uid in data[0].split()[:limit]:
                # BODY.PEEK keeps the message unread until the pipeline has processed it
                typ, msg_data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
                if typ != "OK" or not msg_data or msg_data[0] is None:
                    continue
                msg = email_lib.message_from_bytes(msg_data[0][1], policy=default_policy)
                emails.append(self._to_email(msg, uid.decode()))
            return emails
        finally:
            conn.logout()

    def mark_processed(self, email: Email) -> None:
        conn = self._connect()
        try:
            conn.uid("STORE", email.source_ref, "+FLAGS", "(\\Seen)")
        finally:
            conn.logout()

    def _attachments(self, msg: EmailMessage) -> list[dict]:
        out = []
        for part in msg.iter_attachments():
            data = part.get_payload(decode=True) if not part.is_multipart() else None
            data = data if isinstance(data, bytes) else b""
            entry = {"name": part.get_filename(), "content_type": part.get_content_type(), "size": len(data),
                     "inline": False}
            if (self.s.attachment_text and len([a for a in out if "text" in a]) < attachment_reader.MAX_FILES
                    and attachment_reader.eligible(entry["name"], entry["content_type"], entry["size"])):
                entry["text"] = attachment_reader.extract_text(entry["name"], entry["content_type"], data)
            out.append(entry)
        return out

    def _to_email(self, msg: EmailMessage, uid: str) -> Email:
        plain_part = msg.get_body(preferencelist=("plain",))
        html_part = msg.get_body(preferencelist=("html",))
        html_body = html_part.get_content() if html_part is not None else ""
        if plain_part is not None:
            text = normalize(plain_part.get_content())
        else:
            text = html_to_text(html_body) if html_body else ""
        try:
            received = parsedate_to_datetime(msg["Date"]).isoformat() if msg["Date"] else ""
        except (TypeError, ValueError):
            received = ""
        return Email(
            message_id=(msg["Message-ID"] or f"imap-{uid}").strip(),
            subject=str(msg["Subject"] or ""),
            sender=str(msg["From"] or ""),
            received_at=received,
            body=text,
            source=self.name,
            source_ref=uid,
            conversation_id=_thread_id(msg),
            attachments=self._attachments(msg),
            body_html=html_body,
        )
