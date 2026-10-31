"""งานใน LINE สำหรับเจ้าหน้าที่ (16 Oct 2569 Q8, Q15): the งานของกอง and สั่งงาน tabs in chat.

Every step of a case that needs only typed text or a short list of choices runs here as a few
questions and one confirm; a step with item lines, serials or quantities opens its web form
(ready for LIFF later). Every step goes through services.perform_action or work_orders with the
staff account, so the rules and the audit trail are the web's. Replies name cases and work but
never the requester's text in a push; the details come back only in this 1:1 chat.
"""

import re
from datetime import date

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from operations import identity, options, queues, staff_line, work_orders, workflow
from operations.models import Case, LineMenuItem, WorkOrder

A = LineMenuItem.Action
T = LineMenuItem.Tab
MAX_BUTTONS = 12
CHAT_KINDS = {"text", "textarea", "select", "radio", "number"}
ORDER_NUMBER = re.compile(r"^T\d{2}-\d{6}$")
# LINE buttons hold 20 characters; these steps get a shorter name in chat.
SHORT_LABELS = {
    "approve": "ยืนยันรับเรื่อง",
    "admin_send": "ส่งต่อให้กอง",
    "complete_triage": "คัดกรองเสร็จ",
    "set_priority": "ยืนยันระดับ",
    "request_info": "ขอข้อมูลเพิ่ม",
    "confirm": "บันทึกคำตอบแทน",
    "dispute_resolution": "บันทึกโต้แย้งแทน",
    "decide_inspection": "ตัดสินผลตรวจ",
    "terminate_return": "ยุติส่วนค้างคืน",
}


def web(path):
    return f"{settings.STAFF_WEB_BASE_URL.rstrip('/')}{path}"


def _more(total, shown):
    return f"\n… และอีก {total - shown} รายการในเว็บ" if total > shown else ""


def _order_line(order, today):
    due = ""
    if order.due_on:
        late = order.due_on < today and order.status in WorkOrder.OPEN
        due = f" · {'เกิน' if late else ''}กำหนด {order.due_on.day}/{order.due_on.month}"
    urgent = "ด่วน · " if order.is_urgent else ""
    return f"{order.number} · {urgent}{order.title[:40]} · {order.get_status_display()}{due}"


def _case_line(case):
    owner = case.owner_division.code if case.owner_division_id else ""
    return f"{case.number} · {case.get_kind_display()} · {case.get_status_display()} · {owner}"


# --- The tab buttons ----------------------------------------------------------------------------


def navigation(turn, user):
    """The approved extra menu contains only department-level order creation."""
    quick = []
    if work_orders.may_issue(user):
        quick.append(turn.button("สั่งงาน", "staff_ow_start"))
    turn.say("กรุณาเลือกการดำเนินการ" if quick else "ขณะนี้ไม่มีเมนูเพิ่มเติมสำหรับสิทธิ์ของท่าน", quick)


def handle_item(turn, user, item):
    """A tap on a งานของกอง or สั่งงาน button of the rich menu."""
    if item.action == A.ORDER_NEW and not work_orders.may_issue(user):
        turn.say("การสั่งงานสงวนสิทธิ์สำหรับ Supervisor ระดับฝ่ายเท่านั้น", [web_button()])
        return
    if item.tab == T.ORDER and not identity.staff_scope(user).overseen:
        turn.say("เมนูนี้สงวนสิทธิ์สำหรับ Supervisor เท่านั้น", [web_button()])
        return
    turn.set_prompt()
    {
        A.WORK_NEW: lambda: staff_line.menu(turn, user),
        A.WORK_MINE: lambda: work_mine(turn, user),
        A.WORK_ORDERS: lambda: orders_received(turn, user),
        A.WORK_DUE: lambda: due(turn, user),
        A.WORK_FIND: lambda: find_prompt(turn),
        A.OPEN_WEB: lambda: open_web(turn),
        A.ORDER_NEW: lambda: order_start(turn, user),
        A.ORDER_ISSUED: lambda: orders_issued(turn, user),
        A.ORDER_ASSIGN: lambda: list_page(turn, user, "assign"),
        A.ORDER_LATE: lambda: late(turn, user),
        A.ORDER_TODAY: lambda: today_summary(turn, user),
    }.get(item.action, lambda: staff_line.menu(turn, user))()


def web_button(label="เปิดเว็บ", path="/start/"):
    return (label, web(path))


def open_web(turn):
    turn.say(
        "เปิดเว็บ DED Center สำหรับฟอร์มยาว ไฟล์แนบ และรายงาน ครั้งแรกในแอป LINE ต้องเข้าสู่ระบบด้วยบัญชีเจ้าหน้าที่",
        [web_button(), web_button("งานสั่ง", "/orders/")],
    )


def list_button(turn, label, kind, page=1):
    return turn.button(label, "staff_list", {"kind": kind, "page": page})


def list_page(turn, user, kind="group", page=1):
    from django.core.paginator import Paginator

    if kind == "received":
        from operations import staff_inbox

        return staff_inbox.show(turn, user)
    scope = identity.staff_scope(user)
    cases = queues.visible(user).select_related("owner_division")
    action, key = "staff_case", "case"
    if kind == "mine":
        query, title = cases.filter(assignee=user, status__in=workflow.OPEN), "งานของฉัน"
    elif kind in {"received", "issued", "due_orders"}:
        query = work_orders.tab_orders(
            user, {"received": "mine", "issued": "issued", "due_orders": "due"}[kind]
        )
        title = {"received": "งานสั่งที่ได้รับ", "issued": "งานที่ฉันสั่ง", "due_orders": "งานสั่งใกล้กำหนด"}[
            kind
        ]
        action, key = "staff_order", "order"
    elif kind == "late_orders":
        query = work_orders.visible(user).filter(
            division__in=work_orders.issuable(user),
            status__in=WorkOrder.OPEN,
            due_on__lt=timezone.localdate(),
        )
        title, action, key = "งานสั่งเกินกำหนด", "staff_order", "order"
    elif kind in {"due_cases", "late"}:
        divisions = (
            list(work_orders.issuable(user)) if kind == "late" else list(scope.division_roles)
        )
        query, title = _overdue_cases(user, divisions), "เรื่องเกินกำหนด"
    elif kind == "assign":
        query = cases.filter(
            owner_division__in=work_orders.issuable(user), status__in=workflow.OPEN
        )
        title, action = "เลือกเรื่องที่จะมอบ", "staff_assign_pick"
    else:
        kind = "group"
        query = cases.filter(queues.tab_condition("host", None, user, turn.now))
        title = "งานของกอง"
    query = query.order_by("pk")
    rows = Paginator(query, 5).get_page(page)
    quick = [turn.button(row.number, action, {key: str(row.pk)}) for row in rows]
    for label, number in (("ย้อนกลับ", rows.number - 1), ("ถัดไป", rows.number + 1)):
        if 1 <= number <= rows.paginator.num_pages:
            quick.append(list_button(turn, label, kind, number))
    quick += [list_button(turn, "งานของฉัน", "mine"), list_button(turn, "งานสั่งที่ได้รับ", "received")]
    if scope.overseen:
        quick += [
            list_button(turn, "งานที่ฉันสั่ง", "issued"),
        ]
    if kind in {"due_cases", "late"}:
        quick.append(
            list_button(turn, "งานสั่งใกล้กำหนด", "late_orders" if kind == "late" else "due_orders")
        )
    elif kind == "late_orders":
        quick.append(list_button(turn, "เรื่องเกินกำหนด", "late"))
    elif kind == "group":
        quick.append(turn.button("แจ้งปัญหา", "staff_report"))
    else:
        quick.append(turn.button("หน้าหลัก", "staff_menu"))
    if scope.is_admin:
        from operations.admin_line import nav_button

        quick.append(nav_button(turn, "แอดมิน", "home"))
    lines = [
        _order_line(row, timezone.localdate()) if key == "order" else _case_line(row)
        for row in rows
    ]
    turn.set_prompt()
    turn.say(
        f"{title} {rows.paginator.count} รายการ · หน้า {rows.number}/{rows.paginator.num_pages}\n"
        + "\n".join(lines or ["ยังไม่มีงานสั่ง" if key == "order" else "ยังไม่มีรายการ"]),
        quick,
    )


def work_mine(turn, user):
    return list_page(turn, user, "mine")


def orders_received(turn, user):
    return list_page(turn, user, "received")


def _overdue_cases(user, divisions):
    now = timezone.now()
    cases = queues.visible(user).filter(
        queues.tab_condition("host", None, user, now), owner_division__in=divisions
    )
    return queues.apply_filters(cases, queues.Filters(overdue=True)).select_related(
        "owner_division"
    )


def due(turn, user):
    return list_page(turn, user, "due_cases")


def find_prompt(turn):
    turn.set_prompt("staff_find")
    turn.say("พิมพ์เลขเรื่อง (เช่น W69-000123) เลขงานสั่ง (เช่น T69-000001) หรือเลขใบเบิก/ใบส่งคืน SAP")


def find(turn, user, text):
    number = text.strip().upper()
    if ORDER_NUMBER.fullmatch(number):
        order = work_orders.visible(user).filter(number=number).first()
        if order is not None:
            turn.set_prompt()
            return order_detail(turn, user, order)
    else:
        case = staff_line._case_for(user, number=number) or staff_line._case_for(
            user, sap_number=text.strip()
        )
        if case is not None:
            turn.set_prompt()
            return case_detail(turn, user, case)
    turn.say("ไม่พบเลขเรื่อง งานสั่ง หรือเลข SAP ในสิทธิ์ กรุณาระบุเลขใหม่หรือเลือกเมนูอื่น")


# --- Supervisors --------------------------------------------------------------------------------


def orders_issued(turn, user):
    return list_page(turn, user, "issued")


def late(turn, user):
    return list_page(turn, user, "late_orders")


def today_summary(turn, user):
    """สรุปวันนี้ of the supervised divisions: cases in, closed, open and overdue; งานสั่ง."""
    divisions = list(work_orders.issuable(user))
    now = timezone.now()
    start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    visible = queues.visible(user)
    rows = [f"สรุปวันนี้ {timezone.localdate().day}/{timezone.localdate().month}"]
    for division in divisions:
        own = visible.filter(owner_division=division)
        new = own.filter(submitted_at__gte=start).count()
        closed = own.filter(ended_at__gte=start).count()
        open_now = own.filter(status__in=workflow.OPEN).count()
        overdue = _overdue_cases(user, [division.pk]).count()
        if new or closed or open_now:
            rows.append(
                f"{division.code}: เข้าใหม่ {new} · ปิด {closed} · ค้าง {open_now}"
                + (f" · เกินกรอบเวลา {overdue}" if overdue else "")
            )
    orders = WorkOrder.objects.filter(division__in=divisions)
    rows.append(
        f"งานสั่ง: ค้าง {orders.filter(status__in=WorkOrder.OPEN).count()} · "
        f"เสร็จวันนี้ {orders.filter(done_at__gte=start).count()}"
    )
    turn.say("\n".join(rows), [web_button("ดูรายงานในเว็บ", "/console/")])


def assign_prompt(turn):
    turn.set_prompt("staff_assign")
    turn.say("พิมพ์เลขเรื่องที่จะมอบให้คนในกอง (เช่น P69-000045)")


def assign_case(turn, user, text):
    case = staff_line._case_for(user, number=text.strip().upper())
    if case is None:
        turn.say("ไม่พบเรื่องนี้ในกองที่รับผิดชอบ กรุณาระบุเลขเรื่องใหม่")
        return
    turn.set_prompt()
    step_start(turn, user, case, "assign")


# --- งานสั่ง in chat ------------------------------------------------------------------------------


def order_detail(turn, user, order):
    today = timezone.localdate()
    rows = [
        f"งานสั่ง {order.number} · {order.get_status_display()}"
        + (" · ด่วน" if order.is_urgent else ""),
        order.title,
        f"ถึง {order.division.code}"
        + (f" · {work_orders.name_of(order.assignee)}" if order.assignee_id else " · ทั้งกอง"),
        f"สั่งโดย {work_orders.name_of(order.issued_by)}",
    ]
    if order.due_on:
        late = order.due_on < today and order.status in WorkOrder.OPEN
        rows.append(f"{'เกินกำหนด' if late else 'กำหนด'} {order.due_on.day}/{order.due_on.month}")
    if order.detail:
        rows.append(order.detail[:300])
    if order.result:
        rows.append(f"ผลการทำ: {order.result[:300]}")
    if order.reason and order.status in (WorkOrder.Status.SENT_BACK, WorkOrder.Status.CANCELLED):
        rows.append(f"เหตุผล: {order.reason[:300]}")
    buttons = []
    for name, label in work_orders.steps_for(user, order):
        if name == "reassign":
            buttons.append(("สั่งใหม่ในเว็บ", web(f"/orders/{order.number}/")))
        else:
            buttons.append(turn.button(label, "staff_os", {"order": str(order.pk), "step": name}))
    buttons.append(("เปิดในเว็บ", web(f"/orders/{order.number}/")))
    from operations import staff_inbox

    staff_inbox.detail(turn, rows, [*buttons[:11], turn.button("กลับรายการ", "staff_inbox")])


def order_step(
    turn,
    user,
    order,
    step,
    text="",
    person=None,
    skip_reason=False,
    page=1,
    division=None,
    confirmed=False,
):
    """One step of a งานสั่ง; steps that need words, a person or a division ask first."""
    from django.core.exceptions import PermissionDenied, ValidationError

    if (
        step in ("done", "send_back", "cancel")
        and not text
        and not (skip_reason and step != "done")
    ):
        turn.set_prompt("staff_os_text", order=str(order.pk), step=step)
        turn.say(
            "พิมพ์สั้น ๆ ว่าทำอะไรไปแล้ว"
            if step == "done"
            else "พิมพ์เหตุผลที่ส่งกลับ"
            if step == "send_back"
            else "พิมพ์เหตุผลที่ยกเลิก",
            (
                [
                    turn.button(
                        "ไม่ระบุเหตุผล",
                        "staff_os",
                        {"order": str(order.pk), "step": step, "skip_reason": True},
                    )
                ]
                if step != "done"
                else []
            )
            + [turn.button("กลับงาน", "staff_order", {"order": str(order.pk)})],
        )
        return
    if step == "assign_person" and person is None:
        from django.core.paginator import Paginator

        if step not in dict(work_orders.steps_for(user, order)):
            turn.say("ทำขั้นนี้ไม่ได้แล้ว อาจมีคนทำไปก่อน")
            return
        people = Paginator(work_orders.members(order.division_id), 5).get_page(page)
        quick = [
            turn.button(
                f"{p.pk} {work_orders.name_of(p)}"[:20],
                "staff_os",
                {"order": str(order.pk), "step": step, "user": p.pk},
            )
            for p in people
        ]
        for label, number in (("ย้อนกลับ", people.number - 1), ("ถัดไป", people.number + 1)):
            if 1 <= number <= people.paginator.num_pages:
                quick.append(
                    turn.button(
                        label, "staff_os", {"order": str(order.pk), "step": step, "page": number}
                    )
                )
        quick.append(turn.button("กลับงาน", "staff_order", {"order": str(order.pk)}))
        turn.say(
            f"มอบงาน {order.number} ให้ใครใน {order.division.code} · "
            f"หน้า {people.number}/{people.paginator.num_pages}",
            quick,
        )
        return
    if step == "forward" and not confirmed:
        _forward_ask(turn, user, order, division, page)
        return
    try:
        order = work_orders.act(
            user,
            order,
            step,
            reason=text if step in ("send_back", "cancel") else "",
            result=text if step == "done" else "",
            assignee=person,
            division=division,
        )
    except PermissionDenied:
        turn.set_prompt()
        turn.say("ทำขั้นนี้ไม่ได้แล้ว อาจมีคนทำไปก่อน")
        return
    except ValidationError as exc:
        turn.say("; ".join(exc.messages))
        return
    turn.set_prompt()
    order_detail(turn, user, order)


def _forward_ask(turn, user, order, division, page):
    """ADR 0040 Q2: the department picks the division, then confirms before the order moves."""
    from django.core.paginator import Paginator

    if "forward" not in dict(work_orders.steps_for(user, order)):
        turn.say("ทำขั้นนี้ไม่ได้แล้ว อาจมีคนทำไปก่อน")
        return
    key = {"order": str(order.pk), "step": "forward"}
    back = turn.button("กลับงาน", "staff_order", {"order": str(order.pk)})
    if division is not None:
        turn.say(
            f"ยืนยันส่งต่องาน {order.number} จาก {order.division.code} ไป {division.code}\n"
            "งานจะไม่มีผู้รับผิดชอบจนกว่ากองปลายทางจะมอบหมาย",
            [
                turn.button(
                    "ยืนยันส่งต่อ", "staff_os", {**key, "division": division.pk, "confirm": True}
                ),
                back,
            ],
        )
        return
    rows = Paginator(work_orders.issuable(user).exclude(pk=order.division_id), 9).get_page(page)
    quick = [turn.button(d.code[:20], "staff_os", {**key, "division": d.pk}) for d in rows]
    for label, number in (("ย้อนกลับ", rows.number - 1), ("ถัดไป", rows.number + 1)):
        if 1 <= number <= rows.paginator.num_pages:
            quick.append(turn.button(label, "staff_os", {**key, "page": number}))
    quick.append(back)
    turn.say(
        f"ส่งต่องาน {order.number} ให้กองใด · หน้า {rows.number}/{rows.paginator.num_pages}", quick
    )


# Thai calendar date parser retained for callers of staff_work._typed_date.
TYPED_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$")


def order_start(turn, user, division=None):
    from operations import staff_orders_chat

    staff_orders_chat.start(turn, user)


def _typed_date(text):
    match = TYPED_DATE.match(text.strip())
    if not match:
        return None
    day, month, given = int(match.group(1)), int(match.group(2)), match.group(3)
    today = timezone.localdate()
    if given:
        year = int(given)
        year = year + 2500 if year < 100 else year  # 69 → 2569
        year = year - 543 if year > 2400 else year
    else:
        year = today.year
    try:
        chosen = date(year, month, day)
        if not given and chosen < today:
            chosen = date(year + 1, month, day)  # 5/1 typed in December means next year
    except ValueError:
        return None
    return chosen if chosen >= today else None


# --- Case steps in chat ---------------------------------------------------------------------------


class _Post(dict):
    """What payload_from reads: the answers as a posted form."""

    def get(self, key, default=None):
        value = super().get(key, default)
        return value[0] if isinstance(value, list) else value

    def getlist(self, key):
        value = super().get(key, [])
        return value if isinstance(value, list) else [value]


def _askable(form):
    """The required fields the chat asks (optional notes stay for the web), or None when the
    step needs its web form: item lines, splits, files, or a long list to choose from."""
    fields = []
    for f in form.fields:
        if f.ui_only or f.kind == "hidden":
            continue
        if f.kind == "checks" and f.name == "confirm_irreversible":
            continue
        if f.kind not in CHAT_KINDS:
            return None
        if f.kind in ("select", "radio") and len(f.choices) > 10 and f.required:
            return None
        if f.required:
            fields.append(f)
    return fields


def _form(user, case, action):
    from operations import web_actions

    st = web_actions.situation(user, case)
    offers = {o.name: o for o in web_actions.available(user, case, st)}
    if action not in offers:
        return None, None, None
    return web_actions.build_form(action, st, {}), offers[action], st


def case_detail(turn, user, case):
    """The case and every step this person may take: in chat when it fits, else its web form."""
    from operations import web_actions

    level = case.triage.effective_priority if hasattr(case, "triage") else ""
    rows = [
        f"เรื่อง {case.number} · {case.get_kind_display()}",
        f"สถานะ: {case.get_status_display()} · ระดับ{queues.level_name(level)}",
        f"กอง {case.owner_division.code}"
        + (f" · ผู้รับผิดชอบ {work_orders.name_of(case.assignee)}" if case.assignee else ""),
    ]
    from operations import typed_lines

    if case.sap_number and typed_lines.sap_label(case.kind, case.topic):
        rows.append(f"{typed_lines.sap_label(case.kind, case.topic)}: {case.sap_number}")
    for line in case.lines.select_related("item"):
        for row in line.details:
            unit = line.item.unit if typed_lines.is_shared(line.item) else ""
            rows.append(f"· {typed_lines.label_of(row['group'])} {typed_lines.describe(row, unit)}")
    text = (case.description or case.purpose or case.return_reason or "").strip()
    if text:
        rows.append(text[:300])
    buttons = []
    if options.get(options.STAFF_LINE_ACTIONS) == "act":
        st = web_actions.situation(user, case)
        offers = web_actions.available(user, case, st)
        priority = {"approve": 0, "admin_send": 1, "assign": 2}
        offers.sort(key=lambda offer: priority.get(offer.name, 3))
        for offer in offers[:10]:
            form = web_actions.build_form(offer.name, st, {})
            label = SHORT_LABELS.get(offer.name, offer.label)
            if _askable(form) is not None:
                buttons.append(
                    turn.button(label, "staff_step", {"case": str(case.pk), "action": offer.name})
                )
            else:
                # A long form opens on the web; the label says so when it fits.
                path = reverse("case-action", args=[case.number, offer.name])
                shown = f"{label} (เว็บ)" if len(label) <= 14 else label
                buttons.append((shown, web(path)))
    buttons.append(("เปิดในเว็บ", web(f"/cases/{case.number}/")))
    from operations import staff_inbox

    staff_inbox.detail(turn, rows, [*buttons[:11], turn.button("กลับรายการ", "staff_inbox")])


def step_start(turn, user, case, action):
    if options.get(options.STAFF_LINE_ACTIONS) != "act":
        turn.say("ขั้นนี้ทำในเว็บ", [("เปิดในเว็บ", web(f"/cases/{case.number}/"))])
        return
    form, offer, _ = _form(user, case, action)
    if form is None:
        turn.say("ตอนนี้คุณทำขั้นนี้ในเรื่องนี้ไม่ได้")
        return
    if _askable(form) is None:
        path = reverse("case-action", args=[case.number, action])
        turn.say(f"{offer.label} ใช้ฟอร์มในเว็บ", [("เปิดฟอร์ม", web(path))])
        return
    turn.set_prompt(
        "staff_step", case=str(case.pk), action=action, revision=case.revision, values={}
    )
    if not _askable(form) and not offer.irreversible:
        step_go(turn, user)  # nothing to ask and nothing to undo: one tap does it
        return
    _ask_next(turn, user)


def _state(turn, user):
    data = turn.conversation.prompt_data
    case = staff_line._case_for(user, case_id=data.get("case"))
    if case is None:
        return None, None, None, data
    form, offer, _ = _form(user, case, data.get("action"))
    return case, form, offer, data


def _ask_next(turn, user):
    case, form, offer, data = _state(turn, user)
    if form is None:
        turn.set_prompt()
        turn.say("ขั้นนี้ทำไม่ได้แล้ว เรื่องอาจเปลี่ยนไป")
        return
    values = data.get("values", {})
    for f in _askable(form):
        if f.name in values:
            continue
        lead = f"{case.number} · {offer.label}\n{f.label}" + (f"\n{f.help}" if f.help else "")
        skip = [turn.button("ยกเลิก", "staff_step_cancel")]
        if values:
            skip.insert(0, turn.button("ย้อนกลับ", "staff_step_back"))
        if f.kind in ("select", "radio"):
            choices = sorted(f.choices, key=lambda c: str(c[0]) != str(f.initial or ""))
            turn.say(
                lead,
                [
                    turn.button(str(label), "staff_step_pick", {"v": str(value)})
                    for value, label in choices
                    if str(value) != ""
                ][:MAX_BUTTONS]
                + skip,
            )
        else:
            turn.say(lead + "\nพิมพ์คำตอบ", skip)
        return
    rows = [f"ตรวจก่อนบันทึก: {offer.label} เรื่อง {case.number}"]
    for f in _askable(form):
        value = values.get(f.name, "")
        if value:
            shown = dict((str(v), lbl) for v, lbl in f.choices).get(value, value)
            rows.append(f"{f.label}: {shown}")
    if offer.irreversible:
        rows.append("ขั้นนี้ย้อนกลับไม่ได้")
    turn.say(
        "\n".join(rows),
        [
            turn.button("บันทึก", "staff_step_go"),
            turn.button("แก้คำตอบ", "staff_step_back"),
            turn.button("ยกเลิก", "staff_step_cancel"),
        ],
    )


def step_back(turn, user):
    case, form, _, data = _state(turn, user)
    if form is None:
        turn.set_prompt()
        return list_page(turn, user)
    values = dict(data.get("values", {}))
    for field in reversed(_askable(form)):
        if field.name in values:
            values.pop(field.name)
            break
    turn.set_prompt("staff_step", **{**data, "values": values})
    _ask_next(turn, user)


def step_answer(turn, user, value):
    """An answer to the field being asked (typed text or a choice)."""
    case, form, _, data = _state(turn, user)
    if form is None:
        turn.set_prompt()
        turn.say("ขั้นนี้ทำไม่ได้แล้ว เรื่องอาจเปลี่ยนไป")
        return
    values = dict(data.get("values", {}))
    for f in _askable(form):
        if f.name not in values:
            if f.required and not value:
                turn.say("ข้อนี้ต้องตอบ")
                return
            values[f.name] = value[:2000]
            break
    turn.set_prompt("staff_step", **{**data, "values": values})
    _ask_next(turn, user)


def step_go(turn, user):
    from operations import services, wording
    from operations.contracts import Actor, CommandFailed

    case, form, offer, data = _state(turn, user)
    turn.set_prompt()
    if form is None:
        turn.say("ขั้นนี้ทำไม่ได้แล้ว เรื่องอาจเปลี่ยนไป")
        return
    post = _Post({k: v for k, v in data.get("values", {}).items() if v})
    if offer.irreversible:
        post["confirm_irreversible"] = "1"
    try:
        from operations import web_actions

        payload = web_actions.payload_from(form, post)
        services.perform_form_action(
            Actor.staff(user),
            case.pk,
            offer.name,
            expected_revision=data.get("revision"),
            idempotency_key=f"line-staff-{turn.inbox.pk}",
            payload=payload,
        )
    except CommandFailed as exc:
        turn.say(f"บันทึกไม่ได้: {wording.thai(exc.message) or exc.message or exc.code}")
        return
    except Exception:  # noqa: BLE001 - a bad answer must not break the chat
        turn.say("บันทึกไม่ได้ ลองทำในเว็บ", [("เปิดในเว็บ", web(f"/cases/{case.number}/"))])
        return
    turn.say(f"บันทึกแล้ว: {offer.label} เรื่อง {case.number}")
    case_detail(turn, user, Case.objects.get(pk=case.pk))
