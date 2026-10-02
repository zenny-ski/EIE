"""Gemini-based classifier: same structured contract as the Claude classifier (llm.py),
selected via LLM_PROVIDER=gemini so the two are swappable without touching call sites."""
import time
from typing import Optional

from google import genai
from google.genai import types
from google.genai import errors as genai_errors
from pydantic import BaseModel

from ..models import Category, ClassificationResult, Email, ExtractedInfo
from .llm import SYSTEM_PROMPT, render_email
from .rules import extract_booking_ids

# One quick retry for a momentary 503 (model overloaded). Longer outages and 429 quota errors are
# not retried here: FallbackClassifier defers the email and retries it later with backoff, because
# every attempt counts against the key's quota (free tier: 20 requests a day).
RETRYABLE_STATUS_CODES = {503}
MAX_RETRIES = 1
BASE_DELAY_SECONDS = 2
REQUEST_TIMEOUT_MS = 60_000


class _LLMOutput(BaseModel):
    category: Category
    confidence: int
    reasoning: str
    extracted: ExtractedInfo
    suggested_reply: str


class GeminiClassifier:
    name = "llm"

    def __init__(self, model: str, booking_id_regex: str, api_key: str = "",
                 client: Optional["genai.Client"] = None):
        self.model = model
        self.booking_id_regex = booking_id_regex
        # genai.Client() reads GEMINI_API_KEY / GOOGLE_API_KEY from the environment if api_key is empty.
        self.client = client or genai.Client(api_key=api_key or None,
                                             http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS))

    def _generate(self, email: Email):
        last_exc: Optional[Exception] = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                return self.client.models.generate_content(
                    model=self.model,
                    contents=render_email(email),
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        response_mime_type="application/json",
                        response_schema=_LLMOutput,
                    ),
                )
            except genai_errors.APIError as exc:
                last_exc = exc
                if exc.code not in RETRYABLE_STATUS_CODES or attempt == MAX_RETRIES:
                    raise
                time.sleep(BASE_DELAY_SECONDS * (2 ** attempt))
        raise last_exc  # pragma: no cover - loop always returns or raises above

    def classify(self, email: Email) -> ClassificationResult:
        response = self._generate(email)
        out = response.parsed
        if out is None:
            reason = response.candidates[0].finish_reason if response.candidates else "unknown"
            raise RuntimeError(f"No classification returned (finish_reason={reason})")

        # Regex-found booking IDs are added in case the model missed one.
        for booking_id in extract_booking_ids(f"{email.subject}\n{email.body}", self.booking_id_regex):
            if booking_id not in out.extracted.booking_ids:
                out.extracted.booking_ids.append(booking_id)

        return ClassificationResult(
            category=out.category,
            confidence=max(0, min(100, out.confidence)),
            reasoning=out.reasoning,
            extracted=out.extracted,
            suggested_reply=out.suggested_reply,
            classifier=self.name,
        )
