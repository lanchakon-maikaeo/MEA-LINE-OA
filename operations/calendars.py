"""ปฏิทินนัดของกอง (grill กคว. 27 ต.ค. 2569 Q13, Q14, Q25, Q26, Q34).

Staff of the division open slots when it suits them, and a calendar with default times has those
free on every working day (ADR 0036) until booked or closed. In LINE the requester picks a free
slot on a card per day; the request holds the slot until staff confirm, move or reject it, and an
unconfirmed request counts as taken. Staff decide on the web; the requester hears of the
decision through the case chat (ADR 0024), never a push.
"""

from datetime import datetime, time, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from operations import identity
from operations.models import Appointment, Calendar, CalendarSlot, Holiday

A = Appointment.Status
DAYS = ("จ.", "อ.", "พ.", "พฤ.", "ศ.", "ส.", "อา.")
DAY_NAMES = ("จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์")
MONTH_NAMES = (
    "มกราคม",
    "กุมภาพันธ์",
    "มีนาคม",
    "เมษายน",
    "พฤษภาคม",
    "มิถุนายน",
    "กรกฎาคม",
    "สิงหาคม",
    "กันยายน",
    "ตุลาคม",
    "พฤศจิกายน",
    "ธันวาคม",
)
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
MAX_SHOWN = 12
BOOKABLE_DAYS = 10  # ADR 0036 Q16, Q24: ten working days, from the next working day
CARD_TIMES = 8  # Q16: at most eight times on a day's card


def _open(calendar=None):
    """Open slots anyone may book: a time staff picked for one appointment never is (ADR 0055)."""
    slots = CalendarSlot.objects.filter(is_open=True, calendar__is_active=True, private=False)
    if calendar is not None:
        slots = slots.filter(calendar=calendar)
    return slots.annotate(
        taken=Count("appointments", filter=Q(appointments__status__in=Appointment.HOLDING))
    )


def free_slots(key, now=None):
    """The next free slots of the calendar with this key (an empty list when there is none)."""
    calendar = Calendar.objects.filter(key=key, is_active=True).first()
    if calendar is None:
        return []
    now = now or timezone.now()
    release_expired(now)
    fill_defaults(calendar, bookable_days(now))
    slots = _open(calendar).filter(starts_at__gt=now, taken__lt=F("capacity"))
    return list(slots.order_by("starts_at")[:MAX_SHOWN])


def working_days(first, count):
    """count days from first on that are neither a Saturday, a Sunday nor a วันหยุด."""
    off = set(holidays(first, first + timedelta(days=count * 3 + 30)))
    found, day = [], first
    while len(found) < count:
        if day.weekday() < 5 and day not in off:
            found.append(day)
        day += timedelta(days=1)
    return found


def bookable_days(now=None):
    """ADR 0036 Q24: the days a requester may book, from the next working day on."""
    return working_days(timezone.localdate(now) + timedelta(days=1), BOOKABLE_DAYS)


def _starts(day, text):
    hour, minute = (int(x) for x in text.split(":"))
    return timezone.make_aware(datetime(day.year, day.month, day.day, hour, minute))


@transaction.atomic
def fill_defaults(calendar, days):
    """ADR 0036 Q8, Q22: each default time of the calendar is a slot on these days. A time that
    already has a slot, a closed one included, is left as it is, so a closing stays."""
    if not calendar.default_times or not days:
        return
    list(Calendar.objects.select_for_update().filter(pk=calendar.pk).values_list("pk"))
    now = timezone.now()
    starts = [_starts(day, text) for day in days for text in calendar.default_times]
    have = set(
        CalendarSlot.objects.filter(calendar=calendar, starts_at__in=starts).values_list(
            "starts_at", flat=True
        )
    )
    CalendarSlot.objects.bulk_create(
        [
            CalendarSlot(
                calendar=calendar,
                starts_at=start,
                ends_at=start + timedelta(minutes=calendar.default_minutes),
                capacity=calendar.default_capacity,
                note="ช่วงตั้งต้น",
            )
            for start in starts
            if start not in have and start > now
        ]
    )


def fill_range(calendar, first, last):
    """The default times on the working days from first to last (the staff month)."""
    first = max(first, timezone.localdate() + timedelta(days=1))
    if not calendar.default_times or first > last:
        return
    off = holidays(first, last)
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    fill_defaults(calendar, [d for d in days if d.weekday() < 5 and d not in off])


def day_cards(key, now=None):
    """[(day, [(slot, free)])] for the booking cards (ADR 0036 Q16, Q17): the open slots of the
    bookable days, at most CARD_TIMES a day; a day with no open slot is left out."""
    calendar = Calendar.objects.filter(key=key, is_active=True).first()
    if calendar is None:
        return []
    now = now or timezone.now()
    release_expired(now)
    days = bookable_days(now)
    fill_defaults(calendar, days)
    tomorrow = timezone.localdate(now) + timedelta(days=1)  # a slot staff open on a Saturday shows
    begin = timezone.make_aware(datetime.combine(tomorrow, time.min))
    end = timezone.make_aware(datetime.combine(days[-1] + timedelta(days=1), time.min))
    slots = _open(calendar).filter(starts_at__gt=now, starts_at__gte=begin, starts_at__lt=end)
    found = {}
    for slot in slots.order_by("starts_at"):
        found.setdefault(timezone.localtime(slot.starts_at).date(), []).append(
            (slot, slot.taken < slot.capacity)
        )
    return [(day, found[day][:CARD_TIMES]) for day in sorted(found)][:12]  # LINE: 12 cards


def day_full(day):
    """A card's head: จันทร์ 2 พ.ย. 2569."""
    return f"{DAY_NAMES[day.weekday()]} {day.day} {MONTHS[day.month - 1]} {day.year + 543}"


def day_short(day):
    """A quick reply of a day: จ. 2 พ.ย."""
    return f"{DAYS[day.weekday()]} {day.day} {MONTHS[day.month - 1]}"


def clock(slot):
    return f"{timezone.localtime(slot.starts_at):%H:%M}"


def drop_request(draft):
    """The draft's slot request, when it asks for a call back instead (ADR 0036 Q19)."""
    Appointment.objects.filter(draft=draft, case__isnull=True, status=A.REQUESTED).update(
        status=A.CANCELLED, decided_at=timezone.now()
    )


def holidays(first, last):
    """{date: name} of the วันหยุด from first to last."""
    return dict(Holiday.objects.filter(day__gte=first, day__lte=last).values_list("day", "name"))


def day_label(day):
    """How a day is named where people pick days (รอบใหม่ Q11): อังคาร, 20 ต.ค. 69."""
    year = (day.year + 543) % 100
    return f"{DAY_NAMES[day.weekday()]}, {day.day} {MONTHS[day.month - 1]} {year:02d}"


@transaction.atomic
def set_holiday(actor, day, name):
    identity.require_admin(actor)
    name = " ".join(str(name or "").split())[:120]
    if not name:
        raise ValidationError("ใส่ชื่อวันหยุด")
    holiday, _ = Holiday.objects.update_or_create(
        day=day, defaults={"name": name, "created_by": actor}
    )
    identity.audit(actor, "holiday.set", holiday, day=day.isoformat(), name=name)
    return holiday


@transaction.atomic
def remove_holiday(actor, day):
    identity.require_admin(actor)
    holiday = Holiday.objects.filter(day=day).first()
    if holiday is not None:
        identity.audit(actor, "holiday.removed", holiday, day=day.isoformat(), name=holiday.name)
        holiday.delete()


def month(actor, calendar, first):
    """Weeks (Sunday first, as on the requested picture) of the month that holds first, each
    day with its slots and holiday, for the calendar page (รอบใหม่ Q10). `calendar` may be a
    list: the page shows every calendar of the division in one month (28 ต.ค. 2569 Q6)."""
    shown = list(calendar) if isinstance(calendar, (list, tuple)) else [calendar]
    for one in shown:
        require_member(actor, one, unit=True)
    first = first.replace(day=1)
    start = first - timedelta(days=(first.weekday() + 1) % 7)
    end = start + timedelta(days=42)
    for one in shown:  # ADR 0036: the default times show, so staff can close them
        fill_range(one, start, end - timedelta(days=1))
    begin = timezone.make_aware(datetime(start.year, start.month, start.day))
    slots = (
        CalendarSlot.objects.filter(calendar__in=shown)
        .annotate(
            taken=Count("appointments", filter=Q(appointments__status__in=Appointment.HOLDING))
        )
        .filter(starts_at__gte=begin, starts_at__lt=begin + timedelta(days=42))
        .select_related("calendar")
        .prefetch_related("appointments__case")
        .order_by("starts_at")
    )
    off = holidays(start, end)
    days = {}
    for slot in slots:
        if slot.private and not slot.taken:  # ADR 0055: shown only as its appointment
            continue
        days.setdefault(timezone.localtime(slot.starts_at).date(), []).append(slot)
    weeks = []
    for w in range(6):
        week_days = []
        for d in range(7):
            day = start + timedelta(days=w * 7 + d)
            week_days.append(
                {
                    "date": day,
                    "in_month": day.month == first.month,
                    "slots": days.get(day, []),
                    "holiday": off.get(day, ""),
                    "label": day_label(day),
                }
            )
        if w >= 4 and not any(x["in_month"] for x in week_days):
            break
        weeks.append(week_days)
    return weeks


def short(slot):
    """A quick reply label of at most 20 characters: "อ. 4 พ.ย. 09:00"."""
    start = timezone.localtime(slot.starts_at)
    return f"{DAYS[start.weekday()]} {start.day} {MONTHS[start.month - 1]} {start:%H:%M}"


def label(slot):
    start, end = timezone.localtime(slot.starts_at), timezone.localtime(slot.ends_at)
    return (
        f"วัน{DAY_NAMES[start.weekday()]}ที่ {start.day} {MONTHS[start.month - 1]} "
        f"{start.year + 543} {start:%H:%M}–{end:%H:%M}"
    )


@transaction.atomic
def hold(slot_id, draft):
    """(slot, "") with the draft's request on it, or (None, why not)."""
    slot = CalendarSlot.objects.select_for_update().filter(pk=slot_id).first()
    if slot is None or not slot.is_open or slot.starts_at <= timezone.now():
        return None, "ช่วงนี้ปิดแล้ว เลือกช่วงอื่น"
    taken = slot.appointments.filter(status__in=Appointment.HOLDING).exclude(draft=draft).count()
    if taken >= slot.capacity:
        return None, "มีคนขอช่วงนี้แล้ว เลือกช่วงอื่น"
    Appointment.objects.filter(draft=draft, case__isnull=True, status=A.REQUESTED).exclude(
        slot=slot
    ).update(status=A.CANCELLED, decided_at=timezone.now())
    Appointment.objects.get_or_create(
        draft=draft, slot=slot, status=A.REQUESTED, defaults={"calendar": slot.calendar}
    )
    return slot, ""


def attach(draft, case):
    """The draft's appointment request belongs to the case it became."""
    Appointment.objects.filter(draft=draft, case__isnull=True).exclude(status=A.CANCELLED).update(
        case=case
    )


HOLD = timedelta(minutes=30)  # grill กคว. รอบใหม่ Q8


def release_expired(now=None):
    """Requests of drafts that were never sent, or not sent within 30 minutes of choosing,
    free their slots (the draft holds it again at its summary if it is still free)."""
    from operations.models import ConversationDraft

    now = now or timezone.now()
    gone = [ConversationDraft.Status.DISCARDED, ConversationDraft.Status.EXPIRED]
    Appointment.objects.filter(case__isnull=True, status=A.REQUESTED).filter(
        Q(draft__status__in=gone) | Q(draft__isnull=True) | Q(requested_at__lt=now - HOLD)
    ).update(status=A.CANCELLED, decided_at=now)


# --- Staff --------------------------------------------------------------------------------------


def require_member(actor, calendar, unit=False):
    """Staff of the calendar's กอง; `unit`: ADR 0052 Q19, its แอดมินแผนก/กลุ่มงาน read it too."""
    scope = identity.staff_scope(actor)
    if identity.can_access_division(scope, calendar.division_id):
        return
    if not (unit and calendar.division_id in scope.unit_divisions):
        raise PermissionDenied


def may_decide(actor, appointment):
    """Staff of the กอง, or (ADR 0052 Q19) a unit admin for an appointment of their case."""
    scope = identity.staff_scope(actor)
    if identity.can_access_division(scope, appointment.calendar.division_id):
        return True
    case = appointment.case
    return bool(
        case
        and case.owner_division_id == appointment.calendar.division_id
        and identity.holds(identity.for_case(scope, case), case.owner_division_id)
    )


def visible(actor):
    scope = identity.staff_scope(actor)
    return identity.filter_by_division(Calendar.objects.filter(is_active=True), scope, "division")


@transaction.atomic
def add_slots(actor, calendar, day, times, minutes, capacity=1, note="", on_holiday=False):
    """Open slots on day at each "HH:MM" of times, each `minutes` long; a holiday needs
    on_holiday (รอบใหม่ Q11: allowed, but asked first)."""
    require_member(actor, calendar)
    holiday = Holiday.objects.filter(day=day).first()
    if holiday is not None and not on_holiday:
        raise ValidationError(
            f"{day_label(day)} เป็นวันหยุด ({holiday.name}) ติ๊กยืนยันเปิดในวันหยุดแล้วกดอีกครั้ง"
        )
    if not 5 <= int(minutes) <= 8 * 60:
        raise ValidationError("ช่วงละ 5 นาทีถึง 8 ชั่วโมง")
    if not 1 <= int(capacity) <= 50:
        raise ValidationError("รับได้ 1 ถึง 50 คนต่อช่วง")
    made = []
    for text in times:
        try:
            hour, minute = (int(x) for x in text.strip().replace(".", ":").split(":"))
            start = timezone.make_aware(datetime(day.year, day.month, day.day, hour, minute))
        except ValueError:
            raise ValidationError(f"เวลา '{text}' ไม่ถูกต้อง ใช้แบบ 09:00") from None
        if start <= timezone.now():
            raise ValidationError(f"เวลา {text} ผ่านไปแล้ว")
        made.append(
            CalendarSlot.objects.create(
                calendar=calendar,
                starts_at=start,
                ends_at=start + timedelta(minutes=int(minutes)),
                capacity=int(capacity),
                note=note[:200],
                created_by=actor,
            )
        )
    identity.audit(actor, "calendar.slots_added", calendar, count=len(made), day=day.isoformat())
    return made


@transaction.atomic
def close_slot(actor, slot):
    require_member(actor, slot.calendar)
    slot.is_open = False
    slot.save(update_fields=["is_open"])
    identity.audit(actor, "calendar.slot_closed", slot.calendar, slot=slot.pk)


@transaction.atomic
def close_day(actor, calendar, day):
    """ADR 0036 Q23: a day off; requests already made stay for staff to move or reject."""
    require_member(actor, calendar)
    fill_range(calendar, day, day)
    begin = timezone.make_aware(datetime.combine(day, time.min))
    end = begin + timedelta(days=1)
    closed = CalendarSlot.objects.filter(
        calendar=calendar, is_open=True, starts_at__gte=begin, starts_at__lt=end
    ).update(is_open=False)
    identity.audit(actor, "calendar.day_closed", calendar, day=day.isoformat(), count=closed)
    return closed


@transaction.atomic
def reopen_slot(actor, slot):
    require_member(actor, slot.calendar)
    slot.is_open = True
    slot.save(update_fields=["is_open"])
    identity.audit(actor, "calendar.slot_reopened", slot.calendar, slot=slot.pk)


def _tell(actor, appointment, text):
    """The decision reaches the requester through the case chat (held until they write)."""
    from operations import case_chat

    if appointment.case_id is not None:
        case_chat.send_system(actor, appointment.case, text)  # no LINE: staff call instead


def free_time(calendar, starts_at, keep=None):
    """ADR 0055: a time staff pick freely for one appointment, as (slot, ends_at, warnings).
    The slot is an open one starting at that very time with room, else None and a private slot
    is made on saving. It must be in the future and clear of every unfinished appointment of
    this calendar that fills an overlapping slot; a weekend, a วันหยุด or a closed time only
    warns."""
    if starts_at <= timezone.now():
        raise ValidationError("เลือกเวลาที่ยังไม่ผ่านไป")
    ends_at = starts_at + timedelta(minutes=calendar.default_minutes or 30)
    holding = Q(appointments__status__in=Appointment.HOLDING)
    if keep is not None:
        holding &= ~Q(appointments__pk=keep.pk)
    overlapping = list(
        CalendarSlot.objects.filter(calendar=calendar, starts_at__lt=ends_at, ends_at__gt=starts_at)
        .annotate(taken=Count("appointments", filter=holding))
        .order_by("starts_at")
    )
    for slot in overlapping:
        if slot.taken and slot.taken >= slot.capacity:
            raise ValidationError(f"ชนกับนัดอื่น {label(slot)}")
    exact = next(
        (
            s
            for s in overlapping
            if s.starts_at == starts_at and s.is_open and not s.private and s.taken < s.capacity
        ),
        None,
    )
    day = timezone.localtime(starts_at).date()
    warnings = []
    if day.weekday() >= 5:
        warnings.append(f"วัน{DAY_NAMES[day.weekday()]}")
    if day in (off := holidays(day, day)):
        warnings.append(f"วันหยุด {off[day]}")
    if any(not s.is_open for s in overlapping):
        warnings.append("ช่วงที่ปิดรับนัดไว้")
    return exact, ends_at, warnings


def _private_slot(actor, calendar, starts_at, keep):
    exact, ends_at, _ = free_time(calendar, starts_at, keep)
    if exact is not None:
        return exact
    return CalendarSlot.objects.create(
        calendar=calendar,
        starts_at=starts_at,
        ends_at=ends_at,
        capacity=1,
        private=True,
        note="เวลาที่เจ้าหน้าที่เลือกให้นัดนี้",
        created_by=actor,
    )


@transaction.atomic
def decide(actor, appointment, action, *, slot=None, method="", note="", via="", at=None):
    """confirm, move (to another slot), reject or done; each tells the requester."""
    appointment = Appointment.objects.select_for_update().get(pk=appointment.pk)
    if not may_decide(actor, appointment):
        raise PermissionDenied
    case = appointment.case
    pickup = appointment.calendar.is_pickup  # ADR 0055: โน้ตบุ๊กและหัวอ่าน keeps its flow
    if pickup and case is not None and case.awaits_acceptance and case.assignee_id == actor.pk:
        raise ValidationError("กดรับงานเรื่องนี้ก่อน แล้วจึงตอบนัดได้")  # ADR 0049 Q9, ADR 0055
    if appointment.status in (A.REJECTED, A.CANCELLED, A.DONE):
        raise ValidationError("นัดนี้จบไปแล้ว")
    note = (note or "").strip()[:500]
    if at is not None and not pickup:
        raise ValidationError("เลือกวันเวลาเองได้เฉพาะนัดรับของ")
    if action == "move" and at is not None:  # ADR 0055: a time picked freely
        list(Calendar.objects.select_for_update().filter(pk=appointment.calendar_id))
        slot = _private_slot(actor, appointment.calendar, at, appointment)
    if action == "move":
        if slot is None or slot.calendar_id != appointment.calendar_id:
            raise ValidationError("เลือกช่วงใหม่ในปฏิทินเดียวกัน")
        taken = slot.appointments.filter(status__in=Appointment.HOLDING).exclude(pk=appointment.pk)
        if taken.count() >= slot.capacity:
            raise ValidationError("ช่วงใหม่มีคนนัดเต็มแล้ว")
        appointment.slot, appointment.status = slot, A.CONFIRMED
        text = f"เปลี่ยนนัดเป็น {label(slot)}"
    elif action == "confirm":
        if appointment.slot is None:
            raise ValidationError("นัดนี้ยังไม่มีช่วงเวลา เลือกช่วงก่อน")
        appointment.status = A.CONFIRMED
        text = f"ยืนยันนัด {label(appointment.slot)}"
    elif action == "reject":
        if not note:
            raise ValidationError("บอกเหตุผลที่ไม่รับนัด")
        appointment.status = A.REJECTED
        text = "ยังรับนัดช่วงที่ขอไม่ได้"
    elif action == "done":
        appointment.status = A.DONE
        text = ""
    else:
        raise ValidationError("ไม่รู้จักรายการนี้")
    if method:
        if method not in Appointment.Method.values:
            raise ValidationError("วิธีรับของไม่ถูกต้อง")
        if not appointment.takes_method:  # ADR 0055: a withdrawal's นัดรับของ only
            raise ValidationError("วิธีรับของใช้กับนัดรับของของเรื่องเบิกเท่านั้น")
        appointment.method = method
        text += f"\nวิธีรับของ: {Appointment.Method(method).label}" if text else ""
    if note:
        appointment.note = note
        text += f"\n{note}" if text else ""
    appointment.decided_at, appointment.decided_by = timezone.now(), actor
    appointment.save()
    identity.audit(
        actor,
        f"appointment.{action}",
        appointment,
        status=appointment.status,
        **({"via": via} if via else {}),
    )
    if text:
        _tell(actor, appointment, text)
    return appointment


def _open_or_closed(calendar):
    return CalendarSlot.objects.filter(calendar=calendar).annotate(
        taken=Count("appointments", filter=Q(appointments__status__in=Appointment.HOLDING))
    )
