"""Common command contract v1 for case mutations (E.4), shared by LINE, n8n and web callers.

Callers pass an authenticated `Actor`, never an actor ID in the payload. Payloads are plain JSON
so the same contract works for internal APIs and HTML views. Quantities travel as strings or
integers; floats are refused so no binary rounding ever reaches a stored amount.
"""

import re
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from operations import workflow
from operations.models import Case

SCHEMA_VERSION = 1


class ErrorCode(StrEnum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    REVISION_CONFLICT = "REVISION_CONFLICT"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    ACTION_NOT_ENABLED = "ACTION_NOT_ENABLED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    EXTERNAL_OUTCOME_UNKNOWN = "EXTERNAL_OUTCOME_UNKNOWN"


class CommandFailed(Exception):
    """A refused command. Messages never reveal other requesters' or divisions' cases."""

    def __init__(self, code, message=""):
        super().__init__(f"{code}: {message}" if message else str(code))
        self.code = ErrorCode(code)
        self.message = message


def fail(code, message=""):
    raise CommandFailed(code, message)


@dataclass(frozen=True)
class Actor:
    """Who is acting, resolved by the caller from a session, a signed LINE event or a lease."""

    user: object = None
    requester: object = None

    @classmethod
    def staff(cls, user):
        return cls(user=user)

    @classmethod
    def for_requester(cls, requester):
        return cls(requester=requester)

    @property
    def is_requester(self):
        return self.requester is not None

    @property
    def key(self):
        return f"requester:{self.requester.pk}" if self.is_requester else f"user:{self.user.pk}"


@dataclass(frozen=True)
class CommandResult:
    case_id: str
    case_number: str
    revision: int
    status: str
    action_id: str

    def to_dict(self):
        return asdict(self)


EVENT_CASE_SUBMITTED = "case.submitted"
EVENT_DECISION_RECORDED = "decision.recorded"
EVENT_ENVELOPE_FIELDS = (
    "event_id",
    "event_type",
    "subject_type",
    "subject_id",
    "case_id",
    "case_revision",
    "occurred_at",
    "schema_version",
    "correlation_id",
    "payload",
)

# --- Payload validation --------------------------------------------------------------------

TEXT_MAX = 2000
MAX_LINES = 50
MAX_SERIALS = 500
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:\-]{8,128}$")
QUANTITY = re.compile(r"^\d{1,10}(\.\d+)?$")

STAFF_CHANNELS = frozenset({Case.Channel.PHONE, Case.Channel.LETTER, Case.Channel.COUNTER})
ON_BEHALF_FIELDS = {"requester_id", "channel", "intake_note"}
KIND_FIELDS = {
    Case.Kind.WITHDRAWAL: {"required": {"purpose"}, "optional": {"needed_by"}},
    Case.Kind.RETURN: {"required": {"condition", "reason"}, "optional": {"source_reference"}},
    Case.Kind.PROBLEM: {"required": {"description"}, "optional": {"impact", "related_case_number"}},
}
LINE_FIELDS = {"item_code", "quantity", "serials"}
ACTION_FIELDS = {"reason"}
ACTION_EXTRA_FIELDS = {
    "assign": {"assignee_id"},
    "request_handoff": {"to_division", "reason_category"},
    "decide_routing": {"division"},
    "set_priority": {"priority"},
    "record_facts": {"facts", "clear"},
}


def clean_idempotency_key(value):
    if not isinstance(value, str) or not IDEMPOTENCY_KEY.match(value):
        fail(ErrorCode.VALIDATION_ERROR, "idempotency_key must be 8-128 safe characters.")
    return value


def clean_revision(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        fail(ErrorCode.VALIDATION_ERROR, "expected_revision must be a positive integer.")
    return value


def _text(payload, name, required, max_length=TEXT_MAX):
    value = payload.get(name, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        fail(ErrorCode.VALIDATION_ERROR, f"{name} must be text.")
    value = value.strip()
    if required and not value:
        fail(ErrorCode.VALIDATION_ERROR, f"{name} is required.")
    if len(value) > max_length:
        fail(ErrorCode.VALIDATION_ERROR, f"{name} is too long.")
    return value


def _reject_unknown(data, allowed, where):
    if not isinstance(data, dict):
        fail(ErrorCode.VALIDATION_ERROR, f"{where} must be an object.")
    unknown = set(data) - set(allowed)
    if unknown:
        fail(ErrorCode.VALIDATION_ERROR, f"Unknown {where} fields: {', '.join(sorted(unknown))}.")


def parse_quantity(raw):
    """Exact decimal from an int or numeric string; floats and non-positive values refused."""
    if isinstance(raw, bool) or isinstance(raw, float):
        fail(ErrorCode.VALIDATION_ERROR, "quantity must be a string or integer, not a float.")
    if isinstance(raw, int):
        raw = str(raw)
    if not isinstance(raw, str) or not QUANTITY.match(raw.strip()):
        fail(ErrorCode.VALIDATION_ERROR, "quantity must be a positive number.")
    try:
        value = Decimal(raw.strip())
    except InvalidOperation:
        fail(ErrorCode.VALIDATION_ERROR, "quantity must be a positive number.")
    if value <= 0:
        fail(ErrorCode.VALIDATION_ERROR, "quantity must be greater than zero.")
    return value


def check_quantity_for_item(quantity, item):
    """Whole units for counted or serialised items; at most 2 decimals for divisible units."""
    places = -quantity.normalize().as_tuple().exponent
    if not item.is_divisible and places > 0:
        fail(ErrorCode.VALIDATION_ERROR, f"{item.code} is counted in whole {item.unit}.")
    if item.is_divisible and places > 2:
        fail(ErrorCode.VALIDATION_ERROR, f"{item.code} allows at most 2 decimal places.")
    return quantity.quantize(Decimal("0.01"))


def _serials(raw):
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_SERIALS:
        fail(ErrorCode.VALIDATION_ERROR, "serials must be a list.")
    serials = []
    for value in raw:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 64:
            fail(ErrorCode.VALIDATION_ERROR, "Each serial must be 1-64 characters.")
        serials.append(value.strip())
    if len(set(serials)) != len(serials):
        fail(ErrorCode.VALIDATION_ERROR, "Serials must not repeat.")
    return serials


@dataclass(frozen=True)
class LineInput:
    item_code: str
    quantity: Decimal | None
    serials: tuple


@dataclass(frozen=True)
class Submission:
    kind: str
    lines: tuple
    fields: dict
    related_case_number: str
    requester_id: int | None
    channel: str
    intake_note: str


def parse_submission(payload, on_behalf):
    """Shape and per-kind minimum fields (Q14). Item and unit rules need the DB (services)."""
    if not isinstance(payload, dict):
        fail(ErrorCode.VALIDATION_ERROR, "payload must be an object.")
    kind = payload.get("kind")
    if kind not in KIND_FIELDS:
        fail(ErrorCode.VALIDATION_ERROR, "kind must be withdrawal, return or problem.")
    spec = KIND_FIELDS[kind]
    allowed = {"kind", "lines"} | spec["required"] | spec["optional"]
    if on_behalf:
        allowed |= ON_BEHALF_FIELDS
    _reject_unknown(payload, allowed, "payload")

    fields = {}
    for name in spec["required"]:
        fields[name] = _text(payload, name, True)
    for name in spec["optional"] - {"needed_by", "related_case_number"}:
        fields[name] = _text(payload, name, False, 100 if name == "source_reference" else TEXT_MAX)
    if "needed_by" in spec["optional"]:
        fields["needed_by"] = None
        if payload.get("needed_by"):
            try:
                fields["needed_by"] = date.fromisoformat(payload["needed_by"])
            except (TypeError, ValueError):
                fail(ErrorCode.VALIDATION_ERROR, "needed_by must be YYYY-MM-DD.")

    raw_lines = payload.get("lines") or []
    if not isinstance(raw_lines, list) or len(raw_lines) > MAX_LINES:
        fail(ErrorCode.VALIDATION_ERROR, f"lines must be a list of at most {MAX_LINES}.")
    if kind != Case.Kind.PROBLEM and not raw_lines:
        fail(ErrorCode.VALIDATION_ERROR, "At least one item line is required.")
    lines = []
    for raw in raw_lines:
        _reject_unknown(raw, LINE_FIELDS, "line")
        code = raw.get("item_code")
        if not isinstance(code, str) or not code.strip():
            fail(ErrorCode.VALIDATION_ERROR, "item_code is required.")
        quantity = raw.get("quantity")
        if kind == Case.Kind.PROBLEM:
            quantity = None if quantity in (None, "") else parse_quantity(quantity)
        else:
            if quantity in (None, ""):
                fail(ErrorCode.VALIDATION_ERROR, "quantity is required.")
            quantity = parse_quantity(quantity)
        serials = _serials(raw.get("serials"))
        if kind == Case.Kind.WITHDRAWAL and serials:
            fail(ErrorCode.VALIDATION_ERROR, "Serials are recorded at issue, not on request.")
        lines.append(LineInput(code.strip(), quantity, tuple(serials)))
    if len({line.item_code for line in lines}) != len(lines):
        fail(ErrorCode.VALIDATION_ERROR, "Combine quantities of the same item into one line.")

    requester_id, channel, note = None, Case.Channel.LINE, ""
    if on_behalf:
        requester_id = payload.get("requester_id")
        if isinstance(requester_id, bool) or not isinstance(requester_id, int):
            fail(ErrorCode.VALIDATION_ERROR, "requester_id is required.")
        channel = payload.get("channel")
        if channel not in STAFF_CHANNELS:
            fail(ErrorCode.VALIDATION_ERROR, "channel must be phone, letter or counter.")
        note = _text(payload, "intake_note", True)
    related = ""
    if "related_case_number" in spec["optional"]:
        related = _text(payload, "related_case_number", False, 16)
    return Submission(kind, tuple(lines), fields, related, requester_id, channel, note)


def clean_reason(payload, required):
    return _text(payload, "reason", required)


def parse_action_payload(action, payload):
    payload = {} if payload is None else payload
    _reject_unknown(payload, ACTION_FIELDS | ACTION_EXTRA_FIELDS.get(action, set()), "payload")
    return payload


def contract_document():
    """Machine-readable contract v1: commands, errors, actions, matrix and event envelope."""
    return {
        "schema_version": SCHEMA_VERSION,
        "command": {
            "actor": "from authenticated context, never from the payload",
            "fields": ["case_id", "expected_revision", "idempotency_key", "payload"],
            "result": list(CommandResult.__dataclass_fields__),
        },
        "errors": [code.value for code in ErrorCode],
        "submission": {
            "kinds": {
                kind: {"required": sorted(spec["required"]), "optional": sorted(spec["optional"])}
                for kind, spec in KIND_FIELDS.items()
            },
            "line_fields": sorted(LINE_FIELDS),
            "on_behalf_fields": sorted(ON_BEHALF_FIELDS),
            "staff_channels": sorted(STAFF_CHANNELS),
        },
        "actions": {
            spec.name: {
                "label": spec.label,
                "module": spec.module,
                "enabled": spec.enabled,
                "reason_required": spec.reason_required,
                "payload": sorted(ACTION_FIELDS | ACTION_EXTRA_FIELDS.get(spec.name, set())),
            }
            for spec in workflow.ACTIONS.values()
        },
        "matrix": [
            dict(zip(("action", "kind", "from", "to", "module"), row, strict=True))
            for row in workflow.matrix_rows()
        ],
        "event_envelope": list(EVENT_ENVELOPE_FIELDS),
        "events": [EVENT_CASE_SUBMITTED, EVENT_DECISION_RECORDED],
    }
