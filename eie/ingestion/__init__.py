from typing import Callable, Optional, Protocol

from ..config import Settings
from ..models import Email


class EmailSource(Protocol):
    name: str

    def fetch_unread(self, limit: int) -> list[Email]: ...

    def mark_processed(self, email: Email) -> None: ...


class FallbackSource:
    """Reads from the primary source; if it fails, reads from the secondary instead."""

    name = "fallback"

    def __init__(self, primary: EmailSource, secondary: EmailSource,
                 on_fallback: Optional[Callable[[Exception], None]] = None):
        self.primary = primary
        self.secondary = secondary
        self.on_fallback = on_fallback

    def fetch_unread(self, limit: int) -> list[Email]:
        try:
            return self.primary.fetch_unread(limit)
        except Exception as exc:
            if self.on_fallback:
                self.on_fallback(exc)
            return self.secondary.fetch_unread(limit)

    def mark_processed(self, email: Email) -> None:
        target = self.primary if email.source == self.primary.name else self.secondary
        target.mark_processed(email)


def build_source(settings: Settings, on_fallback: Optional[Callable[[Exception], None]] = None) -> EmailSource:
    from .graph import GraphSource
    from .imap import ImapSource

    mode = settings.email_source
    if mode == "graph":
        return GraphSource(settings)
    if mode == "imap":
        return ImapSource(settings)
    if mode != "auto":
        raise ValueError(f"Unknown EMAIL_SOURCE {mode!r} (expected auto, graph or imap)")

    if settings.graph_configured and settings.imap_configured:
        return FallbackSource(GraphSource(settings), ImapSource(settings), on_fallback)
    if settings.graph_configured:
        return GraphSource(settings)
    if settings.imap_configured:
        return ImapSource(settings)
    raise ValueError("No email source configured: set the GRAPH_* and/or IMAP_* variables in .env")
