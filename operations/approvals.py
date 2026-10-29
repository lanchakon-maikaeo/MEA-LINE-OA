"""เรื่องใหม่เข้ารออนุมัติของกองเจ้าภาพทันที (ADR 0025, grill กคว. 27 ต.ค. 2569 Q13, Q18, Q23–Q26).

With ตัวเลือกระบบ "screening" off (the default) a new case skips รอคัดกรอง: it waits for the
owner division's approval, unless it still waits with the coordinator because no division was
clear, or it is an emergency problem report, which starts at once. Whoever holds the approval
authority of the owner division approves any kind; a case without item rows is approved as a
whole, with the level in the same form. Withdrawals with rows keep the per-line approval.
"""

from django.db import transaction
from django.utils import timezone

from operations import identity, options, policy, routing_words
from operations.contracts import ErrorCode, fail
from operations.models import AuthorityGrant, Case

S = Case.Status
A = AuthorityGrant.Activity
EMERGENCY = "P0"


def no_screening():
    return options.get(options.SCREENING) == "off"


def first_status(case):
    """The status a new case starts in once its owner is known."""
    if not no_screening():
        return S.SUBMITTED
    coordinator = routing_words.coordinator_division()
    if coordinator is not None and case.owner_division_id == coordinator.pk:
        return S.SUBMITTED  # the coordinator still has to name the division
    return S.AWAITING_APPROVAL


def after_move(case):
    """A case the coordinator sent on now waits for the new owner's approval."""
    if no_screening() and case.status == S.SUBMITTED and first_status(case) != S.SUBMITTED:
        case.status = S.AWAITING_APPROVAL


def is_emergency(case):
    state = policy.state_for(case)
    return EMERGENCY in (state.priority_floor, state.suggested_priority, state.confirmed_priority)


def start_emergency(case):
    """Q24: an emergency problem report does not wait for approval. Returns True if moved."""
    from operations import services  # local: services calls this module

    if not (
        no_screening()
        and case.kind == Case.Kind.PROBLEM
        and case.status == S.AWAITING_APPROVAL
        and is_emergency(case)
    ):
        return False
    with transaction.atomic():
        case = Case.objects.select_for_update().get(pk=case.pk)
        if case.status != S.AWAITING_APPROVAL:
            return False
        case.status = S.IN_PROGRESS
        case.revision += 1
        case.updated_at = timezone.now()
        case.save(update_fields=["status", "revision", "updated_at"])
        context = services._Context(label="ระบบ (เรื่องฉุกเฉิน)")
        services._record(
            context,
            case,
            "start_work",
            S.AWAITING_APPROVAL,
            "เรื่องฉุกเฉินเริ่มงานทันทีโดยไม่รอกองรับเรื่อง",
            {},
            services.EVENT_DECISION_RECORDED,
            None,
            None,
        )
        policy.after_decision(case, "start_work", actor_is_staff=False)
    # The alert to the division (email, and LINE for its heads) already goes out for every new
    # emergency case (staff_email.plan_for_event), so nothing more is sent here.
    return True


def move_waiting():
    """Cases already waiting for screening when it was switched off go to approval (Q13);
    returns how many moved. Cases the coordinator still holds stay with it."""
    from operations import services  # local: services calls this module

    if not no_screening():
        return 0
    moved = 0
    for case in Case.objects.filter(status__in=[S.SUBMITTED, S.TRIAGE]).select_related(
        "owner_division"
    ):
        with transaction.atomic():
            case = Case.objects.select_for_update().get(pk=case.pk)
            if first_status(case) != S.AWAITING_APPROVAL or case.kind not in NEXT:
                continue
            from_status = case.status
            case.status = S.AWAITING_APPROVAL
            case.revision += 1
            case.updated_at = timezone.now()
            case.save(update_fields=["status", "revision", "updated_at"])
            services._record(
                services._Context(label="ระบบ (เลิกขั้นคัดกรอง)"),
                case,
                "skip_screening",
                from_status,
                "ADR 0025: เรื่องใหม่เข้ารออนุมัติของกองโดยไม่ต้องคัดกรอง",
                {},
                services.EVENT_DECISION_RECORDED,
                None,
                None,
            )
            policy.after_decision(case, "skip_screening", actor_is_staff=False)
            moved += 1
    return moved


def system_only(ctx, case, payload, reason, target):
    fail(ErrorCode.FORBIDDEN, "ระบบทำขั้นนี้เอง")


def may_approve(user, case):
    return policy.has_authority(user, A.WITHDRAWAL_APPROVAL, case.owner_division_id)


def whole(case):
    """Approved as one: any kind but a withdrawal that lists item rows, and every case while the
    stock work is hidden (ADR 0032: the division takes the case, SAP approves the slip)."""
    return case.kind != Case.Kind.WITHDRAWAL or not options.old_stock() or not case.lines.exists()


NEXT = {
    Case.Kind.WITHDRAWAL: S.AWAITING_EXECUTION,
    Case.Kind.RETURN: S.AWAITING_EXECUTION,
    Case.Kind.PROBLEM: S.IN_PROGRESS,
}


def approve_case(ctx, case, payload, reason, target):
    _check(ctx, case)
    case.status = NEXT[case.kind]
    return {"whole": True}


def deny_case(ctx, case, payload, reason, target):
    _check(ctx, case)
    if not reason:
        fail(ErrorCode.VALIDATION_ERROR, "บอกเหตุผลที่ไม่รับเรื่อง")
    case.status = S.REJECTED
    return {"whole": True}


def _check(ctx, case):
    if ctx.requester is not None or not may_approve(ctx.user, case):
        fail(ErrorCode.FORBIDDEN, "ไม่มีอำนาจรับเรื่องของกองนี้")
    if ctx.user is not None and identity.separated(ctx) and case.submitted_by_id == ctx.user.pk:
        fail(ErrorCode.FORBIDDEN, "ผู้บันทึกเรื่องแทนผู้แจ้งรับเรื่องนั้นเองไม่ได้")


def bulk_approve(actor, cases):
    """Q26: approve many at the level the system proposed; returns (approved, [(case, why)])."""
    from operations import services  # local: services calls this module
    from operations.contracts import Actor, CommandFailed

    done, skipped = [], []
    for case in cases:
        if case.status != S.AWAITING_APPROVAL or not whole(case):
            skipped.append((case, "รับเรื่องจากคิวได้เฉพาะเรื่องที่รอกองรับเรื่องทั้งเรื่อง"))
            continue
        payload = {}
        state = policy.state_for(case)
        if not state.confirmed_priority and state.suggested_priority:
            payload["priority"] = state.suggested_priority
        try:
            services.perform_form_action(
                Actor.staff(actor),
                case.pk,
                "approve",
                expected_revision=case.revision,
                idempotency_key=f"bulk-approve-{case.pk}-{case.revision}",
                payload=payload,
            )
        except CommandFailed as exc:
            skipped.append((case, exc.message or f"รับเรื่องไม่ได้ ({exc.code})"))
        else:
            done.append(case)
    return done, skipped
