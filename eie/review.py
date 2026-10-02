"""Human review actions: approve or reject items in the review queue."""
from typing import Optional

from .models import DESTINATION_FOR_CATEGORY, Category, Destination
from .routing import DispatchValidationError, Dispatcher, field_gaps, payload_from_row
from .routing.destinations import MANDATORY
from .storage import Store


class ReviewError(Exception):
    pass


class ReviewService:
    def __init__(self, store: Store, dispatcher: Dispatcher, settings, mailer=None):
        self.store = store
        self.dispatcher = dispatcher
        self.settings = settings  # read live: the admin screen can change teams and templates
        self.mailer = mailer

    @property
    def base_url(self) -> str:
        return self.settings.public_base_url

    @property
    def teams(self) -> dict:
        return self.settings.team_addresses

    def claim(self, review_id: int, user: str) -> dict:
        """Marks the item as being worked on by `user` ("In Review"); anyone may take over."""
        item = self._pending(review_id)
        self.store.claim_review(review_id, user)
        self.store.audit("review.claimed", user, email_id=item["email_id"], review_id=review_id,
                         previous=item.get("assigned_to"))
        return {"assigned_to": user}

    def dismiss(self, review_id: int, user: str, reason: str) -> dict:
        """Closes an item that needs no action (spam, vendor mail...). A reason is required."""
        item = self._pending(review_id)
        if not reason.strip():
            raise ReviewError("Enter a reason for dismissing this email")
        self.store.resolve_review(review_id, "dismissed", user, reason.strip())
        self.store.set_email_status(item["email_id"], "dismissed")
        self.store.audit("review.dismissed", user, email_id=item["email_id"], review_id=review_id,
                         reason=reason.strip())
        return {"status": "dismissed"}

    def forward(self, review_id: int, user: str, team: str, notes: str = "") -> dict:
        """FR-035: sends the email to a team address (Invoicing, DMT, IT...) without creating any record."""
        item = self._pending(review_id)
        if item["kind"] not in ("ccp_review", "possible_split"):
            raise ReviewError("Only CCP queue items can be forwarded to a team")
        address = self.teams.get(team)
        if not address:
            raise ReviewError(f"Unknown team '{team}'. Configured teams: {', '.join(self.teams) or 'none'}")
        if self.mailer is None:
            raise ReviewError("Forwarding isn't available: no mail transport configured")
        email = self.store.get_email(item["email_id"])
        note = f"Note: {notes.strip()}\n" if notes.strip() else ""
        body = (f"Forwarded by {user} from the EIE review queue.\n{note}\n"
                f"From: {email['sender']}\nReceived: {email['received_at']}\nSubject: {email['subject']}\n\n"
                f"{email['body']}\n")
        try:
            transport, ref = self.mailer.send_message([address], f"Fwd: {email['subject']}", body)
        except Exception as exc:  # the item stays pending so it can be retried
            self.store.audit("forward.failed", user, email_id=email["id"], review_id=review_id, team=team,
                             error=repr(exc))
            raise
        self.store.resolve_review(review_id, "resolved", user, f"Forwarded to {team}. {notes}".strip(),
                                  external_ref=ref)
        self.store.set_email_status(email["id"], "forwarded")
        self.store.audit("review.forwarded", user, email_id=email["id"], review_id=review_id, team=team,
                         to=address, transport=transport, dry_run=transport == "dry_run", notes=notes)
        return {"status": "forwarded", "team": team, "to": address, "transport": transport}

    def approve(self, review_id: int, reviewer: str, *, payload: Optional[dict] = None,
                category: Optional[Category] = None, notes: str = "", acknowledge_gaps: bool = False,
                resolution: Optional[str] = None, link_ref: str = "") -> dict:
        """Approve an item, optionally with an edited payload.

        For CCP review items the reviewer picks the category; with no category (or `unclassified`)
        the item is closed without sending anything downstream. For a possible split (FRD 7.4) the
        reviewer may instead keep the message with the thread's existing record (`resolution="keep"`)
        or attach it to another existing ticket (`resolution="link"` with `link_ref`).
        """
        item = self._pending(review_id)
        if resolution:
            return self._resolve_thread(item, reviewer, resolution, link_ref, notes)
        final_payload = payload if payload is not None else item["payload"]
        destination = Destination(item["destination"])

        if item["kind"] in ("ccp_review", "possible_split") or category is not None:
            if category is None or category == Category.UNCLASSIFIED:
                self.store.resolve_review(review_id, "resolved", reviewer, notes, final_payload)
                self.store.set_email_status(item["email_id"], "resolved")
                self.store.audit("review.resolved", reviewer, email_id=item["email_id"], review_id=review_id,
                                 notes=notes)
                return {"status": "resolved", "external_ref": None}
            destination = DESTINATION_FOR_CATEGORY[category]

        rerouted = destination.value != item["destination"]
        if rerouted and payload is None:  # the stored form was built for another destination
            email = self.store.get_email(item["email_id"])
            final_payload = payload_from_row(destination, email, self.base_url, self._split_parent(email))

        def on_retry(attempt: int, exc: Exception) -> None:
            self.store.audit("destination.retry", reviewer, email_id=item["email_id"], review_id=review_id,
                             destination=destination.value, attempt=attempt, error=repr(exc))

        try:
            external_ref = self.dispatcher.create(destination, final_payload, on_retry, acknowledge_gaps)
        except DispatchValidationError as exc:  # nothing was sent; the item stays pending
            if rerouted:  # show the reviewer the new destination's form so the gaps can be filled in
                self.store.retarget_review(review_id, destination.value, final_payload)
            self.store.audit("destination.blocked", reviewer, email_id=item["email_id"], review_id=review_id,
                             destination=destination.value, tier=exc.tier, missing=exc.missing)
            raise
        except Exception as exc:  # item stays pending so it can be retried
            self.store.audit("destination.failed", reviewer, email_id=item["email_id"], review_id=review_id,
                             destination=destination.value, error=repr(exc))
            raise
        self.store.resolve_review(review_id, "approved", reviewer, notes, final_payload, external_ref,
                                  destination.value)
        self.store.set_email_status(item["email_id"], "approved", external_ref)
        if item["kind"] == "possible_split":  # the reviewer decided this is a genuine new record
            self._confirm_split(item["email_id"])
        if category is not None:
            self._refresh_draft(item["email_id"], category)
        self.store.audit("review.approved", reviewer, email_id=item["email_id"], review_id=review_id,
                         kind=item["kind"], destination=destination.value, external_ref=external_ref,
                         category_override=category.value if category else None,
                         payload_edited=payload is not None, notes=notes,
                         acknowledged_gaps=field_gaps(destination, final_payload)[MANDATORY]
                         if acknowledge_gaps else [])
        return {"status": "approved", "external_ref": external_ref, "destination": destination.value}

    def reject(self, review_id: int, reviewer: str, notes: str = "") -> dict:
        item = self._pending(review_id)
        self.store.resolve_review(review_id, "rejected", reviewer, notes)
        self.store.set_email_status(item["email_id"], "rejected")
        self.store.audit("review.rejected", reviewer, email_id=item["email_id"], review_id=review_id, notes=notes)
        return {"status": "rejected"}

    def preview(self, review_id: int, category: Category) -> dict:
        """The form the reviewer would get if this item were routed to `category` (nothing is saved)."""
        item = self._pending(review_id)
        if category == Category.UNCLASSIFIED:
            raise ReviewError("Nothing to preview: the item would be closed without routing")
        destination = DESTINATION_FOR_CATEGORY[category]
        if destination.value == item["destination"]:
            return item["payload"]
        email = self.store.get_email(item["email_id"])
        return payload_from_row(destination, email, self.base_url, self._split_parent(email))

    def _refresh_draft(self, email_id: int, category: Category) -> None:
        """FR-026: a draft is (re)generated when an email is manually assigned a bucket. The AI draft was
        written for the original bucket, so use the bucket's template, or the built-in one if none is set."""
        from .classification.rules import REPLY_TEMPLATES
        from .models import ExtractedInfo
        from .templates import render_draft

        email = self.store.get_email(email_id)
        if email["category"] == category.value and email["suggested_reply"]:
            return
        extracted = ExtractedInfo(**(email["extracted"] or {}))
        configured = (self.settings.reply_templates.get(category.value) or "").strip()
        base = "" if configured else REPLY_TEMPLATES[category]
        draft = render_draft(self.settings, category.value, email["sender"], email["subject"], extracted, base)
        self.store.set_suggested_reply(email_id, draft)
        self.store.audit("reply.drafted", email_id=email_id, suggested_reply=draft, reason="bucket assigned")

    def _split_parent(self, email: dict) -> Optional[dict]:
        if email.get("thread_role") in ("split", "possible_split") and email.get("parent_email_id"):
            return self.store.get_email(email["parent_email_id"])
        return None

    def _confirm_split(self, email_id: int) -> None:
        email = self.store.get_email(email_id)
        self.store.set_thread_link(email_id, email["parent_email_id"], "split")

    def _resolve_thread(self, item: dict, reviewer: str, resolution: str, link_ref: str, notes: str) -> dict:
        email = self.store.get_email(item["email_id"])
        if item["kind"] != "possible_split" or not email.get("parent_email_id"):
            raise ReviewError("Only a possible-split item can be resolved this way")
        parent_id = email["parent_email_id"]
        if resolution == "keep":
            self.store.set_thread_link(email["id"], parent_id, "appended")
            ref = self.store.get_email(parent_id).get("external_ref")
            self.store.audit("thread.appended", reviewer, email_id=email["id"], parent_email_id=parent_id)
            self.store.audit("thread.message_appended", reviewer, email_id=parent_id, child_email_id=email["id"])
        elif resolution == "link":
            ref = link_ref.strip()
            if not ref:
                raise ReviewError("Enter the ticket reference to attach this message to")
            self.store.set_thread_link(email["id"], parent_id, "linked")
            self.store.audit("thread.linked", reviewer, email_id=email["id"], ticket_ref=ref, parent_email_id=parent_id)
        else:
            raise ReviewError(f"Unknown resolution '{resolution}'")
        self.store.resolve_review(item["id"], "resolved", reviewer, notes, external_ref=ref)
        self.store.set_email_status(email["id"], "appended", ref)
        self.store.audit("review.resolved", reviewer, email_id=email["id"], review_id=item["id"],
                         resolution=resolution, ticket_ref=ref, notes=notes)
        return {"status": "resolved", "external_ref": ref}

    def _pending(self, review_id: int) -> dict:
        item = self.store.get_review(review_id)
        if item is None:
            raise ReviewError(f"Review item {review_id} not found")
        if item["status"] != "pending":
            raise ReviewError(f"Review item {review_id} is already {item['status']}")
        return item
