"""Local web GUI: a JSON API plus a single-page dashboard served from ./static."""
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..adminconfig import SettingsError
from ..app import App
from ..auth import Auth, User
from ..autofetch import AutoFetcher
from ..ingestion.file import JsonFileSource
from ..models import Category, Destination
from ..reply import ReplyError
from ..review import ReviewError
from ..routing import INDECAB_TIERS, DispatchValidationError, field_gaps
from ..storage import STATUS_GROUPS
from ..templates import missing_details_draft

STATIC = Path(__file__).parent / "static"
DEFAULT_SAMPLES = Path(__file__).resolve().parents[2] / "samples" / "sample_emails.json"


class ApproveRequest(BaseModel):
    reviewer: str = ""                    # ignored when logins are on: the signed-in user is used
    notes: str = ""
    payload: Optional[dict] = None
    category: Optional[Category] = None
    acknowledge_gaps: bool = False        # push to IndeCab with Mandatory fields still missing
    resolution: Optional[str] = None      # possible split: "keep" with the existing record, or "link" to a ticket
    link_ref: str = ""


class DraftRequest(BaseModel):
    payload: Optional[dict] = None


class GapsRequest(BaseModel):
    payload: dict


class SettingsUpdate(BaseModel):
    changes: dict = {}
    reset: list[str] = []


class RejectRequest(BaseModel):
    reviewer: str = ""
    notes: str = ""


class ClaimRequest(BaseModel):
    reviewer: str = ""


class DismissRequest(BaseModel):
    reviewer: str = ""
    reason: str = ""


class ForwardRequest(BaseModel):
    reviewer: str = ""
    team: str
    notes: str = ""


class ReplyRequest(BaseModel):
    user: str = ""
    body: str
    to: str = ""
    subject: str = ""
    resend: bool = False


class Contact(BaseModel):
    booking_id: str
    role: str = "passenger"
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None


class AutoFetchRequest(BaseModel):
    enabled: bool


def create_api(app: App) -> FastAPI:
    # The Store is thread-safe, so reads need no lock and stay fast while a fetch is classifying.
    # review_lock stops two reviewers approving the same item at once; demo_lock serialises demo runs.
    review_lock = threading.Lock()
    reply_lock = threading.Lock()
    demo_lock = threading.Lock()
    store = app.store
    s = app.settings
    auth = Auth(s.users)
    fetcher = AutoFetcher(app.pipeline, s.auto_fetch_seconds,
                          enabled=s.auto_fetch and (s.graph_configured or s.imap_configured),
                          on_error=lambda exc: app.notifier.alert("Mailbox ingestion is failing", str(exc)))

    @asynccontextmanager
    async def lifespan(_api):
        fetcher.start()
        app.subscriptions.start()  # keeps the Graph webhook alive when it's configured
        yield
        app.subscriptions.stop()
        fetcher.stop()

    api = FastAPI(title="Email Intelligence Engine", lifespan=lifespan)

    # ---- access control (FRD 14) ----

    def current_user(request: Request) -> User:
        if not auth.enabled:
            return User("", "admin", anonymous=True)
        user = auth.authenticate(request.headers.get("X-EIE-Token", ""))
        if user is None:
            raise HTTPException(401, "Sign in with your access token")
        return user

    def actor(user: User, claimed: str) -> str:
        """Who is acting: the signed-in user, or (logins off) the name typed into the form."""
        name = user.name if not user.anonymous else claimed.strip()
        if not name:
            raise HTTPException(400, "Your name is required")
        return name

    def require_operational(user: User, what: str) -> None:
        if not user.operational:
            raise HTTPException(403, f"Your role can't {what}")

    def review_for(user: User, review_id: int, category: Optional[Category] = None) -> dict:
        item = store.get_review(review_id)
        if item is None:
            raise HTTPException(404, f"Review item {review_id} not found")
        if not user.may_review(item, category.value if category else None):
            raise HTTPException(403, "Your role can only handle Escalation confirmations")
        return item

    def processed(results, deferred: int = 0) -> dict:
        return {"processed": [r.__dict__ for r in results], "deferred": deferred}

    # ---- session / config ----

    @api.get("/api/me")
    def me(user: User = Depends(current_user)):
        return {"name": user.name, "role": user.role, "auth_enabled": auth.enabled, "can": user.capabilities()}

    @api.get("/api/config")
    def config():
        return {
            "email_source": s.email_source,
            "graph_configured": s.graph_configured,
            "imap_configured": s.imap_configured,
            "use_llm": s.use_llm,
            "provider": s.llm_provider if s.use_llm else "rules",
            "model": (s.gemini_model if s.llm_provider == "gemini" else s.anthropic_model) if s.use_llm else "",
            "confidence_high": s.confidence_high,
            "confidence_medium": s.confidence_medium,
            "dry_run": s.dry_run,
            "db_path": s.db_path,
            "auth_enabled": auth.enabled,
            "teams": list(s.team_addresses),
            "notifications": bool(s.notify_ccp_list),
            "timezone": s.timezone,
        }

    # ---- dashboard ----

    @api.get("/api/summary")
    def summary(user: User = Depends(current_user)):
        return store.summary()

    @api.get("/api/kpis")
    def kpis(date_from: Optional[str] = None, date_to: Optional[str] = None, period: Optional[str] = None,
             user: User = Depends(current_user)):
        try:
            return store.kpis(date_from, date_to, period)
        except ValueError as exc:
            raise HTTPException(400, f"Bad date or period: {exc}")

    # ---- emails ----

    @api.get("/api/emails")
    def emails(limit: int = 200, category: Optional[str] = None, tier: Optional[str] = None,
               status_group: Optional[str] = None, date_from: Optional[str] = None,
               date_to: Optional[str] = None, assignee: Optional[str] = None,
               user: User = Depends(current_user)):
        if status_group and status_group not in STATUS_GROUPS:
            raise HTTPException(400, f"status_group must be one of {', '.join(STATUS_GROUPS)}")
        if not user.operational:
            category = "escalation"
        try:
            return store.list_emails(limit, category=category, tier=tier, status_group=status_group,
                                     date_from=date_from, date_to=date_to, assignee=assignee)
        except ValueError as exc:
            raise HTTPException(400, f"Dates must look like 2026-10-02: {exc}")

    @api.get("/api/emails/{email_id}")
    def email_detail(email_id: int, user: User = Depends(current_user)):
        email = store.get_email(email_id)
        if email is None:
            raise HTTPException(404, f"Email {email_id} not found")
        if not user.may_see_email(email):
            raise HTTPException(403, "Your role can only view Escalation emails")
        return {
            "email": email,
            "reviews": store.list_reviews(status=None, email_id=email_id),
            "replies": store.list_replies(email_id),
            "audit": store.audit_entries(email_id),
            "thread": {
                "parent": store.get_email(email["parent_email_id"]) if email.get("parent_email_id") else None,
                "children": store.thread_children(email_id),
            },
        }

    @api.post("/api/emails/{email_id}/reply")
    def send_reply(email_id: int, req: ReplyRequest, user: User = Depends(current_user)):
        require_operational(user, "send replies")
        sender = actor(user, req.user)
        with reply_lock:  # stops a double-click sending the same reply twice
            try:
                return app.replies.send(email_id, sender, req.body, to=req.to,
                                        subject=req.subject, resend=req.resend)
            except ReplyError as exc:
                raise HTTPException(409 if "already sent" in str(exc) else 400, str(exc))

    # ---- review queue ----

    @api.get("/api/reviews")
    def reviews(status: str = "pending", kind: Optional[str] = None, user: User = Depends(current_user)):
        items = store.list_reviews(None if status == "all" else status, kind)
        return [i for i in items if user.may_review(i)]

    @api.get("/api/reviews/{review_id}")
    def review_detail(review_id: int, user: User = Depends(current_user)):
        item = review_for(user, review_id)
        return {"review": item, "email": store.get_email(item["email_id"]),
                "replies": store.list_replies(item["email_id"]),
                "tiers": {tier: list(fields.values()) for tier, fields in INDECAB_TIERS.items()},
                "tier_paths": {tier: list(fields) for tier, fields in INDECAB_TIERS.items()},
                "teams": list(s.team_addresses)}

    @api.get("/api/reviews/{review_id}/preview")
    def review_preview(review_id: int, category: Category, user: User = Depends(current_user)):
        review_for(user, review_id, category)
        try:
            return {"payload": app.reviews.preview(review_id, category)}
        except ReviewError as exc:
            raise HTTPException(409, str(exc))

    @api.post("/api/reviews/{review_id}/missing-details")
    def missing_details(review_id: int, req: DraftRequest, user: User = Depends(current_user)):
        """FR-024/FR-037: a reply draft asking the booker for what's missing, worded by tier."""
        item = review_for(user, review_id)
        gaps = field_gaps(Destination.INDECAB, req.payload if req.payload is not None else item["payload"])
        body = missing_details_draft(store.get_email(item["email_id"])["sender"], gaps, s.missing_templates)
        if not body:
            raise HTTPException(400, "Nothing is missing")
        return {"gaps": gaps, "body": body}

    @api.post("/api/gaps")
    def gaps(req: GapsRequest, user: User = Depends(current_user)):
        """Empty IndeCab fields per tier for a form as it currently looks (so the screen and server agree)."""
        return field_gaps(Destination.INDECAB, req.payload)

    @api.post("/api/reviews/{review_id}/claim")
    def claim(review_id: int, req: ClaimRequest, user: User = Depends(current_user)):
        review_for(user, review_id)
        try:
            return app.reviews.claim(review_id, actor(user, req.reviewer))
        except ReviewError as exc:
            raise HTTPException(409, str(exc))

    @api.post("/api/reviews/{review_id}/approve")
    def approve(review_id: int, req: ApproveRequest, user: User = Depends(current_user)):
        review_for(user, review_id, req.category)
        reviewer = actor(user, req.reviewer)
        with review_lock:
            try:
                return app.reviews.approve(review_id, reviewer, payload=req.payload,
                                           category=req.category, notes=req.notes,
                                           acknowledge_gaps=req.acknowledge_gaps, resolution=req.resolution,
                                           link_ref=req.link_ref)
            except ReviewError as exc:
                raise HTTPException(409, str(exc))
            except DispatchValidationError as exc:
                hint = ("Fill in" if exc.tier == "essential"
                        else "Mandatory fields are missing; fill them in or tick 'push with gaps'")
                raise HTTPException(422, f"Can't send to {exc.destination.value.replace('_', ' ')} yet. "
                                         f"{hint}: {', '.join(exc.missing)}")
            except Exception as exc:
                raise HTTPException(502, f"Destination system call failed: {exc}")

    @api.post("/api/reviews/{review_id}/reject")
    def reject(review_id: int, req: RejectRequest, user: User = Depends(current_user)):
        review_for(user, review_id)
        reviewer = actor(user, req.reviewer)
        with review_lock:
            try:
                return app.reviews.reject(review_id, reviewer, req.notes)
            except ReviewError as exc:
                raise HTTPException(409, str(exc))

    @api.post("/api/reviews/{review_id}/dismiss")
    def dismiss(review_id: int, req: DismissRequest, user: User = Depends(current_user)):
        require_operational(user, "dismiss queue items")
        review_for(user, review_id)
        reviewer = actor(user, req.reviewer)
        with review_lock:
            try:
                return app.reviews.dismiss(review_id, reviewer, req.reason)
            except ReviewError as exc:
                raise HTTPException(409, str(exc))

    @api.post("/api/reviews/{review_id}/forward")
    def forward(review_id: int, req: ForwardRequest, user: User = Depends(current_user)):
        require_operational(user, "forward emails to teams")
        review_for(user, review_id)
        reviewer = actor(user, req.reviewer)
        with review_lock:
            try:
                return app.reviews.forward(review_id, reviewer, req.team, req.notes)
            except ReviewError as exc:
                raise HTTPException(409, str(exc))
            except Exception as exc:
                raise HTTPException(502, f"Forwarding failed: {exc}")

    # ---- audit ----

    @api.get("/api/audit")
    def audit(limit: int = 300, user: User = Depends(current_user)):
        entries = store.audit_entries(limit=limit)
        if user.operational:
            return entries
        visible = {e["id"] for e in store.list_emails(100000, category="escalation")}
        return [a for a in entries if a["email_id"] in visible]

    # ---- mailbox ----

    @api.post("/api/run")
    def run(user: User = Depends(current_user)):
        require_operational(user, "fetch the mailbox")
        try:
            results = fetcher.run_once(manual=True)  # shares the auto-fetch pipeline and its lock
        except ValueError as exc:  # no mailbox configured
            raise HTTPException(400, str(exc))
        return processed(results, fetcher.deferred)

    @api.get("/api/autofetch")
    def autofetch_status(user: User = Depends(current_user)):
        return fetcher.status()

    @api.post("/api/autofetch")
    def autofetch_toggle(req: AutoFetchRequest, user: User = Depends(current_user)):
        require_operational(user, "change auto-fetch")
        if req.enabled and not (s.graph_configured or s.imap_configured):
            raise HTTPException(400, "No mailbox configured: set GRAPH_* or IMAP_* in .env")
        fetcher.enabled = req.enabled
        return fetcher.status()

    @api.post("/api/demo")
    def demo(user: User = Depends(current_user)):
        require_operational(user, "load demo emails")
        with demo_lock:
            return processed(app.pipeline(JsonFileSource(str(DEFAULT_SAMPLES))).run_once())

    @api.api_route("/api/graph/webhook", methods=["GET", "POST"])
    async def graph_webhook(request: Request):
        """Microsoft Graph change notifications (FR-002): a new message triggers an immediate fetch."""
        token = request.query_params.get("validationToken")
        if token:  # subscription handshake: echo the token as plain text
            return PlainTextResponse(token)
        try:
            notifications = (await request.json()).get("value", [])
        except ValueError:
            raise HTTPException(400, "Expected a JSON body")
        secret = s.graph_webhook_secret
        if not any(not secret or n.get("clientState") == secret for n in notifications):
            raise HTTPException(401, "Unknown clientState")

        def fetch_now():
            try:
                fetcher.run_once()
            except Exception as exc:  # polling will retry; this just must not crash the thread
                app.notifier.alert("Mailbox ingestion is failing", str(exc))

        threading.Thread(target=fetch_now, name="eie-webhook-fetch", daemon=True).start()
        return PlainTextResponse("accepted", status_code=202)  # Graph needs an answer within seconds

    # ---- admin settings ----

    def admin_only(user: User) -> None:
        if not user.admin:
            raise HTTPException(403, "Only admins can change settings")

    @api.get("/api/admin/settings")
    def settings_get(user: User = Depends(current_user)):
        admin_only(user)
        return app.admin.snapshot()

    @api.put("/api/admin/settings")
    def settings_put(req: SettingsUpdate, user: User = Depends(current_user)):
        admin_only(user)
        who = user.name or "admin"
        try:
            changed = app.admin.update(req.changes, who) if req.changes else []
            reset = app.admin.reset(req.reset, who) if req.reset else []
        except SettingsError as exc:
            raise HTTPException(400, str(exc))
        return {"changed": changed + reset, **app.admin.snapshot()}

    # ---- requester verification data ----

    @api.post("/api/booking-contacts")
    def booking_contacts(contacts: list[Contact], user: User = Depends(current_user)):
        if not user.admin:
            raise HTTPException(403, "Only admins can load booking contacts")
        stored = store.replace_contacts([c.model_dump() for c in contacts])
        store.audit("contacts.loaded", user.name or "admin", count=stored)
        return {"stored": stored}

    api.mount("/static", StaticFiles(directory=STATIC), name="static")

    @api.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    return api
