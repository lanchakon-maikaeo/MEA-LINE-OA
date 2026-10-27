"""ปฏิทินนัดของกอง (grill กคว. 27 ต.ค. 2569 Q13, Q14, Q25, Q26, Q34).

Staff of the division open slots when it suits them. In LINE the requester picks a free slot (a
quick reply per slot); the request holds the slot until staff confirm, move or reject it, and an
unconfirmed request counts as taken. Staff decide on the web; the requester hears of the
decision through the case chat (ADR 0024), never a push.
"""

from datetime import datetime, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from operations import identity
from operations.models import Appointment, Calendar, CalendarSlot

A = Appointment.Status
DAYS = ("จ.", "อ.", "พ.", "พฤ.", "ศ.", "ส.", "อา.")
DAY_NAMES = ("จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์")
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


def _open(calendar=None):
    slots = CalendarSlot.objects.filter(is_open=True, calendar__is_active=True)
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
    slots = _open(calendar).filter(starts_at__gt=now, taken__lt=F("capacity"))
    return list(slots.order_by("starts_at")[:MAX_SHOWN])


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


def release_expired(now=None):
    """Requests of drafts that were never sent free their slots."""
    from operations.models import ConversationDraft

    Appointment.objects.filter(
        case__isnull=True,
        status=A.REQUESTED,
        draft__status__in=[ConversationDraft.Status.DISCARDED, ConversationDraft.Status.EXPIRED],
    ).update(status=A.CANCELLED, decided_at=now or timezone.now())


# --- Staff --------------------------------------------------------------------------------------


def require_member(actor, calendar):
    scope = identity.staff_scope(actor)
    if not identity.can_access_division(scope, calendar.division_id):
        raise PermissionDenied


def visible(actor):
    scope = identity.staff_scope(actor)
    return identity.filter_by_division(Calendar.objects.filter(is_active=True), scope, "division")


@transaction.atomic
def add_slots(actor, calendar, day, times, minutes, capacity=1, note=""):
    """Open slots on day at each "HH:MM" of times, each `minutes` long."""
    require_member(actor, calendar)
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


def _tell(actor, appointment, text):
    """The decision reaches the requester through the case chat (held until they write)."""
    from operations import case_chat

    if appointment.case_id is not None:
        case_chat.send_system(actor, appointment.case, text)  # no LINE: staff call instead


@transaction.atomic
def decide(actor, appointment, action, *, slot=None, method="", note=""):
    """confirm, move (to another slot), reject or done; each tells the requester."""
    appointment = Appointment.objects.select_for_update().get(pk=appointment.pk)
    require_member(actor, appointment.calendar)
    if appointment.status in (A.REJECTED, A.CANCELLED, A.DONE):
        raise ValidationError("นัดนี้จบไปแล้ว")
    note = (note or "").strip()[:500]
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
        appointment.method = method
        text += f"\nวิธีรับของ: {Appointment.Method(method).label}" if text else ""
    if note:
        appointment.note = note
        text += f"\n{note}" if text else ""
    appointment.decided_at, appointment.decided_by = timezone.now(), actor
    appointment.save()
    identity.audit(actor, f"appointment.{action}", appointment, status=appointment.status)
    if text:
        _tell(actor, appointment, text)
    return appointment


def week(actor, calendar, start):
    """Slots of the seven days from start, with their appointments, for the calendar page."""
    require_member(actor, calendar)
    first = timezone.make_aware(datetime(start.year, start.month, start.day))
    slots = (
        _open_or_closed(calendar)
        .filter(starts_at__gte=first, starts_at__lt=first + timedelta(days=7))
        .prefetch_related("appointments__case")
        .order_by("starts_at")
    )
    days = [{"date": start + timedelta(days=i), "slots": []} for i in range(7)]
    for slot in slots:
        days[(timezone.localtime(slot.starts_at).date() - start).days]["slots"].append(slot)
    return days


def _open_or_closed(calendar):
    return CalendarSlot.objects.filter(calendar=calendar).annotate(
        taken=Count("appointments", filter=Q(appointments__status__in=Appointment.HOLDING))
    )
