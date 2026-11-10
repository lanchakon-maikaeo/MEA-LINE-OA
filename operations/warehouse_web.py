"""หน้าเว็บงานคลัง ปฏิทินนัด และการคุยในเรื่อง (grill กคว. 27 ต.ค. 2569 Q13 ถึง Q39).

งานคลัง: the division's open slips with their stage, the issue day the schedule suggests, and a
bulk form (scan or paste slip numbers, choose a stage). ตาราง: กำหนดจ่ายตามขนาด, issue days and
truck days, saved with a reason. ปฏิทิน: a week of slots with their requests; staff open slots
and confirm, move or reject requests. The case card posts staff messages to the case chat.
"""

from datetime import date, datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST, require_safe

from operations import calendars, case_chat, identity, queues, services, warehouse, workflow
from operations.contracts import Actor
from operations.models import (
    Appointment,
    Calendar,
    CalendarSlot,
    Case,
    CaseMessage,
    CaseMilestone,
    District,
    Division,
    DivisionSchedule,
    Holiday,
)
from operations.templatetags.ui import thai_short
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
TABS = [
    ("all", "ทั้งหมดที่ต้องทำ"),
    ("none", "ยังไม่รับใบเบิก"),
    (S.RECEIVED, "รับใบเบิกแล้ว"),
    (S.KEYED, "คีย์จ่ายแล้ว"),
    (S.ISSUED, "จ่ายแล้ว"),
    (S.DELIVERED, "เขตได้รับแล้ว"),
    ("approval", "รออนุมัติ"),
]
# Calendar colours on the month (Q6): นัดรับของ orange like the logo, notebooks blue.
TONES = {"pickup": "orange", "notebook": "blue"}
SPARE_TONES = ["green", "purple", "teal"]
# The button on each row names what the clerk does next.
NEXT_LABELS = {
    S.RECEIVED: "รับใบเบิก",
    S.KEYED: "คีย์จ่าย",
    S.ISSUED: "จ่ายแล้ว",
    S.DELIVERED: "เขตได้รับแล้ว",
}
# The stage the selection bar offers first on each tab.
BULK_DEFAULT = {"none": S.RECEIVED, S.RECEIVED: S.KEYED, S.KEYED: S.ISSUED, S.ISSUED: S.DELIVERED}


def _division(request, code, unit_posts=False):
    division = Division.objects.filter(code=code, is_active=True).first()
    scope = identity.staff_scope(request.user)
    # ADR 0039: a viewer opens the pages; only staff of the division post to them. ADR 0052: a
    # แอดมินแผนก/กลุ่มงาน opens them too, and posts งานคลัง of their unit's cases.
    if request.method == "GET":
        allowed = identity.may_open_division(scope, division.pk) if division else False
    else:
        allowed = division is not None and (
            identity.can_access_division(scope, division.pk)
            or (unit_posts and division.pk in scope.unit_divisions)
        )
    if not allowed:
        raise Http404
    return division


def _day(text, default=None):
    try:
        return date.fromisoformat(text) if text else default
    except ValueError:
        return None


@login_required
def warehouse_page(request, code):
    division = _division(request, code, unit_posts=True)
    if request.method == "POST":
        back = reverse("warehouse", args=[code]) + (
            f"?tab={request.POST['tab']}" if request.POST.get("tab", "").isalpha() else ""
        )
        numbers = warehouse.read_numbers(request.POST.get("numbers", ""))
        numbers += request.POST.getlist("case")
        stage = request.POST.get("stage", "")
        on = _day(request.POST.get("on"), timezone.localdate())
        planned = _day(request.POST.get("planned_on"))
        if on is None or (request.POST.get("planned_on") and planned is None):
            messages.error(request, "วันที่ไม่ถูกต้อง")
            return redirect(back)
        cases, missing = warehouse.find_cases(request.user, division, numbers)
        if identity.staff_scope(request.user).unit_only:  # ADR 0052: other units' slips hide
            mine = set(
                services.visible_cases(Actor.staff(request.user))
                .filter(pk__in=[c.pk for c in cases])
                .values_list("pk", flat=True)
            )
            missing += [c.number for c in cases if c.pk not in mine]
            cases = [c for c in cases if c.pk in mine]
        try:
            done, skipped = warehouse.record(
                request.user, cases, stage, on, planned, request.POST.get("note", ""), numbers
            )
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return redirect(back)
        if done:
            messages.success(request, f"บันทึก{S(stage).label} {len(done)} ใบ")
        for case, why in skipped[:20]:
            messages.error(request, f"{case.number} ({case.sap_number or '-'}): {why}")
        if missing:
            messages.error(request, "ไม่พบในงานที่ยังไม่จบ: " + ", ".join(missing[:30]))
        return redirect(back)
    kinds = [Case.Kind.WITHDRAWAL, Case.Kind.RETURN]
    scope = identity.staff_scope(request.user)
    # ADR 0052 Q14: a แอดมินแผนก/กลุ่มงาน sees the slips of their unit's cases only.
    shown = services.visible_cases(Actor.staff(request.user)) if scope.unit_only else Case.objects
    cases = (
        shown.filter(owner_division=division, kind__in=kinds)
        .exclude(status__in=workflow.TERMINAL)
        .select_related("district")
        .prefetch_related("milestones", "lines", "appointments", "extra_numbers")
        .order_by("submitted_at")
    )
    today = timezone.localdate()
    # ADR 0039: a viewer sees the stages and gets no form; ADR 0052: a unit admin acts.
    may_act = identity.can_access_division(scope, division.pk) or scope.unit_only
    by_tab = {key: [] for key, _ in TABS}
    for case in cases[:500]:
        done = {m.stage: m for m in case.milestones.all()}
        now = next((done[s] for s in reversed(CaseMilestone.ORDER) if s in done), None)
        waiting = case.status == Case.Status.AWAITING_APPROVAL
        keyed = done.get(S.KEYED)
        received = done.get(S.RECEIVED)
        # ADR 0047: a case of several numbers is late by its earliest pay day.
        plans = [m.planned_on for m in case.milestones.all() if m.stage == S.KEYED and m.planned_on]
        planned = min(plans, default=None)
        several = len(warehouse.payable(case)) > 1
        method = next(
            (a for a in case.appointments.all() if a.status in Appointment.HOLDING and a.method),
            None,
        )
        nxt = None if waiting or not may_act else _next_stage(case, done)
        row = {
            "case": case,
            "stage": now,
            "planned": planned,
            "suggested": (
                warehouse.suggested_issue(case, received.on) if received and not keyed else None
            ),
            "late": bool(planned and planned < today and S.ISSUED not in done),
            "method": method,
            "next": nxt,
            "next_label": NEXT_LABELS.get(nxt, ""),
            "extra": [n.number for n in case.extra_numbers.all()],
            "left": [n for _, n in warehouse.unkeyed(case)] if several and keyed else [],
            "several": several,
        }
        if waiting:
            by_tab["approval"].append(row)
            continue
        by_tab[now.stage if now else "none"].append(row)
        by_tab["all"].append(row)
    # 28 ต.ค. 2569 Q3: tabs by stage, each with its count; all open work by default.
    tab = request.GET.get("tab", "all")
    tab = tab if tab in by_tab else "all"
    return render(
        request,
        "operations/warehouse.html",
        {
            "division": division,
            "rows": by_tab[tab],
            "tabs": [(key, label, len(by_tab[key])) for key, label in TABS],
            "tab": tab,
            "stages": S.choices,
            "bulk_stage": BULK_DEFAULT.get(tab, S.RECEIVED),
            "today": today,
            "schedule": warehouse.schedule_of(division),
            "may_act": may_act,
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
        labels = {
            kind: {st: request.POST.get(f"stage_{kind}_{st}", "") for st in S.values}
            for kind in (Case.Kind.WITHDRAWAL, Case.Kind.RETURN)
        }
        try:
            warehouse.save_schedule(
                request.user,
                division,
                sla,
                request.POST.getlist("issue"),
                trucks,
                request.POST.get("reason", ""),
                delivery=request.POST.get("delivery", DivisionSchedule.Delivery.CHOOSE),
                lead_days=request.POST.get("lead_days", 0),
                contact=request.POST.get("contact", ""),
                stage_labels=labels,
            )
        except (ValidationError, PermissionDenied) as exc:
            messages.error(request, " ".join(getattr(exc, "messages", ["ไม่มีสิทธิ์แก้ตารางนี้"])))
        else:
            messages.success(request, "บันทึกตารางแล้ว")
        return redirect("warehouse-schedule", code=code)
    rows = list(schedule.sla if schedule else [])
    trucks = schedule.truck_days if schedule else {}
    stage_names = schedule.stage_labels if schedule else {}
    # 28 ต.ค. 2569 Q5: one box per group, its every-size row first, then each size.
    groups = [
        {
            "key": key,
            "label": label,
            "default": next((r["days"] for r in rows if r["group"] == key and not r["size"]), ""),
            "sizes": [r for r in rows if r["group"] == key and r["size"]],
        }
        for key, label in GROUPS
    ]
    today = timezone.localdate()
    pick_group = request.GET.get("pg", "meter")
    pick_district = request.GET.get("pd", districts[0].code if districts else "")
    issue, truck = warehouse.preview(schedule, pick_group, pick_district, today)
    return render(
        request,
        "operations/warehouse_schedule.html",
        {
            "division": division,
            "may_act": identity.can_access_division(
                identity.staff_scope(request.user), division.pk
            ),
            "groups": groups,
            "group_choices": GROUPS,
            "weekdays": WEEKDAYS,
            "short_days": list(enumerate(["จ.", "อ.", "พ.", "พฤ.", "ศ."])),
            "issue": schedule.issue_weekdays if schedule else [],
            "districts": [(d, warehouse.truck_weekdays(schedule, d)) for d in districts],
            "unmapped": [
                (k, " ".join(WEEKDAYS[n][1] for n in trucks[k] if n < len(WEEKDAYS)))
                for k in warehouse.unmapped_abbreviations(schedule)
            ],
            "delivery_choices": DivisionSchedule.Delivery.choices,
            "stage_rows": [
                (
                    kind,
                    label,
                    [
                        (st, S(st).label, (stage_names.get(kind) or {}).get(st, ""))
                        for st in S.values
                    ],
                )
                for kind, label in ((Case.Kind.WITHDRAWAL, "ใบเบิก"), (Case.Kind.RETURN, "ใบส่งคืน"))
            ],
            "schedule": schedule,
            "example": {
                "group": pick_group,
                "district": pick_district,
                "today": calendars.day_label(today),
                "issue": calendars.day_label(issue) if issue else "",
                "truck": calendars.day_label(truck) if truck else "",
            },
        },
    )


@login_required
def calendar_page(request, code):
    division = _division(request, code, unit_posts=True)
    choices = list(Calendar.objects.filter(division=division, is_active=True).order_by("pk"))
    if not choices:
        messages.info(request, "กองนี้ยังไม่มีปฏิทินนัด")
        return redirect("division-detail", code=code)
    # 28 ต.ค. 2569 Q6: every calendar of the division in one month, told apart by colour;
    # ?cal=KEY narrows it to one.
    tones = {
        c.pk: TONES.get(c.key) or SPARE_TONES[i % len(SPARE_TONES)] for i, c in enumerate(choices)
    }
    for c in choices:
        c.tone = tones[c.pk]
    picked_cal = next((c for c in choices if c.key == request.GET.get("cal")), None)
    shown = [picked_cal] if picked_cal else choices
    today = timezone.localdate()
    start = (_day(request.GET.get("month"), today) or today).replace(day=1)
    if request.method == "POST":
        return _calendar_post(request, code, choices, picked_cal, start)
    weeks = calendars.month(request.user, shown, start)
    # ADR 0052 Q19: a แอดมินแผนก/กลุ่มงาน sees who booked only for their unit's cases.
    unit = identity.staff_scope(request.user).unit_only
    mine = (
        set(services.visible_cases(Actor.staff(request.user)).values_list("pk", flat=True))
        if unit
        else None
    )
    for week in weeks:
        for d in week:
            for slot in d["slots"]:
                slot.tone = tones[slot.calendar_id]
                for a in slot.appointments.all():
                    a.mine = mine is None or a.case_id in mine
    picked = _day(request.GET.get("day"))
    picked_day = next((d for week in weeks for d in week if picked and d["date"] == picked), None)
    waiting = list(
        Appointment.objects.filter(calendar__in=shown, status=Appointment.Status.REQUESTED)
        .select_related("slot", "case__district", "calendar")
        .order_by("requested_at")
    )
    waiting = [a for a in waiting if mine is None or a.case_id in mine]
    free = {c.key: calendars.free_slots(c.key) for c in shown}
    for a in waiting:
        a.tone = tones[a.calendar_id]
        a.free = free.get(a.calendar.key, [])
    cal_query = f"cal={picked_cal.key}&" if picked_cal else ""
    return render(
        request,
        "operations/calendar.html",
        {
            "division": division,
            "calendars": choices,
            "calendar": picked_cal,
            "cal_query": cal_query,
            "weeks": weeks,
            "weekdays": ["อา.", "จ.", "อ.", "พ.", "พฤ.", "ศ.", "ส."],
            "start": start,
            "month_label": f"{calendars.MONTH_NAMES[start.month - 1]} {start.year + 543}",
            "previous": (start - timedelta(days=1)).replace(day=1),
            "next": (start + timedelta(days=32)).replace(day=1),
            "picked": picked_day,
            "waiting": waiting,
            "methods": [m for m in Appointment.Method.choices if m[0]],
            "today": today,
            "now": timezone.now(),
            "unit": unit,  # ADR 0052 Q19: no slot opening or closing
        },
    )


def _calendar_post(request, code, choices, picked_cal, start):
    day = _day(request.POST.get("day"))
    base = reverse("calendar", args=[code]) + "?" + (f"cal={picked_cal.key}&" if picked_cal else "")
    back = f"{base}month={start.isoformat()}"
    if day is not None:
        back = f"{base}month={day.replace(day=1).isoformat()}&day={day.isoformat()}"
    action = request.POST.get("action", "")
    try:
        if action == "add":
            if day is None:
                raise ValidationError("เลือกวันก่อน")
            key = request.POST.get("calendar") or (picked_cal.key if picked_cal else "")
            calendar = next((c for c in choices if c.key == key), None)
            if calendar is None:
                raise ValidationError("เลือกว่าเป็นนัดเรื่องไหน")
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
            messages.success(request, f"เปิด {len(made)} ช่วงใน{calendar.name}")
        elif action == "close":
            slot = get_object_or_404(
                CalendarSlot, pk=request.POST.get("slot"), calendar__in=choices
            )
            calendars.close_slot(request.user, slot)
            messages.success(request, "ปิดช่วงนี้แล้ว คนที่ขอไว้ยังอยู่")
        elif action == "reopen":
            slot = get_object_or_404(
                CalendarSlot, pk=request.POST.get("slot"), calendar__in=choices
            )
            calendars.reopen_slot(request.user, slot)
            messages.success(request, "เปิดช่วงนี้อีกครั้งแล้ว")
        elif action == "close_day":
            # ADR 0036 Q23: a day off for one calendar; requests already made stay.
            if day is None:
                raise ValidationError("เลือกวันก่อน")
            calendar = next((c for c in choices if c.key == request.POST.get("calendar")), None)
            if calendar is None:
                raise ValidationError("เลือกว่าเป็นนัดเรื่องไหน")
            closed = calendars.close_day(request.user, calendar, day)
            messages.success(request, f"ปิด {closed} ช่วงของ{calendar.name}วันนี้แล้ว คนที่ขอไว้ยังอยู่")
        elif action in ("confirm", "move", "reject", "done"):
            appointment = get_object_or_404(
                Appointment, pk=request.POST.get("appointment"), calendar__in=choices
            )
            slot = None
            if action == "move":
                slot = get_object_or_404(
                    CalendarSlot, pk=request.POST.get("slot"), calendar=appointment.calendar
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
def admin_districts(request):
    """ชื่อย่อ ฟข. (grill กคอ. 28 ต.ค. 2569 Q26): which district each abbreviation of the
    divisions' truck tables is; saved with a reason."""
    districts = list(District.objects.filter(is_active=True).order_by("code"))
    if request.method == "POST":
        names = {d.code: request.POST.get(f"short_{d.code}", "") for d in districts}
        try:
            changed = warehouse.save_short_names(request.user, names, request.POST.get("reason"))
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"บันทึกชื่อย่อแล้ว {changed} เขต")
        return redirect("admin-districts")
    from operations.models import DivisionSchedule as Schedule

    waiting = [
        (s.division.code, warehouse.unmapped_abbreviations(s))
        for s in Schedule.objects.select_related("division").order_by("division__code")
    ]
    return render(
        request,
        "operations/admin_districts.html",
        {"districts": districts, "waiting": [(code, keys) for code, keys in waiting if keys]},
    )


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


DOCK_DAYS = 7  # the chat window lists cases with messages this recent
DOCK_CASES = 20


@login_required
@require_safe
def chat_dock(request):
    """ADR 0042 Q8: the cases this person sees with chat in the last week, newest first, each
    with the requester's messages since staff last answered."""
    if not identity.staff_scope(request.user).may_read:
        raise Http404
    since = timezone.now() - timedelta(days=DOCK_DAYS)
    rows = (
        CaseMessage.objects.filter(case__in=queues.visible(request.user), created_at__gte=since)
        .select_related("case__district")
        .order_by("-created_at", "-pk")[:500]
    )
    found, latest = {}, 0
    for message in rows:  # newest first
        entry = found.setdefault(
            message.case_id, {"case": message.case, "last": message, "unread": 0, "done": False}
        )
        if message.direction == CaseMessage.Direction.STAFF:
            entry["done"] = True
        else:
            latest = max(latest, message.pk)
            entry["unread"] += 0 if entry["done"] else 1
    items = list(found.values())[:DOCK_CASES]
    return render(
        request,
        "operations/_chat_dock_list.html",
        {"items": items, "total": sum(i["unread"] for i in items), "latest": latest},
    )


@login_required
@require_POST
def case_chat_post(request, number):
    case = queues.visible(request.user).filter(number=number).first()
    if case is None:
        raise Http404
    dock = request.headers.get("X-Dock") == "1"
    try:
        case_chat.send(request.user, case, request.POST.get("text", ""))
    except (ValidationError, PermissionDenied) as exc:
        text = " ".join(getattr(exc, "messages", ["ไม่มีสิทธิ์ส่งข้อความในเรื่องนี้"]))
        if dock:
            return JsonResponse({"error": text}, status=400)
        messages.error(request, text)
    else:
        if dock:
            return JsonResponse({"ok": True})
        messages.success(request, "ส่งถึงผู้แจ้งแล้ว")
    return redirect(f"{reverse('case-detail', args=[case.number])}#chat")


def _next_stage(case, done):
    for stage in CaseMilestone.ORDER:
        if stage not in done:
            return stage
    return None


def case_steps(case, panel):
    """ADR 0051 Q1, Q8, Q9: the appointment and warehouse buttons of สิ่งที่ทำได้, an appointment
    the requester waits on first. Each opens its form in the action slot."""
    if not panel["can_act"]:
        return []
    steps = [
        {
            "label": f"{a.calendar.name} {thai_short(a.slot.starts_at)} น."
            if a.slot
            else f"{a.calendar.name} (ยังไม่มีช่วงเวลา)",
            "url": f"{reverse('case-appointment', args=[case.number])}?appointment={a.pk}",
            "appointment": a,
        }
        for a in panel["waiting"]
    ]
    if panel["warehouse"] and case.status not in (
        Case.Status.AWAITING_APPROVAL,
        Case.Status.CLOSED,
    ):
        url = reverse("case-stage", args=[case.number])
        if panel["next_stage"]:
            steps.append(
                {"label": f"บันทึก{panel['next_label']}", "url": url, "stage": panel["next_stage"]}
            )
        if panel["more_keyed"]:
            steps.append(
                {
                    "label": f"บันทึก{panel['keyed_label']}",
                    "url": f"{url}?stage={S.KEYED}",
                    "stage": S.KEYED,
                }
            )
    return steps


def _step_page(request, case, context):
    """The form of one step: a partial for the action slot, the whole page without HTMX."""
    partial = request.headers.get("HX-Request") == "true"
    template = "operations/_step_form.html" if partial else "operations/case_step.html"
    return render(request, template, {"case": case, **context})


@login_required
def case_stage(request, number):
    """Q38 (Q9): the next warehouse stage of one case, from its card. GET: its form (ADR 0051 Q1);
    ?demo=1 an example for สาธิตการใช้งาน that cannot be sent."""
    case = queues.visible(request.user).filter(number=number).first()
    if case is None:
        raise Http404
    if request.method == "GET":
        panel = case_panel(request.user, case)
        demo = request.GET.get("demo") == "1"
        stage = request.GET.get("stage") or panel["next_stage"] or (S.RECEIVED if demo else "")
        if stage not in S.values or case.kind not in (Case.Kind.WITHDRAWAL, Case.Kind.RETURN):
            raise Http404
        if not demo and stage not in {s.get("stage") for s in case_steps(case, panel)}:
            raise PermissionDenied
        label = warehouse.stage_label(case, stage)
        context = {"panel": panel, "stage": stage, "label": label, "title": f"บันทึก{label}"}
        return _step_page(request, case, {**context, "demo": demo})
    if request.method != "POST":
        return HttpResponse(status=405)
    on = _day(request.POST.get("on"), timezone.localdate())
    planned = _day(request.POST.get("planned_on"))
    numbers = request.POST.getlist("number")
    if on is None or (request.POST.get("planned_on") and planned is None):
        messages.error(request, "วันที่ไม่ถูกต้อง")
    elif request.POST.get("pick_numbers") and not numbers:
        messages.error(request, "เลือกเลขอย่างน้อยหนึ่งเลข")
    else:
        try:
            done, skipped = warehouse.record(
                request.user,
                [case],
                request.POST.get("stage", ""),
                on,
                planned,
                request.POST.get("note", ""),
                numbers,
            )
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            for _, why in skipped:
                messages.error(request, why)
            if done:
                label = warehouse.stage_label(case, request.POST["stage"])
                messages.success(request, f"บันทึก{label}แล้ว")
    back = request.POST.get("back", "")
    if back.startswith(reverse("warehouse", args=[case.owner_division.code])):
        return redirect(back)
    return redirect(f"{reverse('case-detail', args=[case.number])}#s-warehouse")


@login_required
def case_appointment(request, number):
    """Q9: confirm, move or reject this case's appointment, from its card. GET: its form for the
    action slot (ADR 0051 Q9)."""
    case = queues.visible(request.user).filter(number=number).first()
    if case is None:
        raise Http404
    if request.method == "GET":
        panel = case_panel(request.user, case)
        wanted = request.GET.get("appointment", "")
        steps = [s for s in case_steps(case, panel) if "appointment" in s]
        step = next((s for s in steps if str(s["appointment"].pk) == wanted), None)
        if step is None:
            raise PermissionDenied
        context = {"panel": panel, "a": step["appointment"], "title": step["label"]}
        return _step_page(request, case, context)
    if request.method != "POST":
        return HttpResponse(status=405)
    appointment = get_object_or_404(Appointment, pk=request.POST.get("appointment"), case=case)
    action = request.POST.get("action", "")
    slot = at = None
    warnings = []
    if action == "move" and request.POST.get("at"):
        # ADR 0055: a time picked freely, kept for this appointment alone.
        at = _moment(request.POST["at"])
        if at is None:
            messages.error(request, "วันเวลาไม่ถูกต้อง")
            return redirect(f"{reverse('case-detail', args=[case.number])}#s-warehouse")
    elif action == "move":
        slot = CalendarSlot.objects.filter(
            pk=request.POST.get("slot") or 0, calendar=appointment.calendar, private=False
        ).first()
    try:
        if at is not None:
            _, _, warnings = calendars.free_time(appointment.calendar, at, appointment)
        calendars.decide(
            request.user,
            appointment,
            action,
            slot=slot,
            method=request.POST.get("method", ""),
            note=request.POST.get("note", ""),
            at=at,
        )
    except (ValidationError, PermissionDenied) as exc:
        messages.error(request, " ".join(getattr(exc, "messages", ["ไม่มีสิทธิ์ทำรายการนี้"])))
    else:
        messages.success(request, "บันทึกนัดแล้ว ผู้แจ้งจะเห็นข้อความตอนส่งข้อความมาครั้งถัดไป")
        for warning in warnings:
            messages.warning(request, f"โปรดทราบ: นัดนี้อยู่ใน{warning}")
    return redirect(f"{reverse('case-detail', args=[case.number])}#s-warehouse")


def _moment(text):
    """A datetime-local value ("2026-11-12T09:30") in local time, or None."""
    try:
        moment = datetime.fromisoformat(str(text or "").strip())
    except ValueError:
        return None
    return timezone.make_aware(moment) if timezone.is_naive(moment) else moment


def case_panel(user, case):
    """What the case card shows: the chat, the warehouse stages and the appointments."""
    scope = identity.staff_scope(user)
    can_chat = identity.can_access_division(scope, case.owner_division_id)
    done = {m.stage: m for m in case.milestones.all()}
    next_stage = _next_stage(case, done)
    received = done.get(S.RECEIVED)
    payable = warehouse.payable(case)
    # ADR 0047: numbers left to key once the case is keyed; ticked one by one when several.
    unkeyed = warehouse.unkeyed(case) if received and payable else []
    open_appointments = [
        a for a in case.appointments.select_related("slot", "calendar") if a.status != "cancelled"
    ]
    return {
        "can_act": identity.holds(scope, case.owner_division_id) or scope.is_admin,
        "next_stage": next_stage,
        "returning": case.kind == Case.Kind.RETURN,
        "next_label": warehouse.stage_label(case, next_stage) if next_stage else "",
        "keyed_label": warehouse.stage_label(case, S.KEYED),
        "suggested": (
            warehouse.suggested_issue(case, received.on, timezone.localdate())
            if (next_stage == S.KEYED or unkeyed) and received and case.kind != Case.Kind.RETURN
            else None
        ),
        "unkeyed": [n for _, n in unkeyed] if len(payable) > 1 else [],
        "more_keyed": next_stage != S.KEYED and bool(unkeyed),
        "per_number": len(payable) > 1,
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
        "chat_any": any(i["who"] != "status" for i in chat),  # strips alone open no window
        "chat_open": can_chat and case_chat._identity(case) is not None,
        "chat_fresh": bool(can_chat and case_chat.fresh_inbox(case)),
        "milestones": list(case.milestones.select_related("recorded_by__staff_profile")),
        "appointments": open_appointments,
        "warehouse": case.kind in (Case.Kind.WITHDRAWAL, Case.Kind.RETURN)
        and warehouse.schedule_of(case.owner_division) is not None,
        "calendar": bool(open_appointments)
        or case.kind in (Case.Kind.WITHDRAWAL, Case.Kind.RETURN)
        and case.owner_division.calendars.exists(),
    }
