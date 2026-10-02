"""Background mailbox polling: runs the pipeline every `interval` seconds."""
import logging
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

log = logging.getLogger("eie")


class AutoFetcher:
    def __init__(self, build_pipeline: Callable, interval: float = 8, enabled: bool = True,
                 on_error: Optional[Callable[[Exception], None]] = None):
        self._build_pipeline = build_pipeline
        self._on_error = on_error  # e.g. alert staff that ingestion is failing (FRD 13.1)
        # Serialises mailbox runs (auto and manual) so two runs never process the same email.
        # The store has its own lock, so the GUI stays responsive while a slow LLM call is in flight.
        self._lock = threading.Lock()
        self.interval = max(1.0, float(interval))
        self.enabled = enabled
        self._pipeline = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_run: Optional[str] = None
        self.last_count = 0
        self.last_error: Optional[str] = None
        self.processed_total = 0
        self.running = False
        self.deferred = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="eie-autofetch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": self.running,
            "deferred": self.deferred,
            "interval": self.interval,
            "last_run": self.last_run,
            "last_count": self.last_count,
            "last_error": self.last_error,
            "processed_total": self.processed_total,
        }

    def run_once(self, manual: bool = False) -> list:
        with self._lock:
            self.running = True
            try:
                return self._run(manual)
            finally:
                self.running = False

    def _run(self, manual: bool) -> list:
        if self._pipeline is None:
            self._pipeline = self._build_pipeline()  # raises ValueError if no mailbox is configured
        # Empty auto polls are not audited, otherwise the log fills with a row every few seconds.
        results = self._pipeline.run_once(audit_empty=manual)
        self.deferred = self._pipeline.deferred
        self.last_run = datetime.now(timezone.utc).isoformat()
        self.last_count = len(results)
        if not manual:  # manual runs report their own results; this counter drives the GUI's auto refresh
            self.processed_total += len(results)
        self.last_error = None
        return results

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            if not self.enabled:
                continue
            try:
                self.run_once()
            except Exception as exc:
                self.last_error = str(exc)
                log.warning("Auto-fetch failed: %s", exc)
                if self._on_error:
                    try:
                        self._on_error(exc)
                    except Exception:
                        log.exception("Error handler failed")
