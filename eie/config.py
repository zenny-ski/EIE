import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


@dataclass
class Settings:
    """Not frozen: the admin settings screen changes some values while the app runs (see eie/adminconfig.py)."""
    db_path: str = "eie.db"
    timezone: str = "Asia/Kolkata"  # IANA name; dashboard periods and date filters use this calendar

    email_source: str = "auto"
    fetch_limit: int = 25
    attachment_text: bool = True  # read text out of PDF/Word/text attachments for the classifier (FRD 4.2)
    mark_as_read: bool = True
    auto_fetch: bool = True
    auto_fetch_seconds: int = 8

    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: str = ""
    graph_mailbox: str = ""

    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"

    use_llm: bool = True
    llm_provider: str = "anthropic"  # anthropic | gemini
    anthropic_model: str = "claude-opus-5"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.7-flash"
    booking_id_regex: str = r"\b((?:BK|BKG|IND|SKL|TRP)[-/]?\d{4,})\b"

    confidence_high: int = 85
    confidence_medium: int = 50
    # Per-bucket overrides (FR-012); -1 = use the global value above
    confidence_high_feedback: int = -1
    confidence_medium_feedback: int = -1
    confidence_high_escalation: int = -1
    confidence_medium_escalation: int = -1

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""

    public_base_url: str = "http://127.0.0.1:8000"  # used for the source-email link sent downstream

    # Comma-separated lists. NOTIFY_CCP: CCP Module Managers and Executives (FR-019); NOTIFY_BOOKING: who is
    # told about New Bookings (FR-020); NOTIFY_ALERTS: ingestion failures (13.1). The latter two default to NOTIFY_CCP.
    notify_ccp: str = ""
    notify_booking: str = ""
    notify_alerts: str = ""
    # "Name=address" pairs for the queue's Forward to Team action (FR-035), e.g. "Invoicing=inv@x.com,IT=it@x.com"
    forward_teams: str = ""
    # "name:role:token" entries. Empty = no login (everyone is an admin); see eie/auth.py for the roles.
    users: str = ""
    graph_webhook_secret: str = ""

    # Admin-editable reply wording (blank = use the AI draft / built-in template). Bucket keys: feedback,
    # escalation, new_booking, unclassified. Missing-details keys: essential, mandatory, semi_mandatory.
    reply_signature: str = ""  # replaces the "[Agent Name]" placeholder in every draft
    reply_templates: dict = field(default_factory=dict)
    missing_templates: dict = field(default_factory=dict)

    dry_run: bool = True
    whatsapp_feedback_url: str = ""
    whatsapp_feedback_token: str = ""
    escalation_url: str = ""
    escalation_token: str = ""
    indecab_url: str = ""
    indecab_token: str = ""

    @property
    def graph_configured(self) -> bool:
        return all((self.graph_tenant_id, self.graph_client_id, self.graph_client_secret, self.graph_mailbox))

    @property
    def imap_configured(self) -> bool:
        return all((self.imap_host, self.imap_user, self.imap_password))

    @staticmethod
    def _split(value: str) -> list[str]:
        return [v.strip() for v in value.replace(";", ",").split(",") if v.strip()]

    @property
    def notify_ccp_list(self) -> list[str]:
        return self._split(self.notify_ccp)

    @property
    def notify_booking_list(self) -> list[str]:
        return self._split(self.notify_booking) or self.notify_ccp_list

    @property
    def notify_alerts_list(self) -> list[str]:
        return self._split(self.notify_alerts) or self.notify_ccp_list

    @property
    def team_addresses(self) -> dict[str, str]:
        teams = {}
        for pair in self._split(self.forward_teams):
            name, _, address = pair.partition("=")
            if name.strip() and "@" in address:
                teams[name.strip()] = address.strip()
        return teams

    def thresholds(self, category) -> tuple[int, int]:
        """(high, medium) confidence thresholds for a bucket."""
        key = getattr(category, "value", category)
        high = getattr(self, f"confidence_high_{key}", -1)
        medium = getattr(self, f"confidence_medium_{key}", -1)
        return (self.confidence_high if high < 0 else high,
                self.confidence_medium if medium < 0 else medium)

    @property
    def smtp_configured(self) -> bool:
        return all((self.smtp_host, self.smtp_user, self.smtp_password))

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        d = cls()
        return cls(
            db_path=_str("EIE_DB_PATH", d.db_path),
            timezone=_str("EIE_TIMEZONE", d.timezone),
            email_source=_str("EMAIL_SOURCE", d.email_source).lower(),
            fetch_limit=_int("FETCH_LIMIT", d.fetch_limit),
            attachment_text=_bool("ATTACHMENT_TEXT", d.attachment_text),
            mark_as_read=_bool("MARK_AS_READ", d.mark_as_read),
            auto_fetch=_bool("AUTO_FETCH", d.auto_fetch),
            auto_fetch_seconds=_int("AUTO_FETCH_SECONDS", d.auto_fetch_seconds),
            graph_tenant_id=_str("GRAPH_TENANT_ID"),
            graph_client_id=_str("GRAPH_CLIENT_ID"),
            graph_client_secret=_str("GRAPH_CLIENT_SECRET"),
            graph_mailbox=_str("GRAPH_MAILBOX"),
            imap_host=_str("IMAP_HOST"),
            imap_port=_int("IMAP_PORT", d.imap_port),
            imap_user=_str("IMAP_USER"),
            imap_password=_str("IMAP_PASSWORD"),
            imap_folder=_str("IMAP_FOLDER", d.imap_folder),
            use_llm=_bool("USE_LLM", d.use_llm),
            llm_provider=_str("LLM_PROVIDER", d.llm_provider).lower(),
            anthropic_model=_str("ANTHROPIC_MODEL", d.anthropic_model),
            gemini_api_key=_str("GEMINI_API_KEY"),
            gemini_model=_str("GEMINI_MODEL", d.gemini_model),
            booking_id_regex=_str("BOOKING_ID_REGEX", d.booking_id_regex),
            confidence_high=_int("CONFIDENCE_HIGH", d.confidence_high),
            confidence_medium=_int("CONFIDENCE_MEDIUM", d.confidence_medium),
            confidence_high_feedback=_int("CONFIDENCE_HIGH_FEEDBACK", -1),
            confidence_medium_feedback=_int("CONFIDENCE_MEDIUM_FEEDBACK", -1),
            confidence_high_escalation=_int("CONFIDENCE_HIGH_ESCALATION", -1),
            confidence_medium_escalation=_int("CONFIDENCE_MEDIUM_ESCALATION", -1),
            smtp_host=_str("SMTP_HOST"),
            smtp_port=_int("SMTP_PORT", d.smtp_port),
            smtp_user=_str("SMTP_USER"),
            smtp_password=_str("SMTP_PASSWORD"),
            smtp_from=_str("SMTP_FROM"),
            public_base_url=_str("PUBLIC_BASE_URL", d.public_base_url),
            notify_ccp=_str("NOTIFY_CCP"),
            notify_booking=_str("NOTIFY_BOOKING"),
            notify_alerts=_str("NOTIFY_ALERTS"),
            forward_teams=_str("FORWARD_TEAMS"),
            users=_str("EIE_USERS"),
            graph_webhook_secret=_str("GRAPH_WEBHOOK_SECRET"),
            dry_run=_bool("DRY_RUN", d.dry_run),
            whatsapp_feedback_url=_str("WHATSAPP_FEEDBACK_URL"),
            whatsapp_feedback_token=_str("WHATSAPP_FEEDBACK_TOKEN"),
            escalation_url=_str("ESCALATION_URL"),
            escalation_token=_str("ESCALATION_TOKEN"),
            indecab_url=_str("INDECAB_URL"),
            indecab_token=_str("INDECAB_TOKEN"),
        )
