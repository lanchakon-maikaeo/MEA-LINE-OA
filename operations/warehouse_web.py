"""หน้าเว็บงานคลัง ปฏิทินนัด และการคุยในเรื่อง (grill กคว. 27 ต.ค. 2569 Q13 ถึง Q39).

งานคลัง: the division's open slips with their stage, the issue day the schedule suggests, and a
bulk form (scan or paste slip numbers, choose a stage). ตาราง: กำหนดจ่ายตามขนาด, issue days and
truck days, saved with a reason. ปฏิทิน: a week of slots with their requests; staff open slots
and confirm, move or reject requests. The case card posts staff messages to the case chat.
"""

from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from operations import calendars, case_chat, identity, queues, warehouse, workflow
from operations.models import (
    Appointment,
    Calendar,
    CalendarSlot,
    Case,
    CaseMilestone,
    District,
    Division,
    Holiday,
)
from operations.views import admin_required

S = CaseMilestone.Stage
GROUPS = [
    ("meter", "มิเตอร์"),
    ("ct", "CT"),
    ("vt", "VT"),
    ("set", "ชุดมิเตอร์ CT VT"),
    ("other", "อุปกรณ์อื่น"),
]
WEEKDAYS = list(enumerate(calendars.DAY_NAMES[:5]))


def _division(request, code):
    division = Division.objects.filter(code=code, is_active=True).first()
    scope = identity.staff_scope(request.user)
    if division is None or not identity.can_access_division(scope, division.pk):
        raise Http404
    return division


def _day(text, default=None):
    try:
        return date.fromisoformat(text) if text else default
    except ValueError:
        return None


@login_required
def warehouse_page(request, code):
    division = _division(request, code)
    if request.method == "POST":
        numbers = warehouse.read_numbers(request.POST.get("numbers", ""))
        numbers += request.POST.getlist("case")
        stage = request.POST.get("stage", "")
        on = _day(request.POST.get("on"), timezone.localdate())
        planned = _day(request.POST.get("planned_on"))
        if on is None or (request.POST.get("planned_on") and planned is None):
            messages.error(request, "วันที่ไม่ถูกต้อง")
            return redirect("warehouse", code=code)
        cases, missing = warehouse.find_cases(request.user, division, numbers)
        try:
            done, skipped = warehouse.record(
                request.user, cases, stage, on, planned, request.POST.get("note", "")
            )
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return redirect("warehouse", code=code)
        if done:
            messages.success(request, f"บันทึก{S(stage).label} {len(done)} ใบ")
        for case, why in skipped[:20]:
            messages.error(request, f"{case.number} ({case.sap_number or '-'}): {why}")
        if missing:
            messages.error(request, "ไม่พบในงานที่ยังไม่จบ: " + ", ".join(missing[:30]))
        return redirect("warehouse", code=code)
    kinds = [Case.Kind.WITHDRAWAL, Case.Kind.RETURN]
    cases = (
        Case.objects.filter(owner_division=division, kind__in=kinds)
        .exclude(status__in=workflow.TERMINAL)
        .select_related("district")
        .prefetch_related("milestones", "lines", "appointments")
        .order_by("submitted_at")
    )
    stage = request.GET.get("stage", "")
    rows = []
    for case in cases[:500]:
        done = {m.stage: m for m in case.milestones.all()}
        now = next((done[s] for s in reversed(CaseMilestone.ORDER) if s in done), None)
        shown = now.stage if now else "none"
        if stage and stage != shown:
            continue
        keyed = done.get(S.KEYED)
        received = done.get(S.RECEIVED)
        method = next(
            (a for a in case.appointments.all() if a.status in Appointment.HOLDING and a.method),
            None,
        )
        rows.append(
            {
                "case": case,
                "stage": now,
                "planned": keyed.planned_on if keyed else None,
                "suggested": (
                    warehouse.suggested_issue(case, received.on) if received and not keyed else None
                ),
                "method": method,
            }
        )
    return render(
        request,
        "operations/warehouse.html",
        {
            "division": division,
            "rows": rows,
            "stages": S.choices,
            "stage": stage,
            "today": timezone.localdate(),
            "schedule": warehouse.schedule_of(division),
        },
    )


@login_required
def schedule_page(request, code):
    division = _division(request, code)
    districts = list(District.objects.filter(is_active=True).order_by("code"))
    schedule = warehouse.schedule_of(division)
    if request.method == "POST":
        sla = [
            {"group": g, "size": s, "days": d}
            for g, s, d in zip(
                request.POST.getlist("sla_group"),
                request.POST.getlist("sla_size"),
                request.POST.getlist("sla_days"),
                strict=False,
            )
        ]
        trucks = {d.code: request.POST.getlist(f"truck_{d.code}") for d in districts}
        try:
            warehouse.save_schedule(
                request.user,
                division,
                sla,
                request.POST.getlist("issue"),
                trucks,
                request.POST.get("reason", ""),
            )
        except (ValidationError, PermissionDenied) as exc:
            messages.error(request, " ".join(getattr(exc, "messages", ["ไม่มีสิทธิ์แก้ตารางนี้"])))
        else:
            messages.success(request, "บันทึกตารางแล้ว")
        return redirect("warehouse-schedule", code=code)
    sla = list(schedule.sla if schedule else []) + [{"group": "", "size": "", "days": ""}] * 3
    trucks = schedule.truck_days if schedule else {}
    return render(
        request,
        "operations/warehouse_schedule.html",
        {
            "division": division,
            "sla": sla,
            "groups": GROUPS,
            "weekdays": WEEKDAYS,
            "issue": schedule.issue_weekdays if schedule else [],
            "districts": [(d, trucks.get(d.code, [])) for d in districts],
            "schedule": schedule,
        },
    )


@login_required
def calendar_page(request, code):
    division = _division(request, code)
    choices = list(Calendar.objects.filter(division=division, is_active=True))
    if not choices:
        messages.info(request, "กองนี้ยังไม่มีปฏิทินนัด")
        return redirect("division-detail", code=code)
    calendar = next((c for c in choices if c.key == request.GET.get("cal")), choices[0])
    today = timezone.localdate()
    # รอบใหม่ Q10: a month on one page, Sunday first, as on the picture the owner sent.
    start = (_day(request.GET.get("month"), today) or today).replace(day=1)
    if request.method == "POST":
        return _calendar_post(request, code, calendar, start)
    weeks = calendars.month(request.user, calendar, start)
    picked = _day(request.GET.get("day"))
    picked_day = next((d for week in weeks for d in week if picked and d["date"] == picked), None)
    waiting = (
        Appointment.objects.filter(calendar=calendar, status=Appointment.Status.REQUESTED)
        .select_related("slot", "case__district")
        .order_by("requested_at")
    )
    return render(
        request,
        "operations/calendar.html",
        {
            "division": division,
            "calendars": choices,
            "calendar": calendar,
            "weeks": weeks,
            "weekdays": ["อา.", "จ.", "อ.", "พ.", "พฤ.", "ศ.", "ส."],
            "start": start,
            "month_label": f"{calendars.MONTH_NAMES[start.month - 1]} {start.year + 543}",
            "previous": (start - timedelta(days=1)).replace(day=1),
            "next": (start + timedelta(days=32)).replace(day=1),
            "picked": picked_day,
            "waiting": waiting,
            "free": calendars.free_slots(calendar.key),
            "methods": [m for m in Appointment.Method.choices if m[0]],
            "today": today,
        },
    )


def _calendar_post(request, code, calendar, start):
    day = _day(request.POST.get("day"))
    back = f"{reverse('calendar', args=[code])}?cal={calendar.key}&month={start.isoformat()}"
    if day is not None:
        month = day.replace(day=1).isoformat()
        back = f"{reverse('calendar', args=[code])}?cal={calendar.key}&month={month}"
        back += f"&day={day.isoformat()}"
    action = request.POST.get("action", "")
    try:
        if action == "add":
            day = _day(request.POST.get("day"))
            if day is None:
                raise ValidationError("เลือกวันก่อน")
            times = [t for t in request.POST.get("times", "").replace(" ", ",").split(",") if t]
            made = calendars.add_slots(
                request.user,
                calendar,
                day,
                times,
                request.POST.get("minutes") or 60,
                request.POST.get("capacity") or 1,
                request.POST.get("note", ""),
                on_holiday=request.POST.get("on_holiday") == "1",
            )
            messages.success(request, f"เปิด {len(made)} ช่วง")
        elif action == "close":
            slot = get_object_or_404(CalendarSlot, pk=request.POST.get("slot"), calendar=calendar)
            calendars.close_slot(request.user, slot)
            messages.success(request, "ปิดช่วงนี้แล้ว คนที่ขอไว้ยังอยู่")
        elif action in ("confirm", "move", "reject", "done"):
            appointment = get_object_or_404(
                Appointment, pk=request.POST.get("appointment"), calendar=calendar
            )
            slot = None
            if action == "move":
                slot = get_object_or_404(
                    CalendarSlot, pk=request.POST.get("slot"), calendar=calendar
                )
            calendars.decide(
                request.user,
                appointment,
                action,
                slot=slot,
                method=request.POST.get("method", ""),
                note=request.POST.get("note", ""),
            )
            messages.success(request, "บันทึกแล้ว ผู้แจ้งจะเห็นข้อความตอนส่งข้อความมาครั้งถัดไป")
        else:
            raise ValidationError("ไม่รู้จักรายการนี้")
    except (ValidationError, PermissionDenied) as exc:
        messages.error(request, " ".join(getattr(exc, "messages", ["ไม่มีสิทธิ์ทำรายการนี้"])))
    return redirect(back)


@admin_required
def admin_holidays(request):
    """วันหยุด (รอบใหม่ Q11): admins pick days on a month grid; each day is named like
    "อังคาร, 20 ต.ค. 69" where it is picked."""
    today = timezone.localdate()
    start = (_day(request.GET.get("month"), today) or today).replace(day=1)
    if request.method == "POST":
        day = _day(request.POST.get("day"))
        try:
            if day is None:
                raise ValidationError("เลือกวันก่อน")
            if request.POST.get("action") == "remove":
                calendars.remove_holiday(request.user, day)
                messages.success(request, f"ลบวันหยุด {calendars.day_label(day)} แล้ว")
            else:
                calendars.set_holiday(request.user, day, request.POST.get("name", ""))
                messages.success(request, f"ตั้ง {calendars.day_label(day)} เป็นวันหยุดแล้ว")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        month = (day or start).replace(day=1).isoformat()
        return redirect(f"{reverse('admin-holidays')}?month={month}")
    first = start - timedelta(days=(start.weekday() + 1) % 7)
    off = calendars.holidays(first, first + timedelta(days=42))
    weeks = [
        [
            {
                "date": d,
                "in_month": d.month == start.month,
                "holiday": off.get(d, ""),
                "label": calendars.day_label(d),
            }
            for d in (first + timedelta(days=w * 7 + i) for i in range(7))
        ]
        for w in range(6)
    ]
    weeks = [w for n, w in enumerate(weeks) if n < 5 or any(d["in_month"] for d in w)]
    year = Holiday.objects.filter(day__year=start.year).order_by("day")
    return render(
        request,
        "operations/admin_holidays.html",
        {
            "weeks": weeks,
            "weekdays": ["อา.", "จ.", "อ.", "พ.", "พฤ.", "ศ.", "ส."],
            "month_label": f"{calendars.MONTH_NAMES[start.month - 1]} {start.year + 543}",
            "previous": (start - timedelta(days=1)).replace(day=1),
            "next": (start + timedelta(days=32)).replace(day=1),
            "year": [(h, calendars.day_label(h.day)) for h in year],
            "year_label": start.year + 543,
            "today": today,
        },
    )


@admin_required
def admin_emergency(request):
    """ผู้รับแจ้งเรื่องฉุกเฉิน (Q33): per division, the staff told of a new emergency case."""
    from operations.models import EmergencyContact, RoleGrant

    if request.method == "POST":
        division = get_object_or_404(Division, pk=request.POST.get("division"), is_active=True)
        wanted = {int(x) for x in request.POST.getlist("user") if x.isdigit()}
        members = set(
            RoleGrant.objects.filter(division=division, revoked_at__isnull=True).values_list(
                "user_id", flat=True
            )
        )
        wanted &= members
        current = set(
            EmergencyContact.objects.filter(division=division).values_list("user_id", flat=True)
        )
        EmergencyContact.objects.filter(division=division, user_id__in=current - wanted).delete()
        for user_id in wanted - current:
            EmergencyContact.objects.create(
                division=division, user_id=user_id, added_by=request.user
            )
        identity.audit(request.user, "division.emergency_contacts", division, people=sorted(wanted))
        messages.success(request, f"บันทึกผู้รับแจ้งเรื่องฉุกเฉินของ {division.code} แล้ว")
        return redirect("admin-emergency")
    rows = []
    for division in Division.objects.filter(is_active=True).order_by("code"):
        grants = (
            RoleGrant.objects.filter(
                division=division, revoked_at__isnull=True, user__is_active=True
            )
            .select_related("user__staff_profile", "user__line_identity")
            .order_by("user__username")
        )
        chosen = set(
            EmergencyContact.objects.filter(division=division).values_list("user_id", flat=True)
        )
        people, seen = [], set()
        for g in grants:
            if g.user_id in seen:
                continue
            seen.add(g.user_id)
            profile = getattr(g.user, "staff_profile", None)
            line = getattr(g.user, "line_identity", None)
            people.append(
                {
                    "user": g.user,
                    "name": profile.display_name if profile else g.user.username,
                    "role": g.get_role_display(),
                    "line": line is not None,
                    "chosen": g.user_id in chosen,
                }
            )
        rows.append({"division": division, "people": people, "count": len(chosen)})
    from operations import notifications

    return render(
        request,
        "operations/admin_emergency.html",
        {"rows": rows, "budget": notifications.budget_state()},
    )


@login_required
def case_chat_thread(request, number):
    """The chat window's messages, polled every 10 seconds (Q15)."""
    case = queues.visible(request.user).filter(number=number).first()
    if case is None:
        raise Http404
    items = case_chat.thread(case)
    scope = identity.staff_scope(request.user)
    can_chat = identity.can_access_division(scope, case.owner_division_id)
    return render(
        request,
        "operations/_case_chat_thread.html",
        {
            "items": items,
            "last": case_chat.last_from_requester(items),
            "fresh": bool(can_chat and case_chat.fresh_inbox(case)),
        },
    )


@login_required
@require_POST
def case_chat_post(request, number):
    case = queues.visible(request.user).filter(number=number).first()
    if case is None:
        raise Http404
    try:
        message = case_chat.send(
            request.user, case, request.POST.get("text", ""), request.POST.get("contact", "")
        )
    except (ValidationError, PermissionDenied) as exc:
        messages.error(request, " ".join(getattr(exc, "messages", ["ไม่มีสิทธิ์ส่งข้อความในเรื่องนี้"])))
    else:
        if message.status == message.Status.SENT:
            messages.success(request, "ส่งถึงผู้แจ้งแล้ว")
        else:
            messages.success(request, "เก็บข้อความไว้ ผู้แจ้งจะได้รับตอนส่งข้อความมาครั้งถัดไป")
    return redirect(f"{reverse('case-detail', args=[case.number])}#chat")


def _next_stage(case, done):
    for stage in CaseMilestone.ORDER:
        if stage not in done:
            return stage
    return None


@login_required
@require_POST
def case_stage(request, number):
    """Q38 (Q9): the next warehouse stage of one case, from its card."""
    case = queues.visible(request.user).filter(number=number).first()
    if case is None:
        raise Http404
    on = _day(request.POST.get("on"), timezone.localdate())
    planned = _day(request.POST.get("planned_on"))
    if on is None or (request.POST.get("planned_on") and planned is None):
        messages.error(request, "วันที่ไม่ถูกต้อง")
    else:
        try:
            done, skipped = warehouse.record(
                request.user,
                [case],
                request.POST.get("stage", ""),
                on,
                planned,
                request.POST.get("note", ""),
            )
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            for _, why in skipped:
                messages.error(request, why)
            if done:
                messages.success(request, f"บันทึก{S(request.POST['stage']).label}แล้ว")
    return redirect(f"{reverse('case-detail', args=[case.number])}#s-warehouse")


@login_required
@require_POST
def case_appointment(request, number):
    """Q9: confirm, move or reject this case's appointment, from its card."""
    case = queues.visible(request.user).filter(number=number).first()
    if case is None:
        raise Http404
    appointment = get_object_or_404(Appointment, pk=request.POST.get("appointment"), case=case)
    action = request.POST.get("action", "")
    slot = None
    if action == "move":
        slot = CalendarSlot.objects.filter(
            pk=request.POST.get("slot") or 0, calendar=appointment.calendar
        ).first()
    try:
        calendars.decide(
            request.user,
            appointment,
            action,
            slot=slot,
            method=request.POST.get("method", ""),
            note=request.POST.get("note", ""),
        )
    except (ValidationError, PermissionDenied) as exc:
        messages.error(request, " ".join(getattr(exc, "messages", ["ไม่มีสิทธิ์ทำรายการนี้"])))
    else:
        messages.success(request, "บันทึกนัดแล้ว ผู้แจ้งจะเห็นข้อความตอนส่งข้อความมาครั้งถัดไป")
    return redirect(f"{reverse('case-detail', args=[case.number])}#s-warehouse")


def case_panel(user, case):
    """What the case card shows: the chat, the warehouse stages and the appointments."""
    scope = identity.staff_scope(user)
    can_chat = identity.can_access_division(scope, case.owner_division_id)
    done = {m.stage: m for m in case.milestones.all()}
    next_stage = _next_stage(case, done)
    received = done.get(S.RECEIVED)
    open_appointments = [
        a for a in case.appointments.select_related("slot", "calendar") if a.status != "cancelled"
    ]
    return {
        "can_act": identity.holds(scope, case.owner_division_id) or scope.is_admin,
        "next_stage": next_stage,
        "next_label": S(next_stage).label if next_stage else "",
        "suggested": (
            warehouse.suggested_issue(case, received.on)
            if next_stage == S.KEYED and received
            else None
        ),
        "today": timezone.localdate(),
        "waiting": [a for a in open_appointments if a.status == Appointment.Status.REQUESTED],
        "free": {
            a.pk: [s for s in calendars.free_slots(a.calendar.key)]
            for a in open_appointments
            if a.status == Appointment.Status.REQUESTED
        },
        "methods": [m for m in Appointment.Method.choices if m[0]],
        "chat": (chat := case_chat.thread(case)),
        "chat_last": case_chat.last_from_requester(chat),
        "chat_open": can_chat and case_chat._identity(case) is not None,
        "chat_contact": case_chat.contact_of(user) if case_chat.needs_contact(user, case) else "",
        "chat_fresh": bool(can_chat and case_chat.fresh_inbox(case)),
        "milestones": list(case.milestones.select_related("recorded_by__staff_profile")),
        "appointments": open_appointments,
        "warehouse": case.kind in (Case.Kind.WITHDRAWAL, Case.Kind.RETURN)
        and warehouse.schedule_of(case.owner_division) is not None,
    }
