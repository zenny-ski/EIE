"""Requester verification (FR-007): is the sender a known passenger, booker or contact on the booking?

Contacts live in the `booking_contacts` table, loaded through POST /api/booking-contacts or
`python -m eie contacts FILE.csv`. When IndeCab's API is available this is the place to query it instead.
"""
from email.utils import parseaddr
from typing import Optional

from .storage import Store


def verify_requester(store: Store, sender: str, booking_ids: list[str]) -> tuple[Optional[bool], dict]:
    """(verified, detail). verified is None when no check was possible, so it isn't shown as a failure."""
    address = parseaddr(sender or "")[1].strip().lower()
    if not booking_ids:
        return None, {"reason": "No booking ID in the email"}
    contacts = store.contacts_for(booking_ids)
    if not contacts:
        return None, {"reason": "Booking not found in the contact directory", "booking_ids": booking_ids}
    for contact in contacts:
        if address and contact["email"] == address:
            return True, {"booking_id": contact["booking_id"], "role": contact["role"], "name": contact["name"]}
    return False, {"reason": "Sender is not a known contact on the booking", "booking_ids": booking_ids}
