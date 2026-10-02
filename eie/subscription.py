"""Keeps the Microsoft Graph webhook subscription alive (FR-002).

Graph change-notification subscriptions expire after at most about three days. While the server runs, this
creates the subscription when it's missing and renews it before it lapses, so mail is picked up within seconds
instead of at the next poll. It needs GRAPH_WEBHOOK_SECRET and a public https PUBLIC_BASE_URL; without those it
does nothing and polling carries on. A failure is audited and alerted, never fatal: polling covers the gap.
"""
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from .config import Settings
from .storage import Store

log = logging.getLogger("eie")
KEY = "graph_subscription"
RENEW_WITHIN = timedelta(hours=12)   # renew when less than this is left
CHECK_EVERY = 3600                   # seconds between checks


def _parse(expiry: str) -> datetime:
    return datetime.fromisoformat(expiry[:19]).replace(tzinfo=timezone.utc)  # Graph sends 7 fractional digits


class SubscriptionManager:
    def __init__(self, settings: Settings, store: Store, notifier=None,
                 graph_factory: Optional[Callable] = None):
        self.s = settings
        self.store = store
        self.notifier = notifier
        self._graph = graph_factory or self._default_graph
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _default_graph(self):
        from .ingestion.graph import GraphSource

        return GraphSource(self.s)

    @property
    def url(self) -> str:
        return self.s.public_base_url.rstrip("/") + "/api/graph/webhook"

    @property
    def enabled(self) -> bool:
        return bool(self.s.graph_configured and self.s.graph_webhook_secret
                    and self.s.public_base_url.lower().startswith("https://"))

    def status(self) -> dict:
        info = self.store.kv_get(KEY) or {}
        return {"enabled": self.enabled, "url": self.url, **{k: info.get(k) for k in ("id", "expires")}}

    def ensure(self, force: bool = False) -> Optional[dict]:
        """Creates or renews the subscription if needed; returns what is stored, or None when disabled."""
        if not self.enabled:
            return None
        info = self.store.kv_get(KEY)
        fresh = (info and info.get("url") == self.url and not force
                 and _parse(info["expires"]) - datetime.now(timezone.utc) > RENEW_WITHIN)
        if fresh:
            return info
        graph = self._graph()
        if info and info.get("url") == self.url:
            try:
                renewed = graph.renew(info["id"])
                info = {**info, "expires": renewed["expirationDateTime"]}
                self.store.kv_set(KEY, info)
                self.store.audit("webhook.renewed", id=info["id"], expires=info["expires"])
                return info
            except Exception as exc:  # e.g. Graph already dropped it: make a new one
                log.info("Renewing the Graph subscription failed (%s); creating a new one", exc)
        created = graph.subscribe(self.url, self.s.graph_webhook_secret)
        info = {"id": created["id"], "expires": created["expirationDateTime"], "url": self.url}
        self.store.kv_set(KEY, info)
        self.store.audit("webhook.subscribed", id=info["id"], expires=info["expires"], url=self.url)
        return info

    def _safe_ensure(self) -> None:
        try:
            self.ensure()
        except Exception as exc:
            log.warning("Graph webhook subscription failed: %s", exc)
            self.store.audit("webhook.subscription_failed", error=str(exc))
            if self.notifier:
                self.notifier.alert("Graph webhook subscription failed (mail is still polled)", str(exc))

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="eie-subscription", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        self._safe_ensure()
        while not self._stop.wait(CHECK_EVERY):
            self._safe_ensure()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
