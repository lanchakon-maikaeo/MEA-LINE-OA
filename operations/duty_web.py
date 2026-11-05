"""ตารางเวร page (ADR 0048): the month at a glance, one day's exceptions, and the weekly rows."""

from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from operations import calendars, duty
from operations.models import DutyShift


def _day(value):
    try:
        return date.fromisoformat(value or "")
    except ValueError:
        return None


def _name(user):
    profile = getattr(user, "staff_profile", None)
    return profile.display_name if profile else user.username


def _chips(rows, linked=None):
    return [
        {
            "pk": s.pk,
            "time": f"{s.start:%H:%M}–{s.end:%H:%M}",
            "name": _name(s.user),
            # a person who left LINE stays in the roster but is not counted on duty
            "unlinked": linked is not None and s.user_id not in linked,
        }
        for s in sorted(rows, key=lambda s: (s.start, s.pk))
        if s.user_id  # a day's "nobody on duty" mark has no person
    ]


@login_required
def admin_duty(request):
    if not duty.may_edit(request.user):
        raise PermissionDenied
    today = timezone.localdate()
    picked = _day(request.GET.get("day") or request.POST.get("day"))
    start = (_day(request.GET.get("month")) or picked or today).replace(day=1)
    if request.method == "POST":
        action = request.POST.get("action")
        weekday = request.POST.get("weekday", "")
        try:
            if action == "add":
                duty.add(
                    request.user,
                    request.POST.get("start"),
                    request.POST.get("end"),
                    request.POST.getlist("user"),
                    weekday=int(weekday) if weekday.isdigit() else None,
                    on=picked if not weekday else None,
                )
            elif action == "remove":
                duty.remove(request.user, int(request.POST.get("shift") or 0))
            elif action == "nobody" and picked:
                duty.nobody(request.user, picked)
            elif action == "reset" and picked:
                duty.reset(request.user, picked)
            else:
                raise ValidationError("ไม่รู้จักคำสั่งนี้")
            messages.success(request, "บันทึกตารางเวรแล้ว")
        except (ValidationError, ValueError) as exc:
            messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))
        query = f"?month={start.isoformat()}" + (f"&day={picked.isoformat()}" if picked else "")
        return redirect(reverse("admin-duty") + query + ("#day" if picked else "#week"))

    first = start - timedelta(days=(start.weekday() + 1) % 7)
    last = first + timedelta(days=41)
    off = calendars.holidays(first, last)
    exceptions = {}
    for s in DutyShift.objects.filter(on__range=(first, last)).select_related(
        "user__staff_profile"
    ):
        exceptions.setdefault(s.on, []).append(s)
    weekly = {}
    for s in DutyShift.objects.filter(weekday__isnull=False).select_related("user__staff_profile"):
        weekly.setdefault(s.weekday, []).append(s)
    admins = duty.admins()
    linked = {u.pk for u in admins}

    def cell(d):
        rows = exceptions.get(d)
        usual = weekly.get(DutyShift.HOLIDAY if d in off else d.weekday(), [])
        return {
            "date": d,
            "in_month": d.month == start.month,
            "holiday": off.get(d, ""),
            "label": calendars.day_label(d),
            "exception": rows is not None,
            "chips": _chips(rows if rows is not None else usual, linked),
        }

    weeks = [[cell(first + timedelta(days=w * 7 + i)) for i in range(7)] for w in range(6)]
    weeks = [w for n, w in enumerate(weeks) if n < 5 or any(d["in_month"] for d in w)]
    return render(
        request,
        "operations/admin_duty.html",
        {
            "weeks": weeks,
            "weekdays": ["อา.", "จ.", "อ.", "พ.", "พฤ.", "ศ.", "ส."],
            "month": start,
            "month_label": f"{calendars.MONTH_NAMES[start.month - 1]} {start.year + 543}",
            "previous": (start - timedelta(days=1)).replace(day=1),
            "next": (start + timedelta(days=32)).replace(day=1),
            "today": today,
            "picked": cell(picked) if picked else None,
            "week": [
                (i, label, _chips(weekly.get(i, []), linked))
                for i, label in enumerate(duty.WEEKDAYS)
            ],
            "admins": [(u.pk, _name(u)) for u in admins],
            "unlinked": [_name(u) for u in duty.admins(linked=False)],
            "now_on": [_name(u) for u in duty.on_duty()],
            "default_start": duty.DEFAULT_START,
            "default_end": duty.DEFAULT_END,
        },
    )
