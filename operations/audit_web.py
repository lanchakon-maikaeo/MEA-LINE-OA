"""Log การใช้งาน: what each บัญชีเจ้าหน้าที่ did, sign-ins included (31 Oct 2569 Q14 to Q17).

The user asked for a page under คนและสิทธิ์ with every sign-in and every other activity of the
staff accounts, in Thai time. Their answers:
- Q14: sign-in, sign-out and failed sign-in (the username typed, never the password) next to
  everything the system already records, newest first, 50 a page, filtered by account, kind of
  activity and dates.
- Q15: a sign-in keeps the IP address and the browser.
- Q16: every activity has a Thai name, never the system's code; one without a name of its own
  shows its group's name, and case steps use the case page's button wording.
- Q17: admins only; the CSV follows the filters and every download is recorded. LINE OA users do
  not sign in, so their history stays on their own page.
"""

import csv
import io
from datetime import date

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.http import require_safe

from operations import identity, reports, web_actions, workflow
from operations.models import AuditEvent, Case, Requester
from operations.views import admin_required

PER_PAGE = 50
LOGIN = "login"  # the kind filter of sign-in, sign-out and failed sign-in
SIGN_IN = ("staff.login", "staff.logout", "staff.login_failed")
MONTHS = (
    "ม.ค.",
    "ก.พ.",
    "มี.ค.",
    "เม.ย.",
    "พ.ค.",
    "มิ.ย.",
    "ก.ค.",
    "ส.ค.",
    "ก.ย.",
    "ต.ค.",
    "พ.ย.",
    "ธ.ค.",
)

# Q16: the group of each action, by the part before its dot.
GROUPS = {
    "case": "งานเรื่อง",
    "cases": "ดาวน์โหลดข้อมูล",
    "audit": "ดาวน์โหลดข้อมูล",
    "staff": "สิทธิ์และบัญชี",
    "role": "สิทธิ์และบัญชี",
    "authority": "สิทธิ์และบัญชี",
    "requester": "ผู้ใช้ LINE OA",
    "registration": "ผู้ใช้ LINE OA",
    "identity": "ผู้ใช้ LINE OA",
    "line_menu": "เมนู LINE",
    "line_flow": "เมนู LINE",
    "line": "เมนู LINE",
    "line_data": "เมนู LINE",
    "ai": "การตั้งค่า AI",
    "chat_answer": "การตั้งค่า AI",
    "knowledge": "การตั้งค่า AI",
    "pii": "การตั้งค่า AI",
    "division": "กองและหน่วยงาน",
    "unit": "กองและหน่วยงาน",
    "district": "กองและหน่วยงาน",
    "entry_group": "กองและหน่วยงาน",
    "policy": "กองและหน่วยงาน",
    "routing_word": "กองและหน่วยงาน",
    "division_schedule": "คลังและปฏิทิน",
    "calendar": "คลังและปฏิทิน",
    "holiday": "คลังและปฏิทิน",
    "registry": "ทะเบียนอุปกรณ์",
    "koj": "ชีทและงานติดตาม",
    "sheet_source": "ชีทและงานติดตาม",
    "order": "งานสั่ง",
    "orders": "งานสั่ง",
    "employees": "ทะเบียนพนักงาน",
    "option": "ตัวเลือกระบบ",
    "monitoring": "ตัวเลือกระบบ",
    "question": "ตัวเลือกระบบ",
    "outbox": "ตัวเลือกระบบ",
    "demo_data": "ตัวเลือกระบบ",
}
OTHER = "กิจกรรมอื่น"
LABELS = {
    "staff.login": "เข้าสู่ระบบ",
    "staff.logout": "ออกจากระบบ",
    "staff.login_failed": "เข้าสู่ระบบไม่สำเร็จ",
    "case.viewed": "เปิดดูเรื่อง",
    "case.submit": "ส่งเรื่อง",
    "case.chat_sent": "ส่งข้อความในแชต",
    "case.attachment_added": "แนบไฟล์",
    "cases.exported": "ดาวน์โหลด CSV คิวงาน",
    "cases.problems_exported": "ดาวน์โหลด CSV ปัญหาและข้อเสนอแนะ",
    "audit.exported": "ดาวน์โหลด CSV Log การใช้งาน",
    "role.granted": "ให้สิทธิ์",
    "role.revoked": "ถอนสิทธิ์",
    "authority.granted": "ให้อำนาจอนุมัติ",
    "staff.created": "สร้างบัญชีเจ้าหน้าที่",
    "staff.suspended": "ระงับบัญชีเจ้าหน้าที่",
    "staff.activated": "เปิดใช้บัญชีเจ้าหน้าที่",
    "staff.password_reset": "ตั้งรหัสผ่านใหม่",
    "staff.sessions_revoked": "ให้ออกจากระบบทุกเครื่อง",
    "staff.line_code_issued": "ขอรหัสเชื่อม LINE",
    "staff.line_linked": "เชื่อม LINE",
    "staff.line_unlinked": "เลิกเชื่อม LINE",
    "staff.away_set": "ตั้งวันไม่อยู่",
    "requester.messages_viewed": "เปิดดูข้อความของผู้ใช้ LINE OA",
    "requester.staff_access": "ตั้งสิทธิ์ให้ผู้ใช้ LINE OA",
    "registration.approved": "รับรองการลงทะเบียน",
    "registration.rejected": "ไม่รับรองการลงทะเบียน",
    "line_menu.published": "เผยแพร่เมนู LINE",
    "line_flow.published": "เผยแพร่ flow LINE",
    "option.changed": "เปลี่ยนตัวเลือกระบบ",
    "ai.version_activated": "เปิดใช้รุ่นการตั้งค่า AI",
    "employees.imported": "นำเข้าทะเบียนพนักงาน",
}
TARGETS = {"User": "บัญชี", "Requester": "ผู้ใช้ LINE OA", "Case": "เรื่อง"}


def label(action):
    """Q16: the Thai name of an action; never its code."""
    if action in LABELS:
        return LABELS[action]
    group, _, step = action.partition(".")
    if group == "case":
        spec = workflow.ACTIONS.get(step)
        return web_actions.BUTTON_LABELS.get(step) or (spec.label if spec else GROUPS["case"])
    return GROUPS.get(group, OTHER)


def thai_time(moment):
    """31 ต.ค. 2569 18:07 น. in Bangkok time."""
    local = timezone.localtime(moment)
    return f"{local.day} {MONTHS[local.month - 1]} {local.year + 543} {local:%H:%M} น."


# --- The page ---------------------------------------------------------------------------------


def _accounts():
    return (
        get_user_model()
        .objects.filter(staff_profile__isnull=False)
        .exclude(username=identity.SYSTEM_USERNAME)
        .select_related("staff_profile")
        .order_by("staff_profile__display_name", "username")
    )


def _kinds():
    names = sorted(set(GROUPS.values()))
    return [(LOGIN, "เข้าและออกจากระบบ"), *((n, n) for n in names)]


def _chosen(query):
    chosen = {}
    account = query.get("account", "")
    if account.isdigit() and _accounts().filter(pk=account).exists():
        chosen["account"] = account
    if query.get("kind") in dict(_kinds()):
        chosen["kind"] = query["kind"]
    for key in ("from", "to"):
        try:
            chosen[key] = date.fromisoformat(query.get(key) or "").isoformat()
        except ValueError:
            pass
    return chosen


def _events(chosen):
    """Activities of staff accounts and failed sign-ins, newest first."""
    events = AuditEvent.objects.filter(
        Q(actor__staff_profile__isnull=False) | Q(action="staff.login_failed")
    ).exclude(actor__username=identity.SYSTEM_USERNAME)
    if "account" in chosen:
        events = events.filter(
            Q(actor_id=chosen["account"])
            | Q(action="staff.login_failed", target_type="User", target_id=chosen["account"])
        )
    kind = chosen.get("kind")
    if kind == LOGIN:
        events = events.filter(action__in=SIGN_IN)
    elif kind:
        prefixes = [p for p, name in GROUPS.items() if name == kind]
        condition = Q(pk__in=[])
        for prefix in prefixes:
            condition |= Q(action__startswith=f"{prefix}.")
        events = events.filter(condition).exclude(action__in=SIGN_IN)
    if "from" in chosen:
        events = events.filter(created_at__date__gte=chosen["from"])
    if "to" in chosen:
        events = events.filter(created_at__date__lte=chosen["to"])
    return events.select_related("actor__staff_profile").order_by("-pk")


def _name(user):
    profile = getattr(user, "staff_profile", None)
    return (profile.display_name if profile else "") or user.username


def _rows(events):
    """One row per event, with the case number or person it concerns."""
    ids = {}
    for event in events:
        ids.setdefault(event.target_type, set()).add(event.target_id)
    numbers = {
        str(pk): number
        for pk, number in Case.objects.filter(
            pk__in=[i for i in ids.get("Case", ()) if len(i) == 36]
        ).values_list("pk", "number")
    }
    users = {
        str(u.pk): _name(u)
        for u in get_user_model()
        .objects.filter(pk__in=[i for i in ids.get("User", ()) if i.isdigit()])
        .select_related("staff_profile")
    }
    requesters = {
        str(pk): name
        for pk, name in Requester.objects.filter(
            pk__in=[i for i in ids.get("Requester", ()) if i.isdigit()]
        ).values_list("pk", "display_name")
    }
    rows = []
    for event in events:
        known = {"Case": numbers, "User": users, "Requester": requesters}.get(event.target_type)
        about = known.get(event.target_id, "") if known is not None else ""
        if about and event.target_type != "Case":
            about = f"{TARGETS[event.target_type]} {about}"
        data = event.data if isinstance(event.data, dict) else {}
        who = _name(event.actor) if event.actor else data.get("username", "")
        rows.append(
            {
                "event": event,
                "when": thai_time(event.created_at),
                "who": who,
                "username": event.actor.username if event.actor else data.get("username", ""),
                "what": label(event.action),
                "about": about,
                "case": numbers.get(event.target_id) if event.target_type == "Case" else None,
                "why": event.reason,
                "ip": data.get("ip", ""),
                "browser": data.get("browser", ""),
            }
        )
    return rows


@admin_required
@require_safe
def admin_log(request):
    chosen = _chosen(request.GET)
    page = Paginator(_events(chosen), PER_PAGE).get_page(request.GET.get("page"))
    return render(
        request,
        "operations/admin_log.html",
        {
            "page": page,
            "rows": _rows(page),
            "chosen": chosen,
            "accounts": [(str(u.pk), f"{_name(u)} ({u.username})") for u in _accounts()],
            "kinds": _kinds(),
            "query": urlencode(chosen),
            "page_query": f"{urlencode(chosen)}&" if chosen else "",
        },
    )


HEADER = (
    "วันเวลา",
    "บัญชี",
    "ชื่อผู้ใช้",
    "กิจกรรม",
    "เกี่ยวข้องกับ",
    "เหตุผล",
    "IP address",
    "เบราว์เซอร์",
)


@admin_required
@require_safe
def admin_log_csv(request):
    """Q17: the rows the filters match, formula-safe, with a BOM for Excel, and recorded."""
    chosen = _chosen(request.GET)
    limit = settings.EXPORT_MAX_ROWS
    events = list(_events(chosen)[: limit + 1])
    truncated = len(events) > limit
    rows = _rows(events[:limit])
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(HEADER)
    for row in rows:
        values = (
            row["when"],
            row["who"],
            row["username"],
            row["what"],
            row["about"],
            row["why"],
            row["ip"],
            row["browser"],
        )
        writer.writerow([reports._cell(v) for v in values])
    if truncated:
        writer.writerow([f"ตัดที่ {limit:,} แถว ใช้ตัวกรองให้แคบลงเพื่อส่งออกส่วนที่เหลือ"])
    identity.audit(
        request.user,
        "audit.exported",
        request.user,
        filters=chosen,
        rows=len(rows),
        truncated=truncated,
    )
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    response = HttpResponse("﻿" + buffer.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="faoj-log-{stamp}.csv"'
    response["Cache-Control"] = "private, no-store"
    return response
