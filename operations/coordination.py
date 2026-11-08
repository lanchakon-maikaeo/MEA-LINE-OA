"""M09 child work (งานย่อย, Q6/Q11/Q18, ADR 0004) and approved deadline pauses (Q12).

A child work gives part of a case to another division; the case keeps its owner. For R09
withdrawals and returns the child work names lines, and once accepted those lines are handled by
the child division: its issuers, receivers and inspectors act on them and division-scoped
authority is checked against it (`division_of`). Such a child work closes itself when its lines
are done; other child works are finished by the child division with a result.
"""

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from operations import identity
from operations.contracts import ErrorCode, clean_text, fail
from operations.models import (
    AuthorityGrant,
    Case,
    CaseLine,
    ChildWork,
    DeadlinePause,
    Delivery,
    Division,
    Role,
)

C = ChildWork.Status
S = Case.Status
K = Case.Kind
RULES = {"", "R07", "R08", "R09"}


# --- Who handles a line ------------------------------------------------------------------------


def child_of(line):
    """The accepted child work handling this line, if any."""
    if line is None:
        return None
    return ChildWork.objects.filter(lines=line, status=C.ACCEPTED).first()


def division_of(line):
    """The division that acts on this line: its accepted child work's, else the case owner."""
    child = child_of(line)
    return child.division_id if child else line.case.owner_division_id


def closes_itself(child):
    """R09 child work of a withdrawal or return ends when its lines are done (Q18)."""
    return child.case.kind in (K.WITHDRAWAL, K.RETURN) and child.lines.exists()


def _line_done(case, line):
    if case.kind == K.WITHDRAWAL:
        # Not decided yet is not done (outstanding is 0 before approval).
        if line.approved_quantity is None or line.outstanding > 0:
            return False
        deliveries = Delivery.objects.filter(lines__line=line).distinct()
        return all(d.settled for d in deliveries)
    from operations.models import ReturnUnit

    if line.return_outstanding > 0:
        return False
    units = ReturnUnit.objects.filter(line=line).exclude(status=ReturnUnit.Status.SENT_BACK)
    return all(u.status == ReturnUnit.Status.FINAL and not u.open_discrepancy for u in units)


def blocks_closing(case):
    """Open child work keeps the case open, except R09 child work whose lines are all done
    (it closes together with the case)."""
    for child in case.child_works.filter(status__in=ChildWork.OPEN).prefetch_related("lines"):
        if child.status == C.ACCEPTED and closes_itself(child):
            if all(_line_done(case, line) for line in child.lines.all()):
                continue
        return True
    return False


def after_action(case, actor=None):
    """Close R09 child work whose lines are done; a case that ended takes its open child work
    with it. Called after every accepted decision on the case."""
    now = timezone.now()
    for child in case.child_works.filter(status__in=ChildWork.OPEN).prefetch_related("lines"):
        if case.status in (S.CANCELLED, S.REJECTED):
            child.status, child.finished_at = C.CANCELLED, now
            child.result_summary = f"เรื่องหลัก{case.get_status_display()}"
            child.save(update_fields=["status", "finished_at", "result_summary"])
        elif child.status == C.ACCEPTED and closes_itself(child):
            if all(_line_done(case, line) for line in child.lines.all()):
                child.status, child.finished_at = C.DONE, now
                child.result_summary = "รายการของงานย่อยจบแล้ว"
                child.save(update_fields=["status", "finished_at", "result_summary"])


# --- Handlers ------------------------------------------------------------------------------------


def _member(ctx, division_id, roles=None):
    return identity.holds(ctx.scope, division_id, roles)


def _child(case, payload):
    seq = payload.get("child")
    if isinstance(seq, bool) or not isinstance(seq, int):
        fail(ErrorCode.VALIDATION_ERROR, "child (its number in this case) is required.")
    child = (
        ChildWork.objects.select_for_update()
        .select_related("division")
        .filter(case=case, seq=seq)
        .first()
    )
    if child is None:
        fail(ErrorCode.VALIDATION_ERROR, "Unknown child work for this case.")
    return child


def _division(code, case):
    division = Division.objects.filter(code=str(code or ""), is_active=True).first()
    if division is None:
        fail(ErrorCode.VALIDATION_ERROR, "Choose an active division.")
    if division.pk == case.owner_division_id:
        fail(ErrorCode.VALIDATION_ERROR, "งานย่อยมอบให้กองอื่น ไม่ใช่กองเจ้าภาพ")
    return division


def _settle_parent(case):
    """Finishing or dropping child work may let a withdrawal or return close (Q6)."""
    if case.kind == K.WITHDRAWAL and case.status in (S.IN_PROGRESS, S.AWAITING_CONFIRMATION):
        from operations import fulfilment

        case.status = fulfilment.settle(case, fulfilment._lines(case))
    elif case.kind == K.RETURN and case.status == S.IN_PROGRESS:
        from operations import fulfilment, returns

        case.status = returns.settle(case, fulfilment._lines(case))


def request_child_work(ctx, case, payload, reason, target):
    """งานย่อย: the owner division gives part of the work to another division (Q6)."""
    if ctx.requester is not None or not _member(ctx, case.owner_division_id):
        fail(ErrorCode.FORBIDDEN, "สมาชิกกองเจ้าภาพเป็นผู้สร้างงานย่อย")
    division = _division(payload.get("division"), case)
    task = clean_text(payload, "task", True)
    rule = payload.get("rule") or ""
    if rule not in RULES:
        fail(ErrorCode.VALIDATION_ERROR, "rule is R07, R08, R09 or empty.")
    numbers = payload.get("lines") or []
    if not isinstance(numbers, list):
        fail(ErrorCode.VALIDATION_ERROR, "lines lists line numbers.")
    lines = list(CaseLine.objects.filter(case=case, line_no__in=numbers))
    if len(lines) != len(set(numbers)):
        fail(ErrorCode.VALIDATION_ERROR, "Unknown line_no for this case.")
    if rule == "R09" and case.lines.exists() and not lines:
        fail(ErrorCode.VALIDATION_ERROR, "งานย่อย R09 ระบุรายการของที่ครอบ (Q11)")
    taken = ChildWork.objects.filter(case=case, status__in=ChildWork.OPEN, lines__in=lines).exists()
    if taken:
        fail(ErrorCode.VALIDATION_ERROR, "รายการนี้อยู่ในงานย่อยที่ยังเปิดอยู่แล้ว")
    if ChildWork.objects.filter(case=case, division=division, status__in=ChildWork.OPEN).exists():
        fail(ErrorCode.VALIDATION_ERROR, "กองนี้มีงานย่อยที่ยังเปิดอยู่ในเรื่องนี้แล้ว")
    # Asking a division that already refused this case is a dispute for the coordinator (Q6).
    refused = ChildWork.objects.filter(case=case, division=division, status=C.DECLINED).exists()
    from operations import units  # local: units reads the chart, not the coordination

    section, work_group = units.unit_for(case, division.pk)  # ADR 0052 Q17
    child = ChildWork.objects.create(
        case=case,
        seq=ChildWork.objects.filter(case=case).count() + 1,
        division=division,
        section=section,
        work_group=work_group,
        rule=rule,
        task=task,
        deliverable=clean_text(payload, "deliverable", False),
        status=C.DISPUTED if refused else C.PENDING,
        requested_by=ctx.user,
        requested_at=timezone.now(),
    )
    child.lines.set(lines)
    return {
        "child": child.seq,
        "division": division.code,
        "status": child.status,
        "lines": sorted(line.line_no for line in lines),
    }


def _answer(ctx, case, payload, reason, accept):
    child = _child(case, payload)
    if ctx.requester is not None or not _member(ctx, child.division_id):
        fail(ErrorCode.FORBIDDEN, "สมาชิกกองปลายทางเป็นผู้ตอบรับงานย่อย")
    if child.status != C.PENDING:
        fail(ErrorCode.VALIDATION_ERROR, "งานย่อยนี้ไม่ได้รอตอบรับ")
    child.status = C.ACCEPTED if accept else C.DECLINED
    child.answered_by, child.answered_at, child.answer_reason = ctx.user, timezone.now(), reason
    child.save()
    return child


def accept_child_work(ctx, case, payload, reason, target):
    child = _answer(ctx, case, payload, reason, True)
    return {"child": child.seq, "division": child.division.code}


def decline_child_work(ctx, case, payload, reason, target):
    """The owner decides what next; asking the same division again goes to the coordinator."""
    child = _answer(ctx, case, payload, reason, False)
    _settle_parent(case)
    return {"child": child.seq, "division": child.division.code}


def decide_child_work(ctx, case, payload, reason, target):
    """A coordinator with routing authority places disputed child work; it is accepted at once."""
    from operations import policy

    if ctx.requester is not None or not policy.has_authority(
        ctx.user, AuthorityGrant.Activity.ROUTING_DECISION, case.owner_division_id
    ):
        fail(ErrorCode.FORBIDDEN, "การตัดสินงานย่อยต้องมีอำนาจตัดสินเส้นทาง")
    child = _child(case, payload)
    if child.status != C.DISPUTED:
        fail(ErrorCode.VALIDATION_ERROR, "งานย่อยนี้ไม่ได้รอผู้ประสานตัดสิน")
    division = _division(payload.get("division"), case)
    other = (
        ChildWork.objects.filter(case=case, division=division, status__in=ChildWork.OPEN)
        .exclude(pk=child.pk)
        .exists()
    )
    if other:
        fail(ErrorCode.VALIDATION_ERROR, "กองนี้มีงานย่อยที่ยังเปิดอยู่ในเรื่องนี้แล้ว")
    child.division, child.status = division, C.ACCEPTED
    child.answered_by, child.answered_at, child.answer_reason = ctx.user, timezone.now(), reason
    child.save()
    return {"child": child.seq, "division": division.code}


def finish_child_work(ctx, case, payload, reason, target):
    child = _child(case, payload)
    if ctx.requester is not None or not _member(ctx, child.division_id):
        fail(ErrorCode.FORBIDDEN, "สมาชิกกองปลายทางเป็นผู้บันทึกผลงานย่อย")
    if child.status != C.ACCEPTED:
        fail(ErrorCode.VALIDATION_ERROR, "งานย่อยนี้ยังไม่ได้รับหรือจบแล้ว")
    if closes_itself(child):
        fail(ErrorCode.VALIDATION_ERROR, "งานย่อยนี้ปิดเองเมื่อรายการของตนจบ (Q18)")
    result = payload.get("result")
    if result not in (C.DONE, C.FAILED):
        fail(ErrorCode.VALIDATION_ERROR, "result is done or failed.")
    child.status = result
    child.result_summary = clean_text(payload, "summary", True)
    child.finished_by, child.finished_at = ctx.user, timezone.now()
    child.save()
    _settle_parent(case)
    return {"child": child.seq, "result": result}


def cancel_child_work(ctx, case, payload, reason, target):
    if ctx.requester is not None or not _member(ctx, case.owner_division_id):
        fail(ErrorCode.FORBIDDEN, "สมาชิกกองเจ้าภาพเป็นผู้ยกเลิกงานย่อย")
    child = _child(case, payload)
    if not child.is_open:
        fail(ErrorCode.VALIDATION_ERROR, "งานย่อยนี้จบแล้ว")
    child.status, child.finished_at, child.result_summary = C.CANCELLED, timezone.now(), reason
    child.save(update_fields=["status", "finished_at", "result_summary"])
    _settle_parent(case)
    return {"child": child.seq}


def pause_deadlines(ctx, case, payload, reason, target):
    """Q12: a hold stops work deadlines only when a supervisor of the owner division says so."""
    if ctx.requester is not None or not _member(ctx, case.owner_division_id, [Role.SUPERVISOR]):
        fail(ErrorCode.FORBIDDEN, "เฉพาะ แอดมินกองเจ้าภาพพักกรอบเวลาได้ (Q12)")
    if DeadlinePause.objects.filter(case=case, ended_at__isnull=True).exists():
        fail(ErrorCode.VALIDATION_ERROR, "กรอบเวลาพักอยู่แล้ว")
    pause = DeadlinePause.objects.create(
        case=case,
        kind=DeadlinePause.Kind.SUPERVISOR,
        reason=reason,
        approved_by=ctx.user,
        started_at=timezone.now(),
    )
    return {"pause_id": pause.pk}


HANDLERS = {
    "request_child_work": request_child_work,
    "accept_child_work": accept_child_work,
    "decline_child_work": decline_child_work,
    "decide_child_work": decide_child_work,
    "finish_child_work": finish_child_work,
    "cancel_child_work": cancel_child_work,
    "pause_deadlines": pause_deadlines,
}


# --- Triage rule and visibility -------------------------------------------------------------------


def r09_ready(case, recommendation):
    """Q18: an R09 case completes triage once every line (or, without lines, every proposed
    division) has child work sent (waiting or accepted)."""
    open_children = case.child_works.filter(
        status__in=[C.PENDING, C.DISPUTED, C.ACCEPTED]
    ).prefetch_related("lines")
    if case.lines.exists():
        covered = {line.pk for child in open_children for line in child.lines.all()}
        missing = [line.line_no for line in case.lines.all() if line.pk not in covered]
        return [f"รายการ {n}" for n in missing]
    wanted = set(recommendation.child_work_owners if recommendation else [])
    have = {child.division.code for child in open_children.select_related("division")}
    return sorted(wanted - have)


def child_condition(divisions):
    """Members of a child division see the parent case (read; they act on their child work)."""
    return Q(Exists(ChildWork.objects.filter(case=OuterRef("pk"), division_id__in=list(divisions))))
