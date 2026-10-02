from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class Category(str, Enum):
    FEEDBACK = "feedback"
    ESCALATION = "escalation"
    NEW_BOOKING = "new_booking"
    UNCLASSIFIED = "unclassified"


class ConfidenceTier(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Action(str, Enum):
    APPEND_TO_THREAD = "append_to_thread"  # same bucket as the thread's existing record: no new record
    AUTO_CREATE = "auto_create"            # created in the destination system without a human
    REVIEW_FORM = "review_form"            # pre-filled form waiting for a reviewer
    POSSIBLE_SPLIT = "possible_split"      # thread message that may be a new intent: reviewer decides (7.4)
    BOOKING_APPROVAL = "booking_approval"  # new booking waiting for human approval before IndeCab
    CCP_REVIEW = "ccp_review"              # CCP review queue (low confidence / unclassified)


class Destination(str, Enum):
    WHATSAPP_FEEDBACK = "whatsapp_feedback"
    ESCALATION_SYSTEM = "escalation_system"
    INDECAB = "indecab"
    CCP_QUEUE = "ccp_queue"


DESTINATION_FOR_CATEGORY = {
    Category.FEEDBACK: Destination.WHATSAPP_FEEDBACK,
    Category.ESCALATION: Destination.ESCALATION_SYSTEM,
    Category.NEW_BOOKING: Destination.INDECAB,
    Category.UNCLASSIFIED: Destination.CCP_QUEUE,
}


@dataclass
class Email:
    message_id: str          # stable, globally unique id (Internet Message-ID where available)
    subject: str
    sender: str
    received_at: str         # ISO-8601
    body: str                # plain text (converted from the HTML when the email has no plain part)
    source: str              # graph | imap | file
    source_ref: str = ""     # id inside the source system (Graph id / IMAP UID), used to mark as read
    conversation_id: str = ""
    attachments: list = field(default_factory=list)  # [{"name", "content_type", "size", "inline", "text"}]
    body_html: str = ""      # the original HTML body, when there is one (FR-003)


# ---- LLM structured output ----

class Passenger(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None


class TripDetails(BaseModel):
    pickup_location: Optional[str] = None
    drop_location: Optional[str] = None
    pickup_date: Optional[str] = None
    pickup_time: Optional[str] = None
    pickup_datetime: Optional[str] = None  # when the email gives them as one string; split by derive_pickup
    vehicle_type: Optional[str] = None
    trip_type: Optional[str] = None  # e.g. airport transfer, local, outstation
    passenger_count: Optional[str] = None
    cost_centre: Optional[str] = None
    concur_id: Optional[str] = None
    flight_number: Optional[str] = None
    train_number: Optional[str] = None
    notes: Optional[str] = None


class ExtractedInfo(BaseModel):
    booking_ids: list[str] = []
    passengers: list[Passenger] = []
    trip: Optional[TripDetails] = None
    company: Optional[str] = None
    trip_date: Optional[str] = None  # date of the trip a feedback/escalation refers to
    sentiment: Optional[str] = None  # positive | neutral | negative
    urgency: Optional[str] = None    # low | medium | high
    summary: Optional[str] = None


class ClassificationResult(BaseModel):
    category: Category
    confidence: int  # 0-100
    reasoning: str
    extracted: ExtractedInfo
    suggested_reply: str
    classifier: str = "llm"  # llm | rules | fallback


@dataclass
class RoutingDecision:
    category: Category
    confidence: int
    tier: ConfidenceTier
    action: Action
    destination: Destination
    reason: str
