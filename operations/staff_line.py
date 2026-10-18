"""เจ้าหน้าที่ใน LINE (ADR 0010; 10 Oct 2569 Q3–Q8): link, alerts and work, kept private.

A staff account links one LINE account with a one-time code from the web, or (when ตัวเลือกระบบ
allows) an employee ID and its proof. One LINE may be a requester and staff at once (ผอ.ฝอจ.).

Privacy: a push names only the case number and what happened, never the requester or their
text, because pushes show on lock screens. The details come back as a reply, only in this 1:1
chat, after the services check again that this account still sees the case. Buttons are bound to
this LINE account and expire; every action goes through services.perform_action with the staff
account, the same rules and audit as the web.

Alerts are the important situations only (user Q5): a new P0, an escalation, a handoff or child
work to the division, a route waiting for a decision, and a district disputing or reopening.
They go to the person responsible, or else the division's supervisors, and the sender bundles
whatever waits for one person into a single push. Their own monthly budget is an option.
"""

import hashlib
import hmac
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from operations import identity, options, orchestration, queues
from operations.models import (
    AuditEvent,
    Case,
    ChannelIdentity,
    District,
    NotificationDelivery,
    OutboxEvent,
    Requester,
    Role,
    RoleGrant,
    StaffLinkCode,
    StaffProfile,
)

N = NotificationDelivery
NAMESPACE = uuid.UUID("2b7c1e0a-6f4d-4c1b-9a3e-55e0c7d8a901")
CODE_MINUTES = 10
MAX_CODE_TRIES = 5  # wrong codes per LINE account per 24 hours
IMPORTANT = {
    "staff_hazard": N.Level.CRITICAL,
    "staff_escalated": N.Level.CRITICAL,
    "staff_routing": N.Level.CRITICAL,
    "staff_handoff": N.Level.NORMAL,
    "staff_child_work": N.Level.NORMAL,
    "staff_dispute": N.Level.NORMAL,
    "staff_reopen": N.Level.NORMAL,
}
# Steps a staff member may take from LINE when ตัวเลือกระบบ allows it; anything irreversible or
# that needs a long form stays on the web.
LINE_ACTIONS = {
    "claim": "รับงาน",
    "start_triage": "เริ่มคัดกรอง",
    "accept_handoff": "รับงานที่ส่งต่อ",
    "request_info": "ขอข้อมูลเพิ่ม",
}


# --- Linking ------------------------------------------------------------------------------------


def _hash(code):
    key = hashlib.sha256(f"staff-link:{settings.SECRET_KEY}".encode()).digest()
    return hmac.new(key, str(code).encode(), hashlib.sha256).hexdigest()


@transaction.atomic
def issue_code(user):
    """A new code for บัญชีของฉัน; earlier unused codes of this account stop working."""
    now = timezone.now()
    StaffLinkCode.objects.filter(user=user, used_at__isnull=True).update(expires_at=now)
    code = f"{secrets.randbelow(10**6):06d}"
    StaffLinkCode.objects.create(
        user=user, code_hash=_hash(code), expires_at=now + timedelta(minutes=CODE_MINUTES)
    )
    identity.audit(user, "staff.line_code_issued", user)
    return code


def staff_of(identity_row):
    """The working staff account of this LINE, or None."""
    user = identity_row.staff_user
    if user is None or not user.is_active or not identity.staff_scope(user).has_any_access:
        return None
    return user


def _link(identity_row, user, method):
    if ChannelIdentity.objects.filter(staff_user=user).exclude(pk=identity_row.pk).exists():
        ChannelIdentity.objects.filter(staff_user=user).update(
            staff_user=None, staff_linked_at=None
        )
    identity_row.staff_user, identity_row.staff_linked_at = user, timezone.now()
    identity_row.save(update_fields=["staff_user", "staff_linked_at"])
    identity.audit(user, "staff.line_linked", identity_row, actor_label="line", method=method)
    _relink(identity_row)


def _relink(identity_row):
    """The LINE's menu follows its staff link (16 ต.ค. 2569 Q9), after the commit."""
    from operations import line_menu  # local: line_menu reads staff scopes through identity

    transaction.on_commit(lambda: line_menu.relink(identity_row))


def _failed_tries(identity_row):
    return AuditEvent.objects.filter(
        action="staff.line_link_failed",
        target_type="ChannelIdentity",
        target_id=str(identity_row.pk),
        created_at__gte=timezone.now() - timedelta(days=1),
    ).count()


@transaction.atomic
def link_with_code(identity_row, code):
    """(user, "") or (None, reason) for "เชื่อม 123456" typed in LINE."""
    if _failed_tries(identity_row) >= MAX_CODE_TRIES:
        return None, "locked"
    row = (
        StaffLinkCode.objects.select_for_update()
        .filter(code_hash=_hash(code), used_at__isnull=True, expires_at__gt=timezone.now())
        .select_related("user")
        .first()
    )
    if row is None or not row.user.is_active:
        identity.audit(None, "staff.line_link_failed", identity_row, actor_label="line")
        return None, "no_match"
    row.used_at = timezone.now()
    row.save(update_fields=["used_at"])
    _link(identity_row, row.user, "code")
    return row.user, ""


@transaction.atomic
def link_with_employee(identity_row, employee_id, proof):
    """ตัวเลือกระบบ code_or_employee: the employee check passes and a staff account carries
    this employee ID; the link is made at once and audited (user Q3)."""
    from operations import employees

    if options.get(options.STAFF_LINK) != "code_or_employee":
        return None, "off"
    employee, reason = employees.verify(identity_row, employee_id, proof)
    if employee is None:
        return None, reason
    profile = (
        StaffProfile.objects.select_related("user")
        .filter(employee_id=employee.employee_id, user__is_active=True)
        .first()
    )
    if profile is None:
        return None, "no_account"
    _link(identity_row, profile.user, "employee")
    return profile.user, ""


def _otp_target(identity_row, employee_id):
    """(employee, staff user, "") when an active staff account carries this employee ID and the
    roster can mail it a code; else (None, None, reason): "locked" after the day's failures, or
    "no_match" (counted as a failure, so IDs cannot be walked through LINE)."""
    from operations import email_codes, employees
    from operations.models import Employee

    if employees.failed_today(identity_row) >= employees.MAX_FAILED:
        return None, None, "locked"
    employee_id = (employee_id or "").strip()[:32]
    employee = Employee.objects.filter(employee_id=employee_id, is_active=True).first()
    profile = (
        StaffProfile.objects.select_related("user")
        .filter(employee_id=employee_id, user__is_active=True)
        .first()
        if employee_id
        else None
    )
    if employee is None or profile is None or "otp" not in email_codes.methods(employee):
        identity.audit(None, "identity.employee_check_failed", identity_row, actor_label="line")
        return None, None, "no_match"
    return employee, profile.user, ""


@transaction.atomic
def send_link_code(identity_row, employee_id):
    """16 Oct 2569 Q10: mail a 6-digit code to the roster e-mail of the staff account that
    carries this employee ID. Returns "" or why not: off, locked, no_match, limit, unavailable."""
    from operations import email_codes

    if options.get(options.STAFF_LINK) != "code_or_otp":
        return "off"
    employee, _, reason = _otp_target(identity_row, employee_id)
    if employee is None:
        return reason
    return email_codes.issue(identity_row, employee)


@transaction.atomic
def link_with_otp(identity_row, employee_id, typed):
    """(user, "", 0) when the code is right and the link is made; else (None, reason, left)."""
    from operations import email_codes
    from operations.models import Employee

    employee = Employee.objects.filter(employee_id=(employee_id or "").strip()).first()
    profile = (
        StaffProfile.objects.select_related("user")
        .filter(employee_id=employee.employee_id, user__is_active=True)
        .first()
        if employee
        else None
    )
    if employee is None or profile is None:
        return None, "expired", 0
    ok, reason, left = email_codes.check(identity_row, employee, typed)
    if not ok:
        return None, reason, left
    _link(identity_row, profile.user, "otp")
    return profile.user, "", 0


@transaction.atomic
def unlink(identity_row, actor=None, reason=""):
    user = identity_row.staff_user
    if user is None:
        return False
    identity_row.staff_user, identity_row.staff_linked_at = None, None
    identity_row.save(update_fields=["staff_user", "staff_linked_at"])
    identity.audit(actor or user, "staff.line_unlinked", identity_row, reason, user=user.pk)
    _relink(identity_row)
    return True


# --- Internal reporters (Q8) ----------------------------------------------------------------------


@transaction.atomic
def internal_requester(identity_row, user):
    """ผู้แจ้งภายใน: the staff member reporting a problem or suggestion in their division's name.
    It becomes this LINE's requester only when the LINE has none (a registered ผอ. keeps theirs)."""
    if identity_row.requester_id is not None:
        return identity_row.requester
    scope = identity.staff_scope(user)
    division = next(iter(queues.own_divisions(scope)), None)
    district, _ = District.objects.get_or_create(
        code=identity.UNKNOWN_DISTRICT,
        defaults={"name": "ไม่ระบุหน่วยงาน (เขต/ฝ่าย)", "is_active": False},
    )
    name = getattr(getattr(user, "staff_profile", None), "display_name", user.username)
    requester = Requester.objects.create(
        display_name=f"{name} ({division.code})" if division else name,
        district=district,
        approved_by=identity.system_user(),
        is_internal=True,
        division=division,
    )
    identity_row.requester = requester
    identity_row.status = ChannelIdentity.Status.ACTIVE
    identity_row.save(update_fields=["requester", "status"])
    identity.audit(user, "requester.internal_created", requester, actor_label="line")
    return requester


# --- Alerts -------------------------------------------------------------------------------------


def _important_people(case, users):
    """The person responsible, else the supervisors (ผอ.) among those told, else everyone."""
    people = [u for u in users if u.is_active]
    if case is not None and case.assignee_id and any(u.pk == case.assignee_id for u in people):
        return [u for u in people if u.pk == case.assignee_id][:1]
    heads = set(
        RoleGrant.objects.filter(
            user__in=people, role=Role.SUPERVISOR, revoked_at__isnull=True
        ).values_list("user_id", flat=True)
    )
    return [u for u in people if u.pk in heads] or people


def _text(kind, case):
    from operations.staff_email import SUBJECTS

    # No requester name, no requester text: the push shows on the lock screen.
    return f"{SUBJECTS[kind]}\nเรื่อง {case.number} · กอง {case.owner_division.code}"


def plan(kind, key, case, users):
    """LINE deliveries for staff with a linked LINE; nothing when the budget option is 0."""
    if kind not in IMPORTANT or case is None or options.get(options.STAFF_LINE_BUDGET) == "0":
        return []
    from operations.conversation import text_message

    now = timezone.now()
    planned = []
    for user in _important_people(case, users):
        line = ChannelIdentity.objects.filter(staff_user=user, is_following=True).first()
        if line is None:
            continue
        token = _token(line, "staff_case", {"case": str(case.pk)}, now)
        text = _text(kind, case)
        level = IMPORTANT[kind]
        delay = timedelta(0) if level == N.Level.CRITICAL else timedelta(minutes=5)
        try:
            with transaction.atomic():
                delivery = N.objects.create(
                    channel=N.Channel.LINE_PUSH,
                    kind=kind,
                    level=level,
                    identity=line,
                    recipient_user=user,
                    case=case,
                    text=text,
                    message=text_message(text, [(f"ดูเรื่อง {case.number}", token)]),
                    source_event=uuid.uuid5(NAMESPACE, f"{key}:{user.pk}"),
                    send_after=now + delay,
                )
        except IntegrityError:
            continue
        OutboxEvent.objects.create(
            event_type=orchestration.EVENT_NOTIFICATION,
            subject_type="delivery",
            subject_id=str(delivery.pk),
            case=case,
            case_revision=case.revision,
            correlation_id=case.pk,
            payload={"delivery_id": delivery.pk, "kind": kind, "level": level},
            occurred_at=now,
            next_attempt_at=delivery.send_after,
        )
        planned.append(delivery)
    return planned


def _token(identity_row, action, value, now):
    from operations.models import PostbackToken

    token = PostbackToken.objects.create(
        token=secrets.token_urlsafe(16),
        identity=identity_row,
        action=action,
        value=value,
        expires_at=now + timedelta(days=7),
    )
    return f"t={token.token}"


def is_staff_delivery(delivery):
    return delivery.kind.startswith("staff_") and delivery.channel == N.Channel.LINE_PUSH


def recipient_problem(delivery):
    """Re-checked when sending: still linked, still working, still sees the case."""
    from operations import services
    from operations.contracts import Actor

    user = delivery.recipient_user
    if (
        user is None
        or delivery.identity.staff_user_id != user.pk
        or staff_of(delivery.identity) is None
    ):
        return "staff LINE no longer linked or account inactive"
    if delivery.kind == "staff_registration":
        if not identity.staff_scope(user).is_admin or not user.staff_profile.notify_registrations:
            return "registration notifications no longer allowed"
        return ""
    if delivery.work_order_id is not None:
        from operations import work_orders  # local: work_orders plans through this path

        return work_orders.recipient_problem(delivery)
    if not services.visible_cases(Actor.staff(user)).filter(pk=delivery.case_id).exists():
        return "staff no longer sees the case"
    return ""


def budget_allows(level, now):
    """ตัวเลือกระบบ staff_line_budget: normal alerts stop at 90%, critical ones at 100%."""
    budget = int(options.get(options.STAFF_LINE_BUDGET))
    month = timezone.localtime(now).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    used = (
        N.objects.filter(
            channel=N.Channel.LINE_PUSH,
            kind__startswith="staff_",
            status__in=[N.Status.SENT, N.Status.UNKNOWN],
            sent_at__gte=month,
        ).aggregate(total=Sum("counted"))["total"]
        or 0
    )
    if level == N.Level.CRITICAL:
        return used < budget
    return used < budget * 0.9


# --- The staff side of a LINE chat ---------------------------------------------------------------


def _case_for(user, case_id=None, number=None):
    from operations import services
    from operations.contracts import Actor

    cases = services.visible_cases(Actor.staff(user))
    if case_id:
        return cases.filter(pk=case_id).first()
    return cases.filter(number=number).first()


def menu(turn, user):
    from operations import staff_work

    staff_work.list_page(turn, user)


def case_detail(turn, user, case):
    from operations import web_actions

    level = case.triage.effective_priority if hasattr(case, "triage") else ""
    lines = [
        f"เรื่อง {case.number} · {case.get_kind_display()}",
        f"สถานะ: {case.get_status_display()} · ระดับ{queues.level_name(level)}",
        f"กอง {case.owner_division.code}"
        + (f" · ผู้รับผิดชอบ {case.assignee.staff_profile.display_name}" if case.assignee else ""),
    ]
    text = (case.description or case.purpose or case.return_reason or "").strip()
    if text:
        lines.append(text[:300])
    buttons = []
    if options.get(options.STAFF_LINE_ACTIONS) == "act":
        offered = {o.name for o in web_actions.available(user, case)}
        for action, label in LINE_ACTIONS.items():
            if action in offered:
                buttons.append(
                    turn.button(label, "staff_act", {"case": str(case.pk), "action": action})
                )
    url = f"{settings.STAFF_WEB_BASE_URL.rstrip('/')}/cases/{case.number}/"
    buttons.append(("เปิดในเว็บ", url))
    turn.say("\n".join(lines), buttons)


def act(turn, user, case, action, reason=""):
    """One step from LINE through the same service as the web."""
    from operations import services, wording
    from operations.contracts import Actor, CommandFailed

    if options.get(options.STAFF_LINE_ACTIONS) != "act" or action not in LINE_ACTIONS:
        turn.say("ขั้นนี้ทำในเว็บ")
        return
    if action == "request_info" and not reason:
        turn.set_prompt("staff_info", case=str(case.pk))
        turn.say(
            f"เรื่อง {case.number}: ระบุข้อมูลเพิ่มเติมที่ต้องการจากหน่วยงาน (เขต/ฝ่าย) ข้อความนี้ถึงผู้แจ้งทาง LINE"
        )
        return
    try:
        services.perform_action(
            Actor.staff(user),
            case.pk,
            action,
            expected_revision=Case.objects.get(pk=case.pk).revision,
            idempotency_key=f"line-staff-{turn.inbox.pk}",
            payload={"reason": reason} if reason else {},
        )
    except CommandFailed as exc:
        turn.say(f"ทำไม่ได้: {wording.thai(exc.message) or exc.message or exc.code}")
        return
    turn.set_prompt()
    turn.say(f"บันทึกแล้ว: {LINE_ACTIONS[action]} เรื่อง {case.number}")
