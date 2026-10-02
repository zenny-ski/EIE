"""Downstream system adapters and the payloads sent to them.

The real APIs of the WhatsApp Feedback Platform, Escalation Management System and IndeCab aren't wired
up yet: each is a JSON POST to a configurable URL. With DRY_RUN=true (or no URL set) nothing is sent and
a dry-run reference is returned, so the full pipeline can be exercised safely.

Calls are retried up to three times with exponential backoff on network errors, 429 and 5xx (a 4xx other
than 429 means the payload is wrong, so retrying is pointless). IndeCab pushes are blocked until the
mandatory fields are filled in.
"""
import re
import time
import uuid
from typing import Callable, Optional

import requests

from ..config import Settings
from ..models import ClassificationResult, Destination, ExtractedInfo

ATTEMPTS = 3
BACKOFF_SECONDS = 2.0  # waits 2s, then 4s

# IndeCab field tiers (FRD 10.3). Essential gaps block the push (FR-023); Mandatory gaps don't block but
# the reviewer must acknowledge them and the booking is pushed marked as incomplete; Semi-Mandatory gaps
# are informational. A path may list alternatives with "|" (any one satisfies it). The real tiers are set
# per client group in IndeCab: until that API is wired up, edit the defaults here.
ESSENTIAL, MANDATORY, SEMI_MANDATORY = "essential", "mandatory", "semi_mandatory"
INDECAB_TIERS = {
    ESSENTIAL: {
        "trip.pickup_location": "Pickup location",
        "trip.pickup_date": "Pickup date",
    },
    MANDATORY: {
        "trip.pickup_time": "Pickup time",
        "company": "Client / company",
        "trip.cost_centre": "Cost centre",
        "trip.concur_id": "Concur ID",
        "passengers.0.name": "Passenger name",
        "passengers.0.phone": "Passenger phone",
        "trip.drop_location": "Drop location",
        "trip.vehicle_type": "Vehicle class",
    },
    SEMI_MANDATORY: {
        "trip.flight_number|trip.train_number": "Flight or train number",
    },
}


DEFAULT_INDECAB_TIERS = {tier: dict(fields) for tier, fields in INDECAB_TIERS.items()}

_ISO_DATETIME = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{1,2}:\d{2})")
_TIME = re.compile(r"\b(\d{1,2}:\d{2}(?:\s?[ap]\.?m\.?)?|\d{1,2}(?:\.\d{2})?\s?[ap]\.?m\.?)(?!\w)", re.I)


def set_indecab_tiers(tiers: dict) -> None:
    """Replaces the field tiers in place (the admin settings screen and tests use this)."""
    INDECAB_TIERS.clear()
    INDECAB_TIERS.update({tier: dict(fields) for tier, fields in tiers.items()})


def derive_pickup(trip: Optional[dict]) -> dict:
    """Fills pickup_date / pickup_time from a combined pickup_datetime ("2026-10-05T09:30", "25 Sept at 7am")
    when they are empty, so emails that give both in one phrase still satisfy the separate fields."""
    trip = dict(trip or {})
    combined = (trip.get("pickup_datetime") or "").strip()
    if not combined:
        return trip
    iso = _ISO_DATETIME.match(combined)
    if iso:
        date, time_part = iso.group(1), iso.group(2)
    else:
        found = _TIME.search(combined)
        time_part = found.group(0).strip() if found else ""
        date = _TIME.sub("", combined) if found else combined
        date = re.sub(r"\b(at|from|by|around|@)\b", "", date, flags=re.I)
        date = " ".join(date.replace(",", " ").split()).strip(" -")
    if _blank(trip.get("pickup_date")) and date:
        trip["pickup_date"] = date
    if _blank(trip.get("pickup_time")) and time_part:
        trip["pickup_time"] = time_part
    return trip


class DispatchValidationError(Exception):
    """Nothing was sent. `tier` is the tier of the gaps: essential (hard block) or mandatory (needs an
    acknowledged gap)."""

    def __init__(self, destination: Destination, missing: list[str], tier: str = ESSENTIAL):
        self.destination = destination
        self.missing = missing
        self.tier = tier
        super().__init__(f"{destination.value}: missing {tier.replace('_', '-')} fields: {', '.join(missing)}")


def _lookup(payload: dict, path: str):
    node = payload
    for part in path.split("."):
        try:
            node = node[int(part)] if isinstance(node, list) else node.get(part)
        except (IndexError, ValueError, AttributeError, TypeError):
            return None
        if node is None:
            return None
    return node


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def field_gaps(destination: Destination, payload: dict) -> dict[str, list[str]]:
    """Labels of the empty fields per tier (IndeCab only; the other systems have no field tiers here)."""
    gaps: dict[str, list[str]] = {tier: [] for tier in INDECAB_TIERS}
    if destination != Destination.INDECAB:
        return gaps
    payload = payload | {"trip": derive_pickup(payload.get("trip"))}
    for tier, fields in INDECAB_TIERS.items():
        for paths, label in fields.items():
            if all(_blank(_lookup(payload, path)) for path in paths.split("|")):
                gaps[tier].append(label)
    return gaps


def build_payload(destination: Destination, email: dict, result: ClassificationResult,
                  base_url: str = "", parent: Optional[dict] = None) -> dict:
    """The pre-filled form data for a destination. `email` is the stored email row."""
    x = result.extracted
    base = {
        "source_email_id": email["id"],
        "source_email_link": f"{base_url.rstrip('/')}/#/emails/{email['id']}" if base_url else None,
        "message_id": email["message_id"],
        "sender": email["sender"],
        "subject": email["subject"],
        "received_at": email["received_at"],
        "booking_ids": x.booking_ids,
        "summary": x.summary,
        "confidence": result.confidence,
        "requester_verified": email.get("requester_verified"),
    }
    if parent:  # FR-034: records split off a thread point back to it
        base |= {
            "parent_email_id": parent["id"],
            "parent_ref": parent.get("external_ref"),
            "parent_link": f"{base_url.rstrip('/')}/#/emails/{parent['id']}" if base_url else None,
        }
    if destination == Destination.WHATSAPP_FEEDBACK:
        return base | {"trip_date": x.trip_date, "sentiment": x.sentiment, "passengers": [p.model_dump() for p in x.passengers]}
    if destination == Destination.ESCALATION_SYSTEM:
        return base | {
            "urgency": x.urgency or "high",
            "trip_date": x.trip_date,
            "company": x.company,
            "passengers": [p.model_dump() for p in x.passengers],
            "description": result.reasoning,
        }
    if destination == Destination.INDECAB:
        return base | {
            "company": x.company,
            "passengers": [p.model_dump() for p in x.passengers],
            "trip": derive_pickup(x.trip.model_dump() if x.trip else {}),
        }
    # CCP queue: everything the reviewer needs to decide
    return base | {
        "suggested_category": result.category.value,
        "reasoning": result.reasoning,
        "extracted": x.model_dump(),
    }


def payload_from_row(destination: Destination, email: dict, base_url: str = "",
                     parent: Optional[dict] = None) -> dict:
    """Rebuilds a destination payload from a stored email row (used when a reviewer re-routes an item)."""
    result = ClassificationResult(
        category=email["category"] or "unclassified", confidence=email["confidence"] or 0, reasoning="",
        extracted=ExtractedInfo(**(email["extracted"] or {})), suggested_reply=email["suggested_reply"] or "",
    )
    return build_payload(destination, email, result, base_url, parent)


class Dispatcher:
    def __init__(self, settings: Settings, timeout: int = 30, backoff: float = BACKOFF_SECONDS,
                 sleep: Callable[[float], None] = time.sleep):
        self.dry_run = settings.dry_run
        self.timeout = timeout
        self.backoff = backoff
        self._sleep = sleep
        self.endpoints = {
            Destination.WHATSAPP_FEEDBACK: (settings.whatsapp_feedback_url, settings.whatsapp_feedback_token),
            Destination.ESCALATION_SYSTEM: (settings.escalation_url, settings.escalation_token),
            Destination.INDECAB: (settings.indecab_url, settings.indecab_token),
        }

    def create(self, destination: Destination, payload: dict,
               on_retry: Optional[Callable[[int, Exception], None]] = None,
               acknowledge_gaps: bool = False) -> str:
        """Creates the record downstream and returns its external reference.

        Raises DispatchValidationError (nothing sent) for Essential gaps, or for Mandatory gaps the
        reviewer hasn't acknowledged. `on_retry(attempt, error)` is called after each failed attempt that
        will be retried.
        """
        if destination == Destination.CCP_QUEUE:
            raise ValueError("The CCP queue is internal; nothing to dispatch")
        gaps = field_gaps(destination, payload)
        if gaps[ESSENTIAL]:
            raise DispatchValidationError(destination, gaps[ESSENTIAL], ESSENTIAL)
        if gaps[MANDATORY] and not acknowledge_gaps:
            raise DispatchValidationError(destination, gaps[MANDATORY], MANDATORY)
        if destination == Destination.INDECAB and (gaps[MANDATORY] or gaps[SEMI_MANDATORY]):
            payload = payload | {"missing_fields": {MANDATORY: gaps[MANDATORY], SEMI_MANDATORY: gaps[SEMI_MANDATORY]}}
        url, token = self.endpoints[destination]
        if self.dry_run or not url:
            return f"dryrun-{destination.value}-{uuid.uuid4().hex[:8]}"

        headers = {"Authorization": f"Bearer {token}"} if token else {}
        for attempt in range(1, ATTEMPTS + 1):
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=self.timeout)
                if resp.status_code == 429 or resp.status_code >= 500:
                    resp.raise_for_status()
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
                if attempt == ATTEMPTS:
                    raise
                if on_retry:
                    on_retry(attempt, exc)
                self._sleep(self.backoff * 2 ** (attempt - 1))
                continue
            resp.raise_for_status()  # other 4xx: not retried
            try:
                body = resp.json()
            except ValueError:
                body = {}
            return str(body.get("id") or body.get("reference") or resp.headers.get("Location") or "created")
        raise AssertionError("unreachable")
