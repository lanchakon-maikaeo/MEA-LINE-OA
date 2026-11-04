"""งานสั่ง (16 Oct 2569 Q6, Q7, Q14): work a Supervisor gives a division or one of its members.

Only a Supervisor issues one: the แอดมินฝ่าย for any division, a แอดมินกอง for
their own. มอบหมาย → กำลังดำเนินการ → เสร็จสิ้น → ปิดงาน (ADR 0044: ดำเนินการ takes the place of
รับทราบ and เริ่มทำ); the person or division doing it may send it back
with a reason, and a Supervisor closes or cancels it. The doer is told when it is assigned, the
issuer when it is acknowledged, done or sent back, and the doer once near the due date and once
after it (with the issuer). Every step is audited; nothing here counts in the district reports.
"""

import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from operations import identity, numbering, options, orchestration
from operations.models import (
    VIEWER_ROLES,
    AuditEvent,
    ChannelIdentity,
    Division,
    NotificationDelivery,
    OutboxEvent,
    Role,
    RoleGrant,
    WorkOrder,
)

N = NotificationDelivery
S = WorkOrder.Status
NAMESPACE = uuid.UUID("0c5e2f7a-91d3-4b8e-a6f2-3d7c1b9e4a55")
TITLE_MAX = 200

# What each notice says (also the e-mail subject).
SUBJECTS = {
    "staff_order_new": "มีงานสั่งถึงคุณ",
    "staff_order_ack": "งานที่คุณสั่งเริ่มดำเนินการแล้ว",
    "staff_order_done": "งานที่คุณสั่งเสร็จสิ้นแล้ว",
    "staff_order_back": "งานที่คุณสั่งถูกส่งกลับ",
    "staff_order_soon": "งานสั่งใกล้ถึงกำหนด",
    "staff_order_late": "งานสั่งเกินกำหนดแล้ว",
}

# Steps: label, the statuses it starts from, who takes it ("doer", "supervisor" or
# "department"), and what
# it needs typed. The web page and (later) LINE offer exactly these.
STEPS = {
    # ADR 0044: one press starts the work and settles ด่วน or ปกติ (รับทราบ is no step of its own).
    "start": ("ดำเนินการ", (S.ASSIGNED, S.ACKNOWLEDGED), "doer", "urgent"),
    "done": ("เสร็จสิ้น", WorkOrder.OPEN, "doer", "result"),
    "send_back": ("ส่งกลับผู้สั่ง", WorkOrder.OPEN, "doer", "reason"),
    "assign_person": ("มอบหมายผู้รับผิดชอบ", WorkOrder.OPEN, "supervisor", "person"),
    "reassign": ("สั่งใหม่", (S.SENT_BACK,), "supervisor", "target"),
    # ADR 0040 Q2, Q7: the department moves open work to another division at once, unassigned.
    "forward": ("ส่งต่อให้กอง", WorkOrder.OPEN, "department", "division"),
    "close": ("ปิดงาน", (S.DONE,), "supervisor", ""),
    "cancel": ("ยกเลิกงาน", (*WorkOrder.OPEN, S.SENT_BACK), "supervisor", "reason"),
}


# --- Who --------------------------------------------------------------------------------------


def may_issue(user):
    """แอดมินฝ่าย, and แอดมินระบบ who does all a แอดมินฝ่าย does (ADR 0041)."""
    return identity.staff_scope(user).is_department_supervisor


def issuable(user):
    """Divisions supervised by this person, also used for assigning existing cases."""
    scope = identity.staff_scope(user)
    return Division.objects.filter(pk__in=scope.overseen, is_active=True).order_by("kind", "code")


def visible(user):
    """Orders of the divisions a person works in or supervises, and those they issued or got;
    admins see all."""
    scope = identity.staff_scope(user)
    orders = WorkOrder.objects.all()
    if scope.is_admin or scope.is_support:
        return orders
    if not scope.may_read:
        return orders.none()
    divisions = [*scope.division_roles, *scope.overseen, *scope.viewed]  # ADR 0039
    return orders.filter(Q(division__in=divisions) | Q(issued_by=user) | Q(assignee=user))


def is_doer(user, order, scope=None):
    scope = scope or identity.staff_scope(user)
    if order.assignee_id:
        return order.assignee_id == user.pk
    return identity.holds(scope, order.division_id)


def is_supervisor(user, order, scope=None):
    scope = scope or identity.staff_scope(user)
    return order.issued_by_id == user.pk or order.division_id in scope.overseen


def steps_for(user, order):
    """The steps this person may take on this order now, in page order."""
    scope = identity.staff_scope(user)
    allowed = []
    for name, (label, starts, who, _) in STEPS.items():
        if order.status not in starts:
            continue
        accepts_for_division = name == "start" and order.division_id in scope.overseen
        if who == "doer" and not (is_doer(user, order, scope) or accepts_for_division):
            continue
        if who == "supervisor" and not is_supervisor(user, order, scope):
            continue
        if who == "department" and not scope.is_department_supervisor:
            continue
        allowed.append((name, label))
    return allowed


def members(division_id):
    """Active people of a division, once each, by name."""
    grants = (
        RoleGrant.objects.filter(
            division_id=division_id, revoked_at__isnull=True, user__is_active=True
        )
        .exclude(role__in=VIEWER_ROLES)  # ADR 0039: nobody gives work to a viewer
        .select_related("user__staff_profile")
        .order_by("user__username")
    )
    seen, people = set(), []
    for grant in grants:
        if grant.user_id not in seen:
            seen.add(grant.user_id)
            people.append(grant.user)
    return people


def recipients(division_id):
    """Who an order to this กอง may name (ADR 0041): its people, then the แอดมินฝ่าย and
    แอดมินระบบ, who give each other work too."""
    people = members(division_id)
    seen = {u.pk for u in people}
    for grant in (
        RoleGrant.objects.filter(
            role__in=[Role.DEPARTMENT_SUPERVISOR, Role.ADMIN],
            revoked_at__isnull=True,
            user__is_active=True,
        )
        .select_related("user__staff_profile")
        .order_by("user__username")
    ):
        if grant.user_id not in seen:
            seen.add(grant.user_id)
            people.append(grant.user)
    return people


def name_of(user):
    profile = getattr(user, "staff_profile", None)
    return profile.display_name if profile else user.username


def _check_target(division, assignee):
    if division is None or not division.is_active:
        raise ValidationError("เลือกกองที่ใช้งานอยู่")
    if assignee is not None and assignee.pk not in {u.pk for u in recipients(division.pk)}:
        raise ValidationError("ผู้รับงานที่เลือกต้องเป็นสมาชิกของกองนั้น แอดมินฝ่าย หรือแอดมินระบบ")


def _require_issuer(actor, division):
    if division.pk not in identity.staff_scope(actor).overseen:
        raise PermissionDenied("เฉพาะ แอดมินกองนี้หรือ แอดมินฝ่ายสั่งงานได้")


# --- Issuing and steps ------------------------------------------------------------------------


@transaction.atomic
def create(
    actor,
    title,
    division,
    assignee=None,
    detail="",
    due_on=None,
    urgent=False,
    channel=WorkOrder.Channel.WEB,
):
    """A new งานสั่ง; the doer is told at once (Q14)."""
    if not may_issue(actor):
        raise PermissionDenied("การสั่งงานสงวนสิทธิ์สำหรับแอดมินฝ่ายและแอดมินระบบ")
    title = " ".join((title or "").split())
    if not title:
        raise ValidationError("บอกว่าให้ทำอะไร")
    if len(title) > TITLE_MAX:
        raise ValidationError(f"ชื่องานยาวได้ไม่เกิน {TITLE_MAX} ตัวอักษร")
    if channel not in WorkOrder.Channel.values:
        raise ValidationError("ช่องทางสั่งงานไม่ถูกต้อง")
    _check_target(division, assignee)
    _require_issuer(actor, division)
    now = timezone.now()
    if due_on is not None and due_on < timezone.localdate(now):
        raise ValidationError("วันกำหนดต้องไม่ใช่วันที่ผ่านมาแล้ว")
    order = WorkOrder.objects.create(
        number=numbering.issue(division, now),  # ADR 0045: the cases' sequence too
        title=title,
        detail=(detail or "").strip(),
        division=division,
        assignee=assignee,
        issued_by=actor,
        channel=channel,
        due_on=due_on,
        is_urgent=bool(urgent),
    )
    identity.audit(
        actor,
        "order.created",
        order,
        number=order.number,
        division=division.code,
        assignee=assignee.pk if assignee else None,
        due_on=due_on.isoformat() if due_on else None,
        urgent=order.is_urgent,
        channel=order.channel,
    )
    _notify("staff_order_new", order, doers(order), f"new:{order.pk}")
    return order


@transaction.atomic
def act(
    actor, order, step, reason="", result="", assignee=None, division=None, due_on=None, urgent=None
):
    """Take one step; the order is locked, and the step must still be allowed. ดำเนินการ may
    change ด่วน or ปกติ (urgent True or False; None keeps it)."""
    if step not in STEPS:
        raise ValidationError("ไม่รู้จักขั้นนี้")
    order = WorkOrder.objects.select_for_update().get(pk=order.pk)
    if step not in dict(steps_for(actor, order)):
        raise PermissionDenied("ทำขั้นนี้ไม่ได้ในตอนนี้")
    reason, result = (reason or "").strip(), (result or "").strip()
    now = timezone.now()
    data = {}
    notice = None
    if step == "start":
        first = order.acknowledged_at is None
        if first:
            order.acknowledged_by, order.acknowledged_at = actor, now
        order.status = S.IN_PROGRESS
        if urgent is not None and bool(urgent) != order.is_urgent:
            order.is_urgent = data["urgent"] = bool(urgent)
        if first:
            notice = ("staff_order_ack", [order.issued_by])
    elif step == "done":
        if not result:
            raise ValidationError("แจ้งผลสั้น ๆ ว่าทำอะไรไปแล้ว")
        order.status, order.result, order.done_at = S.DONE, result, now
        notice = ("staff_order_done", [order.issued_by])
    elif step == "send_back":
        order.status, order.reason = S.SENT_BACK, reason
        notice = ("staff_order_back", [order.issued_by])
    elif step == "assign_person":
        _check_target(order.division, assignee)
        if assignee is None:
            raise ValidationError("เลือกคนในกอง")
        order.assignee = assignee
        data["assignee"] = assignee.pk
        notice = ("staff_order_new", [assignee])
    elif step in ("reassign", "forward"):
        if step == "forward":
            if division is None or division.pk == order.division_id:
                raise ValidationError("เลือกกองปลายทางที่ไม่ใช่กองเดิม")
            assignee, due_on = None, None
        division = division or order.division
        _check_target(division, assignee)
        _require_issuer(actor, division)
        if due_on is not None and due_on < timezone.localdate(now):
            raise ValidationError("วันกำหนดต้องไม่ใช่วันที่ผ่านมาแล้ว")
        order.division, order.assignee = division, assignee
        order.due_on = due_on or order.due_on
        order.status, order.reason = S.ASSIGNED, ""
        order.acknowledged_by = order.acknowledged_at = None
        order.reminded_soon = order.reminded_late = False
        data.update(division=division.code, assignee=assignee.pk if assignee else None)
        notice = ("staff_order_new", None)
    elif step == "close":
        order.status, order.ended_at = S.CLOSED, now
    elif step == "cancel":
        order.status, order.reason, order.ended_at = S.CANCELLED, reason, now
    order.save()
    identity.audit(
        actor,
        f"order.{step}",
        order,
        reason or result,
        number=order.number,
        status=order.status,
        **data,
    )
    if notice is not None:
        kind, users = notice
        users = doers(order) if users is None else users
        # Each notice is planned once per step taken, even when the same step repeats.
        _notify(kind, order, users, f"{step}:{order.pk}:{now.isoformat()}")
    return order


def timeline(order):
    """Who did what, oldest first, from the audit trail."""
    rows = []
    events = AuditEvent.objects.filter(
        target_type="WorkOrder", target_id=str(order.pk)
    ).select_related("actor__staff_profile")
    labels = {
        "created": "สั่งงาน",
        "attachment_added": "แนบไฟล์",
        "acknowledge": "รับทราบ",  # a step of its own until ADR 0044
        **{name: label for name, (label, *_rest) in STEPS.items()},
    }
    for event in events.order_by("created_at", "id"):
        step = event.action.split(".", 1)[1]
        rows.append(
            {
                "at": event.created_at,
                "what": labels.get(step, step),
                "who": name_of(event.actor) if event.actor else "ระบบ",
                "why": event.reason,
                "status": S(event.data["status"]).label if event.data.get("status") else "",
            }
        )
    return rows


# --- Notices ----------------------------------------------------------------------------------


def doers(order):
    """Who must act (ตัวเลือก staff_notify): the person named, else the division's Supervisors
    (everyone when it has none); or everyone in the division when the option says so."""
    people = members(order.division_id)
    if options.get(options.STAFF_NOTIFY) == "division":
        if order.assignee_id and all(u.pk != order.assignee_id for u in people):
            people.append(order.assignee)
        return people
    if order.assignee_id:
        return [order.assignee]
    heads = set(
        RoleGrant.objects.filter(
            division_id=order.division_id, role=Role.SUPERVISOR, revoked_at__isnull=True
        ).values_list("user_id", flat=True)
    )
    return [u for u in people if u.pk in heads] or people


def text(kind, order):
    rows = [SUBJECTS[kind], f"งาน {order.number}: {order.title}"]
    target = f"ถึง {order.division.code}"
    if order.assignee_id:
        target += f" · {name_of(order.assignee)}"
    rows.append(target)
    if order.due_on:
        rows.append(f"กำหนด {order.due_on.day}/{order.due_on.month}/{order.due_on.year + 543}")
    if order.is_urgent:
        rows.append("งานด่วน")
    if kind == "staff_order_back" and order.reason:
        rows.append(f"เหตุผล: {order.reason}")
    return "\n".join(rows)


def _deliver(channel, kind, order, user, key, now, **fields):
    level = N.Level.CRITICAL if order.is_urgent else N.Level.NORMAL
    try:
        with transaction.atomic():
            delivery = N.objects.create(
                channel=channel,
                kind=kind,
                level=level,
                recipient_user=user,
                work_order=order,
                source_event=uuid.uuid5(NAMESPACE, f"{key}:{user.pk}:{channel}"),
                send_after=now,
                **fields,
            )
    except IntegrityError:
        return None  # planned already
    OutboxEvent.objects.create(
        event_type=orchestration.EVENT_NOTIFICATION,
        subject_type="delivery",
        subject_id=str(delivery.pk),
        correlation_id=uuid.uuid5(NAMESPACE, f"order:{order.pk}"),
        payload={"delivery_id": delivery.pk, "kind": kind, "level": level},
        occurred_at=now,
        next_attempt_at=now,
    )
    return delivery


def _notify(kind, order, users, key):
    """A LINE message to people who linked LINE, and an e-mail when staff e-mail is on."""
    from operations import staff_email
    from operations.conversation import text_message

    now = timezone.now()
    body = text(kind, order)
    planned = []
    line_on = options.get(options.STAFF_LINE_BUDGET) != "0"
    for user in {u.pk: u for u in users if u is not None and u.is_active}.values():
        line = (
            ChannelIdentity.objects.filter(staff_user=user, is_following=True).first()
            if line_on
            else None
        )
        if line is not None:
            from operations import staff_line

            button = staff_line._token(line, "staff_order", {"order": str(order.pk)}, now)
            planned.append(
                _deliver(
                    N.Channel.LINE_PUSH,
                    kind,
                    order,
                    user,
                    key,
                    now,
                    identity=line,
                    text=body,
                    message=text_message(body, [(f"ดูงาน {order.number}", button)]),
                )
            )
        if staff_email.enabled() and user.email:
            # LINE gets a button instead (ADR 0041); the e-mail keeps the web link.
            path = reverse("order-detail", args=[order.number])
            link = f"{body}\nเปิดดู: {settings.STAFF_WEB_BASE_URL.rstrip('/')}{path}"
            planned.append(
                _deliver(N.Channel.EMAIL, kind, order, user, key, now, email=user.email, text=link)
            )
    return [d for d in planned if d is not None]


def recipient_problem(delivery):
    """Re-checked when sending: the person still sees the order and it still waits on them."""
    user = delivery.recipient_user
    if user is None or not user.is_active:
        return "recipient inactive"
    order = delivery.work_order
    if not visible(user).filter(pk=order.pk).exists():
        return "recipient no longer sees the order"
    if order.status in WorkOrder.ENDED:
        return "order ended"
    return ""


def remind(now=None):
    """One notice near the due date (ตัวเลือก order_remind_days) and one after it."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    days = int(options.get(options.ORDER_REMIND))
    sent = 0
    open_orders = WorkOrder.objects.filter(status__in=WorkOrder.OPEN, due_on__isnull=False)
    if days:
        soon = open_orders.filter(
            reminded_soon=False, due_on__gte=today, due_on__lte=today + timedelta(days=days)
        )
        for order in soon.select_related("division", "assignee__staff_profile"):
            WorkOrder.objects.filter(pk=order.pk).update(reminded_soon=True)
            _notify("staff_order_soon", order, doers(order), f"soon:{order.pk}")
            sent += 1
    late = open_orders.filter(reminded_late=False, due_on__lt=today)
    for order in late.select_related("division", "assignee__staff_profile", "issued_by"):
        WorkOrder.objects.filter(pk=order.pk).update(reminded_late=True)
        _notify("staff_order_late", order, [*doers(order), order.issued_by], f"late:{order.pk}")
        sent += 1
    return sent


# --- Lists ------------------------------------------------------------------------------------


def tab_orders(user, tab, now=None):
    now = now or timezone.now()
    scope = identity.staff_scope(user)
    orders = visible(user).select_related("division", "assignee__staff_profile", "issued_by")
    working = (*WorkOrder.OPEN, S.SENT_BACK, S.DONE)
    if tab == "mine":
        mine = Q(assignee=user) | Q(assignee__isnull=True, division__in=list(scope.division_roles))
        return orders.filter(mine, status__in=WorkOrder.OPEN).order_by("due_on", "created_at")
    if tab == "issued":
        return orders.filter(issued_by=user, status__in=working).order_by("-updated_at")
    if tab == "overseen":
        return orders.filter(division__in=list(scope.overseen), status__in=working).order_by(
            "due_on", "-created_at"
        )
    if tab == "due":
        days = max(int(options.get(options.ORDER_REMIND)), 1)
        limit = timezone.localdate(now) + timedelta(days=days)
        return orders.filter(status__in=WorkOrder.OPEN, due_on__lte=limit).order_by("due_on")
    cutoff = now - timedelta(days=30)
    return orders.filter(status__in=WorkOrder.ENDED, ended_at__gte=cutoff).order_by("-ended_at")
