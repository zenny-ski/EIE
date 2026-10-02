"""Microsoft Graph mailbox reader (app-only / client-credentials flow)."""
import msal
import requests

from .. import attachments as attachment_reader
from ..config import Settings
from ..models import Email
from ..textutil import html_to_text, normalize

GRAPH = "https://graph.microsoft.com/v1.0"
SELECT = "id,internetMessageId,conversationId,subject,from,receivedDateTime,body,hasAttachments"
EXPAND = "attachments($select=id,name,contentType,size,isInline)"

HINTS = {
    401: "the token was rejected - check the tenant ID, client ID and secret",
    403: "the app lacks permission - grant the Mail.ReadWrite application permission and admin consent",
    404: "the mailbox wasn't found - check GRAPH_MAILBOX",
}


def _raise_for_graph_error(resp: requests.Response) -> None:
    if resp.ok:
        return
    try:
        err = resp.json().get("error", {})
        detail = f"{err.get('code')}: {err.get('message')}"
    except ValueError:
        detail = resp.text[:300]
    hint = HINTS.get(resp.status_code, "")
    raise RuntimeError(f"Graph API returned {resp.status_code} ({detail})" + (f" - {hint}" if hint else ""))


class GraphSource:
    name = "graph"

    def __init__(self, settings: Settings, timeout: int = 30):
        if not settings.graph_configured:
            raise ValueError("Graph is not configured (GRAPH_TENANT_ID/CLIENT_ID/CLIENT_SECRET/MAILBOX)")
        self.mailbox = settings.graph_mailbox
        self.timeout = timeout
        self.read_attachments = settings.attachment_text
        self._app = msal.ConfidentialClientApplication(
            settings.graph_client_id,
            authority=f"https://login.microsoftonline.com/{settings.graph_tenant_id}",
            client_credential=settings.graph_client_secret,
        )

    def _headers(self) -> dict:
        result = self._app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in result:
            raise RuntimeError(f"Graph auth failed: {result.get('error')}: {result.get('error_description')}")
        return {"Authorization": f"Bearer {result['access_token']}"}

    def fetch_unread(self, limit: int) -> list[Email]:
        resp = requests.get(
            f"{GRAPH}/users/{self.mailbox}/mailFolders/inbox/messages",
            headers=self._headers(),
            params={"$filter": "isRead eq false", "$top": str(limit), "$select": SELECT, "$expand": EXPAND},
            timeout=self.timeout,
        )
        _raise_for_graph_error(resp)
        emails = []
        for m in resp.json().get("value", []):
            email = self._to_email(m)
            self._read_attachments(email)
            emails.append(email)
        return sorted(emails, key=lambda e: e.received_at)

    def mark_processed(self, email: Email) -> None:
        resp = requests.patch(
            f"{GRAPH}/users/{self.mailbox}/messages/{email.source_ref}",
            headers=self._headers(),
            json={"isRead": True},
            timeout=self.timeout,
        )
        _raise_for_graph_error(resp)

    def send_mail(self, to: list[str], subject: str, body: str) -> None:
        """Sends a new message from the mailbox (needs Mail.Send); used for forwards and notifications."""
        headers = self._headers()
        headers.pop("Prefer", None)
        resp = requests.post(
            f"{GRAPH}/users/{self.mailbox}/sendMail",
            headers=headers,
            json={"message": {"subject": subject, "body": {"contentType": "Text", "content": body},
                              "toRecipients": [{"emailAddress": {"address": a}} for a in to]},
                  "saveToSentItems": True},
            timeout=self.timeout,
        )
        if resp.status_code == 403:
            raise RuntimeError("Graph refused to send - grant the Mail.Send application permission and admin consent")
        _raise_for_graph_error(resp)

    def subscribe(self, notification_url: str, secret: str, minutes: int = 4200) -> dict:
        """Creates a change-notification subscription so Graph calls our webhook when mail arrives (FR-002).
        Subscriptions expire after at most ~3 days, so re-run this to renew; polling covers any gap."""
        from datetime import datetime, timedelta, timezone

        expires = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
        headers = self._headers()
        headers.pop("Prefer", None)
        resp = requests.post(
            f"{GRAPH}/subscriptions",
            headers=headers,
            json={"changeType": "created", "notificationUrl": notification_url,
                  "resource": f"users/{self.mailbox}/mailFolders/inbox/messages",
                  "expirationDateTime": expires, "clientState": secret},
            timeout=self.timeout,
        )
        _raise_for_graph_error(resp)
        return resp.json()

    def renew(self, subscription_id: str, minutes: int = 4200) -> dict:
        """Extends an existing subscription; Graph answers 404 once it has expired."""
        from datetime import datetime, timedelta, timezone

        expires = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
        headers = self._headers()
        headers.pop("Prefer", None)
        resp = requests.patch(f"{GRAPH}/subscriptions/{subscription_id}", headers=headers,
                              json={"expirationDateTime": expires}, timeout=self.timeout)
        _raise_for_graph_error(resp)
        return resp.json()

    def reply(self, source_ref: str, comment: str) -> None:
        """Replies in the original thread (needs the Mail.Send application permission)."""
        headers = self._headers()
        headers.pop("Prefer", None)
        resp = requests.post(
            f"{GRAPH}/users/{self.mailbox}/messages/{source_ref}/reply",
            headers=headers,
            json={"comment": comment},
            timeout=self.timeout,
        )
        if resp.status_code == 403:
            raise RuntimeError("Graph refused the reply - grant the Mail.Send application permission and admin consent")
        _raise_for_graph_error(resp)

    def _read_attachments(self, email: Email) -> None:
        """Downloads the readable attachments (a few, small ones) and attaches their text for the classifier."""
        wanted = [a for a in email.attachments if a.get("id") and attachment_reader.eligible(
            a["name"], a["content_type"], a["size"], a["inline"])][:attachment_reader.MAX_FILES]
        for a in email.attachments:
            if a in wanted and getattr(self, "read_attachments", False):
                try:
                    resp = requests.get(f"{GRAPH}/users/{self.mailbox}/messages/{email.source_ref}"
                                        f"/attachments/{a['id']}/$value", headers=self._headers(), timeout=self.timeout)
                    if resp.ok:
                        a["text"] = attachment_reader.extract_text(a["name"], a["content_type"], resp.content)
                except requests.RequestException:
                    pass  # the attachment is still listed; the email is classified without its text
            a.pop("id", None)

    def _to_email(self, m: dict) -> Email:
        body = m.get("body") or {}
        content = body.get("content") or ""
        is_html = (body.get("contentType") or "").lower() == "html"
        text = html_to_text(content) if is_html else normalize(content)
        sender = (m.get("from") or {}).get("emailAddress") or {}
        return Email(
            message_id=m.get("internetMessageId") or m["id"],
            subject=m.get("subject") or "",
            sender=f"{sender.get('name', '')} <{sender.get('address', '')}>".strip(),
            received_at=m.get("receivedDateTime") or "",
            body=text,
            source=self.name,
            source_ref=m["id"],
            conversation_id=m.get("conversationId") or "",
            attachments=[{"id": a.get("id"), "name": a.get("name"), "content_type": a.get("contentType"),
                          "size": a.get("size"), "inline": bool(a.get("isInline"))}
                         for a in m.get("attachments") or []],
            body_html=content if is_html else "",
        )
