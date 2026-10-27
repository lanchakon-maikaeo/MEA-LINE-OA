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
)

S = CaseMilestone.Stage
GROUPS = [("meter", "มิเตอร์"), ("ct", "CT"), ("vt", "VT"), ("other", "อุปกรณ์อื่น")]
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
    start = _day(request.GET.get("week"), today) or today
    start -= timedelta(days=start.weekday())
    if request.method == "POST":
        return _calendar_post(request, code, calendar, start)
    days = calendars.week(request.user, calendar, start)
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
            "days": days,
            "start": start,
            "previous": start - timedelta(days=7),
            "next": start + timedelta(days=7),
            "waiting": waiting,
            "free": calendars.free_slots(calendar.key),
            "methods": [m for m in Appointment.Method.choices if m[0]],
            "today": today,
        },
    )


def _calendar_post(request, code, calendar, start):
    back = f"{reverse('calendar', args=[code])}?cal={calendar.key}&week={start.isoformat()}"
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


def case_panel(user, case):
    """What the case card shows: the chat, the warehouse stages and the appointments."""
    scope = identity.staff_scope(user)
    can_chat = identity.can_access_division(scope, case.owner_division_id)
    return {
        "chat": case_chat.thread(case),
        "chat_open": can_chat and case_chat._identity(case) is not None,
        "chat_contact": case_chat.contact_of(user) if case_chat.needs_contact(user, case) else "",
        "chat_fresh": bool(can_chat and case_chat.fresh_inbox(case)),
        "milestones": list(case.milestones.select_related("recorded_by__staff_profile")),
        "appointments": list(case.appointments.select_related("slot", "calendar")),
        "warehouse": case.kind in (Case.Kind.WITHDRAWAL, Case.Kind.RETURN)
        and warehouse.schedule_of(case.owner_division) is not None,
    }
