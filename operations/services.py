"""M02 case services: submit and transition cases under contract v1.

Each accepted command commits the case change, its Decision, AuditEvent, OutboxEvent and
ActionReceipt in one transaction, or nothing at all. Permissions are re-read from the database
on every call, including when a stored result is replayed for a repeated idempotency key.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from functools import wraps

from django.contrib.auth import get_user_model
from django.db import InterfaceError, OperationalError, connection, transaction
from django.utils import timezone

from operations import identity, workflow
from operations.contracts import (
    EVENT_CASE_SUBMITTED,
    EVENT_DECISION_RECORDED,
    Actor,
    CommandFailed,
    CommandResult,
    ErrorCode,
    check_quantity_for_item,
    clean_idempotency_key,
    clean_reason,
    clean_revision,
    fail,
    parse_action_payload,
    parse_submission,
)
from operations.models import (
    ActionReceipt,
    Case,
    CaseLine,
    CaseNumberCounter,
    Decision,
    Division,
    EquipmentType,
    OutboxEvent,
    Requester,
    Role,
)

User = get_user_model()
S = Case.Status
NUMBER_PREFIX = {Case.Kind.WITHDRAWAL: "W", Case.Kind.RETURN: "R", Case.Kind.PROBLEM: "P"}


def _dependency_guard(command):
    """Database outages surface as DEPENDENCY_UNAVAILABLE after the transaction rolled back."""

    @wraps(command)
    def wrapper(*args, **kwargs):
        try:
            return command(*args, **kwargs)
        except (OperationalError, InterfaceError) as exc:
            raise CommandFailed(ErrorCode.DEPENDENCY_UNAVAILABLE, "Database unavailable.") from exc

    return wrapper


# --- Actor context and visibility -------------------------------------------------------------


@dataclass(frozen=True)
class _Context:
    label: str
    user: object = None
    scope: object = None
    requester: object = None


def _resolve(actor):
    if not isinstance(actor, Actor):
        fail(ErrorCode.AUTH_REQUIRED)
    if actor.is_requester:
        requester = (
            Requester.objects.select_related("district")
            .filter(pk=actor.requester.pk, status=Requester.Status.ACTIVE)
            .first()
        )
        if requester is None:
            fail(ErrorCode.FORBIDDEN, "Requester is not active.")
        return _Context(label=actor.key, requester=requester)
    if not getattr(actor.user, "is_authenticated", False):
        fail(ErrorCode.AUTH_REQUIRED)
    scope = identity.staff_scope(actor.user)
    if not scope.has_any_access:
        fail(ErrorCode.FORBIDDEN, "No role.")
    return _Context(label=actor.key, user=actor.user, scope=scope)


def _can_view(ctx, case):
    if ctx.requester is not None:
        return case.requester_id == ctx.requester.pk
    return identity.can_access_division(ctx.scope, case.owner_division_id)


def _is_member(ctx, division_id, roles=None):
    """Holds a role in the division itself; admin rights alone do not make someone a member."""
    if ctx.scope is None:
        return False
    held = ctx.scope.division_roles.get(division_id, frozenset())
    return bool(held if roles is None else held & set(roles))


def visible_cases(actor):
    """Own-requester rule for requesters, owner-division rule for staff, all for admins."""
    ctx = _resolve(actor)
    cases = Case.objects.select_related("district", "owner_division", "requester")
    if ctx.requester is not None:
        return identity.filter_by_requester(cases, ctx.requester)
    return identity.filter_by_division(cases, ctx.scope, "owner_division")


def _parse_case_id(case_id):
    try:
        return uuid.UUID(str(case_id))
    except ValueError:
        fail(ErrorCode.NOT_FOUND)


def get_case(actor, case_id):
    case = visible_cases(actor).filter(pk=_parse_case_id(case_id)).first()
    if case is None:
        fail(ErrorCode.NOT_FOUND)
    return case


# --- Idempotency and recording --------------------------------------------------------------


def _request_hash(data):
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _lock_receipt_key(ctx, action, key):
    """Serialise concurrent retries of one key before looking for its receipt (PostgreSQL)."""
    digest = hashlib.sha256(f"{ctx.label}|{action}|{key}".encode()).digest()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s)", [int.from_bytes(digest[:8], "big", signed=True)]
        )


def _replay(ctx, action, key, request_hash):
    receipt = (
        ActionReceipt.objects.select_related("case")
        .filter(actor_key=ctx.label, action=action, idempotency_key=key)
        .first()
    )
    if receipt is None:
        return None
    if receipt.request_hash != request_hash:
        fail(ErrorCode.IDEMPOTENCY_CONFLICT, "Key was already used with a different request.")
    if not _can_view(ctx, receipt.case):
        fail(ErrorCode.NOT_FOUND)
    return CommandResult(**receipt.result)


def _record(ctx, case, action, from_status, reason, data, event_type, key, request_hash):
    decision = Decision.objects.create(
        case=case,
        action=action,
        from_status=from_status,
        to_status=case.status,
        revision=case.revision,
        actor=ctx.user,
        actor_label=ctx.label,
        reason=reason,
        data=data,
    )
    identity.audit(
        ctx.user,
        f"case.{action}",
        case,
        reason,
        actor_label=ctx.label,
        number=case.number,
        from_status=from_status,
        to_status=case.status,
        revision=case.revision,
    )
    OutboxEvent.objects.create(
        event_type=event_type,
        subject_type="case",
        subject_id=str(case.pk),
        case=case,
        case_revision=case.revision,
        correlation_id=decision.action_id,
        payload={
            "action": action,
            "action_id": str(decision.action_id),
            "case_number": case.number,
            "kind": case.kind,
            "from_status": from_status,
            "to_status": case.status,
        },
        occurred_at=decision.created_at,
    )
    result = CommandResult(
        case_id=str(case.pk),
        case_number=case.number,
        revision=case.revision,
        status=case.status,
        action_id=str(decision.action_id),
    )
    ActionReceipt.objects.create(
        actor_key=ctx.label,
        action=action,
        idempotency_key=key,
        request_hash=request_hash,
        case=case,
        result=result.to_dict(),
    )
    return result


# --- Submission ------------------------------------------------------------------------------


def _next_number(kind, now):
    """Gap-free per prefix and Buddhist-era year: the counter row is locked and rolls back."""
    prefix, year_be = NUMBER_PREFIX[kind], timezone.localtime(now).year + 543
    CaseNumberCounter.objects.bulk_create(
        [CaseNumberCounter(prefix=prefix, year_be=year_be)], ignore_conflicts=True
    )
    counter = CaseNumberCounter.objects.select_for_update().get(prefix=prefix, year_be=year_be)
    counter.last_value += 1
    counter.save(update_fields=["last_value"])
    return f"{prefix}{year_be % 100:02d}-{counter.last_value:06d}"


def _build_lines(submission):
    codes = [line.item_code for line in submission.lines]
    items = {i.code: i for i in EquipmentType.objects.filter(code__in=codes, is_active=True)}
    lines = []
    for number, line in enumerate(submission.lines, start=1):
        item = items.get(line.item_code)
        if item is None:
            fail(ErrorCode.VALIDATION_ERROR, f"Unknown item {line.item_code}.")
        quantity = line.quantity
        if quantity is not None:
            quantity = check_quantity_for_item(quantity, item)
        if line.serials and not item.is_serialized:
            fail(ErrorCode.VALIDATION_ERROR, f"{item.code} is not tracked by serial.")
        if submission.kind == Case.Kind.RETURN and item.is_serialized:
            if len(line.serials) != quantity:
                fail(ErrorCode.VALIDATION_ERROR, f"Give one serial per returned {item.code}.")
        lines.append(
            CaseLine(
                line_no=number, item=item, requested_quantity=quantity, serials=list(line.serials)
            )
        )
    return lines


def _submission_requester(ctx, submission, intake):
    if ctx.requester is not None:
        return ctx.requester
    # Filing on behalf of a requester is a duty of intake staff (not of admin rights alone).
    if not _is_member(ctx, intake.pk):
        fail(ErrorCode.FORBIDDEN, "Only intake staff can file a case for a requester.")
    requester = (
        Requester.objects.select_related("district")
        .filter(pk=submission.requester_id, status=Requester.Status.ACTIVE)
        .first()
    )
    if requester is None:
        fail(ErrorCode.VALIDATION_ERROR, "Requester must be approved and active.")
    return requester


@_dependency_guard
def submit_case(actor, *, idempotency_key, payload):
    """Create a case from a confirmed draft (LINE) or a staff intake form (on behalf)."""
    key = clean_idempotency_key(idempotency_key)
    request_hash = _request_hash({"payload": payload})
    with transaction.atomic():
        ctx = _resolve(actor)
        _lock_receipt_key(ctx, "submit", key)
        replayed = _replay(ctx, "submit", key, request_hash)
        if replayed is not None:
            return replayed
        submission = parse_submission(payload, on_behalf=ctx.requester is None)
        intake = Division.objects.filter(is_intake=True, is_active=True).first()
        if intake is None:
            fail(ErrorCode.DEPENDENCY_UNAVAILABLE, "No active intake division is configured.")
        requester = _submission_requester(ctx, submission, intake)
        if not requester.district.is_active:
            fail(ErrorCode.VALIDATION_ERROR, "Requester district is not active.")
        lines = _build_lines(submission)
        related = None
        if submission.related_case_number:
            related = Case.objects.filter(
                number=submission.related_case_number, requester=requester
            ).first()
            if related is None:
                fail(ErrorCode.VALIDATION_ERROR, "Related case not found for this requester.")
        fields = dict(submission.fields)
        if "reason" in fields:
            fields["return_reason"] = fields.pop("reason")
        now = timezone.now()
        case = Case.objects.create(
            number=_next_number(submission.kind, now),
            kind=submission.kind,
            requester=requester,
            district=requester.district,
            owner_division=intake,
            submitted_by=ctx.user,
            channel=submission.channel,
            intake_note=submission.intake_note,
            related_case=related,
            submitted_at=now,
            updated_at=now,
            **fields,
        )
        for line in lines:
            line.case = case
        CaseLine.objects.bulk_create(lines)
        data = {"channel": case.channel, "on_behalf": ctx.requester is None}
        return _record(ctx, case, "submit", "", "", data, EVENT_CASE_SUBMITTED, key, request_hash)


# --- Transitions -----------------------------------------------------------------------------


def _require_owner_staff(ctx, case):
    """Staff of the owner division, or an admin acting for any division."""
    if ctx.requester is not None or not identity.can_access_division(
        ctx.scope, case.owner_division_id
    ):
        fail(ErrorCode.FORBIDDEN, "Only staff of the owner division can do this.")


def _take(ctx, case, payload, reason, target):
    # Becoming the responsible person needs real membership of the owner division.
    if not _is_member(ctx, case.owner_division_id):
        fail(ErrorCode.FORBIDDEN, "Only staff of the owner division can take a case.")
    if case.assignee_id == ctx.user.pk:
        fail(ErrorCode.VALIDATION_ERROR, "You are already responsible for this case.")
    case.assignee = ctx.user
    case.status = target
    return {"assignee_id": ctx.user.pk}


def _assign(ctx, case, payload, reason, target):
    if not (ctx.scope and ctx.scope.is_admin) and not _is_member(
        ctx, case.owner_division_id, [Role.SUPERVISOR]
    ):
        fail(ErrorCode.FORBIDDEN, "Only a supervisor of the owner division can assign.")
    assignee_id = payload.get("assignee_id")
    if isinstance(assignee_id, bool) or not isinstance(assignee_id, int):
        fail(ErrorCode.VALIDATION_ERROR, "assignee_id is required.")
    assignee = User.objects.filter(pk=assignee_id, is_active=True).first()
    if (
        assignee is None
        or case.owner_division_id not in identity.staff_scope(assignee).division_roles
    ):
        fail(ErrorCode.VALIDATION_ERROR, "Assignee must be active staff of the owner division.")
    if case.assignee_id == assignee.pk:
        fail(ErrorCode.VALIDATION_ERROR, "This person is already responsible.")
    previous = case.assignee_id
    case.assignee = assignee
    return {"assignee_id": assignee.pk, "previous_assignee_id": previous}


def _pause(ctx, case, payload, reason, target):
    _require_owner_staff(ctx, case)
    case.resume_status = case.status
    case.status = target
    return {"resume_status": case.resume_status}


def _unpause(ctx, case, payload, reason, target):
    _require_owner_staff(ctx, case)
    case.status, case.resume_status = case.resume_status, ""
    return {}


def _cancel(ctx, case, payload, reason, target):
    if ctx.requester is not None:
        # Requesters may withdraw their own case only before work has started.
        if case.status not in {S.SUBMITTED, S.TRIAGE}:
            fail(ErrorCode.FORBIDDEN, "The case can no longer be cancelled by the requester.")
    else:
        _require_owner_staff(ctx, case)
        if not reason:
            fail(ErrorCode.VALIDATION_ERROR, "A reason is required.")
    case.status, case.resume_status = target, ""
    return {}


def _decline(ctx, case, payload, reason, target):
    _require_owner_staff(ctx, case)
    case.status = target
    return {}


HANDLERS = {
    "start_triage": _take,
    "claim": _take,
    "assign": _assign,
    "request_info": _pause,
    "hold": _pause,
    "info_received": _unpause,
    "resume": _unpause,
    "cancel": _cancel,
    "decline": _decline,
}


@_dependency_guard
def perform_action(actor, case_id, action, *, expected_revision, idempotency_key, payload=None):
    """Apply one matrix action to a case the actor can see, at the revision they last saw."""
    spec = workflow.ACTIONS.get(action)
    if spec is None:
        fail(ErrorCode.VALIDATION_ERROR, "Unknown action.")
    key = clean_idempotency_key(idempotency_key)
    case_uuid = _parse_case_id(case_id)
    clean_revision(expected_revision)
    request_hash = _request_hash(
        {"case_id": str(case_uuid), "expected_revision": expected_revision, "payload": payload}
    )
    with transaction.atomic():
        ctx = _resolve(actor)
        _lock_receipt_key(ctx, action, key)
        case = Case.objects.select_for_update().filter(pk=case_uuid).first()
        # Unknown and out-of-scope cases are indistinguishable.
        if case is None or not _can_view(ctx, case):
            fail(ErrorCode.NOT_FOUND)
        replayed = _replay(ctx, action, key, request_hash)
        if replayed is not None:
            return replayed
        if not spec.enabled:
            fail(ErrorCode.ACTION_NOT_ENABLED, f"{action} arrives with {spec.module}.")
        payload = parse_action_payload(action, payload)
        reason = clean_reason(payload, spec.reason_required)
        if expected_revision != case.revision:
            fail(ErrorCode.REVISION_CONFLICT, "The case changed; reload and try again.")
        targets = spec.targets(case.kind, case.status)
        if not targets:
            fail(ErrorCode.INVALID_TRANSITION, f"{action} is not allowed while {case.status}.")
        # M02 actions have exactly one target; RESUME is resolved by the handler.
        (target,) = targets
        from_status = case.status
        data = HANDLERS[action](ctx, case, payload, reason, target)
        now = timezone.now()
        case.revision += 1
        case.updated_at = now
        if case.status in workflow.TERMINAL:
            case.ended_at = now
        case.save()
        return _record(
            ctx, case, action, from_status, reason, data, EVENT_DECISION_RECORDED, key, request_hash
        )
