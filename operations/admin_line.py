"""Admin registration commands in a linked private LINE chat, using the web's permissions."""

import re
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from operations import identity, orchestration
from operations.models import (
    ChannelIdentity,
    Division,
    NotificationDelivery,
    OutboxEvent,
    RegistrationRequest,
    Requester,
    Role,
    RoleGrant,
    StaffProfile,
)

COMMAND = re.compile(r"^(approve|อนุมัติ|reject|ปฏิเสธ|ดูคำขอ)\s+#?(\d+)(?:\s+(.*))?$", re.I)

ROLE_COMMAND = re.compile(
    r"^เพิ่มสิทธิ์\s+(\d+)\s+(สมาชิกกอง|supervisor|supervisorฝ่าย)\s+(\S+)(?:\s+(.+))?$", re.I
)
REVOKE_COMMAND = re.compile(r"^ถอนสิทธิ์\s+(\d+)(?:\s+(.+))?$")
USER_COMMAND = re.compile(r"^ดูผู้ใช้\s+(\d+)$")


@transaction.atomic
def set_notifications(actor, user, enabled):
    identity.require_admin(actor)
    identity.require_admin(user)
    profile = StaffProfile.objects.select_for_update().get(user=user)
    profile.notify_registrations = enabled
    profile.save(update_fields=["notify_registrations"])
    identity.audit(actor, "staff.registration_notifications", user, enabled=enabled)


def notify(registration):
    """Queue minimal alerts; the sender rechecks the administrator and opt-in before delivery."""
    N = NotificationDelivery
    for line in ChannelIdentity.objects.filter(
        staff_user__is_active=True,
        staff_user__staff_profile__notify_registrations=True,
        is_following=True,
    ).select_related("staff_user"):
        if not identity.staff_scope(line.staff_user).is_admin:
            continue
        key = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"faoj:registration:{registration.pk}:{registration.status}:{line.staff_user_id}",
        )
        text = f"คำขอลงทะเบียน #{registration.pk}: {registration.get_status_display()}\n"
        text += f"พิมพ์ ดูคำขอ {registration.pk} เพื่อดูรายละเอียด"
        delivery, created = N.objects.get_or_create(
            source_event=key,
            kind="staff_registration",
            defaults={
                "identity": line,
                "recipient_user": line.staff_user,
                "registration": registration,
                "level": N.Level.NORMAL,
                "text": text,
                "send_after": timezone.now(),
            },
        )
        if created:
            from operations import staff_line
            from operations.conversation import text_message

            button = staff_line._token(
                line, "admin_registration", {"id": registration.pk}, timezone.now()
            )
            delivery.message = text_message(text, [("ดูคำขอ", button)])
            delivery.save(update_fields=["message"])
            OutboxEvent.objects.create(
                event_type=orchestration.EVENT_NOTIFICATION,
                subject_type="delivery",
                subject_id=str(delivery.pk),
                correlation_id=key,
                payload={"delivery_id": delivery.pk},
                occurred_at=timezone.now(),
            )


def nav_button(turn, label, view, **values):
    return turn.button(label, "admin_nav", {"view": view, **values})


def home(turn):
    turn.set_prompt()
    turn.say(
        "แอดมิน LINE · เลือกรายการ",
        [
            nav_button(turn, "คำขอรอรับรอง", "pending"),
            nav_button(turn, "ผู้ใช้ LINE", "users"),
            nav_button(turn, "เปิดแจ้งสมัคร", "command", command="รับแจ้งสมัคร เปิด"),
            nav_button(turn, "ปิดแจ้งสมัคร", "command", command="รับแจ้งสมัคร ปิด"),
            turn.button("งานของกอง", "staff_menu"),
        ],
    )


def navigate(turn, user, value):
    from django.core.paginator import Paginator

    view = value.get("view", "home")
    if view != "confirm":
        turn.set_prompt()
    back = nav_button(turn, "เมนูแอดมิน", "home")
    if view == "home":
        home(turn)
    elif view in {"pending", "users", "divisions", "grants"}:
        if view == "pending":
            query = RegistrationRequest.objects.filter(status="pending").order_by("pk")
            label = "คำขอรอรับรอง"
        elif view == "users":
            query = Requester.objects.filter(is_anonymous=False, status="active").order_by(
                "display_name", "pk"
            )
            label = "ผู้ใช้ LINE"
        elif view == "grants":
            users = ChannelIdentity.objects.filter(
                requester_id=value.get("requester"), staff_user__isnull=False
            ).values("staff_user_id")
            query = (
                RoleGrant.objects.filter(user_id__in=users, revoked_at=None)
                .select_related("division")
                .order_by("pk")
            )
            label = "สิทธิ์ของผู้ใช้"
        else:
            query = Division.objects.filter(is_active=True).order_by("code")
            label = "เลือกกอง"
        rows = Paginator(query, 5).get_page(value.get("page", 1))
        quick, text = [], []
        for row in rows:
            if view == "pending":
                text.append(f"#{row.pk} {row.submitted_name}")
                quick.append(turn.button(f"คำขอ #{row.pk}", "admin_registration", {"id": row.pk}))
            elif view == "users":
                text.append(f"#{row.pk} {row.display_name}")
                quick.append(
                    nav_button(turn, f"ผู้ใช้ #{row.pk}", "command", command=f"ดูผู้ใช้ {row.pk}")
                )
            elif view == "grants":
                text.append(
                    f"#{row.pk} {row.get_role_display()} · "
                    f"{row.division.code if row.division else 'ระดับฝ่าย'}"
                )
                quick.append(
                    nav_button(turn, f"ถอนสิทธิ์ #{row.pk}", "confirm", command=f"ถอนสิทธิ์ {row.pk}")
                )
            else:
                command = f"เพิ่มสิทธิ์ {value['requester']} {value['role']} {row.code}"
                quick.append(nav_button(turn, row.code, "confirm", command=command))
        for label_page, number in (("ย้อนกลับ", rows.number - 1), ("ถัดไป", rows.number + 1)):
            if 1 <= number <= rows.paginator.num_pages:
                quick.append(turn.button(label_page, "admin_nav", {**value, "page": number}))
        turn.say(
            f"{label} {rows.paginator.count} รายการ · "
            f"หน้า {rows.number}/{rows.paginator.num_pages}\n" + "\n".join(text),
            [*quick, back],
        )
    elif view == "roles":
        quick = [
            nav_button(turn, label, "divisions", requester=value["requester"], role=role)
            for role, label in [("สมาชิกกอง", "สมาชิกกอง"), ("supervisor", "Supervisor กอง")]
        ]
        quick.append(
            nav_button(
                turn,
                "Supervisor ฝ่าย",
                "confirm",
                command=f"เพิ่มสิทธิ์ {value['requester']} supervisorฝ่าย -",
            )
        )
        turn.say("เลือกบทบาทที่จะเพิ่ม", [*quick, back])
    elif view == "confirm":
        turn.set_prompt("admin_choice", command=value["command"])
        turn.say(
            "ตรวจคำสั่งก่อนยืนยัน\n" + value["command"], [turn.button("ยืนยัน", "admin_confirm"), back]
        )
    elif view == "command":
        handle(turn, user, value.get("command", ""), None)


def detail(turn, registration):
    message = (
        f"คำขอ #{registration.pk} · {registration.get_status_display()}\n"
        f"ชื่อ: {registration.submitted_name}\n"
        f"หน่วยงาน: {registration.requested_district.name}\n"
        f"การยืนยัน: {registration.get_verified_by_display() or 'รอตรวจ'}"
    )
    quick = []
    if registration.status == RegistrationRequest.Status.PENDING:
        quick += [
            nav_button(turn, label, "command", command=f"{action} {registration.pk}")
            for action, label in [("approve", "รับรอง"), ("reject", "ปฏิเสธ")]
        ]
        message += "\nกดรับรองหรือปฏิเสธได้ เหตุผลไม่บังคับ"
    if registration.identity.requester_id:
        message += f"\nเลขผู้ใช้: {registration.identity.requester_id} (พิมพ์ ดูผู้ใช้ ตามด้วยเลขนี้)"
        quick.append(
            nav_button(
                turn, "ดูผู้ใช้", "command", command=f"ดูผู้ใช้ {registration.identity.requester_id}"
            )
        )
    turn.say(
        message,
        [*quick, nav_button(turn, "กลับรายการ", "pending"), nav_button(turn, "เมนูแอดมิน", "home")],
    )


def handle(turn, user, command, token):
    navigation = token is not None and token.action in {"admin_nav", "admin_confirm"}
    if navigation:
        if not identity.staff_scope(user).is_admin:
            turn.say("คำสั่งนี้ใช้ได้เฉพาะแอดมิน")
            return True
        if token.action == "admin_confirm":
            data = turn.conversation.prompt_data
            if turn.conversation.prompt != "admin_choice" or token.value.get(
                "prompt_revision"
            ) != data.get("prompt_revision"):
                turn.say("คำสั่งนี้เป็นฉบับเก่า เปิดรายการใหม่อีกครั้ง")
                return True
            command = data.get("command", "")
            turn.set_prompt()
            return handle(turn, user, command, None)
        navigate(turn, user, token.value)
        return True
    match = COMMAND.fullmatch(command)
    role_match = ROLE_COMMAND.fullmatch(command)
    revoke_match = REVOKE_COMMAND.fullmatch(command)
    user_match = USER_COMMAND.fullmatch(command)
    special = command in {"แอดมิน", "คำขอรออนุมัติ", "รับแจ้งสมัคร เปิด", "รับแจ้งสมัคร ปิด"}
    token_request = token is not None and token.action == "admin_registration"
    if not (match or special or token_request or role_match or revoke_match or user_match):
        return False
    if not identity.staff_scope(user).is_admin:
        turn.say("คำสั่งนี้ใช้ได้เฉพาะแอดมิน")
        return True
    try:
        if role_match:
            requester = Requester.objects.filter(pk=int(role_match[1]), is_anonymous=False).first()
            if requester is None:
                turn.say("ไม่พบผู้ใช้รายนี้")
                return True
            role = {
                "สมาชิกกอง": Role.STAFF,
                "supervisor": Role.SUPERVISOR,
                "supervisorฝ่าย": Role.DEPARTMENT_SUPERVISOR,
            }[role_match[2].lower()]
            code = role_match[3]
            division = Division.objects.filter(code=code, is_active=True).first()
            if role == Role.DEPARTMENT_SUPERVISOR:
                if code != "-":
                    turn.say("Supervisor ฝ่ายใช้ - แทนกอง")
                    return True
                division = None
            elif division is None:
                turn.say("ไม่พบกองนี้")
                return True
            identity.grant_requester_role(user, requester, role, division, role_match[4])
            turn.say(
                "เพิ่มสิทธิ์ให้ผู้ใช้ LINE แล้ว",
                [
                    nav_button(turn, "ดูผู้ใช้", "command", command=f"ดูผู้ใช้ {requester.pk}"),
                    nav_button(turn, "เมนูแอดมิน", "home"),
                ],
            )
        elif revoke_match:
            grant = RoleGrant.objects.filter(pk=int(revoke_match[1])).first()
            if grant is None:
                turn.say("ไม่พบสิทธิ์นี้")
                return True
            identity.revoke_role(user, grant, revoke_match[2])
            turn.say(
                "ถอนสิทธิ์แล้ว",
                [nav_button(turn, "ผู้ใช้ LINE", "users"), nav_button(turn, "เมนูแอดมิน", "home")],
            )
        elif user_match:
            requester = Requester.objects.filter(pk=int(user_match[1]), is_anonymous=False).first()
            if requester is None:
                turn.say("ไม่พบผู้ใช้รายนี้")
                return True
            lines = ChannelIdentity.objects.filter(requester=requester, staff_user__isnull=False)
            grants = RoleGrant.objects.filter(
                user_id__in=lines.values("staff_user_id"), revoked_at=None
            ).select_related("division")
            shown = [
                f"สิทธิ์ #{g.pk}: {g.get_role_display()} · "
                f"{g.division.code if g.division else 'ระดับฝ่าย'}"
                for g in grants
            ]
            turn.say(
                f"ผู้ใช้ #{requester.pk}: {requester.display_name}\n"
                + "\n".join(shown or ["ยังไม่มีสิทธิ์งานภายใน"]),
                [nav_button(turn, "เพิ่มสิทธิ์", "roles", requester=requester.pk)]
                + [
                    nav_button(turn, f"ถอนสิทธิ์ #{g.pk}", "confirm", command=f"ถอนสิทธิ์ {g.pk}")
                    for g in grants
                ][:8]
                + [nav_button(turn, "สิทธิ์ทั้งหมด", "grants", requester=requester.pk)]
                + [nav_button(turn, "กลับผู้ใช้", "users"), nav_button(turn, "เมนูแอดมิน", "home")],
            )
        elif command.startswith("รับแจ้งสมัคร "):
            enabled = command.endswith("เปิด")
            set_notifications(user, user, enabled)
            turn.say("เปิดรับแจ้งการสมัครใน LINE นี้แล้ว" if enabled else "ปิดรับแจ้งการสมัครแล้ว")
        elif command == "แอดมิน":
            home(turn)
        elif command == "คำขอรออนุมัติ":
            navigate(turn, user, {"view": "pending"})
        else:
            pk = token.value.get("id") if token_request else int(match[2])
            registration = (
                RegistrationRequest.objects.filter(pk=pk)
                .select_related("requested_district")
                .first()
            )
            if registration is None:
                turn.say("ไม่พบคำขอนี้")
                return True
            action = "ดูคำขอ" if token_request else match[1].lower()
            if action == "ดูคำขอ":
                detail(turn, registration)
            else:
                reason = (match[3] or "").strip()
                if action in {"approve", "อนุมัติ"}:
                    identity.approve_registration(user, registration, reason)
                else:
                    identity.reject_registration(user, registration, reason)
                turn.say(
                    f"บันทึกผลคำขอ #{registration.pk} แล้ว",
                    [
                        nav_button(turn, "กลับรายการ", "pending"),
                        nav_button(turn, "เมนูแอดมิน", "home"),
                    ],
                )
    except (ValidationError, PermissionDenied) as exc:
        messages = getattr(exc, "messages", None) or ["ไม่มีสิทธิ์ทำรายการนี้"]
        turn.say("; ".join(messages))
    return True
