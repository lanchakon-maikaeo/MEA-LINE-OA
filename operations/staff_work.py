"""งานใน LINE สำหรับเจ้าหน้าที่ (16 Oct 2569 Q8, Q15): the งานของกอง and สั่งงาน tabs in chat.

Every step of a case that needs only typed text or a short list of choices runs here as a few
questions and one confirm; a step with item lines, serials or quantities opens its web form
(ready for LIFF later). Every step goes through services.perform_action or work_orders with the
staff account, so the rules and the audit trail are the web's. Replies name cases and work but
never the requester's text in a push; the details come back only in this 1:1 chat.
"""

import re
from datetime import date, timedelta

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
    "complete_triage": "คัดกรองเสร็จ",
    "set_priority": "ยืนยันระดับ",
    "dispute_resolution": "บันทึกโต้แย้งแทนเขต",
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


def handle_item(turn, user, item):
    """A tap on a งานของกอง or สั่งงาน button of the rich menu."""
    if item.tab == T.ORDER and not work_orders.may_issue(user):
        turn.say("เมนูสั่งงานใช้ได้เฉพาะ Supervisor", [web_button()])
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
        A.ORDER_ASSIGN: lambda: assign_prompt(turn),
        A.ORDER_LATE: lambda: late(turn, user),
        A.ORDER_TODAY: lambda: today_summary(turn, user),
    }.get(item.action, lambda: staff_line.menu(turn, user))()


def web_button(label="เปิดเว็บ", path="/start/"):
    return (label, web(path))


def open_web(turn):
    turn.say(
        "เปิดเว็บระบบรับเรื่อง ฝอจ. สำหรับฟอร์มยาว ไฟล์แนบ และรายงาน "
        "ครั้งแรกในแอป LINE ต้องเข้าสู่ระบบด้วยบัญชีเจ้าหน้าที่",
        [web_button(), web_button("งานสั่ง", "/orders/")],
    )


def work_mine(turn, user):
    cases = (
        queues.visible(user)
        .filter(assignee=user, status__in=workflow.OPEN)
        .select_related("owner_division")
        .order_by("submitted_at")
    )
    total = cases.count()
    if not total:
        turn.say("ยังไม่มีเรื่องที่มอบให้คุณ", [web_button()])
        return
    shown = list(cases[:10])
    turn.say(
        f"เรื่องที่มอบให้คุณ {total} เรื่อง\n"
        + "\n".join(_case_line(c) for c in shown)
        + _more(total, len(shown)),
        [turn.button(c.number, "staff_case", {"case": str(c.pk)}) for c in shown],
    )


def orders_received(turn, user):
    orders = work_orders.tab_orders(user, "mine")
    total = orders.count()
    if not total:
        turn.say("ยังไม่มีงานสั่งถึงคุณหรือกองของคุณ")
        return
    today = timezone.localdate()
    shown = list(orders[:10])
    turn.say(
        f"งานสั่งที่ได้รับ {total} งาน\n"
        + "\n".join(_order_line(o, today) for o in shown)
        + _more(total, len(shown)),
        [turn.button(o.number, "staff_order", {"order": str(o.pk)}) for o in shown],
    )


def _overdue_cases(user, divisions):
    now = timezone.now()
    cases = queues.visible(user).filter(
        queues.tab_condition("host", None, user, now), owner_division__in=divisions
    )
    return queues.apply_filters(cases, queues.Filters(overdue=True)).select_related(
        "owner_division"
    )


def due(turn, user):
    """ใกล้หรือเกินกำหนด: overdue cases of the person's divisions and งานสั่ง near its date."""
    scope = identity.staff_scope(user)
    cases = _overdue_cases(user, list(scope.division_roles))
    orders = work_orders.tab_orders(user, "due")
    if not cases.exists() and not orders.exists():
        turn.say("ไม่มีเรื่องหรืองานสั่งที่ใกล้หรือเกินกำหนด")
        return
    today = timezone.localdate()
    shown_cases, shown_orders = list(cases[:6]), list(orders[:6])
    rows = [f"เรื่องเกินกรอบเวลา {cases.count()} เรื่อง"]
    rows += [_case_line(c) for c in shown_cases]
    rows += [f"งานสั่งใกล้หรือเกินกำหนด {orders.count()} งาน"]
    rows += [_order_line(o, today) for o in shown_orders]
    buttons = [turn.button(c.number, "staff_case", {"case": str(c.pk)}) for c in shown_cases]
    buttons += [turn.button(o.number, "staff_order", {"order": str(o.pk)}) for o in shown_orders]
    turn.say("\n".join(rows), buttons)


def find_prompt(turn):
    turn.set_prompt("staff_find")
    turn.say("พิมพ์เลขเรื่อง (เช่น W69-000123) หรือเลขงานสั่ง (เช่น T69-000001)")


def find(turn, user, text):
    number = text.strip().upper()
    if ORDER_NUMBER.fullmatch(number):
        order = work_orders.visible(user).filter(number=number).first()
        if order is not None:
            turn.set_prompt()
            return order_detail(turn, user, order)
    else:
        case = staff_line._case_for(user, number=number)
        if case is not None:
            turn.set_prompt()
            return case_detail(turn, user, case)
    turn.say("ไม่พบเลขนี้ในงานของคุณ ลองพิมพ์ใหม่ หรือกดเมนูอื่น")


# --- Supervisors --------------------------------------------------------------------------------


def orders_issued(turn, user):
    orders = work_orders.tab_orders(user, "issued")
    total = orders.count()
    if not total:
        turn.say("ยังไม่มีงานที่คุณสั่งค้างอยู่", [turn.button("สั่งงานใหม่", "staff_ow_start")])
        return
    today = timezone.localdate()
    shown = list(orders[:10])
    done = sum(1 for o in orders if o.status == WorkOrder.Status.DONE)
    head = f"งานที่คุณสั่ง {total} งาน" + (f" · รอคุณปิดงาน {done}" if done else "")
    turn.say(
        head + "\n" + "\n".join(_order_line(o, today) for o in shown) + _more(total, len(shown)),
        [turn.button(o.number, "staff_order", {"order": str(o.pk)}) for o in shown],
    )


def late(turn, user):
    """เกินกำหนดทุกกองที่ดูแล: overdue cases and งานสั่ง per supervised division."""
    scope = identity.staff_scope(user)
    divisions = list(work_orders.issuable(user))
    today = timezone.localdate()
    rows, buttons = [], []
    for division in divisions:
        cases = _overdue_cases(user, [division.pk]).count()
        orders = WorkOrder.objects.filter(
            division=division, status__in=WorkOrder.OPEN, due_on__lt=today
        ).count()
        if cases or orders:
            rows.append(f"{division.code}: เรื่องเกินกรอบเวลา {cases} · งานสั่งเกินกำหนด {orders}")
    late_orders = WorkOrder.objects.filter(
        division__in=list(scope.overseen), status__in=WorkOrder.OPEN, due_on__lt=today
    ).order_by("due_on")[:6]
    buttons += [turn.button(o.number, "staff_order", {"order": str(o.pk)}) for o in late_orders]
    if not rows:
        turn.say("ไม่มีงานเกินกำหนดในกองที่คุณดูแล")
        return
    turn.say("งานเกินกำหนดในกองที่ดูแล\n" + "\n".join(rows), [*buttons, web_button()])


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
        turn.say("ไม่พบเรื่องนี้ในกองที่คุณดูแล ลองพิมพ์เลขใหม่")
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
    turn.say("\n".join(rows), buttons)


def order_step(turn, user, order, step, text="", person=None):
    """One step of a งานสั่ง; steps that need words or a person ask first."""
    from django.core.exceptions import PermissionDenied, ValidationError

    if step in ("done", "send_back", "cancel") and not text:
        turn.set_prompt("staff_os_text", order=str(order.pk), step=step)
        turn.say(
            "พิมพ์สั้น ๆ ว่าทำอะไรไปแล้ว"
            if step == "done"
            else "พิมพ์เหตุผลที่ส่งกลับ"
            if step == "send_back"
            else "พิมพ์เหตุผลที่ยกเลิก"
        )
        return
    if step == "assign_person" and person is None:
        people = work_orders.members(order.division_id)[:MAX_BUTTONS]
        turn.say(
            f"มอบงาน {order.number} ให้ใครใน {order.division.code}",
            [
                turn.button(
                    work_orders.name_of(p),
                    "staff_os",
                    {"order": str(order.pk), "step": step, "user": p.pk},
                )
                for p in people
            ],
        )
        return
    try:
        order = work_orders.act(
            user,
            order,
            step,
            reason=text if step in ("send_back", "cancel") else "",
            result=text if step == "done" else "",
            assignee=person,
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


# The new-order questions: division, person, what, due date, urgent, then confirm.
DUE_CHOICES = (("", "ไม่กำหนด"), ("1", "พรุ่งนี้"), ("3", "ภายใน 3 วัน"), ("7", "ภายใน 7 วัน"))
TYPED_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$")


def order_start(turn, user, division=None):
    divisions = list(work_orders.issuable(user))
    if not divisions:
        turn.say("คุณยังไม่ได้เป็น Supervisor ของกองใด")
        return
    if division is None and len(divisions) == 1:
        division = divisions[0]
    if division is None:
        turn.set_prompt("staff_ow", step="division")
        turn.say(
            "สั่งงานถึงกองไหน",
            [turn.button(d.code, "staff_ow", {"division": d.pk}) for d in divisions[:MAX_BUTTONS]],
        )
        return
    people = work_orders.members(division.pk)[: MAX_BUTTONS - 1]
    turn.set_prompt("staff_ow", step="person", division=division.pk)
    turn.say(
        f"สั่งถึงทั้ง {division.code} หรือคนไหน",
        [turn.button("ทั้งกอง", "staff_ow", {"person": 0})]
        + [turn.button(work_orders.name_of(p), "staff_ow", {"person": p.pk}) for p in people],
    )


def order_answer(turn, user, value=None, text=""):
    """The next answer of the new-order questions, from a button `value` or typed `text`."""
    from django.contrib.auth import get_user_model
    from django.core.exceptions import PermissionDenied, ValidationError

    data = dict(turn.conversation.prompt_data)
    step = data.get("step")
    value = value or {}
    if "division" in value:
        division = work_orders.issuable(user).filter(pk=value["division"]).first()
        if division is None:
            return order_start(turn, user)
        return order_start(turn, user, division)
    if step == "person" and "person" in value:
        data.update(step="title", person=value["person"] or None)
        turn.set_prompt("staff_ow", **data)
        turn.say("พิมพ์ว่าให้ทำอะไร (สั้น ๆ ไม่เกิน 200 ตัวอักษร)")
        return
    if step == "title" and text:
        data.update(step="due", title=text[: work_orders.TITLE_MAX])
        turn.set_prompt("staff_ow", **data)
        turn.say(
            "กำหนดเสร็จเมื่อไร (หรือพิมพ์วันที่ เช่น 25/10)",
            [turn.button(label, "staff_ow", {"due": days}) for days, label in DUE_CHOICES],
        )
        return
    if step == "due" and ("due" in value or text):
        due_on = None
        if "due" in value and value["due"]:
            due_on = timezone.localdate() + timedelta(days=int(value["due"]))
        elif text:
            due_on = _typed_date(text)
            if due_on is None:
                turn.say("อ่านวันที่ไม่ออก พิมพ์แบบ 25/10 หรือกดปุ่ม")
                return
        data.update(step="urgent", due=due_on.isoformat() if due_on else "")
        turn.set_prompt("staff_ow", **data)
        turn.say(
            "งานด่วนไหม",
            [
                turn.button("ปกติ", "staff_ow", {"urgent": 0}),
                turn.button("ด่วน", "staff_ow", {"urgent": 1}),
            ],
        )
        return
    if step == "urgent" and "urgent" in value:
        data.update(step="confirm", urgent=bool(value["urgent"]))
        turn.set_prompt("staff_ow", **data)
        return _order_summary(turn, user, data)
    if step == "confirm" and value.get("go"):
        User = get_user_model()
        division = work_orders.issuable(user).filter(pk=data.get("division")).first()
        person = User.objects.filter(pk=data["person"]).first() if data.get("person") else None
        try:
            order = work_orders.create(
                user,
                data.get("title", ""),
                division,
                person,
                due_on=date.fromisoformat(data["due"]) if data.get("due") else None,
                urgent=data.get("urgent", False),
            )
        except (ValidationError, PermissionDenied) as exc:
            turn.set_prompt()
            messages = getattr(exc, "messages", None) or ["สั่งงานนี้ไม่ได้"]
            turn.say("; ".join(messages))
            return
        turn.set_prompt()
        turn.say(
            f"สั่งงาน {order.number} แล้ว คนทำได้รับข้อความแล้ว",
            [turn.button(order.number, "staff_order", {"order": str(order.pk)})],
        )
        return
    if step == "confirm":
        return _order_summary(turn, user, data)
    turn.say("เลือกจากปุ่ม หรือพิมพ์คำตอบ")


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


def _order_summary(turn, user, data):
    from django.contrib.auth import get_user_model

    division = work_orders.issuable(user).filter(pk=data.get("division")).first()
    person = get_user_model().objects.filter(pk=data.get("person") or 0).first()
    due_on = date.fromisoformat(data["due"]) if data.get("due") else None
    rows = [
        "ตรวจก่อนสั่งงาน",
        f"ให้ทำ: {data.get('title', '')}",
        f"ถึง: {division.code if division else '-'}"
        + (f" · {work_orders.name_of(person)}" if person else " · ทั้งกอง"),
        f"กำหนด: {due_on.day}/{due_on.month}/{due_on.year + 543}" if due_on else "กำหนด: ไม่กำหนด",
        "งานด่วน" if data.get("urgent") else "งานปกติ",
    ]
    turn.say(
        "\n".join(rows),
        [
            turn.button("สั่งงาน", "staff_ow", {"go": 1}),
            turn.button("เริ่มใหม่", "staff_ow_start"),
            ("แนบไฟล์ในเว็บ", web("/orders/new/")),
        ],
    )


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
        if f.kind in ("select", "radio") and len(f.choices) > MAX_BUTTONS and f.required:
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
    text = (case.description or case.purpose or case.return_reason or "").strip()
    if text:
        rows.append(text[:300])
    buttons = []
    if options.get(options.STAFF_LINE_ACTIONS) == "act":
        st = web_actions.situation(user, case)
        for offer in web_actions.available(user, case, st)[:10]:
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
    turn.say("\n".join(rows), buttons)


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
        skip = []
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
        [turn.button("บันทึก", "staff_step_go"), turn.button("ยกเลิก", "staff_step_cancel")],
    )


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
        services.perform_action(
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
