import re
import time
from typing import Callable, Optional

from ..config import Settings
from ..models import ClassificationResult, Email
from .rules import RuleClassifier

# 429 rate limit, 5xx server trouble, 529 Anthropic overloaded: usually gone within a minute or two.
TRANSIENT_STATUS_CODES = {408, 429, 500, 502, 503, 504, 529}
MAX_DEFERRALS = 5
# Wait before retrying a deferred email: 15s, 30s, 60s, 120s, 240s (about 8 minutes in all).
# Retrying on every fetch would spend the LLM quota on one email; free Gemini keys get 20 requests a day.
BASE_RETRY_SECONDS = 15


class ClassificationFailed(Exception):
    """The model could not classify this email (FRD 13.2). `hint` is what the keyword rules could read from it,
    used only to pre-fill the review form: it is not a classification and proposes no bucket."""

    def __init__(self, error: Exception, hint: ClassificationResult):
        super().__init__(str(error))
        self.error = error
        self.hint = hint


class ClassificationDeferred(Exception):
    """The LLM is temporarily unavailable; leave the email unread and try again on a later fetch.
    `waiting` is True when the email was skipped without calling the LLM (its retry time hasn't come)."""

    def __init__(self, message: str, waiting: bool = False):
        super().__init__(message)
        self.waiting = waiting


def is_daily_quota(exc: Exception) -> bool:
    """A per-day quota won't reset for hours, so retrying within minutes only wastes calls."""
    return "PerDay" in str(exc)


def is_transient(exc: Exception) -> bool:
    if is_daily_quota(exc):
        return False
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in TRANSIENT_STATUS_CODES:
        return True
    name = type(exc).__name__
    return isinstance(exc, (TimeoutError, ConnectionError)) or "Timeout" in name or "Connection" in name


def retry_hint_seconds(exc: Exception) -> float:
    """The wait the provider asked for, e.g. Gemini's "Please retry in 43.6s" / retryDelay '43s'."""
    m = re.search(r"retry in ([\d.]+)s|retryDelay'?\"?:\s*'?\"?([\d.]+)s", str(exc))
    return float(m.group(1) or m.group(2)) if m else 0.0


class FallbackClassifier:
    """Tries the LLM first. A temporary outage defers the email (up to max_deferrals retries, spaced out
    with backoff). Any other error, a daily quota, or an outage that outlasts the retries raises
    ClassificationFailed, so the email lands in the CCP queue flagged as a system error instead of being
    filed on a keyword guess."""

    def __init__(self, primary, fallback: RuleClassifier,
                 on_failure: Optional[Callable[[Email, Exception], None]] = None,
                 max_deferrals: int = MAX_DEFERRALS, base_retry_seconds: float = BASE_RETRY_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self.primary = primary
        self.fallback = fallback
        self.on_failure = on_failure
        self.max_deferrals = max_deferrals
        self.base_retry_seconds = base_retry_seconds
        self.clock = clock
        self._deferrals: dict[str, tuple[int, float]] = {}  # message_id -> (attempts, retry_at)

    def classify(self, email: Email) -> ClassificationResult:
        attempts, retry_at = self._deferrals.get(email.message_id, (0, 0.0))
        wait = retry_at - self.clock()
        if wait > 0:
            raise ClassificationDeferred(f"retrying in {wait:.0f}s", waiting=True)
        try:
            result = self.primary.classify(email)
            self._deferrals.pop(email.message_id, None)
            return result
        except Exception as exc:
            attempt = attempts + 1
            if is_transient(exc) and attempt <= self.max_deferrals:
                delay = max(retry_hint_seconds(exc), self.base_retry_seconds * 2 ** (attempt - 1))
                self._deferrals[email.message_id] = (attempt, self.clock() + delay)
                raise ClassificationDeferred(
                    f"attempt {attempt}/{self.max_deferrals}, next try in {delay:.0f}s: {exc}") from exc
            self._deferrals.pop(email.message_id, None)
            if self.on_failure:
                self.on_failure(email, exc)
            raise ClassificationFailed(exc, self.fallback.classify(email)) from exc


def build_classifier(settings: Settings, on_failure: Optional[Callable[[Email, Exception], None]] = None):
    rules = RuleClassifier(settings.booking_id_regex)
    if not settings.use_llm:
        return rules

    if settings.llm_provider == "gemini":
        from .gemini import GeminiClassifier

        primary = GeminiClassifier(settings.gemini_model, settings.booking_id_regex, settings.gemini_api_key)
    else:
        from .llm import LLMClassifier

        primary = LLMClassifier(settings.anthropic_model, settings.booking_id_regex)

    return FallbackClassifier(primary, rules, on_failure)
