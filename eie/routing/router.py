"""Confidence-based routing rules.

  Unclassified (any confidence)      -> CCP review queue
  Any category, LOW confidence       -> CCP review queue
  New booking, MEDIUM/HIGH           -> human approval, then IndeCab
  Feedback / Escalation, HIGH        -> auto-created in destination system
  Feedback / Escalation, MEDIUM      -> pre-filled form for a reviewer
"""
from ..models import (
    DESTINATION_FOR_CATEGORY,
    Action,
    Category,
    ClassificationResult,
    ConfidenceTier,
    Destination,
    RoutingDecision,
)


def confidence_tier(score: int, high: int, medium: int) -> ConfidenceTier:
    if score >= high:
        return ConfidenceTier.HIGH
    if score >= medium:
        return ConfidenceTier.MEDIUM
    return ConfidenceTier.LOW


def decide(result: ClassificationResult, high: int, medium: int) -> RoutingDecision:
    tier = confidence_tier(result.confidence, high, medium)

    def decision(action: Action, destination: Destination, reason: str) -> RoutingDecision:
        return RoutingDecision(result.category, result.confidence, tier, action, destination, reason)

    if result.category == Category.UNCLASSIFIED:
        return decision(Action.CCP_REVIEW, Destination.CCP_QUEUE, "Email could not be classified")
    if tier == ConfidenceTier.LOW:
        return decision(Action.CCP_REVIEW, Destination.CCP_QUEUE,
                        f"Confidence {result.confidence} is below the medium threshold ({medium})")

    destination = DESTINATION_FOR_CATEGORY[result.category]
    if result.category == Category.NEW_BOOKING:
        return decision(Action.BOOKING_APPROVAL, destination, "New bookings always need human approval")
    if tier == ConfidenceTier.HIGH:
        return decision(Action.AUTO_CREATE, destination,
                        f"Confidence {result.confidence} meets the high threshold ({high})")
    return decision(Action.REVIEW_FORM, destination,
                    f"Confidence {result.confidence} is between {medium} and {high}: needs review")
