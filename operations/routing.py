"""M06 decisions on a case: handoff, routing decisions, priority and facts (B.3, B.5, B.6).

Handlers run inside services.perform_action, which locks the case, checks the matrix and the
revision, then records the Decision, audit, outbox event and receipt. Recommendations never act
by themselves: each change here is a person's decision with the authority the backend checks.
"""

from django.core.exceptions import ValidationError
from django.utils import timezone

from operations import ai_routing, coordination, identity, policy, routing_words, workflow
from operations.contracts import ErrorCode, fail
from operations.models import (
    AckClock,
    AuthorityGrant,
    Decision,
    Division,
    EquipmentType,
    Handoff,
    RoutingReview,
    Section,
)

A = AuthorityGrant.Activity
BOOLEAN_FACTS = ("hazard_reported", "registry_only", "serial_mismatch", "shortage")
STAFF_FACTS = (*BOOLEAN_FACTS, "severe_impact", "equipment")


def _member(ctx, division_id):
    """Holds any role in the division (or is ผู้ช่วยเหลือระบบ); admin rights alone do not."""
    return identity.holds(ctx.scope, division_id)


def _require_member(ctx, division_id, message):
    if ctx.requester is not None or not _member(ctx, division_id):
        fail(ErrorCode.FORBIDDEN, message)


def _owner_staff_or_admin(ctx, case):
    if ctx.requester is not None or not (
        _member(ctx, case.owner_division_id) or (ctx.scope and ctx.scope.is_admin)
    ):
        fail(ErrorCode.FORBIDDEN, "Only staff of the owner division can do this.")


def _pending(case):
    return (
        Handoff.objects.select_for_update().filter(case=case, status=Handoff.Status.PENDING).first()
    )


def _open_review(case):
    return (
        RoutingReview.objects.select_for_update()
        .filter(case=case, status=RoutingReview.Status.OPEN)
        .first()
    )


def _division(code):
    division = Division.objects.filter(code=str(code or ""), is_active=True).first()
    if division is None:
        fail(ErrorCode.VALIDATION_ERROR, "Choose an active division.")
    return division


def previous_owners(case):
    """Divisions the case already belonged to (a handoff back to one of them is a loop)."""
    owners = set()
    for data in Decision.objects.filter(case=case).values_list("data", flat=True):
        if isinstance(data, dict) and data.get("owner_from"):
            owners.add(data["owner_from"])
    return owners


def _move_owner(case, division):
    previous = case.owner_division.code
    case.owner_division = division
    case.assignee = None  # M02: a new owner division starts without a responsible person
    return previous


def end_with_case(case, actor=None):
    """A case that ended (cancelled, rejected, closed) takes its waiting handoff with it, so the
    target division is no longer asked to accept work that is over."""
    if case.status not in workflow.TERMINAL:
        return
    Handoff.objects.filter(case=case, status=Handoff.Status.PENDING).update(
        status=Handoff.Status.ENDED,
        decided_by=actor,
        decided_at=timezone.now(),
        decision_reason=f"เรื่อง{case.get_status_display()}ก่อนตอบรับ",
    )


def request_handoff(ctx, case, payload, reason, target):
    _owner_staff_or_admin(ctx, case)
    to = _division(payload.get("to_division"))
    if to.pk == case.owner_division_id:
        fail(ErrorCode.VALIDATION_ERROR, "The case already belongs to this division.")
    if _pending(case) is not None:
        fail(ErrorCode.VALIDATION_ERROR, "A handoff is already waiting for an answer.")
    if _open_review(case) is not None:
        fail(ErrorCode.VALIDATION_ERROR, "The route is waiting for a coordinator's decision.")
    state = policy.state_for(case)
    recommendation = state.current_recommendation
    keywords = _keywords(case, payload, recommendation)
    category = str(payload.get("reason_category") or "")
    if recommendation is None or recommendation.proposed_owner_id != to.pk:
        # Choosing differently from the proposal is allowed, with the reason recorded (B.6).
        allowed = (
            policy.current_policy().doc.get("handoff_reasons", {})
            if policy.current_policy()
            else {}
        )
        if category not in allowed:
            fail(
                ErrorCode.VALIDATION_ERROR,
                "เลือกประเภทการส่งต่อ",
            )
    now = timezone.now()
    declined_before = Handoff.objects.filter(
        case=case, to_division=to, status=Handoff.Status.DECLINED
    ).exists()
    if declined_before or to.code in previous_owners(case):
        # A loop or a repeated request after a refusal is a routing dispute (Q13/Q23).
        review = RoutingReview.objects.create(
            case=case,
            trigger=RoutingReview.Trigger.DISPUTE,
            attempted_division=to,
            opened_by=ctx.user,
            opened_at=now,
            reason=reason,
        )
        return {"outcome": "routing_review", "review_id": review.pk, "to_division": to.code}
    handoff = Handoff.objects.create(
        case=case,
        from_division=case.owner_division,
        to_division=to,
        reason_category=category,
        reason=reason,
        recommendation=recommendation,
        requested_by=ctx.user,
        requested_at=now,
        keywords=keywords,
    )
    return {
        "outcome": "handoff_requested",
        "handoff_id": handoff.pk,
        "to_division": to.code,
        "followed_proposal": bool(recommendation and recommendation.proposed_owner_id == to.pk),
        "recommendation_id": recommendation.pk if recommendation else None,
        **({"keywords": keywords} if keywords else {}),
    }


def _keywords(case, payload, recommendation):
    """คำสำคัญให้ระบบจำ (ADR 0013, Q2, Q8): only when ส่วนกลาง hands on a case the rules could
    not place, and only words from the case's own text."""
    wanted = payload.get("keywords")
    if not wanted:
        return []
    if not routing_words.may_teach(case, recommendation):
        fail(
            ErrorCode.VALIDATION_ERROR,
            "คำสำคัญใช้ได้เฉพาะเรื่องที่ ฝอจ. ส่งต่อ เมื่อกติกาบอกกองไม่ได้",
        )
    try:
        return routing_words.clean(case, wanted)
    except ValidationError as exc:
        fail(ErrorCode.VALIDATION_ERROR, " ".join(exc.messages))


def _answer(ctx, case, accept, reason):
    handoff = _pending(case)
    if handoff is None:
        fail(ErrorCode.VALIDATION_ERROR, "No handoff is waiting for an answer.")
    _require_member(ctx, handoff.to_division_id, "Only staff of the receiving division can answer.")
    handoff.status = Handoff.Status.ACCEPTED if accept else Handoff.Status.DECLINED
    handoff.decided_by, handoff.decided_at, handoff.decision_reason = (
        ctx.user,
        timezone.now(),
        reason,
    )
    handoff.save(update_fields=["status", "decided_by", "decided_at", "decision_reason"])
    data = {"handoff_id": handoff.pk, "to_division": handoff.to_division.code}
    if accept:
        data["owner_from"] = _move_owner(case, handoff.to_division)
        data["owner_to"] = handoff.to_division.code
        routing_words.after_handoff(case, handoff, ctx.user)  # learn or move (ADR 0013)
    return data


def accept_handoff(ctx, case, payload, reason, target):
    return _answer(ctx, case, True, reason)


def decline_handoff(ctx, case, payload, reason, target):
    # The owner stays responsible and the case keeps its age (B.3).
    return _answer(ctx, case, False, reason)


def request_routing_decision(ctx, case, payload, reason, target):
    handoff = _pending(case)
    allowed = _member(ctx, case.owner_division_id) or (ctx.scope and ctx.scope.is_admin)
    if handoff is not None and _member(ctx, handoff.to_division_id):
        allowed = True
    if ctx.requester is not None or not allowed:
        fail(ErrorCode.FORBIDDEN, "Only the owner or the receiving division can ask.")
    if _open_review(case) is not None:
        fail(ErrorCode.VALIDATION_ERROR, "A coordinator's decision is already requested.")
    data = {}
    if handoff is not None:
        handoff.status, handoff.decided_by, handoff.decided_at = (
            Handoff.Status.WITHDRAWN,
            ctx.user,
            timezone.now(),
        )
        handoff.save(update_fields=["status", "decided_by", "decided_at"])
        data["withdrawn_handoff_id"] = handoff.pk
    review = RoutingReview.objects.create(
        case=case,
        trigger=RoutingReview.Trigger.REQUESTED,
        opened_by=ctx.user,
        opened_at=timezone.now(),
        reason=reason,
    )
    return {**data, "review_id": review.pk}


def decide_routing(ctx, case, payload, reason, target):
    """A coordinator with authority picks the owner; it applies at once (Q13)."""
    if ctx.requester is not None or not policy.has_authority(
        ctx.user, A.ROUTING_DECISION, case.owner_division_id
    ):
        fail(ErrorCode.FORBIDDEN, "Routing decisions need an active routing authority.")
    review = _open_review(case)
    # Only unanswered intake/handoff clocks reach the coordinator; work deadlines go to the
    # division's supervisors (M09 Q9).
    escalated = AckClock.objects.filter(
        case=case,
        stopped_at__isnull=True,
        escalated_at__isnull=False,
        kind__in=[AckClock.Kind.INTAKE, AckClock.Kind.HANDOFF],
    ).exists()
    if review is None and not escalated:
        fail(ErrorCode.VALIDATION_ERROR, "Nothing is waiting for a coordinator on this case.")
    division = _division(payload.get("division"))
    recommendation = policy.state_for(case).current_recommendation
    keywords = _keywords(case, payload, recommendation)
    now = timezone.now()
    data = {"division": division.code}
    handoff = _pending(case)
    if handoff is not None:
        handoff.status, handoff.decided_by, handoff.decided_at = (
            Handoff.Status.WITHDRAWN,
            ctx.user,
            now,
        )
        handoff.save(update_fields=["status", "decided_by", "decided_at"])
        data["withdrawn_handoff_id"] = handoff.pk
    if review is None:
        review = RoutingReview.objects.create(
            case=case,
            trigger=RoutingReview.Trigger.REQUESTED,
            opened_by=ctx.user,
            opened_at=now,
            reason="เรื่องไม่มีผู้ตอบรับเกินกำหนดและถูกยกระดับ",
        )
    review.status, review.decided_by, review.decided_at = (
        RoutingReview.Status.DECIDED,
        ctx.user,
        now,
    )
    review.chosen_division, review.decision_reason = division, reason
    review.save()
    data["review_id"] = review.pk
    if division.pk != case.owner_division_id:
        previous = case.owner_division
        data["owner_from"] = _move_owner(case, division)
        data["owner_to"] = division.code
        routing_words.after_decision(case, recommendation, previous, division, ctx.user, keywords)
        if keywords:
            data["keywords"] = keywords
    return data


def set_priority(ctx, case, payload, reason, target):
    """Confirm or change the level; below the rule floor needs an authority exception (Q14)."""
    exception_holder = ctx.requester is None and policy.has_authority(
        ctx.user, A.PRIORITY_EXCEPTION, case.owner_division_id
    )
    if ctx.requester is not None or not (_member(ctx, case.owner_division_id) or exception_holder):
        fail(ErrorCode.FORBIDDEN, "Only staff of the owner division can set the priority.")
    value = payload.get("priority")
    if value not in policy.PRIORITIES:
        fail(ErrorCode.VALIDATION_ERROR, "Priority must be P0-P3.")
    state = policy.state_for(case)
    previous = state.confirmed_priority
    data = {"priority": value, "previous": previous, "suggested": state.suggested_priority}
    floor = state.priority_floor
    if floor and policy.rank(value) > policy.rank(floor):
        if not exception_holder:
            fail(
                ErrorCode.FORBIDDEN,
                f"{value} is below the rule floor {floor}; an authority exception is required.",
            )
        basis = state.current_recommendation.floor_basis if state.current_recommendation else []
        state.floor_exception = {
            "floor": floor,
            "basis": basis,
            "by": ctx.user.pk,
            "at": timezone.now().isoformat(),
            "reason": reason,
        }
        data["exception"] = {"floor": floor, "basis": basis}
    state.confirmed_priority = value
    state.confirmed_division_id = case.owner_division_id
    state.save(
        update_fields=["confirmed_priority", "confirmed_division", "floor_exception", "updated_at"]
    )
    return data


def _clean_equipment(value):
    if not isinstance(value, list) or len(value) > 20:
        fail(ErrorCode.VALIDATION_ERROR, "equipment is a list of {category, item}.")
    rows = []
    for row in value:
        if not isinstance(row, dict) or set(row) - {"category", "item"}:
            fail(ErrorCode.VALIDATION_ERROR, "equipment rows are {category, item}.")
        item = row.get("item") or None
        category = row.get("category")
        if item is not None:
            found = EquipmentType.objects.filter(code=item, is_active=True).first()
            if found is None:
                fail(ErrorCode.VALIDATION_ERROR, f"Unknown item {item}.")
            category = category or found.category
            if found.category != category:
                fail(ErrorCode.VALIDATION_ERROR, f"{item} is not in {category}.")
        if category not in policy.TOPICS or (item and category == policy.USAGE):
            fail(ErrorCode.VALIDATION_ERROR, "Unknown category.")
        rows.append({"category": category, "item": item})
    return rows


def record_facts(ctx, case, payload, reason, target):
    """การแก้ข้อเท็จจริง: what the text really says, with a reason; not an exception (Q14)."""
    _require_member(ctx, case.owner_division_id, "Only staff of the owner division can do this.")
    facts = payload.get("facts") or {}
    clear = payload.get("clear") or []
    if not isinstance(facts, dict) or not isinstance(clear, list) or not (facts or clear):
        fail(ErrorCode.VALIDATION_ERROR, "Give facts to confirm or names to clear.")
    unknown = (set(facts) | set(clear)) - set(STAFF_FACTS)
    if unknown:
        fail(ErrorCode.VALIDATION_ERROR, f"Unknown facts: {sorted(unknown)}")
    cleaned = {}
    for name, value in facts.items():
        if name in BOOLEAN_FACTS and not isinstance(value, bool):
            fail(ErrorCode.VALIDATION_ERROR, f"{name} is true or false.")
        if name == "severe_impact" and value not in (True, False, None):
            fail(ErrorCode.VALIDATION_ERROR, "severe_impact is true, false or null (not stated).")
        cleaned[name] = _clean_equipment(value) if name == "equipment" else value
    state = policy.state_for(case)
    stored = dict(state.staff_facts or {})
    now = timezone.now().isoformat()
    for name in clear:
        stored.pop(name, None)
    for name, value in cleaned.items():
        stored[name] = {"value": value, "by": ctx.user.pk, "at": now, "reason": reason}
    state.staff_facts = stored
    state.save(update_fields=["staff_facts", "updated_at"])
    return {"facts": cleaned, "cleared": sorted(clear)}


def complete_triage(ctx, case, payload, reason, target):
    """Q15: the owner division (not intake, except R09), nothing waiting, a confirmed level."""
    _require_member(ctx, case.owner_division_id, "Only staff of the owner division can do this.")
    if _pending(case) is not None or _open_review(case) is not None:
        fail(ErrorCode.VALIDATION_ERROR, "A handoff or routing decision is still open.")
    state = policy.state_for(case)
    rules = state.current_recommendation.rules if state.current_recommendation else []
    if case.owner_division.is_intake and "R09" not in rules:
        fail(ErrorCode.VALIDATION_ERROR, "Hand the case to the responsible division first.")
    if "R09" in rules:
        missing = coordination.r09_ready(case, state.current_recommendation)
        if missing:
            fail(
                ErrorCode.VALIDATION_ERROR,
                f"เรื่องหลายกอง (R09) ต้องส่งงานย่อยครบก่อนจบคัดกรอง: {', '.join(missing)} (M09 Q18)",
            )
    if not state.confirmed_priority or state.confirmed_division_id != case.owner_division_id:
        fail(ErrorCode.VALIDATION_ERROR, "The owner division confirms the priority first.")
    floor = state.priority_floor
    if (
        floor
        and policy.rank(state.confirmed_priority) > policy.rank(floor)
        and policy.effective_priority(state) != state.confirmed_priority
    ):
        fail(ErrorCode.VALIDATION_ERROR, f"Confirm a level of at least {floor} first.")
    case.status = target
    ai_routing.after_triage(case, ctx.user)  # ADR 0013 Q31: kept, so the keyword is learned
    return {"priority": state.confirmed_priority, "rules": rules}


def set_section(ctx, case, payload, reason, target):
    """Q12: the owner division says which of its แผนก looks after the case (or none)."""
    if ctx.requester is not None or not _member(ctx, case.owner_division_id):
        fail(ErrorCode.FORBIDDEN, "Only staff of the owner division can set the section.")
    value = payload.get("section")
    section = None
    if value not in (None, ""):
        try:
            section = Section.objects.get(
                pk=int(value), division_id=case.owner_division_id, is_active=True
            )
        except (TypeError, ValueError, Section.DoesNotExist):
            fail(ErrorCode.VALIDATION_ERROR, "Choose a section of the owner division.")
    previous = case.section
    if previous == section:
        fail(ErrorCode.VALIDATION_ERROR, "The case is already with this section.")
    case.section = section
    case.status = target
    return {
        "section": section.name if section else "",
        "previous": previous.name if previous else "",
    }


def set_sap_number(ctx, case, payload, reason, target):
    """23 Oct 2569: staff of the owner division correct the SAP slip number the requester typed;
    the event keeps the old and the new number."""
    from operations import typed_lines

    if ctx.requester is not None or not _member(ctx, case.owner_division_id):
        fail(ErrorCode.FORBIDDEN, "เฉพาะเจ้าหน้าที่ของกองเจ้าภาพแก้เลข SAP ได้")
    number, problem = typed_lines.clean_sap(payload.get("sap_number"))
    if problem:
        fail(ErrorCode.VALIDATION_ERROR, problem)
    if number == case.sap_number:
        fail(ErrorCode.VALIDATION_ERROR, "เลข SAP นี้เป็นเลขเดิมของเรื่องอยู่แล้ว")
    problem = typed_lines.sap_in_use(number, None, exclude=case)
    if problem:
        fail(ErrorCode.VALIDATION_ERROR, problem)
    previous = case.sap_number
    case.sap_number = number
    case.status = target
    return {"sap_number": number, "previous": previous}


HANDLERS = {
    "set_section": set_section,
    "set_sap_number": set_sap_number,
    "complete_triage": complete_triage,
    "request_handoff": request_handoff,
    "accept_handoff": accept_handoff,
    "decline_handoff": decline_handoff,
    "request_routing_decision": request_routing_decision,
    "decide_routing": decide_routing,
    "set_priority": set_priority,
    "record_facts": record_facts,
}
