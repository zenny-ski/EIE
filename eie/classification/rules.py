"""Keyword/regex classifier. Used offline (USE_LLM=false) and as a fallback when the LLM call fails.

Its confidence is capped below the auto-create threshold, so rule-based results always get a human look.
"""
import re

from ..models import Category, ClassificationResult, Email, ExtractedInfo, Passenger

RULES_MAX_CONFIDENCE = 70

KEYWORDS = {
    Category.ESCALATION: [
        "escalat", "urgent", "not arrived", "no show", "no-show", "did not turn up", "didn't turn up",
        "unsafe", "rash driving", "harass", "overcharg", "stranded", "missed my flight", "missed the flight",
        "unacceptable", "legal action", "worst", "immediately", "again and again", "terminate",
    ],
    Category.FEEDBACK: [
        "feedback", "thank you", "thanks for", "great service", "rating", "experience", "suggestion",
        "polite", "punctual", "happy with", "satisfied", "well done", "appreciate", "courteous",
    ],
    Category.NEW_BOOKING: [
        "book a cab", "book a car", "booking request", "new booking", "need a cab", "need a car",
        "require a cab", "require a car", "please arrange", "arrange a cab", "arrange a car", "pickup",
        "pick-up", "pick up", "airport transfer", "reserve", "outstation", "drop at", "drop to",
    ],
}

PHONE_RE = re.compile(r"(?<!\w)(\+?\d[\d\s-]{8,}\d)(?!\w)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

REPLY_TEMPLATES = {
    Category.FEEDBACK: (
        "Dear Customer,\n\nThank you for sharing your feedback. We have recorded it and shared it with the "
        "relevant team.\n\nRegards,\n[Agent Name]"
    ),
    Category.ESCALATION: (
        "Dear Customer,\n\nWe sincerely apologise for the inconvenience. Your concern has been escalated to "
        "our operations team, who will contact you shortly.\n\nRegards,\n[Agent Name]"
    ),
    Category.NEW_BOOKING: (
        "Dear Customer,\n\nThank you for your booking request. We are reviewing the details and will share "
        "the confirmation shortly.\n\nRegards,\n[Agent Name]"
    ),
    Category.UNCLASSIFIED: (
        "Dear Customer,\n\nThank you for your email. Our team will review it and get back to you.\n\n"
        "Regards,\n[Agent Name]"
    ),
}


def extract_booking_ids(text: str, pattern: str) -> list[str]:
    ids = []
    for match in re.finditer(pattern, text, flags=re.IGNORECASE):
        value = (match.group(1) if match.groups() else match.group(0)).upper()
        if value not in ids:
            ids.append(value)
    return ids


class RuleClassifier:
    name = "rules"

    def __init__(self, booking_id_regex: str):
        self.booking_id_regex = booking_id_regex

    def classify(self, email: Email) -> ClassificationResult:
        text = "\n".join([email.subject, email.body] + [a.get("text") or "" for a in email.attachments])
        lowered = text.lower()
        scores = {cat: sum(1 for kw in kws if kw in lowered) for cat, kws in KEYWORDS.items()}
        best = max(scores, key=scores.get)
        ranked = sorted(scores.values(), reverse=True)
        top, runner_up = ranked[0], ranked[1]

        if top == 0:
            category, confidence, reasoning = Category.UNCLASSIFIED, 30, "No category keywords matched."
        else:
            category = best
            margin = top - runner_up
            confidence = min(RULES_MAX_CONFIDENCE, 35 + 10 * top + 5 * margin)
            reasoning = f"Keyword match: {', '.join(f'{c.value}={s}' for c, s in scores.items())}."

        passengers = [Passenger(phone=p.strip()) for p in PHONE_RE.findall(email.body)[:3]]
        return ClassificationResult(
            category=category,
            confidence=confidence,
            reasoning=reasoning,
            extracted=ExtractedInfo(
                booking_ids=extract_booking_ids(text, self.booking_id_regex),
                passengers=passengers,
                summary=email.subject or None,
            ),
            suggested_reply=REPLY_TEMPLATES[category],
            classifier=self.name,
        )
