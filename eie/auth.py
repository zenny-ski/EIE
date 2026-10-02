"""Roles and access rules (FRD 14).

Users come from EIE_USERS as "name:role:token" entries, e.g.
    EIE_USERS=asha:module_manager:s3cret1,ravi:escalation_manager:s3cret2,meera:admin:s3cret3
The GUI asks for the token once and sends it as the X-EIE-Token header. With EIE_USERS empty there is no
login and everyone acts as an admin (fine for a local demo, not for a shared deployment).

  Super Admin / Admin   everything, including loading booking contacts
  Module Manager        queue triage, quick-create confirmation, HITL + IndeCab push, replies, forwards
  Executive             same as Module Manager
  Escalation Manager    only Escalation quick-create confirmations and their emails; no New Booking HITL,
                        no IndeCab push, no replies
"""
import hmac
from dataclasses import dataclass
from typing import Optional

ROLES = ("super_admin", "admin", "escalation_manager", "module_manager", "executive")
OPERATIONAL = {"super_admin", "admin", "module_manager", "executive"}
ADMINS = {"super_admin", "admin"}


@dataclass(frozen=True)
class User:
    name: str
    role: str
    anonymous: bool = False  # auth is off: the name comes from the request body instead

    @property
    def operational(self) -> bool:
        return self.role in OPERATIONAL

    @property
    def admin(self) -> bool:
        return self.role in ADMINS

    def may_see_email(self, email: dict) -> bool:
        return self.operational or email.get("category") == "escalation"

    def may_review(self, item: dict, override_category: Optional[str] = None) -> bool:
        """Escalation Managers only handle Escalation confirmations, never bookings or the CCP queue."""
        if self.operational:
            return True
        return (item["destination"] == "escalation_system" and item["kind"] == "review_form"
                and override_category in (None, "escalation"))

    def capabilities(self) -> dict:
        return {"review_all": self.operational, "reply": self.operational, "forward": self.operational,
                "dismiss": self.operational, "fetch": self.operational, "admin": self.admin}


class Auth:
    def __init__(self, spec: str):
        self.users: dict[str, User] = {}  # token -> user
        for entry in [e.strip() for e in spec.split(",") if e.strip()]:
            parts = entry.split(":")
            if len(parts) != 3 or not all(p.strip() for p in parts):
                raise ValueError(f"EIE_USERS entry {entry!r} must look like name:role:token")
            name, role, token = (p.strip() for p in parts)
            if role not in ROLES:
                raise ValueError(f"EIE_USERS: unknown role {role!r} (expected one of {', '.join(ROLES)})")
            self.users[token] = User(name, role)

    @property
    def enabled(self) -> bool:
        return bool(self.users)

    def authenticate(self, token: str) -> Optional[User]:
        found = None
        for known, user in self.users.items():  # compare against every token so timing doesn't leak which
            if hmac.compare_digest(known.encode(), (token or "").encode()):
                found = user
        return found
