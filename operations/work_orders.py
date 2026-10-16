"""งานสั่ง (16 Oct 2569 Q6, Q7, Q14): work a Supervisor gives a division or one of its members.

Only a Supervisor issues one: the Supervisor ระดับฝ่าย for any division, a Supervisor ของกอง for
their own. มอบหมาย → รับทราบ → กำลังทำ → เสร็จ; the person or division doing it may send it back
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
from django.utils import timezone

from operations import identity, options, orchestration
from operations.models import (
    AuditEvent,
    CaseNumberCounter,
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
    "staff_order_ack": "รับทราบงานที่คุณสั่งแล้ว",
    "staff_order_done": "งานที่คุณสั่งเสร็จแล้ว",
    "staff_order_back": "งานที่คุณสั่งถูกส่งกลับ",
    "staff_order_soon": "งานสั่งใกล้ถึงกำหนด",
    "staff_order_late": "งานสั่งเกินกำหนดแล้ว",
}

# Steps: label, the statuses it starts from, who takes it ("doer" or "supervisor"), and what
# it needs typed. The web page and (later) LINE offer exactly these.
STEPS = {
    "acknowledge": ("รับทราบ", (S.ASSIGNED,), "doer", ""),
    "start": ("เริ่มทำ", (S.ASSIGNED, S.ACKNOWLEDGED), "doer", ""),
    "done": ("ทำเสร็จแล้ว", WorkOrder.OPEN, "doer", "result"),
    "send_back": ("ส่งกลับผู้สั่ง", WorkOrder.OPEN, "doer", "reason"),
    "assign_person": ("มอบให้คนในกอง", WorkOrder.OPEN, "supervisor", "person"),
    "reassign": ("สั่งใหม่", (S.SENT_BACK,), "supervisor", "target"),
    "close": ("ปิดงาน", (S.DONE,), "supervisor", ""),
    "cancel": ("ยกเลิกงาน", (*WorkOrder.OPEN, S.SENT_BACK), "supervisor", "reason"),
}


# --- Who --------------------------------------------------------------------------------------


def may_issue(user):
    return bool(identity.staff_scope(user).overseen)


def issuable(user):
    """Divisions this person may give work to (the ones they supervise)."""
    scope = identity.staff_scope(user)
    return Division.objects.filter(pk__in=scope.overseen, is_active=True).order_by("kind", "code")


def visible(user):
    """Orders of the divisions a person works in or supervises, and those they issued or got;
    admins see all."""
    scope = identity.staff_scope(user)
    orders = WorkOrder.objects.all()
    if scope.is_admin or scope.is_support:
        return orders
    if not scope.has_any_access:
        return orders.none()
    divisions = [*scope.division_roles, *scope.overseen]
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
        if who == "doer" and not is_doer(user, order, scope):
            continue
        if who == "supervisor" and not is_supervisor(user, order, scope):
            continue
        allowed.append((name, label))
    return allowed


def members(division_id):
    """Active people of a division, once each, by name."""
    grants = (
        RoleGrant.objects.filter(
            division_id=division_id, revoked_at__isnull=True, user__is_active=True
        )
        .select_related("user__staff_profile")
        .order_by("user__username")
    )
    seen, people = set(), []
    for grant in grants:
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
    if assignee is not None and assignee.pk not in {u.pk for u in members(division.pk)}:
        raise ValidationError("คนที่เลือกต้องเป็นสมาชิกของกองนั้น")


def _require_issuer(actor, division):
    if division.pk not in identity.staff_scope(actor).overseen:
        raise PermissionDenied("เฉพาะ Supervisor ของกองนี้หรือ Supervisor ระดับฝ่ายสั่งงานได้")


def _number(now):
    """T69-000001: gap-free per Buddhist-era year, like case numbers."""
    year_be = timezone.localtime(now).year + 543
    CaseNumberCounter.objects.bulk_create(
        [CaseNumberCounter(prefix="T", year_be=year_be)], ignore_conflicts=True
    )
    counter = CaseNumberCounter.objects.select_for_update().get(prefix="T", year_be=year_be)
    counter.last_value += 1
    counter.save(update_fields=["last_value"])
    return f"T{year_be % 100:02d}-{counter.last_value:06d}"


# --- Issuing and steps ------------------------------------------------------------------------


@transaction.atomic
def create(actor, title, division, assignee=None, detail="", due_on=None, urgent=False):
    """A new งานสั่ง; the doer is told at once (Q14)."""
    title = " ".join((title or "").split())
    if not title:
        raise ValidationError("บอกว่าให้ทำอะไร")
    if len(title) > TITLE_MAX:
        raise ValidationError(f"ชื่องานยาวได้ไม่เกิน {TITLE_MAX} ตัวอักษร")
    _check_target(division, assignee)
    _require_issuer(actor, division)
    now = timezone.now()
    if due_on is not None and due_on < timezone.localdate(now):
        raise ValidationError("วันกำหนดต้องไม่ใช่วันที่ผ่านมาแล้ว")
    order = WorkOrder.objects.create(
        number=_number(now),
        title=title,
        detail=(detail or "").strip(),
        division=division,
        assignee=assignee,
        issued_by=actor,
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
    )
    _notify("staff_order_new", order, doers(order), f"new:{order.pk}")
    return order


@transaction.atomic
def act(actor, order, step, reason="", result="", assignee=None, division=None, due_on=None):
    """Take one step; the order is locked, and the step must still be allowed."""
    if step not in STEPS:
        raise ValidationError("ไม่รู้จักขั้นนี้")
    order = WorkOrder.objects.select_for_update().get(pk=order.pk)
    if step not in dict(steps_for(actor, order)):
        raise PermissionDenied("ทำขั้นนี้ไม่ได้ในตอนนี้")
    reason, result = (reason or "").strip(), (result or "").strip()
    now = timezone.now()
    data = {}
    notice = None
    if step in {"acknowledge", "start"}:
        first = order.acknowledged_at is None
        if first:
            order.acknowledged_by, order.acknowledged_at = actor, now
        order.status = S.ACKNOWLEDGED if step == "acknowledge" else S.IN_PROGRESS
        if first:
            notice = ("staff_order_ack", [order.issued_by])
    elif step == "done":
        if not result:
            raise ValidationError("เล่าสั้น ๆ ว่าทำอะไรไปแล้ว")
        order.status, order.result, order.done_at = S.DONE, result, now
        notice = ("staff_order_done", [order.issued_by])
    elif step == "send_back":
        if not reason:
            raise ValidationError("บอกเหตุผลที่ส่งกลับ")
        order.status, order.reason = S.SENT_BACK, reason
        notice = ("staff_order_back", [order.issued_by])
    elif step == "assign_person":
        _check_target(order.division, assignee)
        if assignee is None:
            raise ValidationError("เลือกคนในกอง")
        order.assignee = assignee
        data["assignee"] = assignee.pk
        notice = ("staff_order_new", [assignee])
    elif step == "reassign":
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
        if not reason:
            raise ValidationError("บอกเหตุผลที่ยกเลิก")
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
    rows.append(f"เปิดดู: {settings.STAFF_WEB_BASE_URL.rstrip('/')}/orders/{order.number}/")
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
                    message=text_message(body),
                )
            )
        if staff_email.enabled() and user.email:
            planned.append(
                _deliver(N.Channel.EMAIL, kind, order, user, key, now, email=user.email, text=body)
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

TABS = {
    "mine": "งานของฉัน",
    "issued": "ที่ฉันสั่ง",
    "overseen": "ทุกกองที่ดูแล",
    "due": "ใกล้หรือเกินกำหนด",
    "ended": "ปิดแล้ว",
}


def tabs_for(user):
    scope = identity.staff_scope(user)
    keys = ["mine"]
    if scope.overseen or WorkOrder.objects.filter(issued_by=user).exists():
        keys.append("issued")
    if scope.overseen:
        keys.append("overseen")
    return [*keys, "due", "ended"]


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


def waiting_count(user):
    """For the menu: open orders waiting on this person, plus finished ones they should close."""
    scope = identity.staff_scope(user)
    if not scope.has_any_access:
        return 0
    mine = tab_orders(user, "mine").count()
    to_close = visible(user).filter(issued_by=user, status=S.DONE).count()
    return mine + to_close


def row(order, today):
    """What a list row shows."""
    late = order.due_on is not None and order.status in WorkOrder.OPEN and order.due_on < today
    soon = (
        order.due_on is not None
        and order.status in WorkOrder.OPEN
        and not late
        and order.due_on <= today + timedelta(days=1)
    )
    return {"order": order, "late": late, "soon": soon}
