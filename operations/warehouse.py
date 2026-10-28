"""งานคลังของใบเบิก กคว. (grill กคว. 27 ต.ค. 2569 Q16 ถึง Q20, Q27, Q30, Q31, Q45, Q46).

Staff record the stages of many slips at once: รับใบเบิกแล้ว (วันรับงาน, the day the issue
time starts), คีย์จ่ายแล้ว with the issue day they set, จ่ายแล้ว and เขตได้รับแล้ว. The issue
day suggested is วันรับงาน plus the working days of the slowest row (กำหนดจ่ายตามขนาด), moved to
the next day the division issues. Setting it sends ฟข. one message: the day, the truck day of
its district and a choice to come and collect (Q27, Q46); the choice is the confirmation. จ่ายแล้ว
tells them too. Every message is a held reply (ADR 0024), never a push.

grill กคอ. (28 ต.ค. 2569): a division may send by truck by default (DivisionSchedule.delivery):
with no กำหนดจ่ายตามขนาด the issue day is the district's next truck day after คีย์จ่ายแล้ว
(plus lead_days), the truck is chosen for ฟข. at once and มารับเอง is the one button. A truck
table may name a ฟข. by its abbreviation (ชื่อย่อ ฟข.) until an admin maps it to a district;
a ฟข. with no truck day hears the division's contact instead. Returns are received, not
issued, so their stages offer no delivery; each division may rename its stages.
"""

import re
import secrets
from datetime import date, datetime, time, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from operations import calendars, identity
from operations.models import (
    Appointment,
    Calendar,
    Case,
    CaseMilestone,
    DivisionSchedule,
    PostbackToken,
)

S = CaseMilestone.Stage
DAYS = calendars.DAY_NAMES
PICKUP = "pickup"
MAX_BULK = 300
SPLIT = re.compile(r"[\s,，、;]+")


# --- The schedule --------------------------------------------------------------------------------


def schedule_of(division):
    return DivisionSchedule.objects.filter(division=division).first()


def working_days_after(start, days):
    """Monday to Friday, skipping วันหยุด (รอบใหม่ Q11)."""
    off = calendars.holidays(start, start + timedelta(days=days * 3 + 30))
    day = start
    while days > 0:
        day += timedelta(days=1)
        if day.weekday() < 5 and day not in off:
            days -= 1
    return day


def next_weekday(start, weekdays):
    """The first day on or after start whose weekday is in weekdays and is no holiday (start
    when none are set)."""
    if not weekdays:
        return start
    off = calendars.holidays(start, start + timedelta(days=60))
    for offset in range(60):
        day = start + timedelta(days=offset)
        if day.weekday() in weekdays and day not in off:
            return day
    return start


def sla_days(schedule, case):
    """The working days of the slowest row: a size's own line, else its group's default."""
    if schedule is None or not schedule.sla:
        return None
    group = (case.intake_data or {}).get("group", "")
    if group and not case.lines.exists():
        # A tracking case lists no sizes (รอบใหม่ Q4): the row of its kind of equipment.
        rule = next(
            (r for r in schedule.sla if r.get("group") == group and not r.get("size")), None
        )
        return int(rule["days"]) if rule else None
    found = []
    for line in case.lines.all():
        for row in line.details or []:
            group, size = row.get("group", ""), row.get("size", "")
            rule = next(
                (r for r in schedule.sla if r.get("group") == group and r.get("size") == size),
                None,
            ) or next(
                (r for r in schedule.sla if r.get("group") == group and not r.get("size")), None
            )
            if rule is not None:
                found.append(int(rule["days"]))
    return max(found) if found else None


def suggested_issue(case, received_on, keyed_on=None):
    schedule = schedule_of(case.owner_division)
    days = sla_days(schedule, case)
    if days is None:
        # กคอ. Q40: no กำหนดจ่ายตามขนาด, the goods go on the district's next truck day.
        if schedule is None or schedule.delivery != schedule.Delivery.TRUCK:
            return None
        start = working_days_after(keyed_on or received_on, schedule.lead_days)
        return truck_day(case, start)
    return next_weekday(
        working_days_after(received_on, days), schedule.issue_weekdays if schedule else []
    )


def truck_weekdays(schedule, district):
    """The truck days of a district: by its code, else by its ชื่อย่อ ฟข. (กคอ. Q26)."""
    if schedule is None or district is None:
        return []
    days = schedule.truck_days or {}
    return days.get(district.code) or (days.get(district.short_name) if district.short_name else [])


def unmapped_abbreviations(schedule):
    """Abbreviations of the truck table no district carries yet."""
    from operations.models import District

    known = set(District.objects.exclude(short_name="").values_list("short_name", flat=True))
    codes = set(District.objects.values_list("code", flat=True))
    keys = (schedule.truck_days or {}) if schedule else {}
    return sorted(k for k in keys if k not in codes and k not in known)


def truck_day(case, on_or_after):
    weekdays = truck_weekdays(schedule_of(case.owner_division), case.district)
    return next_weekday(on_or_after, weekdays) if weekdays else None


def stage_label(case, stage):
    """The division's own name of a stage for this kind of case (Q7, Q32), else the default."""
    schedule = schedule_of(case.owner_division)
    names = (schedule.stage_labels or {}).get(case.kind, {}) if schedule else {}
    return names.get(stage) or S(stage).label


def preview(schedule, group, district_code, received_on):
    """Q5's worked example: a slip of the group's every-size row received on a day, and the
    first truck day for that district from the issue day. (None, None) when not set."""
    rule = next(
        (
            r
            for r in (schedule.sla if schedule else [])
            if r.get("group") == group and not r.get("size")
        ),
        None,
    )
    if rule is None:
        return None, None
    issue = next_weekday(
        working_days_after(received_on, int(rule["days"])), schedule.issue_weekdays
    )
    from operations.models import District

    weekdays = truck_weekdays(schedule, District.objects.filter(code=district_code).first())
    return issue, next_weekday(issue, weekdays) if weekdays else None


@transaction.atomic
def save_schedule(actor, division, sla, issue_weekdays, truck_days, reason, **more):
    """กำหนดจ่ายตามขนาด, issue days and truck days; staff of the division, with a reason.
    `more` may set delivery, lead_days, contact and stage_labels (grill กคอ.)."""
    scope = identity.staff_scope(actor)
    if not identity.can_access_division(scope, division.pk):
        raise PermissionDenied
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("ใส่เหตุผลที่แก้ตาราง")
    clean = []
    for row in sla:
        group, size = str(row.get("group", "")).strip(), str(row.get("size", "")).strip()[:40]
        # A row with no days is left out: clearing the days removes it (28 ต.ค. 2569 Q5).
        if not str(row.get("days", "")).strip():
            continue
        if group not in ("meter", "ct", "vt", "set", "other"):
            raise ValidationError("กลุ่มต้องเป็น มิเตอร์ CT VT ชุด หรืออุปกรณ์อื่น")
        try:
            days = int(row.get("days"))
        except (TypeError, ValueError):
            raise ValidationError("จำนวนวันต้องเป็นตัวเลข") from None
        if not 0 <= days <= 120:
            raise ValidationError("จำนวนวันต้องอยู่ระหว่าง 0 ถึง 120")
        clean.append({"group": group, "size": size, "days": days})
    weekdays = sorted({int(d) for d in issue_weekdays if str(d).isdigit() and int(d) < 7})
    trucks = {
        str(code): sorted({int(d) for d in days if str(d).isdigit() and int(d) < 7})
        for code, days in truck_days.items()
        if days
    }
    schedule, _ = DivisionSchedule.objects.get_or_create(division=division)
    # Rows of abbreviations no district carries yet are not on the form: keep them (Q26).
    for key in unmapped_abbreviations(schedule):
        trucks.setdefault(key, schedule.truck_days[key])
    schedule.sla, schedule.issue_weekdays, schedule.truck_days = clean, weekdays, trucks
    if "delivery" in more:
        if more["delivery"] not in DivisionSchedule.Delivery.values:
            raise ValidationError("เลือกวิธีส่งของจากรายการ")
        schedule.delivery = more["delivery"]
    if "lead_days" in more:
        try:
            schedule.lead_days = int(more["lead_days"] or 0)
        except (TypeError, ValueError):
            raise ValidationError("วันเผื่อก่อนวันรถต้องเป็นตัวเลข") from None
        if not 0 <= schedule.lead_days <= 20:
            raise ValidationError("วันเผื่อก่อนวันรถต้องอยู่ระหว่าง 0 ถึง 20")
    if "contact" in more:
        schedule.contact = " ".join(str(more["contact"] or "").split())[:100]
    if "stage_labels" in more:
        labels = {}
        for kind, names in (more["stage_labels"] or {}).items():
            if kind not in (Case.Kind.WITHDRAWAL, Case.Kind.RETURN):
                continue
            kept = {k: " ".join(str(v).split())[:40] for k, v in names.items() if k in S.values}
            labels[kind] = {k: v for k, v in kept.items() if v}
        schedule.stage_labels = {k: v for k, v in labels.items() if v}
    schedule.updated_by = actor
    schedule.save()
    identity.audit(actor, "division_schedule.updated", division, reason, rows=len(clean))
    return schedule


@transaction.atomic
def save_short_names(actor, names, reason):
    """ชื่อย่อ ฟข. of the districts by code; an admin, with a reason. Returns how many changed."""
    from operations.models import District

    identity.require_admin(actor)
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("ใส่เหตุผลที่แก้ชื่อย่อ")
    clean = {}
    for code, name in names.items():
        name = "".join(str(name or "").split())[:16]
        if name and not name.endswith("."):
            name += "."
        clean[code] = name
    given = [n for n in clean.values() if n]
    if len(given) != len(set(given)):
        raise ValidationError("ชื่อย่อซ้ำกัน หนึ่งเขตมีชื่อย่อเดียว")
    changed = 0
    for district in District.objects.filter(code__in=clean):
        if district.short_name != clean[district.code]:
            before = district.short_name
            district.short_name = clean[district.code]
            district.save(update_fields=["short_name"])
            identity.audit(
                actor,
                "district.short_name",
                district,
                reason,
                before=before,
                after=district.short_name,
            )
            changed += 1
    return changed


# --- Stages -------------------------------------------------------------------------------------


def stages(case):
    return {m.stage: m for m in CaseMilestone.objects.filter(case=case)}


def current(case):
    done = stages(case)
    for stage in reversed(CaseMilestone.ORDER):
        if stage in done:
            return done[stage]
    return None


def read_numbers(text):
    return [n for n in SPLIT.split(str(text or "").strip()) if n][: MAX_BULK + 1]


def find_cases(actor, division, numbers):
    """(cases, numbers not found) among the division's open withdrawals and returns: a case
    number, or a slip number as typed."""
    from operations import workflow

    found, missing = [], []
    kinds = [Case.Kind.WITHDRAWAL, Case.Kind.RETURN]
    base = Case.objects.filter(owner_division=division, kind__in=kinds)
    base = base.exclude(status__in=workflow.TERMINAL)
    for number in numbers:
        hit = list(base.filter(number=number.upper())) or list(base.filter(sap_number=number))
        if not hit:
            missing.append(number)
        for case in hit:
            if case not in found:
                found.append(case)
    return found, missing


@transaction.atomic
def record(actor, cases, stage, on, planned_on=None, note=""):
    """Record one stage for many cases; returns (done, skipped [(case, why)])."""
    if stage not in S.values:
        raise ValidationError("ขั้นไม่ถูกต้อง")
    if len(cases) > MAX_BULK:
        raise ValidationError(f"ครั้งละไม่เกิน {MAX_BULK} ใบ")
    if on > timezone.localdate():
        raise ValidationError("วันที่ต้องไม่เกินวันนี้")
    scope = identity.staff_scope(actor)
    order = CaseMilestone.ORDER
    done, skipped = [], []
    for case in cases:
        if not identity.holds(scope, case.owner_division_id) and not scope.is_admin:
            skipped.append((case, "ไม่ใช่งานของกองคุณ"))
            continue
        if case.status == Case.Status.AWAITING_APPROVAL:  # ADR 0025, 28 ต.ค. 2569 Q4
            skipped.append((case, "ยังรออนุมัติ บันทึกขั้นงานคลังได้หลังกองอนุมัติ"))
            continue
        have = stages(case)
        if stage in have:
            skipped.append((case, f"บันทึก{S(stage).label}ไว้แล้ว"))
            continue
        earlier = order[: order.index(stage)]
        missing = [s for s in earlier if s not in have]
        if missing:
            skipped.append((case, f"ยังไม่ได้บันทึก{S(missing[0]).label}"))
            continue
        plan = planned_on
        returning = case.kind == Case.Kind.RETURN  # received, not issued (grill กคอ. Q32)
        if stage == S.KEYED and plan is None and not returning:
            plan = suggested_issue(case, have[S.RECEIVED].on, on)
        if stage == S.KEYED and plan is None and not returning:
            skipped.append((case, "ใส่วันจ่าย (ตารางกำหนดจ่ายยังไม่มีขนาดนี้)"))
            continue
        CaseMilestone.objects.create(
            case=case,
            stage=stage,
            on=on,
            planned_on=plan if stage == S.KEYED else None,
            note=note[:300],
            recorded_by=actor,
        )
        identity.audit(actor, f"case.stage_{stage}", case, number=case.number, on=on.isoformat())
        done.append(case)
        if returning:
            if stage == S.DELIVERED and _close(actor, case):
                _tell(actor, case, f"{case.number} {stage_label(case, stage)} ปิดเรื่องแล้ว")
        elif stage == S.KEYED:
            _offer(actor, case, plan)
        elif stage == S.ISSUED:
            _tell(actor, case, f"จ่ายแล้ว {thai_date(on)}" + _method_text(case))
        elif stage == S.DELIVERED and _close(actor, case):
            _tell(actor, case, f"{case.number} เขตได้รับของแล้ว ปิดเรื่องแล้ว")
    return done, skipped


def _close(actor, case):
    """Q37: a tracking withdrawal or return (no item rows) ends when the district has it."""
    from operations import services, workflow  # local: services builds on its peers

    case = Case.objects.select_for_update().get(pk=case.pk)
    if case.status in workflow.TERMINAL or case.lines.exists() or not case.sap_kind:
        return False
    from_status = case.status
    case.status = Case.Status.CLOSED
    case.revision += 1
    case.updated_at = case.ended_at = timezone.now()
    case.save(update_fields=["status", "revision", "updated_at", "ended_at"])
    scope = identity.staff_scope(actor)
    context = services._Context(label="ปิดเมื่อเขตได้รับของ", user=actor, scope=scope)
    services._record(
        context,
        case,
        "close_on_delivery",
        from_status,
        "เขตได้รับแล้ว (grill กคว. รอบใหม่ Q37)",
        {},
        services.EVENT_DECISION_RECORDED,
        None,
        None,
    )
    from operations import policy  # local: policy reads the database

    policy.after_decision(case, "close_on_delivery", actor_is_staff=True)
    return True


def thai_date(day):
    return f"วัน{DAYS[day.weekday()]}ที่ {day.day} {calendars.MONTHS[day.month - 1]} {day.year + 543}"


def _method_text(case):
    chosen = (
        Appointment.objects.filter(case=case, status__in=Appointment.HOLDING)
        .exclude(method="")
        .first()
    )
    return f"\nวิธีรับ: {method_label(case, chosen.method)}" if chosen else ""


def method_label(case, method):
    """How the goods come, naming the division's truck ("รถ กคว. ไปส่ง")."""
    if method == Appointment.Method.TRUCK:
        return f"รถ {case.owner_division.code} ไปส่ง"
    return Appointment.Method(method).label if method else ""


def _identity(case):
    from operations import case_chat

    return case_chat._identity(case)


def _token(found, action, value):
    """A button for a message sent outside a chat turn (the held reply carries it)."""
    token = PostbackToken.objects.create(
        token=secrets.token_urlsafe(16),
        identity=found,
        action=action,
        value=value,
        expires_at=timezone.now() + timedelta(days=14),
    )
    return f"t={token.token}"


def _current_method(case):
    chosen = Appointment.objects.filter(case=case, status__in=Appointment.HOLDING).first()
    return chosen.method if chosen else ""


def offer_buttons(case, found, issue_on):
    """Quick replies for ฟข.: the truck day of its district, or coming to collect. Where the
    truck is the default, only the way not yet chosen is offered (กคอ. Q16)."""
    buttons = []
    code = case.owner_division.code
    schedule = schedule_of(case.owner_division)
    by_truck = schedule is not None and schedule.delivery == schedule.Delivery.TRUCK
    chosen = _current_method(case) if by_truck else ""
    truck = truck_day(case, issue_on)
    if truck is not None and chosen != Appointment.Method.TRUCK:
        value = {"case": str(case.pk), "method": "truck", "on": truck.isoformat()}
        month = calendars.MONTHS[truck.month - 1]
        label = f"รถส่ง {calendars.DAYS[truck.weekday()]} {truck.day} {month}"
        buttons.append([label, _token(found, "delivery", value), f"รับของทางรถ {thai_date(truck)}"])
    if chosen != Appointment.Method.PICKUP:
        value = {"case": str(case.pk), "method": "pickup"}
        buttons.append(["มารับเอง", _token(found, "delivery", value), f"มารับของเองที่ {code}"])
    return buttons


def _offer(actor, case, issue_on):
    """Q27: the issue day and the ways to get the goods in one message."""
    from operations import case_chat

    found = _identity(case)
    if found is None:
        return
    code = case.owner_division.code
    schedule = schedule_of(case.owner_division)
    truck = truck_day(case, issue_on)
    if schedule is not None and schedule.delivery == schedule.Delivery.TRUCK:
        # กคอ. Q16: the truck is chosen already; มารับเอง is there for whoever cannot wait.
        if truck is not None:
            calendar = pickup_calendar(case.owner_division)
            if calendar is not None:
                _choose(case, calendar, Appointment.Method.TRUCK, note=f"วันส่งรถ {truck}")
            text = f"รถ {code} จะนำของไปส่งถึงเขต{thai_date(truck)}\nถ้าจะมารับเองที่ {code} กดมารับเอง"
        else:
            text = f"กำหนดจ่าย {thai_date(issue_on)}\nยังไม่มีวันรถของเขตนี้"
            if schedule.contact:
                text += f" สอบถามวันส่งได้ที่ {schedule.contact}"
        case_chat.send_system(actor, case, text, offer_buttons(case, found, issue_on))
        return
    text = f"กำหนดจ่าย {thai_date(issue_on)}\nเลือกวิธีรับของ"
    if truck is not None:
        text += f" รถ {code} ไปส่งเขต{thai_date(truck)} หรือมารับเอง"
    else:
        text += f" มารับเองที่ {code} (ยังไม่มีวันส่งรถของเขตนี้)"
    case_chat.send_system(actor, case, text, offer_buttons(case, found, issue_on))


def _tell(actor, case, text):
    from operations import case_chat

    if _identity(case) is not None:
        case_chat.send_system(actor, case, text)


# --- ฟข. chooses in LINE ---------------------------------------------------------------------


def on_choice(turn, token):
    """The tap on รถส่ง… or มารับเอง; มารับเอง then lists the free collection slots."""
    from operations import conversation, services

    value = token.value
    case = (
        services.visible_cases(conversation._actor(turn)).filter(pk=value.get("case")).first()
        if turn.requester
        else None
    )
    if case is None:
        return conversation._menu(turn, "เรื่องนี้ไม่ใช่ของคุณ")
    if stages(case).get(S.ISSUED):
        return conversation._menu(turn, f"{case.number} จ่ายแล้ว เปลี่ยนวิธีรับไม่ได้")
    calendar = pickup_calendar(case.owner_division)
    if calendar is None:
        return conversation._menu(turn, "กองนี้ยังไม่เปิดปฏิทินนัดรับของ")
    method = value.get("method")
    if method == "truck":
        _choose(case, calendar, Appointment.Method.TRUCK, note=f"วันส่งรถ {value.get('on', '')}")
        day = date.fromisoformat(value["on"])
        code = case.owner_division.code
        return conversation._menu(turn, f"{case.number}: รับของทางรถ {code} {thai_date(day)}")
    if method == "pickup" and value.get("slot"):
        slot = calendar.slots.filter(pk=value["slot"]).first()
        holding = slot.appointments.filter(status__in=Appointment.HOLDING) if slot else None
        if slot is None or not slot.is_open or holding.exclude(case=case).count() >= slot.capacity:
            turn.say("ช่วงนี้เต็มหรือปิดแล้ว เลือกช่วงอื่น")
        else:
            _choose(case, calendar, Appointment.Method.PICKUP, slot=slot)
            code = case.owner_division.code
            return conversation._menu(
                turn, f"{case.number}: ขอนัดมารับ {calendars.label(slot)} รอ {code} ยืนยัน"
            )
    keyed = stages(case).get(S.KEYED)
    first_day = keyed.planned_on if keyed and keyed.planned_on else timezone.localdate()
    after = timezone.make_aware(datetime.combine(first_day, time.min))
    slots = [s for s in calendars.free_slots(calendar.key) if s.starts_at >= after]
    code = case.owner_division.code
    if not slots:
        _choose(case, calendar, Appointment.Method.PICKUP)
        return conversation._menu(turn, f"{case.number}: มารับเอง ยังไม่มีช่วงว่าง {code} จะนัดกลับ")
    quick = []
    for slot in slots[:12]:
        value = {"case": str(case.pk), "method": "pickup", "slot": slot.pk}
        label, data = turn.button(calendars.short(slot), "delivery", value)
        quick.append((label, data, calendars.label(slot)))
    turn.say(f"{case.number}: เลือกช่วงที่จะมารับ", quick)


def pickup_calendar(division):
    """The division's นัดรับของ calendar: key "pickup" (กคว.) or "pickup-…" (กคอ.)."""
    return (
        Calendar.objects.filter(division=division, is_active=True, key__startswith=PICKUP)
        .order_by("pk")
        .first()
    )


def _choose(case, calendar, method, slot=None, note=""):
    """One way to get the goods per case: a new choice replaces the earlier one."""
    Appointment.objects.filter(case=case, calendar=calendar, status__in=Appointment.HOLDING).update(
        status=Appointment.Status.CANCELLED, decided_at=timezone.now()
    )
    # The truck day is the division's own offer, so choosing it confirms it (Q46).
    truck = method == Appointment.Method.TRUCK
    status = Appointment.Status.CONFIRMED if truck else Appointment.Status.REQUESTED
    return Appointment.objects.create(
        calendar=calendar, case=case, slot=slot, method=method, status=status, note=note
    )


def status_lines(case):
    """For เรื่องของฉัน: the stage, the issue day and how the goods come."""
    lines = []
    now = current(case)
    if now is not None:
        label = stage_label(case, now.stage)
        lines.append(f"ขั้นงานคลัง: {label} ({now.on:%d/%m}/{now.on.year + 543})")
    keyed = stages(case).get(S.KEYED)
    if keyed and keyed.planned_on and case.kind != Case.Kind.RETURN:
        lines.append(f"กำหนดจ่าย: {thai_date(keyed.planned_on)}")
    method = _method_text(case).strip()
    if method:
        lines.append(method)
    return lines
