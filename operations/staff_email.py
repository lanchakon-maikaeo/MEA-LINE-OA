"""การแจ้งเตือนเจ้าหน้าที่ (M10 round 1 Q10, round 2 Q5): email to FAOJ staff, never LINE.

Seven situations someone must act on and could miss: a handoff or child work reaching a
division, a route waiting for a coordinator, a dispute or reopening, a deadline passed, an
escalation, and a new P0 case. Each becomes one NotificationDelivery per recipient (email channel)
sent through the same outbox → n8n → /internal/v1/notifications/send path as LINE pushes, so a
replay never mails twice. Emails carry the case number, level, division, district and a link;
no requester name or text. Sending is off until STAFF_EMAIL_ENABLED is set.
"""

import uuid

from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from operations import orchestration, queues, services
from operations.contracts import Actor
from operations.models import (
    AuthorityGrant,
    Case,
    CaseTriage,
    ChildWork,
    Decision,
    Division,
    Handoff,
    NotificationDelivery,
    OutboxEvent,
    Role,
    RoleGrant,
)

N = NotificationDelivery
NAMESPACE = uuid.UUID("6f0f3c8e-4d6b-4a63-9b2a-7f1d2a4c9e10")

SUBJECTS = {
    "staff_handoff": "มีเรื่องส่งต่อมาถึงกอง",
    "staff_child_work": "มีงานย่อยใหม่ถึงกอง",
    "staff_routing": "มีเรื่องรอผู้ประสานตัดสิน",
    "staff_dispute": "ผู้แจ้งยังไม่ยอมรับผลการแก้ปัญหา",
    "staff_reopen": "ผู้แจ้งเปิดเรื่องใหม่",
    "staff_overdue": "เรื่องเกินกรอบเวลา",
    "staff_escalated": "เรื่องถูกยกระดับมาถึงคุณ",
    "staff_hazard": "เรื่องใหม่แจ้งเหตุอันตราย (ฉุกเฉิน)",
    "staff_registry": "พบส่วนต่างทะเบียนใหม่จากไฟล์ SAP",  # M11 Q19: one per import
    "staff_support": "ผู้ช่วยเหลือระบบทำแทนกองในเรื่องของกอง",  # ADR 0007
    "staff_admin_waiting": "มีเรื่องรอแอดมิน",  # ADR 0030: LINE only
}
CRITICAL = {"staff_hazard", "staff_escalated", "staff_routing"}


def enabled():
    return settings.STAFF_EMAIL_ENABLED


# --- Recipients --------------------------------------------------------------------------------


def _with_email(users):
    """Active people, once each; email planning skips those without an address, and staff
    LINE alerts (ADR 0010) reach those with a linked LINE."""
    seen, people = set(), []
    for user in users:
        if user.pk not in seen and user.is_active:
            seen.add(user.pk)
            people.append(user)
    return people


def members(division_id, roles=None):
    grants = RoleGrant.objects.filter(
        division_id=division_id, revoked_at__isnull=True, user__is_active=True
    ).select_related("user")
    if roles:
        grants = grants.filter(role__in=roles)
    return _with_email(g.user for g in grants.order_by("user_id"))


def routing_holders(case, now=None):
    now = now or timezone.now()
    grants = (
        AuthorityGrant.objects.filter(
            activity=AuthorityGrant.Activity.ROUTING_DECISION,
            revoked_at__isnull=True,
            valid_from__lte=now,
        )
        .exclude(valid_until__lte=now)
        .filter(Q(division__isnull=True) | Q(division_id=case.owner_division_id))
    )
    return _with_email(g.user for g in grants.select_related("user").order_by("user_id"))


def _assignee_or_owner(case):
    if case.assignee_id and case.assignee.is_active:
        return [case.assignee]
    return members(case.owner_division_id)


# --- Planning ----------------------------------------------------------------------------------


def _text(kind, case, detail=""):
    level = (
        CaseTriage.objects.filter(case=case).values_list("effective_priority", flat=True).first()
    )
    rows = [
        SUBJECTS[kind],
        f"เรื่อง {case.number} ({case.get_kind_display()}) · สถานะ {case.get_status_display()}",
        f"ระดับ{queues.level_name(level)} · กองที่ดูแล {case.owner_division.code}"
        f" · {case.district.name}",
    ]
    if detail:
        rows.append(detail)
    rows.append(f"เปิดดู: {settings.STAFF_WEB_BASE_URL.rstrip('/')}/cases/{case.number}/")
    rows.append("อีเมลนี้ส่งอัตโนมัติจาก DED Center (ข้อมูลกองและกติกาเป็นข้อมูลจำลอง)")
    return "\n".join(rows)


def plan(kind, key, case, users, detail="", text=None, chosen=False, division_id=None):
    """One email per recipient for one situation; the same key never plans twice. Nothing is
    planned while staff email is off, so the queue holds no mail that will never be sent.
    `case` is None only for situations about no single case (M11 registry summary). Staff
    with a linked LINE get the important ones there too (ADR 0010), whether email is on or not."""
    from operations import staff_line  # local: staff_line reads subjects from this module

    if case is not None and case.is_example:
        return []  # ตัวอย่าง (15 ต.ค. 2569 Q33): no email or LINE about it
    staff_line.plan(kind, key, case, users, chosen, division_id)
    if not enabled():
        return []
    now = timezone.now()
    text = text or _text(kind, case, detail)
    planned = []
    for user in users:
        if not user.email:
            continue
        try:
            with transaction.atomic():
                delivery = N.objects.create(
                    channel=N.Channel.EMAIL,
                    kind=kind,
                    level=N.Level.CRITICAL if kind in CRITICAL else N.Level.NORMAL,
                    email=user.email,
                    recipient_user=user,
                    case=case,
                    text=text,
                    source_event=uuid.uuid5(NAMESPACE, f"{key}:{user.pk}"),
                    send_after=now,
                )
        except IntegrityError:
            continue  # planned already (the worker retried the event)
        OutboxEvent.objects.create(
            event_type=orchestration.EVENT_NOTIFICATION,
            subject_type="delivery",
            subject_id=str(delivery.pk),
            case=case,
            case_revision=case.revision if case else None,
            correlation_id=case.pk if case else uuid.uuid5(NAMESPACE, key),
            payload={"delivery_id": delivery.pk, "kind": kind, "level": delivery.level},
            occurred_at=now,
            next_attempt_at=now,
        )
        planned.append(delivery)
    return planned


def plan_for_event(event):
    """Decisions and new cases from the outbox (deadline events come from the sweep)."""
    if event.event_type == orchestration.EVENT_CASE_SUBMITTED:
        case = Case.objects.filter(pk=event.subject_id).select_related("district").first()
        if case is not None and case.held_by_admin and not case.is_example:
            from operations import staff_line  # local: staff_line reads subjects from here

            # ADR 0030 Q21, Q25: LINE to the admins who keep this on (no email, no division).
            staff_line.plan(
                "staff_admin_waiting", f"admin:{case.pk}", case, staff_line.admin_receivers(), True
            )
        floor = (
            CaseTriage.objects.filter(case=case).values_list("priority_floor", flat=True).first()
        )
        if case is None or floor != "P0":
            return []
        # The desk that holds new cases: intake, or (ADR 0008) the owner division itself.
        desk = Division.objects.filter(is_intake=True, is_active=True).first()
        desk_id = desk.pk if desk else case.owner_division_id
        # ADR 0031 Q15: email to the members; LINE to the person responsible, else the unit's
        # ผู้รับแจ้งเรื่องสำคัญ (which took over the division's ผู้รับแจ้งเรื่องฉุกเฉิน, Q12).
        return plan(
            "staff_hazard", f"hazard:{case.pk}", case, members(desk_id), division_id=desk_id
        )
    if event.event_type != orchestration.EVENT_DECISION:
        return []
    decision = (
        Decision.objects.select_related("case__owner_division", "case__district", "case__assignee")
        .filter(action_id=event.payload.get("action_id"))
        .first()
    )
    if decision is None:
        return []
    return _plan_support(decision) + _plan_decision(decision)


def _plan_support(decision):
    """ADR 0007: the owner division's supervisors hear of each การทำแทนกอง."""
    acted = decision.data.get("support") if isinstance(decision.data, dict) else None
    if not acted:
        return []
    case = decision.case
    from operations.casepage import action_label

    detail = f"ขั้น: {action_label(decision.action)} · เหตุผล: {decision.reason}"
    return plan(
        "staff_support",
        f"support:{decision.action_id}",
        case,
        members(case.owner_division_id, [Role.SUPERVISOR]),
        detail,
    )


def _plan_decision(decision):
    case, key = decision.case, f"decision:{decision.action_id}"
    if decision.action == "request_handoff":
        handoff = Handoff.objects.filter(case=case, status=Handoff.Status.PENDING).first()
        if handoff:
            return plan(
                "staff_handoff",
                key,
                case,
                members(handoff.to_division_id),
                division_id=handoff.to_division_id,
            )
    if decision.action == "request_child_work":
        child = ChildWork.objects.filter(case=case).order_by("-seq").first()
        if child and child.status == ChildWork.Status.PENDING:
            return plan(
                "staff_child_work",
                key,
                case,
                members(child.division_id),
                f"งาน: {child.task}",
                division_id=child.division_id,
            )
    if decision.action in {"request_routing_decision", "decline_handoff", "decline_child_work"}:
        from operations.models import RoutingReview

        waiting = (
            RoutingReview.objects.filter(case=case, status=RoutingReview.Status.OPEN).exists()
            or ChildWork.objects.filter(case=case, status=ChildWork.Status.DISPUTED).exists()
        )
        if waiting:
            return plan("staff_routing", key, case, routing_holders(case))
    if case.held_by_admin and decision.action in ("dispute_resolution", "reopen"):
        from operations import staff_line  # local: staff_line reads subjects from here

        kind = "staff_dispute" if decision.action == "dispute_resolution" else "staff_reopen"
        staff_line.plan(kind, key, case, staff_line.admin_receivers(), True)  # ADR 0030
        return []
    if decision.action == "dispute_resolution":
        return plan("staff_dispute", key, case, _assignee_or_owner(case))
    if decision.action == "reopen":
        return plan("staff_reopen", key, case, _assignee_or_owner(case))
    return []


def plan_for_clock(clock, stage, target=""):
    """Sweep events: a deadline passed (stage 'overdue') or was escalated ('escalated')."""
    case = clock.case
    due = timezone.localtime(clock.due_at)
    detail = f"{clock.get_kind_display()} · กอง {clock.division.code} · ครบ {due:%d/%m %H:%M} น."
    if stage == "overdue":
        if clock.work and case.assignee_id:
            users = _assignee_or_owner(case)
        else:
            users = members(clock.division_id)
        return plan("staff_overdue", f"clock:{clock.pk}:overdue", case, users, detail)
    if target.startswith("supervisors:"):
        users = members(clock.division_id, [Role.SUPERVISOR])
    else:
        coordinator = Division.objects.filter(code=target, is_active=True).first()
        users = members(coordinator.pk) if coordinator else []
    return plan("staff_escalated", f"clock:{clock.pk}:escalated", case, users, detail)


# --- Sending -----------------------------------------------------------------------------------


def _problem(delivery):
    """Re-checked when sending: still on, still a working account that still sees the case."""
    if not enabled():
        return "email disabled"
    user = delivery.recipient_user
    if user is None or not user.is_active or not user.email:
        return "recipient inactive or without email"
    if user.email != delivery.email:
        return "email changed since planning"
    if delivery.kind == "staff_registry":
        from operations import registry

        if not registry.is_registry_staff(user):
            return "recipient is no longer registry staff"
    if delivery.work_order_id is not None:
        from operations import work_orders  # local: work_orders plans through this module

        return work_orders.recipient_problem(delivery)
    if delivery.case_id is not None:
        try:
            visible = services.visible_cases(Actor.staff(user)).filter(pk=delivery.case_id)
        except Exception:  # noqa: BLE001 - no role any more
            return "recipient has no access"
        if not visible.exists():
            return "recipient no longer sees the case"
    return ""


def send(delivery_id, now=None):
    from operations.notifications import SendResult

    now = now or timezone.now()
    with transaction.atomic():
        delivery = (
            N.objects.select_for_update(of=("self",))
            .select_related("recipient_user", "case", "work_order")
            .get(pk=delivery_id)
        )
        if delivery.status != N.Status.PLANNED:
            return SendResult("already")
        problem = _problem(delivery)
        if problem:
            delivery.status, delivery.detail = N.Status.SKIPPED, problem
            delivery.save(update_fields=["status", "detail"])
            return SendResult("skipped")
        delivery.attempts += 1
        delivery.save(update_fields=["attempts"])
        from operations.work_orders import SUBJECTS as ORDER_SUBJECTS

        title = SUBJECTS.get(delivery.kind) or ORDER_SUBJECTS.get(delivery.kind, "แจ้งเตือน")
        subject = f"[ฝอจ.] {title}"
        if delivery.case_id:
            subject += f" {delivery.case.number}"
        elif delivery.work_order_id:
            subject += f" {delivery.work_order.number}"
        to, body = delivery.email, delivery.text
    try:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [to])
    except OSError as exc:
        N.objects.filter(pk=delivery.pk).update(detail=f"email {type(exc).__name__}"[:200])
        return SendResult("retry", final=False)
    N.objects.filter(pk=delivery.pk).update(status=N.Status.SENT, sent_at=now, detail="email")
    return SendResult("sent")
