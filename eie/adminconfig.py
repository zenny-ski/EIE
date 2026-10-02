"""Admin-editable settings (FRD 4.3: "nothing is hardcoded").

The `.env` file and the code give the defaults. Anything an admin saves in the Settings screen is stored in the
database and applied on top at startup and immediately when saved; "reset" removes the override. Every change
is validated as a whole before anything is applied, and written to the audit log with the before and after.
"""
import logging
import re
from dataclasses import replace

from .models import Category, TripDetails
from .routing.destinations import DEFAULT_INDECAB_TIERS, INDECAB_TIERS, set_indecab_tiers
from .storage import Store
from .templates import BUCKETS, PLACEHOLDERS, TEMPLATES, check_template

log = logging.getLogger("eie")

THRESHOLD_KEYS = ("confidence_high", "confidence_medium", "confidence_high_feedback",
                  "confidence_medium_feedback", "confidence_high_escalation", "confidence_medium_escalation")
NOTIFY_KEYS = ("notify_ccp", "notify_booking", "notify_alerts")
KEYS = THRESHOLD_KEYS + NOTIFY_KEYS + ("forward_teams", "reply_signature", "reply_templates",
                                       "missing_templates", "indecab_tiers")
TIERS = ("essential", "mandatory", "semi_mandatory")
PAYLOAD_ROOTS = {"company", "trip", "passengers", "booking_ids", "summary"}
EMAIL_RE = re.compile(r"^[^@\s,;=]+@[^@\s,;=]+\.[^@\s,;=]+$")
PATH_RE = re.compile(r"^[a-z_]+(\.[a-z0-9_]+)*$")
MAX_TEXT = 5000


class SettingsError(ValueError):
    """The submitted settings are invalid; the message says which and why."""


def _percent(key: str, value, allow_unset: bool) -> int:
    if value is None or value == "":
        if allow_unset:
            return -1  # "use the global value"
        raise SettingsError(f"{key}: a number from 0 to 100 is required")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise SettingsError(f"{key}: must be a whole number from 0 to 100") from None
    if not 0 <= number <= 100:
        raise SettingsError(f"{key}: must be between 0 and 100")
    return number


def _emails(key: str, value) -> str:
    items = [v.strip() for v in str(value or "").replace(";", ",").split(",") if v.strip()]
    for item in items:
        if not EMAIL_RE.match(item):
            raise SettingsError(f"{key}: '{item}' is not a valid email address")
    return ", ".join(items)


class AdminConfig:
    def __init__(self, store: Store, settings):
        self.store = store
        self.settings = settings
        set_indecab_tiers(DEFAULT_INDECAB_TIERS)  # each app starts from the code defaults
        self.defaults = self._read_all()
        self.load()

    # ---- reading ----

    def _read_all(self) -> dict:
        s = self.settings
        values = {key: (None if getattr(s, key) < 0 else getattr(s, key)) if key in THRESHOLD_KEYS[2:]
                  else getattr(s, key) for key in THRESHOLD_KEYS}
        values.update({key: getattr(s, key) for key in NOTIFY_KEYS})
        values["forward_teams"] = [{"name": n, "address": a} for n, a in s.team_addresses.items()]
        values["reply_signature"] = s.reply_signature
        values["reply_templates"] = {b: s.reply_templates.get(b, "") for b in BUCKETS}
        values["missing_templates"] = {t: s.missing_templates.get(t, "") for t in TIERS}
        values["indecab_tiers"] = [{"path": p, "label": label, "tier": tier}
                                   for tier, fields in INDECAB_TIERS.items() for p, label in fields.items()]
        return values

    def snapshot(self) -> dict:
        return {
            "values": self._read_all(),
            "defaults": self.defaults,
            "overridden": sorted(k[len("setting:"):] for k in self.store.kv_prefix("") if k.startswith("setting:")),
            "buckets": list(BUCKETS),
            "tiers": list(TIERS),
            "placeholders": list(PLACEHOLDERS),
            "builtin_missing": dict(TEMPLATES),
            "trip_fields": list(TripDetails.model_fields),
            "timezone": self.settings.timezone,
        }

    # ---- validation (returns the normalised value; raises SettingsError) ----

    def _validate(self, key: str, value):
        if key in THRESHOLD_KEYS:
            return _percent(key, value, allow_unset=key not in ("confidence_high", "confidence_medium"))
        if key in NOTIFY_KEYS:
            return _emails(key, value)
        if key == "reply_signature":
            text = str(value or "").strip()
            if len(text) > MAX_TEXT:
                raise SettingsError("reply_signature: too long")
            return text
        if key == "forward_teams":
            return self._validate_teams(value)
        if key == "reply_templates":
            return self._validate_templates(key, value, BUCKETS, required=())
        if key == "missing_templates":
            return self._validate_templates(key, value, TIERS, required=("items",))
        if key == "indecab_tiers":
            return self._validate_tiers(value)
        raise SettingsError(f"Unknown setting '{key}'")

    @staticmethod
    def _validate_teams(value) -> str:
        if not isinstance(value, list):
            raise SettingsError("forward_teams: expected a list of {name, address}")
        seen, pairs = set(), []
        for row in value:
            name, address = str((row or {}).get("name", "")).strip(), str((row or {}).get("address", "")).strip()
            if not name and not address:
                continue  # an empty row in the form
            if not name or any(c in name for c in ",;="):
                raise SettingsError(f"forward_teams: '{name}' is not a usable team name (no commas or '=')")
            if not EMAIL_RE.match(address):
                raise SettingsError(f"forward_teams: '{address}' is not a valid email address")
            if name.lower() in seen:
                raise SettingsError(f"forward_teams: '{name}' is listed twice")
            seen.add(name.lower())
            pairs.append(f"{name}={address}")
        return ",".join(pairs)

    @staticmethod
    def _validate_templates(key: str, value, allowed: tuple, required: tuple) -> dict:
        if not isinstance(value, dict):
            raise SettingsError(f"{key}: expected an object")
        clean = {}
        for name, text in value.items():
            if name not in allowed:
                raise SettingsError(f"{key}: unknown entry '{name}' (expected {', '.join(allowed)})")
            text = str(text or "").strip()
            if len(text) > MAX_TEXT:
                raise SettingsError(f"{key}.{name}: too long")
            if text:  # blank = use the built-in wording
                problem = check_template(text, required)
                if problem:
                    raise SettingsError(f"{key}.{name}: {problem}")
            clean[name] = text
        return clean

    @staticmethod
    def _validate_tiers(value) -> dict:
        if not isinstance(value, list):
            raise SettingsError("indecab_tiers: expected a list of {path, label, tier}")
        tiers: dict = {t: {} for t in TIERS}
        seen = set()
        trip_fields = set(TripDetails.model_fields)
        for row in value:
            path = str((row or {}).get("path", "")).strip()
            label = str((row or {}).get("label", "")).strip()
            tier = (row or {}).get("tier")
            if not path and not label:
                continue
            if tier not in TIERS:
                raise SettingsError(f"indecab_tiers: tier of '{label or path}' must be one of {', '.join(TIERS)}")
            if not label or len(label) > 60:
                raise SettingsError(f"indecab_tiers: '{path}' needs a label (up to 60 characters)")
            for alternative in path.split("|"):  # "a|b" is satisfied by either field
                if not PATH_RE.match(alternative):
                    raise SettingsError(f"indecab_tiers: '{alternative}' is not a field path like trip.cost_centre")
                parts = alternative.split(".")
                if parts[0] not in PAYLOAD_ROOTS:
                    raise SettingsError(f"indecab_tiers: '{alternative}' must start with one of "
                                        f"{', '.join(sorted(PAYLOAD_ROOTS))}")
                if parts[0] == "trip" and (len(parts) != 2 or parts[1] not in trip_fields):
                    raise SettingsError(f"indecab_tiers: '{alternative}' is not a trip field "
                                        f"({', '.join(sorted(trip_fields))})")
            if path in seen:
                raise SettingsError(f"indecab_tiers: '{path}' is listed twice")
            seen.add(path)
            tiers[tier][path] = label
        return tiers

    # ---- applying ----

    def _apply(self, key: str, value) -> None:
        if key == "forward_teams":
            self.settings.forward_teams = value
        elif key == "indecab_tiers":
            set_indecab_tiers(value)
        elif key in ("reply_templates", "missing_templates"):
            setattr(self.settings, key, {k: v for k, v in value.items() if v})
        else:
            setattr(self.settings, key, value)

    def _check_thresholds(self, candidate: dict) -> None:
        """The medium floor can't exceed the high threshold, globally or for any bucket."""
        trial = replace(self.settings, **{k: v for k, v in candidate.items() if k in THRESHOLD_KEYS})
        for label, category in (("global", None), ("Feedback", Category.FEEDBACK),
                                ("Escalation", Category.ESCALATION)):
            high, medium = (trial.confidence_high, trial.confidence_medium) if category is None \
                else trial.thresholds(category)
            if medium > high:
                raise SettingsError(f"{label}: the medium floor ({medium}) can't be above the high threshold ({high})")

    def load(self) -> None:
        for key, value in self.store.kv_prefix("setting:").items():
            if key not in KEYS:
                continue
            try:
                self._apply(key, self._validate(key, value))
            except SettingsError as exc:  # a stale or hand-edited row must not stop the app starting
                log.warning("Ignoring saved setting %s: %s", key, exc)

    def update(self, changes: dict, user: str) -> list[str]:
        """Validates every change first, then saves and applies them. Returns the keys that changed."""
        clean = {key: self._validate(key, value) for key, value in changes.items()}
        self._check_thresholds(clean)
        before = self._read_all()
        for key, value in clean.items():
            self._apply(key, value)
        after = self._read_all()
        for key in clean:  # saved in the form the screen sends, so `load` can validate it again
            self.store.kv_set(f"setting:{key}", after[key], user)
        changed = [key for key in clean if before[key] != after[key]]
        for key in changed:
            self.store.audit("settings.changed", user, key=key, before=before[key], after=after[key])
        return changed

    def reset(self, keys: list[str], user: str) -> list[str]:
        for key in keys:
            if key not in KEYS:
                raise SettingsError(f"Unknown setting '{key}'")
        before = self._read_all()
        for key in keys:
            self.store.kv_delete(f"setting:{key}")
            self._apply(key, self._default_applied(key))
        after = self._read_all()
        changed = [key for key in keys if before[key] != after[key]]
        for key in changed:
            self.store.audit("settings.reset", user, key=key, before=before[key], after=after[key])
        return changed

    def _default_applied(self, key: str):
        """The default in the form `_apply` expects."""
        default = self.defaults[key]
        if key in THRESHOLD_KEYS[2:]:
            return -1 if default is None else default
        if key == "forward_teams":
            return self._validate_teams(default)
        if key == "indecab_tiers":
            return self._validate_tiers(default)
        return default
