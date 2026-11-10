"""ขั้นงานคลังและนัดในการ์ด LINE ของเจ้าหน้าที่ (ADR 0055).

The warehouse stages and the appointment answers of สิ่งที่ทำได้ asked in chat, as a few
questions and one confirm, and saved by warehouse.record and calendars.decide exactly as the
web saves them (the same checks, audit, timeline and messages to the requester, marked ทาง
LINE). They share the "staff_step" prompt of staff_work: its data carries kind "stage" or
"appointment", and staff_work hands every answer, ย้อนกลับ and บันทึก here.
"""

from datetime import date, datetime, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from operations import calendars, case_menu, staff_line, warehouse
from operations.models import Appointment, Case, CaseMilestone

S = CaseMilestone.Stage
GREEN, RED, GREY = "#16804A", "#C62828", "#888888"
ALL = "*"  # ทุกเลขที่เหลือ
DECISIONS = {"confirm": "ยืนยันนัด", "move": "ย้ายนัด", "reject": "ไม่รับนัด"}


def _work():
    from operations import staff_work  # local: staff_work hands its prompt to this module

    return staff_work


# --- Starting -------------------------------------------------------------------------------


def stale(turn, user, case):
    """ADR 0055: a button of an older card for a step that is gone shows the fresh card."""
    turn.set_prompt()
    turn.say(f"{case.number}: ขั้นนี้ทำไม่ได้แล้ว สถานะของเรื่องเปลี่ยนไป นี่คือการ์ดล่าสุด")
    _work().case_detail(turn, user, Case.objects.get(pk=case.pk))


def stage_start(turn, user, case, stage):
    if case_menu.build(user, case).find("stage", stage) is None:
        return stale(turn, user, case)
    turn.set_prompt(
        "staff_step",
        kind="stage",
        case=str(case.pk),
        stage=stage,
        revision=case.revision,
        values={},
    )
    ask(turn, user)


def appointment_start(turn, user, case, appointment):
    item = case_menu.build(user, case).find("appointment", appointment)
    if item is None:
        return stale(turn, user, case)
    if not item.line:
        turn.say(f"{item.label}: ขั้นนี้ทำในเว็บ")  # โน้ตบุ๊กและหัวอ่าน, as before ADR 0055
        return
    turn.set_prompt(
        "staff_step",
        kind="appointment",
        case=str(case.pk),
        appointment=str(appointment),
        revision=case.revision,
        values={},
    )
    ask(turn, user)


def _state(turn, user):
    """(case, menu item, data) of the step in progress; the item is None once it is gone."""
    data = turn.conversation.prompt_data
    case = staff_line._case_for(user, case_id=data.get("case"))
    if case is None:
        return None, None, data
    name = data.get("stage") if data.get("kind") == "stage" else data.get("appointment")
    return case, case_menu.build(user, case).find(data.get("kind"), name), data


# --- The questions --------------------------------------------------------------------------


def _questions(case, data, values):
    """The names asked in order: which number, the day, the pay day (stages); the answer, the
    time, how the goods come, the reason (appointments)."""
    if data.get("kind") == "stage":
        stage = data["stage"]
        asked = []
        if stage == S.KEYED and len(warehouse.payable(case)) > 1 and len(_unkeyed(case)) > 1:
            asked.append("number")
        asked.append("on")
        if stage == S.KEYED and case.kind != Case.Kind.RETURN:
            asked.append("planned_on")
        return asked
    decision = values.get("decision")
    if decision == "reject":
        return ["decision", "note"]
    asked = ["decision"]
    if decision in ("confirm", "move") and _asks_method(case, data):
        asked.append("method")
    return asked


def _unkeyed(case):
    return [number for _, number in warehouse.unkeyed(case)]


def _appointment(case, data):
    return (
        Appointment.objects.select_related("slot", "calendar")
        .filter(pk=data.get("appointment") or 0, case=case)
        .first()
    )


def _asks_method(case, data):
    """How the goods come is asked on the collection calendar of a withdrawal only."""
    appointment = _appointment(case, data)
    return appointment is not None and appointment.takes_method


def _buttons(turn, values):
    out = [turn.button("ยกเลิก", "staff_step_cancel")]
    if values:
        out.insert(0, turn.button("ย้อนกลับ", "staff_step_back"))
    return out


def _picker(turn, label, mode, **limits):
    data = turn.button(label, "staff_step_pick", {"date": "1"})[1]
    return {"type": "datetimepicker", "label": label, "data": data, "mode": mode, **limits}


def ask(turn, user):
    case, item, data = _state(turn, user)
    if case is None:
        turn.set_prompt()
        turn.say("ไม่พบเรื่องนี้ในงานของคุณแล้ว")
        return
    if item is None:
        return stale(turn, user, case)
    values = data.get("values", {})
    for name in _questions(case, data, values):
        if name in values:
            continue
        return _ask_one(turn, user, case, item, data, values, name)
    return _confirm(turn, user, case, item, data, values)


def _ask_one(turn, user, case, item, data, values, name):
    lead = f"{case.number} · {item.label}"
    back = _buttons(turn, values)
    today = timezone.localdate()
    b = turn.button
    if name == "number":
        left = _unkeyed(case)
        picks = [b(f"ทุกเลขที่เหลือ ({len(left)} เลข)", "staff_step_pick", {"v": ALL})]
        picks += [b(n[:20], "staff_step_pick", {"v": n}) for n in left[:10]]
        return turn.say(f"{lead}\nบันทึกเลขไหน", [*picks, *back])
    if name == "on":
        picks = [
            b("วันนี้", "staff_step_pick", {"v": today.isoformat()}),
            _picker(turn, "เลือกวันที่", "date", initial=today.isoformat(), max=today.isoformat()),
        ]
        text = f"{lead}\nบันทึกเป็นวันที่เท่าไร\nย้อนหลังได้ แต่ไม่เกินวันนี้ พิมพ์ วัน/เดือน/ปี ก็ได้"
        return turn.say(text, [*picks, *back])
    if name == "planned_on":
        suggested = _suggested(case, values)
        picks = []
        if suggested is not None:
            label = f"{calendars.day_short(suggested)} ตามตาราง"
            picks.append(b(label, "staff_step_pick", {"v": suggested.isoformat()}))
        picks.append(_picker(turn, "เลือกวันจ่าย", "date", initial=(suggested or today).isoformat()))
        text = f"{lead}\nวันจ่าย (ผู้แจ้งจะเห็นวันนี้)"
        text += (
            f"\nตามตารางกำหนดจ่ายคือ{warehouse.thai_date(suggested)}"
            if suggested
            else "\nตารางกำหนดจ่ายยังไม่มีขนาดนี้ เลือกวันจ่ายเอง"
        )
        return turn.say(text, [*picks, *back])
    if name == "decision":
        return _calendar_card(turn, user, case, item, data, values)
    if name == "method":
        appointment = _appointment(case, data)
        methods = [m for m, _ in Appointment.Method.choices if m]
        methods.sort(key=lambda m: m != (appointment.method if appointment else ""))
        picks = [
            b(warehouse.method_label(case, m)[:20], "staff_step_pick", {"v": m}) for m in methods
        ]
        return turn.say(f"{lead}\nรับของทางไหน", [*picks, *back])
    if name == "note":
        return turn.say(f"{lead}\nพิมพ์เหตุผลที่ไม่รับนัด (ผู้แจ้งจะเห็นข้อความนี้)", back)
    return None


def _suggested(case, values):
    received = warehouse.stages(case).get(S.RECEIVED)
    on = _day(values.get("on")) or timezone.localdate()
    return warehouse.suggested_issue(case, received.on, on) if received else None


def _day(text):
    try:
        return date.fromisoformat(str(text or "")[:10])
    except ValueError:
        return None


def _at(text):
    """A datetime picked in LINE ("2026-11-12T09:30"), in local time."""
    try:
        moment = datetime.fromisoformat(str(text or ""))
    except ValueError:
        return None
    return timezone.make_aware(moment) if timezone.is_naive(moment) else moment


# --- The appointment card ---------------------------------------------------------------------


def _calendar_card(turn, user, case, item, data, values):
    """ADR 0055 (Q8): like the หัวอ่าน booking card. The request first, with ยืนยันนัด and
    ไม่รับนัด; then a card per day, the requested time green, full times grey, any other time
    moves the appointment there; เลือกวันเวลาอื่น picks a time freely."""
    appointment = _appointment(case, data)
    if appointment is None:
        return stale(turn, user, case)
    b = turn.button
    rows = [f"เรื่อง {case.number}", f"ปฏิทิน: {appointment.calendar.name}"]
    rows.append(
        f"ช่วงที่ขอ: {calendars.label(appointment.slot)}"
        if appointment.slot
        else "ช่วงที่ขอ: ยังไม่มีช่วงเวลา เลือกวันเวลาให้"
    )
    if appointment.method and appointment.takes_method:
        rows.append(f"วิธีรับที่ขอ: {warehouse.method_label(case, appointment.method)}")
    if appointment.note:
        rows.append(f"หมายเหตุ: {appointment.note[:200]}")
    actions = []
    if appointment.slot:
        actions.append(_cell(b("ยืนยันนัด", "staff_step_pick", {"v": "confirm"}), GREEN, True))
    actions.append(_cell(b("ไม่รับนัด", "staff_step_pick", {"v": "reject"}), RED, False))
    soon = timezone.localtime(turn.now + timedelta(hours=1)).replace(second=0, microsecond=0)
    picker = _picker(turn, "เลือกวันเวลาอื่น", "datetime", min=f"{soon:%Y-%m-%dT%H:%M}")
    actions.append(
        {"type": "button", "style": "link", "height": "sm", "color": GREEN, "action": picker}
    )
    request = {
        "type": "bubble",
        "size": "kilo",
        "header": _head("คำขอนัด", "#E8F5E9", "#1B5E20"),
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                *({"type": "text", "text": r, "size": "sm", "wrap": True} for r in rows),
                {"type": "separator", "margin": "md"},
                *actions,
            ],
        },
    }
    from operations import conversation  # local: conversation routes to this module

    bubbles = [request, *_day_bubbles(turn, appointment)][:12]
    turn.say(f"{case.number} · {item.label}\nยืนยันช่วงที่ขอ เลือกช่วงอื่นเพื่อย้ายนัด หรือไม่รับนัด")
    quick = [conversation._postback(label, data, label) for label, data in _buttons(turn, values)]
    turn.replies.append(
        {
            "type": "flex",
            "altText": f"นัด{appointment.calendar.name} เรื่อง {case.number}",
            "contents": {"type": "carousel", "contents": bubbles},
            "quickReply": {"items": quick},
        }
    )


def _head(text, background, color):
    return {
        "type": "box",
        "layout": "vertical",
        "paddingAll": "12px",
        "backgroundColor": background,
        "contents": [
            {"type": "text", "text": text, "weight": "bold", "size": "sm", "color": color}
        ],
    }


def _cell(button, color, filled, display=None):
    label, data = button
    return {
        "type": "box",
        "layout": "vertical",
        "height": "40px",
        "justifyContent": "center",
        "cornerRadius": "md",
        "borderWidth": "1px",
        "borderColor": color,
        "backgroundColor": color if filled else "#FFFFFF",
        "action": {
            "type": "postback",
            "label": label[:40],
            "data": data,
            "displayText": display or label,
        },
        "contents": [
            {
                "type": "text",
                "text": label,
                "size": "sm",
                "align": "center",
                "weight": "bold" if filled else "regular",
                "color": "#FFFFFF" if filled else color,
                "wrap": True,
            }
        ],
    }


def _full(text):
    return {
        "type": "box",
        "layout": "vertical",
        "height": "40px",
        "justifyContent": "center",
        "cornerRadius": "md",
        "backgroundColor": "#F2F2F2",
        "contents": [
            {"type": "text", "text": text, "size": "sm", "color": "#AAAAAA", "align": "center"}
        ],
    }


def _day_bubbles(turn, appointment):
    asked = appointment.slot_id
    cards = calendars.day_cards(appointment.calendar.key, turn.now)
    if appointment.slot is not None and not any(
        s.pk == asked for _, times in cards for s, _ in times
    ):
        # The requested time (a private one, or a day past the booking window) on its own day.
        day = timezone.localtime(appointment.slot.starts_at).date()
        cards = sorted([*cards, (day, [(appointment.slot, True)])], key=lambda c: c[0])
    bubbles = []
    for day, times in cards:
        cells = []
        for slot, free in times:
            when = calendars.clock(slot)
            if slot.pk == asked:
                value = {"v": "confirm"}
                cells.append(_cell(turn.button(when, "staff_step_pick", value), GREEN, True))
            elif free:
                value = {"v": f"slot:{slot.pk}"}
                button = turn.button(when, "staff_step_pick", value)
                cells.append(_cell(button, GREEN, False, f"ย้ายนัดไป {calendars.short(slot)}"))
            else:
                cells.append(_full(f"{when} เต็ม"))
        rows = []
        for start in range(0, len(cells), 2):
            pair = cells[start : start + 2]
            rows.append(
                {
                    "type": "box",
                    "layout": "horizontal",
                    "spacing": "sm",
                    "contents": pair + ([{"type": "filler"}] if len(pair) == 1 else []),
                }
            )
        bubbles.append(
            {
                "type": "bubble",
                "size": "kilo",
                "header": _head(calendars.day_full(day), "#F5F5F5", "#333333"),
                "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": rows},
            }
        )
    return bubbles


# --- Answers ----------------------------------------------------------------------------------


def answer(turn, user, value, picked=""):
    """A tap or a typed answer to the question being asked; `picked` is what a LINE picker
    chose (a date, or a date and time)."""
    case, item, data = _state(turn, user)
    if case is None or item is None:
        return ask(turn, user)
    values = dict(data.get("values", {}))
    asking = next((n for n in _questions(case, data, values) if n not in values), None)
    if asking is None:
        return ask(turn, user)
    value = (picked or value or "").strip()
    if asking in ("on", "planned_on"):
        day = _day(value) or _work()._typed_date(value, past=asking == "on")
        if day is None:
            turn.say("เลือกวันที่จากปุ่ม หรือพิมพ์ วัน/เดือน/ปี เช่น 15/11/2569")
            return None
        if asking == "on" and day > timezone.localdate():
            turn.say("วันที่บันทึกต้องไม่เกินวันนี้")
            return None
        value = day.isoformat()
    elif asking == "number":
        if value != ALL and value not in _unkeyed(case):
            turn.say("เลือกเลขจากปุ่ม")
            return None
    elif asking == "decision":
        if picked:
            at = _at(picked)
            appointment = _appointment(case, data)
            try:
                _, _, warnings = calendars.free_time(appointment.calendar, at, appointment)
            except (ValidationError, TypeError, AttributeError) as exc:
                text = " ".join(getattr(exc, "messages", ["เลือกวันเวลาใหม่"]))
                turn.say(f"เลือกเวลานี้ไม่ได้: {text}")
                return None
            values["at"] = at.isoformat()
            value = "move"
        elif value.startswith("slot:"):
            values["slot"] = value[5:]
            value = "move"
        if value not in DECISIONS:
            turn.say("กดเลือกจากการ์ดนัด")
            return None
    elif asking == "method":
        if value not in Appointment.Method.values or not value:
            turn.say("เลือกวิธีรับของจากปุ่ม")
            return None
    elif asking == "note" and not value:
        turn.say("พิมพ์เหตุผลที่ไม่รับนัด")
        return None
    values[asking] = value[:500]
    turn.set_prompt("staff_step", **{**data, "values": values})
    return ask(turn, user)


def back(turn, user):
    case, _, data = _state(turn, user)
    values = dict(data.get("values", {}))
    if case is not None:
        for name in reversed(_questions(case, data, values)):
            if name in values:
                values.pop(name)
                if name == "decision":
                    values.pop("slot", None)
                    values.pop("at", None)
                break
    turn.set_prompt("staff_step", **{**data, "values": values})
    ask(turn, user)


# --- Confirm and save -------------------------------------------------------------------------


def _consequence(case, stage):
    """What the requester will see, or what happens to the case (ADR 0055)."""
    if stage == S.KEYED and case.kind != Case.Kind.RETURN:
        return "ผู้แจ้งจะเห็นวันจ่ายและเลือกวิธีรับของ เมื่อส่งข้อความมาครั้งถัดไป"
    if stage == S.ISSUED and case.kind != Case.Kind.RETURN:
        return "ผู้แจ้งจะเห็นว่าจ่ายแล้ว เมื่อส่งข้อความมาครั้งถัดไป"
    if stage == S.DELIVERED and warehouse.tracking(case):
        return "งานของเรื่องนี้จะจบ แล้วเรื่องรอผู้แจ้งกดยืนยันผลก่อนปิดเรื่อง"
    return ""


def _confirm(turn, user, case, item, data, values):
    rows = [f"โปรดตรวจสอบก่อนบันทึก: {item.label} เรื่อง {case.number}"]
    if data.get("kind") == "stage":
        number = values.get("number")
        if number:
            rows.append(f"เลขที่: {'ทุกเลขที่เหลือ' if number == ALL else number}")
        rows.append(f"วันที่: {warehouse.thai_date(_day(values['on']))}")
        if values.get("planned_on"):
            rows.append(f"วันจ่าย: {warehouse.thai_date(_day(values['planned_on']))}")
        lead = _consequence(case, data["stage"])
    else:
        decision = values["decision"]
        rows[0] = f"โปรดตรวจสอบก่อนบันทึก: {DECISIONS[decision]} เรื่อง {case.number}"
        appointment = _appointment(case, data)
        warnings = []
        if values.get("at"):
            at = _at(values["at"])
            try:
                _, _, warnings = calendars.free_time(appointment.calendar, at, appointment)
            except ValidationError as exc:
                turn.say("เลือกเวลานี้ไม่ได้แล้ว: " + " ".join(exc.messages))
                values.pop("decision")
                values.pop("at")
                turn.set_prompt("staff_step", **{**data, "values": values})
                return ask(turn, user)
            rows.append(f"เวลานัดใหม่: {_when(at)}")
        elif values.get("slot"):
            slot = appointment.calendar.slots.filter(pk=values["slot"]).first()
            rows.append(f"เวลานัดใหม่: {calendars.label(slot) if slot else '-'}")
        elif appointment.slot:
            rows.append(f"เวลานัด: {calendars.label(appointment.slot)}")
        if values.get("method"):
            rows.append(f"วิธีรับของ: {warehouse.method_label(case, values['method'])}")
        if values.get("note"):
            rows.append(f"เหตุผล: {values['note']}")
        rows += [f"โปรดทราบ: เป็น{w}" for w in warnings]
        lead = "ผู้แจ้งจะเห็นข้อความนัดนี้ เมื่อส่งข้อความมาครั้งถัดไป"
    if lead:
        rows.append(lead)
    turn.say(
        "\n".join(rows),
        [
            turn.button("บันทึก", "staff_step_go"),
            turn.button("แก้ไขคำตอบ", "staff_step_back"),
            turn.button("ยกเลิก", "staff_step_cancel"),
        ],
    )
    return None


def _when(at):
    local = timezone.localtime(at)
    return f"{calendars.day_full(local.date())} {local:%H:%M} น."


def go(turn, user):
    case, item, data = _state(turn, user)
    turn.set_prompt()
    if case is None:
        turn.say("ไม่พบเรื่องนี้ในงานของคุณแล้ว")
        return
    if item is None:
        return stale(turn, user, case)
    values = data.get("values", {})
    try:
        if data.get("kind") == "stage":
            number = values.get("number")
            done, skipped = warehouse.record(
                user,
                [case],
                data["stage"],
                _day(values["on"]),
                _day(values.get("planned_on")),
                "",
                [] if number in (None, ALL) else [number],
                via="line",
            )
            if skipped:
                turn.say(f"ไม่สามารถบันทึกได้: {skipped[0][1]}")
                return
        else:
            appointment = _appointment(case, data)
            slot = None
            if values.get("slot"):
                slot = appointment.calendar.slots.filter(pk=values["slot"]).first()
            calendars.decide(
                user,
                appointment,
                values["decision"],
                slot=slot,
                method=values.get("method", ""),
                note=values.get("note", ""),
                via="line",
                at=_at(values["at"]) if values.get("at") else None,
            )
    except (ValidationError, PermissionDenied) as exc:
        text = " ".join(getattr(exc, "messages", ["ไม่มีสิทธิ์ทำรายการนี้"]))
        turn.say(f"ไม่สามารถบันทึกได้: {text}")
        return
    turn.say(f"บันทึกเรียบร้อยแล้ว: {item.label} เรื่อง {case.number}")
    _work().case_detail(turn, user, Case.objects.get(pk=case.pk))
