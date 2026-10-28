"""Common command contract v1 for case mutations (E.4), shared by LINE, n8n and web callers.

Callers pass an authenticated `Actor`, never an actor ID in the payload. Payloads are plain JSON
so the same contract works for internal APIs and HTML views. Quantities travel as strings or
integers; floats are refused so no binary rounding ever reaches a stored amount.
"""

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
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
    # ADR 0008: the other cases a mixed request was split into, [{"case_id", "case_number"}].
    split: list = field(default_factory=list)

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

STAFF_CHANNELS = frozenset(
    {Case.Channel.PHONE, Case.Channel.LETTER, Case.Channel.COUNTER, Case.Channel.FALLBACK_FORM}
)
ON_BEHALF_FIELDS = {"requester_id", "channel", "intake_note", "reported_at"}
# เมนู LINE (15 ต.ค. 2569 Q20): the division the person chose in the menu, and the choice's name.
MENU_FIELDS = {"division_code", "menu_label", "sap_kind", "intake_data"}
# เวลาแจ้งจริง may go back this far (an outage plus a weekend), never into the future.
REPORTED_AT_MAX_AGE = timedelta(days=30)
REPORTED_AT_SKEW = timedelta(minutes=5)
KIND_FIELDS = {
    # M09 Q5: withdrawals and returns may refer to the requester's earlier case (e.g. a problem).
    # sap_number (user, 23 Oct 2569): the SAP slip the request follows.
    Case.Kind.WITHDRAWAL: {
        "required": {"purpose"},
        "optional": {"needed_by", "related_case_number", "sap_number"},
    },
    Case.Kind.RETURN: {
        "required": {"condition", "reason"},
        "optional": {"source_reference", "related_case_number", "sap_number"},
    },
    Case.Kind.PROBLEM: {
        "required": {"description"},
        # sap_number: ติดตามสถานะการซ่อม (topic repair, 24 Oct 2569) cites its SAP number.
        "optional": {
            "impact",
            "related_case_number",
            "topic",
            "latitude",
            "longitude",
            "sap_number",
            "meters",
        },
    },
}
LINE_FIELDS = {"item_code", "quantity", "serials", "details"}
SAP_PATTERN = (r"[A-Za-z0-9/-]{4,30}", "เลข SAP ใช้ตัวอักษร ตัวเลข ขีด หรือ / ได้ 4 ถึง 30 ตัว")
TRACKING_PATTERNS = {
    "": SAP_PATTERN,
    "slip": SAP_PATTERN,
    "return_slip": SAP_PATTERN,
    "work_order": SAP_PATTERN,
    "meter": (r"[A-Za-z0-9-]{4,20}", "หมายเลขเครื่องวัดฯ ใช้ตัวอักษร ตัวเลข หรือขีด 4 ถึง 20 ตัว"),
    "project": (r"[\w\u0E00-\u0E7F ./()-]{2,60}", "ชื่อโครงการยาว 2 ถึง 60 ตัว"),
}
NO_ITEM_TOPICS = {Case.Topic.USAGE, Case.Topic.SUGGESTION, Case.Topic.INQUIRY, Case.Topic.SYSTEM}
# ADR 0028: any action may carry a status notice for the requester and an internal note.
ACTION_FIELDS = {"reason", "notice", "internal_note"}
ACTION_EXTRA_FIELDS = {
    "assign": {"assignee_id"},
    "request_handoff": {"to_division", "reason_category", "keywords"},
    "decide_routing": {"division", "keywords"},
    "set_priority": {"priority"},
    "set_section": {"section"},
    "set_sap_number": {"sap_number"},
    "record_facts": {"facts", "clear"},
    # M07: lines are [{"line_no": n, ...}] of the case; deliveries are numbered per case.
    "approve": {"lines"},
    "allocate": {"lines"},
    "issue": {"lines", "recipient_name", "document_number"},
    "confirm": {"delivery", "result", "lines", "channel", "intake_note"},  # M07; M09 uses channel
    "resolve_discrepancy": {"delivery", "resolution", "reference", "lines"},
    "terminate_outstanding": {"lines"},
    "cancel": {"resolution", "reference"},
    "close": {"reference"},
    # M08: units are numbered per case; split maps ready/repair/other (or pending) to amounts.
    "return_instructions": {"place", "from_date", "to_date", "note"},
    "receive": {"items", "carrier_name", "document_number"},
    "inspect": {"unit", "split", "reference"},
    "decide_inspection": {"unit", "split", "reference"},
    "resolve_return_discrepancy": {"unit", "resolution", "reference", "replaces"},
    "terminate_return": {"lines", "category", "reference"},
    # M09: children are numbered per case; an intake answer carries channel and intake_note.
    "resolve": {"category", "message", "internal_note"},
    "dispute_resolution": {"channel", "intake_note"},
    "request_child_work": {"division", "task", "deliverable", "rule", "lines"},
    "accept_child_work": {"child"},
    "decline_child_work": {"child"},
    "decide_child_work": {"child", "division"},
    "finish_child_work": {"child", "result", "summary"},
    "cancel_child_work": {"child"},
}
# M11 Q3/Q13: in manual_reference mode these cite a registry document and carry its evidence.
for _action in (
    "issue",
    "receive",
    "inspect",
    "decide_inspection",
    "resolve_discrepancy",
    "resolve_return_discrepancy",
):
    ACTION_EXTRA_FIELDS[_action] = ACTION_EXTRA_FIELDS[_action] | {"evidence"}


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
    details: tuple = ()  # rows the requester typed for a shared item (typed_lines)


@dataclass(frozen=True)
class Submission:
    kind: str
    lines: tuple
    fields: dict
    related_case_number: str
    requester_id: int | None
    channel: str
    intake_note: str
    reported_at: datetime | None = None
    division_code: str = ""
    menu_label: str = ""
    meters: tuple = ()  # เครื่องวัดในเรื่องการใช้ไฟฟ้า (usage_meters)
    intake_data: dict | None = None  # values a published LINE flow collected (ADR 0023)


def parse_submission(payload, on_behalf):
    """Shape and per-kind minimum fields (Q14). Item and unit rules need the DB (services)."""
    if not isinstance(payload, dict):
        fail(ErrorCode.VALIDATION_ERROR, "payload must be an object.")
    kind = payload.get("kind")
    if kind not in KIND_FIELDS:
        fail(ErrorCode.VALIDATION_ERROR, "kind must be withdrawal, return or problem.")
    spec = KIND_FIELDS[kind]
    allowed = {"kind", "lines"} | spec["required"] | spec["optional"] | MENU_FIELDS
    if on_behalf:
        allowed |= ON_BEHALF_FIELDS
    _reject_unknown(payload, allowed, "payload")

    fields = {}
    for name in spec["required"]:
        fields[name] = _text(payload, name, True)
    for name in spec["optional"] - {
        "needed_by",
        "related_case_number",
        "topic",
        "latitude",
        "longitude",
        "sap_number",
        "meters",
    }:
        fields[name] = _text(payload, name, False, 100 if name == "source_reference" else TEXT_MAX)
    if "sap_number" in spec["optional"]:
        # เลขติดตาม (grill กคว. 27 ต.ค. 2569 รอบใหม่ Q1, Q2): the kind says which pattern applies.
        fields["sap_kind"] = _text(payload, "sap_kind", False, 16)
        if fields["sap_kind"] not in TRACKING_PATTERNS:
            fail(
                ErrorCode.VALIDATION_ERROR,
                "sap_kind must be slip, return_slip, work_order, meter or project.",
            )
        fields["sap_number"] = _text(payload, "sap_number", False, 60)
        pattern, problem = TRACKING_PATTERNS[fields["sap_kind"]]
        if fields["sap_number"] and not re.fullmatch(pattern, fields["sap_number"]):
            fail(ErrorCode.VALIDATION_ERROR, problem)
    if "needed_by" in spec["optional"]:
        fields["needed_by"] = None
        if payload.get("needed_by"):
            try:
                fields["needed_by"] = date.fromisoformat(payload["needed_by"])
            except (TypeError, ValueError):
                fail(ErrorCode.VALIDATION_ERROR, "needed_by must be YYYY-MM-DD.")

    if "latitude" in spec["optional"]:
        fields.update(_location(payload))
    if "topic" in spec["optional"]:
        fields["topic"] = payload.get("topic") or ""
        if fields["topic"] not in ("", *Case.Topic.values):
            fail(
                ErrorCode.VALIDATION_ERROR,
                "topic must be one of: " + ", ".join(Case.Topic.values) + ".",
            )

    raw_lines = payload.get("lines") or []
    if not isinstance(raw_lines, list) or len(raw_lines) > MAX_LINES:
        fail(ErrorCode.VALIDATION_ERROR, f"lines must be a list of at most {MAX_LINES}.")
    # A tracking case (เลขติดตาม with its kind) lists no items: they live in SAP (Q3, Q4).
    if kind != Case.Kind.PROBLEM and not raw_lines and not fields.get("sap_kind"):
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
        from operations import typed_lines  # local: it reads the database

        try:
            details = typed_lines.clean_details(raw.get("details"))
        except ValueError as exc:
            fail(ErrorCode.VALIDATION_ERROR, str(exc))
        lines.append(LineInput(code.strip(), quantity, tuple(serials), tuple(details)))
    if lines and fields.get("topic") in NO_ITEM_TOPICS:
        fail(ErrorCode.VALIDATION_ERROR, "This topic lists no items.")
    if len({line.item_code for line in lines}) != len(lines):
        fail(ErrorCode.VALIDATION_ERROR, "Combine quantities of the same item into one line.")

    requester_id, channel, note, reported_at = None, Case.Channel.LINE, "", None
    if on_behalf:
        requester_id = payload.get("requester_id")
        if isinstance(requester_id, bool) or not isinstance(requester_id, int):
            fail(ErrorCode.VALIDATION_ERROR, "requester_id is required.")
        channel = payload.get("channel")
        if channel not in STAFF_CHANNELS:
            fail(
                ErrorCode.VALIDATION_ERROR,
                "channel must be phone, letter, counter or fallback_form.",
            )
        note = _text(payload, "intake_note", True)
        reported_at = _reported_at(payload.get("reported_at"))
        if channel == Case.Channel.FALLBACK_FORM and reported_at is None:
            fail(ErrorCode.VALIDATION_ERROR, "reported_at is required for the fallback form.")
    related = ""
    if "related_case_number" in spec["optional"]:
        related = _text(payload, "related_case_number", False, 16)
    division_code = _text(payload, "division_code", False, 16)
    menu_label = _text(payload, "menu_label", False, 120)
    meters = ()
    if "meters" in spec["optional"]:
        from operations import usage_meters  # local: it reads the database for districts

        try:
            meters = usage_meters.clean(payload.get("meters"))
        except ValueError as exc:
            fail(ErrorCode.VALIDATION_ERROR, str(exc))
        if meters and fields.get("topic") != Case.Topic.USAGE:
            fail(ErrorCode.VALIDATION_ERROR, "ระบุเครื่องวัดได้เฉพาะเรื่องการใช้ไฟฟ้า")
    intake_data = payload.get("intake_data") or {}
    if not isinstance(intake_data, dict) or len(json.dumps(intake_data, ensure_ascii=False)) > 5000:
        fail(ErrorCode.VALIDATION_ERROR, "intake_data must be a small object.")
    return Submission(
        kind,
        tuple(lines),
        fields,
        related,
        requester_id,
        channel,
        note,
        reported_at,
        division_code,
        menu_label,
        meters,
        intake_data,
    )


def _location(payload):
    """ตำแหน่งที่แจ้ง (ADR 0010): both coordinates or neither, within the world."""
    lat, lng = payload.get("latitude"), payload.get("longitude")
    if lat in (None, "") and lng in (None, ""):
        return {}
    try:
        lat, lng = Decimal(str(lat)), Decimal(str(lng))
    except (InvalidOperation, ValueError):
        fail(ErrorCode.VALIDATION_ERROR, "latitude and longitude must be numbers.")
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        fail(ErrorCode.VALIDATION_ERROR, "latitude and longitude must be numbers.")
    return {
        "latitude": lat.quantize(Decimal("0.000001")),
        "longitude": lng.quantize(Decimal("0.000001")),
    }


def _reported_at(value):
    """เวลาแจ้งจริง: ISO date-time (local Bangkok time when naive), past 30 days, not future."""
    from django.utils import timezone

    if value in (None, ""):
        return None
    try:
        moment = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        moment = None
    if moment is None:
        fail(ErrorCode.VALIDATION_ERROR, "reported_at must be an ISO date-time.")
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment)
    now = timezone.now()
    if moment > now + REPORTED_AT_SKEW or moment < now - REPORTED_AT_MAX_AGE:
        fail(ErrorCode.VALIDATION_ERROR, "reported_at must be within the past 30 days.")
    return moment


def clean_reason(payload, required):
    return _text(payload, "reason", required)


def clean_text(payload, name, required, max_length=TEXT_MAX):
    return _text(payload, name, required, max_length)


def clean_serials(raw):
    return _serials(raw)


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
                "enabled_kinds": spec.enabled_kinds,
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
