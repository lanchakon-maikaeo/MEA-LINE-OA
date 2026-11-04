"""ตารางเวร (ADR 0048, grill 4 พ.ย. 2569 Q6–Q8, Q18, Q19, Q22).

Alerts meant for แอดมินฝ่าย go to whoever is on duty now; with nobody on duty they go to every
แอดมินฝ่าย, as before the roster. Duty decides alerts only: everyone still sees and takes cases.
A week has rows for Monday to Sunday and one for วันหยุด (the days in the holiday calendar); a
day may have exceptions on the month calendar, which replace that day's rows. A shift whose end
is not after its start runs past midnight. แอดมินระบบ and แอดมินฝ่าย edit; each change is audited.
"""

from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from operations import identity
from operations.models import AuditEvent, DutyShift, Holiday, Role, RoleGrant

DEFAULT_START, DEFAULT_END = time(7, 30), time(15, 30)  # Q22
WEEKDAYS = ("จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์", "วันหยุด")


def may_edit(user):
    scope = identity.staff_scope(user)
    return scope.is_admin or scope.is_department_supervisor


def _require(user):
    if not may_edit(user):
        raise PermissionDenied


def admins():
    """Every active แอดมินฝ่าย, in name order."""
    ids = RoleGrant.objects.filter(
        role=Role.DEPARTMENT_SUPERVISOR, revoked_at__isnull=True
    ).values_list("user_id", flat=True)
    return list(
        get_user_model()
        .objects.filter(pk__in=ids, is_active=True)
        .select_related("staff_profile")
        .order_by("staff_profile__display_name", "username")
    )


def day_rows(day, holidays=None):
    """The shifts of one day: its exceptions if any, else วันหยุด's or its weekday's rows."""
    rows = [s for s in DutyShift.objects.filter(on=day).select_related("user__staff_profile")]
    if rows:
        return rows, True
    off = day in holidays if holidays is not None else Holiday.objects.filter(day=day).exists()
    weekday = DutyShift.HOLIDAY if off else day.weekday()
    return list(
        DutyShift.objects.filter(weekday=weekday).select_related("user__staff_profile")
    ), False


def _covers(shift, moment, today):
    """Does this shift of `today` cover the local `moment`?"""
    start = datetime.combine(today, shift.start)
    end = datetime.combine(today + timedelta(days=shift.end <= shift.start), shift.end)
    return start <= moment < end


def on_duty(now=None):
    """The แอดมินฝ่าย on duty now (yesterday's overnight shifts included)."""
    moment = timezone.localtime(now or timezone.now()).replace(tzinfo=None)
    allowed = {u.pk: u for u in admins()}
    people = {}
    for day in (moment.date(), moment.date() - timedelta(days=1)):
        rows, _ = day_rows(day)
        for shift in rows:
            if shift.user_id in allowed and _covers(shift, moment, day):
                people[shift.user_id] = allowed[shift.user_id]
    return list(people.values())


def receivers(now=None):
    """Who gets an alert meant for แอดมินฝ่าย: those on duty, else all of them (Q7)."""
    return on_duty(now) or admins()


def _times(start, end):
    try:
        start, end = time.fromisoformat(start), time.fromisoformat(end)
    except (TypeError, ValueError) as exc:
        raise ValidationError("ใส่เวลาเริ่มและเวลาจบ เช่น 07:30") from exc
    if start == end:
        raise ValidationError("เวลาเริ่มกับเวลาจบต้องไม่เท่ากัน")
    return start, end


def _people(user_ids):
    allowed = {u.pk: u for u in admins()}
    people = [allowed[int(i)] for i in user_ids if str(i).isdigit() and int(i) in allowed]
    if not people:
        raise ValidationError("เลือกแอดมินฝ่ายอย่างน้อยหนึ่งคน")
    return people


def _audit(actor, action, subject, **data):
    AuditEvent.objects.create(
        actor=actor, action=action, target_type="DutyShift", target_id=str(subject), data=data
    )


@transaction.atomic
def add(actor, start, end, user_ids, weekday=None, on=None):
    """Add one shift for each person picked, to a weekday row or as an exception on `on`."""
    _require(actor)
    start, end = _times(start, end)
    people = _people(user_ids)
    if on is None and (weekday is None or not 0 <= weekday <= DutyShift.HOLIDAY):
        raise ValidationError("เลือกวัน")
    if on is not None:
        if not DutyShift.objects.filter(on=on).exists():
            # The first exception starts from the day's usual rows, so only the change is typed.
            rows, _ = day_rows(on)
            DutyShift.objects.bulk_create(
                DutyShift(on=on, start=s.start, end=s.end, user=s.user, created_by=actor)
                for s in rows
            )
        DutyShift.objects.filter(on=on, user__isnull=True).delete()
    for user in people:
        DutyShift.objects.get_or_create(
            weekday=None if on else weekday,
            on=on,
            start=start,
            end=end,
            user=user,
            defaults={"created_by": actor},
        )
    _audit(
        actor,
        "duty.added",
        on.isoformat() if on else WEEKDAYS[weekday],
        start=start.strftime("%H:%M"),
        end=end.strftime("%H:%M"),
        people=[u.pk for u in people],
    )


@transaction.atomic
def remove(actor, shift_id):
    """Take one shift off; the last exception of a day leaves that day with nobody on duty."""
    _require(actor)
    shift = DutyShift.objects.select_for_update().filter(pk=shift_id).first()
    if shift is None:
        raise ValidationError("ไม่พบเวรนี้")
    if shift.on and not DutyShift.objects.filter(on=shift.on).exclude(pk=shift.pk).exists():
        DutyShift.objects.create(on=shift.on, start=shift.start, end=shift.end, created_by=actor)
    shift.delete()
    _audit(actor, "duty.removed", shift_id, user=shift.user_id)


@transaction.atomic
def nobody(actor, on):
    """Nobody on duty on this day: alerts go to every แอดมินฝ่าย."""
    _require(actor)
    DutyShift.objects.filter(on=on).delete()
    DutyShift.objects.create(on=on, start=DEFAULT_START, end=DEFAULT_END, created_by=actor)
    _audit(actor, "duty.nobody", on.isoformat())


@transaction.atomic
def reset(actor, on):
    """Drop a day's exceptions: it follows the weekly rows again."""
    _require(actor)
    DutyShift.objects.filter(on=on).delete()
    _audit(actor, "duty.reset", on.isoformat())
