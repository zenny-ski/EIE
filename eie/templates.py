"""Reply templates for New Booking follow-ups that ask for missing details (FRD 10.3, FR-024/FR-037).

The tone depends on the most serious tier that has gaps. Templates are plain strings here; making them
editable per client group in admin settings is a later step.
"""
import re
from typing import Optional

from .routing.destinations import ESSENTIAL, MANDATORY, SEMI_MANDATORY

BUCKETS = ("feedback", "escalation", "new_booking", "unclassified")
PLACEHOLDERS = ("name", "subject", "booking_id", "summary", "company")  # usable as {name} in a template

TEMPLATES = {
    ESSENTIAL: (
        "Dear {name},\n\nThank you for your booking request. To go ahead we need the following details, "
        "as the booking cannot be created without them:\n\n{items}\n\n"
        "Please reply with these and we will take it from there.\n\nRegards,\n[Agent Name]"
    ),
    MANDATORY: (
        "Dear {name},\n\nThank you for your booking request - we have understood the trip. To finalise it we "
        "still need:\n\n{items}\n\nPlease reply with these at your earliest convenience.\n\nRegards,\n[Agent Name]"
    ),
    SEMI_MANDATORY: (
        "Dear {name},\n\nThank you for your booking request - it is in hand and the cab details will follow. "
        "To help us make the journey smoother, could you also share:\n\n{items}\n\nRegards,\n[Agent Name]"
    ),
}


def _first_name(sender: str) -> str:
    display = re.sub(r"\s*<[^>]*>\s*", "", sender or "").strip().strip('"')
    return display.split()[0] if display and "@" not in display else "Customer"


_FIELD = re.compile(r"\{(\w+)\}")
_ANY_BRACES = re.compile(r"\{[^{}]*\}")


def fill(template: str, **values) -> str:
    """Replaces {name}-style placeholders. Only plain {word} fields are substituted (no attribute or index
    access, no format specs) and anything else, including an unknown {word}, is left exactly as written."""
    return _FIELD.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), template)


def check_template(template: str, required: tuple = ()) -> Optional[str]:
    """An error message if the template can't be used, else None (used when an admin saves it)."""
    for found in _ANY_BRACES.findall(template):
        if not _FIELD.fullmatch(found):
            return f"has an unsupported placeholder {found}; only plain ones like {{name}} work"
    for key in required:
        if "{" + key + "}" not in template:
            return f"must contain {{{key}}}"
    return None


def missing_details_draft(sender: str, gaps: dict[str, list[str]], overrides: Optional[dict] = None) -> str:
    """The follow-up draft for the most serious tier with gaps; the lower tiers' gaps are listed too.
    `overrides` are the admin-edited templates by tier (blank = built-in)."""
    tier = next((t for t in (ESSENTIAL, MANDATORY, SEMI_MANDATORY) if gaps.get(t)), None)
    if tier is None:
        return ""
    template = ((overrides or {}).get(tier) or "").strip() or TEMPLATES[tier]
    labels = [label for t in (ESSENTIAL, MANDATORY, SEMI_MANDATORY) for label in gaps.get(t, [])]
    return fill(template, name=_first_name(sender), items="\n".join(f"- {label}" for label in labels))


def render_draft(settings, category: str, sender: str, subject: str, extracted, ai_draft: str) -> str:
    """The reply draft for an email: the admin's template for its bucket if there is one, else the draft the
    classifier wrote. The configured signature replaces the "[Agent Name]" placeholder either way."""
    template = (settings.reply_templates.get(category) or "").strip()
    text = ai_draft
    if template:
        text = fill(template, name=_first_name(sender), subject=subject or "",
                    booking_id=(extracted.booking_ids[0] if extracted.booking_ids else ""),
                    summary=extracted.summary or "", company=extracted.company or "")
    signature = (settings.reply_signature or "").strip()
    return text.replace("[Agent Name]", signature) if signature else text
