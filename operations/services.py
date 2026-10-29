"""M02 case services: submit and transition cases under contract v1.

Each accepted command commits the case change, its Decision, AuditEvent, OutboxEvent and
ActionReceipt in one transaction, or nothing at all. Permissions are re-read from the database
on every call, including when a stored result is replayed for a repeated idempotency key.
M06 adds routing decisions (operations/routing.py) and a fresh proposal after every decision
(operations/policy.py), in the same transaction. M07 adds withdrawal fulfilment
(operations/fulfilment.py) and the inventory seam (operations/inventory.py).
"""

import dataclasses
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from functools import wraps

from django.contrib.auth import get_user_model
from django.db import InterfaceError, OperationalError, connection, transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from operations import (
    approvals,
    coordination,
    fulfilment,
    identity,
    policy,
    problems,
    registry,
    returns,
    routing,
    routing_words,
    support,
    units,
    workflow,
)
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
    clean_text,
    fail,
    parse_action_payload,
    parse_submission,
)
from operations.models import (
    AckClock,
    ActionReceipt,
    Case,
    CaseLine,
    CaseNumberCounter,
    ChildWork,
    Decision,
    District,
    Division,
    EquipmentType,
    Handoff,
    OutboxEvent,
    Requester,
    Role,
    RoutingReview,
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


def _waiting_for_coordinator(case_ref):
    """Open routing review, an unanswered (escalated) intake/handoff/child request, or child work
    a coordinator must place: what routing-authority holders see (M06, M09 Q6)."""
    return Q(held_by_admin=False) & (
        Q(Exists(RoutingReview.objects.filter(case=case_ref, status=RoutingReview.Status.OPEN)))
        | Q(
            Exists(
                AckClock.objects.filter(
                    case=case_ref,
                    stopped_at__isnull=True,
                    escalated_at__isnull=False,
                    kind__in=[
                        AckClock.Kind.INTAKE,
                        AckClock.Kind.HANDOFF,
                        AckClock.Kind.CHILD_ACCEPT,
                    ],
                )
            )
        )
        | Q(Exists(ChildWork.objects.filter(case=case_ref, status=ChildWork.Status.DISPUTED)))
    )


def coordinator_waiting_condition():
    """What waits for a routing-authority holder, for the M10 "รอฉันตัดสิน" tab."""
    return _waiting_for_coordinator(OuterRef("pk"))


def _staff_condition(ctx):
    """Owner division, a handoff waiting for one of their divisions, or a routing decision
    within their authority. Other divisions' queues stay closed (B.2)."""
    divisions = list(ctx.scope.division_roles)
    # 16 Oct 2569 Q5: a Supervisor reads every case of the divisions they oversee.
    condition = Q(owner_division__in=[*divisions, *ctx.scope.overseen]) | Q(
        Exists(
            Handoff.objects.filter(
                case=OuterRef("pk"), status=Handoff.Status.PENDING, to_division__in=divisions
            )
        )
    )
    scope = policy.routing_scope(ctx.user)
    if scope:
        waiting = _waiting_for_coordinator(OuterRef("pk"))
        condition |= waiting if scope == "all" else waiting & Q(owner_division__in=scope)
    # M07 Q12: approval/allocation authority holders see withdrawals waiting for them; Q4:
    # intake staff see deliveries waiting for a receipt they may record on the district's behalf.
    condition |= coordination.child_condition(divisions)  # M09 Q6: child divisions read
    condition |= fulfilment.authority_condition(ctx.user)
    condition |= returns.certification_condition(ctx.user)  # M08 Q9: conflicts to decide
    condition |= registry.case_condition(divisions)  # M11: registry staff read cases citing SAP
    intake = Division.objects.filter(is_intake=True).values_list("pk", flat=True).first()
    if intake in divisions:
        condition |= fulfilment.receipt_condition()
    return condition


def _can_view(ctx, case):
    if ctx.requester is not None:
        return case.requester_id == ctx.requester.pk
    if case.held_by_admin:
        return ctx.scope.is_admin or ctx.scope.is_support  # ADR 0030: the admins hold it
    if identity.can_access_division(ctx.scope, case.owner_division_id):
        return True
    return Case.objects.filter(_staff_condition(ctx), pk=case.pk).exists()


def _is_member(ctx, division_id, roles=None):
    """Holds a role in the division itself (or is ผู้ช่วยเหลือระบบ, ADR 0007); admin rights
    alone do not make someone a member."""
    return identity.holds(ctx.scope, division_id, roles)


def visible_cases(actor):
    """Own-requester rule for requesters, owner-division rule for staff, all for admins."""
    ctx = _resolve(actor)
    cases = Case.objects.select_related("district", "owner_division", "requester")
    if ctx.requester is not None:
        return identity.filter_by_requester(cases, ctx.requester)
    if ctx.scope.is_admin or ctx.scope.is_support:
        return cases
    return cases.filter(_staff_condition(ctx)).exclude(held_by_admin=True)


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


def record_system(case, action, reason, data, label="ระบบ (ผังหน่วยงาน)"):
    """A step the system took on its own (ADR 0031), on the timeline like any decision."""
    return _record(
        _Context(label=label),
        case,
        action,
        case.status,
        reason,
        data,
        EVENT_DECISION_RECORDED,
        None,
        None,
    )


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
        **({"support": data["support"]} if isinstance(data, dict) and "support" in data else {}),
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
    if key is not None:
        _receipt(ctx, action, key, request_hash, case, result)
    return result


def _receipt(ctx, action, key, request_hash, case, result):
    ActionReceipt.objects.create(
        actor_key=ctx.label,
        action=action,
        idempotency_key=key,
        request_hash=request_hash,
        case=case,
        result=result.to_dict(),
    )


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
                line_no=number,
                item=item,
                requested_quantity=quantity,
                serials=list(line.serials),
                details=list(line.details),
            )
        )
    return lines


def preview_owners(payload):
    """The division codes a draft would go to, without writing anything: the menu's division,
    else the rules' (empty while the draft cannot be read yet or an intake desk takes all)."""
    if payload.get("division_code"):
        return [payload["division_code"]]
    if identity.intake_division() is not None:
        return []
    try:
        submission = parse_submission(payload, on_behalf=False)
        lines = _build_lines(submission)
    except CommandFailed:
        return []
    routes = _route(submission.kind, lines, None, timezone.now())
    return [owner.code for owner, _ in routes if owner is not None]


def _check_sap(submission, fields, requester, routes, intake):
    """เลขใบเบิก/ใบส่งคืน SAP (user, 23 Oct 2569): an open case already citing the number
    refuses it; a LINE withdrawal or return to a division that follows SAP slips needs one, and
    ติดตามสถานะการซ่อม needs its SAP repair number or a numbered piece (25 Oct 2569 Q7)."""
    from operations import typed_lines

    number = fields.get("sap_number", "")
    if number:
        # เลขติดตาม with its kind may repeat: it tracks, it counts nothing (Q3, Q17).
        problem = "" if fields.get("sap_kind") else typed_lines.sap_in_use(number, requester)
        if problem:
            fail(ErrorCode.VALIDATION_ERROR, problem)
        return
    if submission.channel != Case.Channel.LINE or intake is not None:
        return
    if fields.get("topic") == Case.Topic.REPAIR:
        # 25 Oct 2569 Q7: the SAP repair number, or a number on the piece in the typed rows.
        if not typed_lines.identified([line for _, group in routes for line in group]):
            fail(ErrorCode.VALIDATION_ERROR, "ระบุเลข SAP งานซ่อมหรือหมายเลขอุปกรณ์ก่อนส่งเรื่อง")
        return
    owners = [owner.code for owner, _ in routes if owner is not None]
    if typed_lines.needs_sap(submission.kind, owners):
        fail(ErrorCode.VALIDATION_ERROR, "ระบุเลขใบเบิกหรือใบส่งคืน SAP ก่อนส่งเรื่อง")


def _check_meters(submission, fields, intake):
    """เรื่องการใช้ไฟฟ้า (25 Oct 2569 Q11): each meter's district must be an active district, and
    a LINE report names at least one meter."""
    codes = {m["district"] for m in submission.meters}
    known = set(
        District.objects.filter(code__in=codes, is_active=True).values_list("code", flat=True)
    )
    if codes - known:
        fail(ErrorCode.VALIDATION_ERROR, "เขตของเครื่องวัดไม่ถูกต้อง")
    if (
        fields.get("topic") == Case.Topic.USAGE
        and submission.channel == Case.Channel.LINE
        and intake is None
        and not submission.meters
    ):
        fail(ErrorCode.VALIDATION_ERROR, "ระบุหมายเลขเครื่องวัดและเขตก่อนส่งเรื่อง")


def _submission_requester(ctx, submission, intake):
    if ctx.requester is not None:
        return ctx.requester
    # Filing on behalf of a requester is a duty of intake staff (not of admin rights alone);
    # without an intake desk (ADR 0008) any division's staff file, and the rules route the case.
    if intake is not None and not _is_member(ctx, intake.pk):
        fail(ErrorCode.FORBIDDEN, "Only intake staff can file a case for a requester.")
    if intake is None and not (ctx.scope and (ctx.scope.is_support or ctx.scope.division_roles)):
        fail(ErrorCode.FORBIDDEN, "Only division staff can file a case for a requester.")
    requester = (
        Requester.objects.select_related("district")
        .filter(pk=submission.requester_id, status=Requester.Status.ACTIVE, is_anonymous=False)
        .first()
    )
    if requester is None:
        fail(ErrorCode.VALIDATION_ERROR, "Requester must be approved and active.")
    return requester


def _check_anonymous(requester, submission):
    """Anonymous reports are problem-only, require a location and have a daily limit."""
    from operations import options

    if submission.kind != Case.Kind.PROBLEM:
        fail(ErrorCode.FORBIDDEN, "Withdrawals and returns need a registered requester.")
    if submission.fields.get("latitude") is None:
        fail(ErrorCode.VALIDATION_ERROR, "An anonymous report needs a location.")
    limit = int(options.get(options.ANONYMOUS_LIMIT))
    since = timezone.now() - timedelta(days=1)
    if Case.objects.filter(requester=requester, submitted_at__gte=since).count() >= limit:
        fail(ErrorCode.VALIDATION_ERROR, "Daily limit of anonymous reports reached.")


def _coordinator():
    current = policy.current_policy()
    code = current.units.get("coordinator") if current else None
    division = Division.objects.filter(code=code, is_active=True).first() if code else None
    if division is None:
        fail(ErrorCode.DEPENDENCY_UNAVAILABLE, "No intake division or coordinator is configured.")
    return division


def _route(kind, lines, intake, now):
    """[(owner, lines)]: everything to the intake desk when there is one; otherwise (ADR 0008)
    one group per division the rules name for a withdrawal or return, unmatched lines together
    (owner None: the full proposal decides), and a problem whole."""
    current = policy.current_policy()
    if intake is not None or kind == Case.Kind.PROBLEM or not lines or current is None:
        return [(None, lines)]
    if not current.in_effect(now):
        return [(None, lines)]
    groups = {}
    for line in lines:
        found, _, code = policy.match(current, kind, line.item, now)
        groups.setdefault(code if found == "ok" else None, []).append(line)
    owners = {
        d.code: d
        for d in Division.objects.filter(code__in=[c for c in groups if c], is_active=True)
    }
    routed = {}
    for code, group in groups.items():
        routed.setdefault(owners.get(code), []).extend(group)
    return list(routed.items())


def _proposed_owner(case, coordinator, now):
    """The owner the rules propose for this case, or the coordinator when they cannot tell;
    such a case may go where คำที่ระบบจำ send it (ADR 0013)."""
    current = policy.current_policy()
    facts = policy.build_facts(policy.case_data(case), {}, {}, current, policy.active_catalog())
    result = policy.evaluate(current, facts, coordinator.code, now)
    proposed = Division.objects.filter(code=result["proposed_owner"], is_active=True).first()
    if proposed is None or proposed == coordinator:
        return routing_words.place(case, result, coordinator) or coordinator
    return proposed


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
        intake = identity.intake_division()
        coordinator = None if intake else _coordinator()
        requester = _submission_requester(ctx, submission, intake)
        if requester.is_anonymous:
            _check_anonymous(requester, submission)
        elif requester.is_internal or requester.is_external:
            # ผู้แจ้งภายใน (Q8): staff report problems and suggestions in their division's name.
            if submission.kind != Case.Kind.PROBLEM:
                fail(ErrorCode.FORBIDDEN, "Withdrawals and returns need a registered requester.")
        elif not requester.district.is_active:
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
        cases = []
        # เมนู LINE (15 ต.ค. 2569 Q20): the division chosen in the menu takes the whole request.
        chosen = (
            Division.objects.filter(code=submission.division_code, is_active=True).first()
            if submission.division_code
            else None
        )
        routes = [(chosen, lines)] if chosen and intake is None else None
        held = submission.hold_for_admin and intake is None
        if held:
            routes = [(coordinator, lines)]  # ADR 0030: an admin holds it, the rules wait
        routes = list(routes or _route(submission.kind, lines, intake, now))
        _check_sap(submission, fields, requester, routes, intake)
        _check_meters(submission, fields, intake)
        for owner, group in routes:
            case = Case.objects.create(
                number=_next_number(submission.kind, now),
                kind=submission.kind,
                requester=requester,
                district=requester.district,
                owner_division=intake or owner or coordinator,
                submitted_by=ctx.user,
                channel=submission.channel,
                intake_note=submission.intake_note,
                reported_at=submission.reported_at,
                related_case=related or (cases[0] if cases else None),
                menu_label=submission.menu_label,
                meters=list(submission.meters),
                intake_data=dict(submission.intake_data or {}),
                held_by_admin=held,
                submitted_at=now,
                updated_at=now,
                **fields,
            )
            for number, line in enumerate(group, start=1):
                line.case, line.line_no = case, number
            CaseLine.objects.bulk_create(group)
            if intake is None:
                # ADR 0008: straight to the division the rules name; unclear → coordinator.
                case.owner_division = owner or _proposed_owner(case, coordinator, now)
                case.section = units.section_for(case)  # ADR 0031 Q14
                case.status = approvals.first_status(case)  # ADR 0025
                case.save(update_fields=["owner_division", "section", "status"])
            cases.append(case)
        numbers = [c.number for c in cases]
        results = []
        for case in cases:
            data = {"channel": case.channel, "on_behalf": ctx.requester is None}
            if len(cases) > 1:
                data["split"] = numbers  # one request, one case per division
            acting_for = support.acting_for(ctx.scope, case, case.owner_division)
            if acting_for is not None:
                data["support"] = acting_for
            if case.reported_at:
                data["reported_at"] = case.reported_at.isoformat()
            results.append(
                _record(ctx, case, "submit", "", "", data, EVENT_CASE_SUBMITTED, None, None)
            )
            policy.after_submit(case)
            approvals.start_emergency(case)  # ADR 0025 Q24
            units.assign_new(case)  # ADR 0031 Q20: the ผู้รับผิดชอบประจำงาน takes it at once
        first = dataclasses.replace(
            results[0],
            split=[{"case_id": r.case_id, "case_number": r.case_number} for r in results[1:]],
        )
        _receipt(ctx, "submit", key, request_hash, cases[0], first)
        return first


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
    case.auto_assigned = False  # ADR 0031 Q24: a person chose; the system leaves it alone
    case.status = target
    return {"assignee_id": ctx.user.pk}


def route_by_ai(case_id, run):
    """ตัวเลือกระบบ routing_ai "direct" (ADR 0013): an unclear case that waits with ส่วนกลาง and
    that the routing AI placed by a registered duty goes to that division. The system records
    the move; no person acted. Anything that moved meanwhile leaves only the proposal."""
    with transaction.atomic():
        case = Case.objects.select_for_update().select_related("owner_division").get(pk=case_id)
        coordinator = routing_words.coordinator_division()
        waiting = (
            coordinator is not None
            and case.owner_division_id == coordinator.pk
            and case.status == Case.Status.SUBMITTED
            and run.division.is_active
            and not Handoff.objects.filter(case=case, status=Handoff.Status.PENDING).exists()
            and not RoutingReview.objects.filter(
                case=case, status=RoutingReview.Status.OPEN
            ).exists()
        )
        if not waiting:
            policy.refresh(case, "routing_ai")
            return None
        from_status = case.status
        data = {
            "owner_from": routing._move_owner(case, run.division),
            "owner_to": run.division.code,
            "entry": run.entry.title if run.entry else "",
            "quote": run.quote,
            "routing_run": run.pk,
        }
        case.section = units.section_for(case)
        case.revision += 1
        case.updated_at = timezone.now()
        case.save()
        type(run).objects.filter(pk=run.pk).update(applied=True)
        context = _Context(label="AI ส่งกองตามหน้าที่")
        _record(
            context, case, "route_by_ai", from_status, "", data, EVENT_DECISION_RECORDED, None, None
        )
        policy.after_decision(case, "route_by_ai", actor_is_staff=False)
        units.after_owner_change(case)
        return case


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
    case.auto_assigned = False  # ADR 0031 Q24
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
    elif case.kind == Case.Kind.WITHDRAWAL and fulfilment.approved(case):
        # M07 Q7: after approval only approval authority cancels, and says what happens to
        # goods already issued; outstanding approvals end with the case.
        data = fulfilment.cancel_after_approval(ctx, case, payload, reason)
        case.status, case.resume_status = target, ""
        return data
    else:
        _require_owner_staff(ctx, case)
        if case.kind == Case.Kind.RETURN:
            returns.cancel_guard(case)  # M08 Q8: goods that arrived must be inspected
    if payload.get("resolution") or payload.get("reference"):
        fail(ErrorCode.VALIDATION_ERROR, "Nothing was approved; no resolution is needed.")
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
    **routing.HANDLERS,
    **fulfilment.HANDLERS,
    **returns.HANDLERS,
    **coordination.HANDLERS,
    **problems.HANDLERS,
    # ADR 0025: recorded by the system only
    "start_work": approvals.system_only,
    "skip_screening": approvals.system_only,
}

# Shared lifecycle actions: each kind has its own rules (M07 Q6, M08 Q11/Q13, M09 Q1-Q4).
BY_KIND = {
    "confirm": {Case.Kind.WITHDRAWAL: fulfilment.confirm, Case.Kind.PROBLEM: problems.confirm},
    "close": {
        Case.Kind.WITHDRAWAL: fulfilment.close,
        Case.Kind.RETURN: returns.close,
        Case.Kind.PROBLEM: problems.close,
    },
    "reopen": {Case.Kind.PROBLEM: problems.reopen},
}


def _handler(action, case):
    if action in BY_KIND:
        return BY_KIND[action][case.kind]
    return HANDLERS[action]


@transaction.atomic
def perform_form_action(actor, case_id, action, *, expected_revision, idempotency_key, payload):
    """Commit the form's priority confirmation and triage decision together on every channel."""
    payload = dict(payload)
    with_level = action in ("complete_triage", "approve")  # ADR 0025 Q25: approve sets it too
    priority = payload.pop("priority", None) if with_level else None
    if priority:
        perform_action(
            actor,
            case_id,
            "set_priority",
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            payload={
                "priority": priority,
                **{k: v for k, v in payload.items() if k not in ("notice", "internal_note")},
            },
        )
        expected_revision += 1
    return perform_action(
        actor,
        case_id,
        action,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        payload=payload,
    )


def _with_notes(ctx, case, action, from_status, payload, reason, data):
    """ADR 0028: the status notice the requester reads and the internal note staff keep."""
    from operations import requester_steps  # local: requester_steps reads options

    notice = clean_text(payload, "notice", False) if "notice" in payload else ""
    internal = clean_text(payload, "internal_note", False) if "internal_note" in payload else ""
    if case.status == from_status:
        notice = ""  # a notice belongs to a status change
    else:
        notice = requester_steps.notice_of(action, {**payload, "notice": notice}, reason)
        staff = ctx.requester is None
        if (
            staff
            and not notice
            and requester_steps.notice_required(action, from_status, case.status)
        ):
            fail(ErrorCode.VALIDATION_ERROR, "เขียนข้อความแจ้งสถานะถึงผู้แจ้งก่อนบันทึก")
    if not (notice or internal) or (data is not None and not isinstance(data, dict)):
        return data
    return {
        **(data or {}),
        **({"notice": notice} if notice else {}),
        **({"internal_note": internal} if internal else {}),
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
        if not spec.enabled_for(case.kind):
            fail(ErrorCode.ACTION_NOT_ENABLED, f"{action} arrives with {spec.module}.")
        payload = parse_action_payload(action, payload)
        acting_for = support.acting_for(ctx.scope, case)
        # ADR 0007: every การทำแทนกอง by ผู้ช่วยเหลือระบบ carries a reason.
        reason = clean_reason(payload, spec.reason_required)
        if expected_revision != case.revision:
            fail(ErrorCode.REVISION_CONFLICT, "The case changed; reload and try again.")
        targets = spec.targets(case.kind, case.status)
        if not targets:
            fail(ErrorCode.INVALID_TRANSITION, f"{action} is not allowed while {case.status}.")
        # One target, or (M07) several the handler chooses from by the line counters; RESUME is
        # resolved by the handler.
        target = next(iter(targets)) if len(targets) == 1 else targets
        from_status, from_owner = case.status, case.owner_division_id
        data = _handler(action, case)(ctx, case, payload, reason, target)
        if case.owner_division_id != from_owner:
            case.section = units.section_for(case)  # Q12: the new owner's own แผนก, if any
        if acting_for is not None:
            data = {**(data or {}), "support": acting_for}
        data = _with_notes(ctx, case, action, from_status, payload, reason, data)
        if len(targets) > 1 and case.status not in targets:
            raise RuntimeError(f"{action} chose {case.status}, outside the matrix.")
        coordination.after_action(case)  # M09: R09 child work ends with its lines or the case
        routing.end_with_case(case, ctx.user)  # a waiting handoff ends with the case
        now = timezone.now()
        case.revision += 1
        case.updated_at = now
        if case.status in workflow.TERMINAL and from_status not in workflow.TERMINAL:
            case.ended_at = now
        case.save()
        result = _record(
            ctx, case, action, from_status, reason, data, EVENT_DECISION_RECORDED, key, request_hash
        )
        policy.after_decision(case, action, actor_is_staff=ctx.requester is None)
        # ADR 0031 Q22, Q24: a new owner picks its own person; a new แผนก moves a system pick.
        if case.owner_division_id != from_owner:
            units.after_owner_change(case)
        elif action == "set_section":
            units.after_section_change(case)
        return result
