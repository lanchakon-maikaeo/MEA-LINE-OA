"""Division reports and the scoped CSV export (M10 round 1 Q12, Q13).

Both start from services.visible_cases through queues, so a report or a file never holds
another division's cases. Durations are service minutes on the active calendar (emergencies counted
continuously), averaged over the last 30 days; all figures come from synthetic data until the
pilot.
"""

import csv
import io
from datetime import timedelta

from django.conf import settings
from django.db.models import Count
from django.utils import timezone

from operations import queues, workflow
from operations.models import AckClock, Case, CaseTriage, ChildWork, Decision

WINDOW_DAYS = 30
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _average(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values)) if values else None


def _clock_minutes(clock, calendar):
    used = calendar.minutes_between(clock.started_at, clock.stopped_at, clock.priority)
    return max(used - clock.paused_minutes, 0)


def division_report(user, division, now=None):
    """One division's numbers (or every division's for `division` None in the admin view)."""
    now = now or timezone.now()
    admin_all = division is None
    visible = queues.visible(user)
    host = visible.filter(queues.tab_condition("host", division, user, now, admin_all))
    owned_open = visible.filter(status__in=workflow.OPEN)
    if division is not None:
        owned_open = owned_open.filter(owner_division=division)
    open_clocks = AckClock.objects.filter(case__in=owned_open, stopped_at__isnull=True)
    since = now - timedelta(days=WINDOW_DAYS)
    calendar = queues.calendar_now()

    ack = AckClock.objects.filter(
        kind__in=[AckClock.Kind.INTAKE, AckClock.Kind.HANDOFF],
        stopped_at__gte=since,
        case__in=visible,
    )
    resolve = AckClock.objects.filter(
        kind=AckClock.Kind.RESOLVE, stopped_at__gte=since, case__in=visible
    )
    closed = visible.filter(status=Case.Status.CLOSED, ended_at__gte=since).exclude(
        kind=Case.Kind.PROBLEM
    )
    if division is not None:
        ack = ack.filter(division=division)
        resolve = resolve.filter(division=division)
        closed = closed.filter(owner_division=division)
    finished = []
    starts = dict(
        Decision.objects.filter(case__in=closed, action="complete_triage")
        .order_by("case_id", "id")
        .values_list("case_id", "created_at")
    )
    levels = dict(
        CaseTriage.objects.filter(case__in=closed).values_list("case_id", "effective_priority")
    )
    for pk, ended_at in closed.values_list("pk", "ended_at"):
        if pk in starts:
            finished.append(calendar.minutes_between(starts[pk], ended_at, levels.get(pk, "")))

    by_status = dict(owned_open.values_list("status").annotate(n=Count("pk")))
    by_kind = dict(owned_open.values_list("kind").annotate(n=Count("pk")))
    by_level = {}
    for triage in CaseTriage.objects.filter(case__in=owned_open):
        level = triage.effective_priority or triage.suggested_priority or "none"
        by_level[level] = by_level.get(level, 0) + 1
    total = owned_open.count()
    children = ChildWork.objects.filter(status__in=ChildWork.OPEN, case__in=visible)
    if division is not None:
        children = children.filter(division=division)
    return {
        "division": division,
        "open": host.count(),
        "total": total,
        "overdue": open_clocks.filter(due_at__lte=now, paused_at__isnull=True)
        .values("case")
        .distinct()
        .count(),
        "escalated": open_clocks.filter(escalated_at__isnull=False)
        .values("case")
        .distinct()
        .count(),
        "district": visible.filter(
            queues.tab_condition("district", division, user, now, admin_all)
        ).count(),
        "children": children.count(),
        "by_status": [
            (label, by_status.get(value, 0))
            for value, label in Case.Status.choices
            if value in workflow.OPEN and by_status.get(value)
        ],
        "by_kind": [(label, by_kind.get(value, 0)) for value, label in Case.Kind.choices],
        "by_level": [(key, by_level.get(key, 0)) for key in ("P0", "P1", "P2", "P3", "none")],
        "avg_ack": _average(_clock_minutes(c, calendar) for c in ack),
        "ack_count": ack.count(),
        "avg_resolve": _average(_clock_minutes(c, calendar) for c in resolve),
        "resolve_count": resolve.count(),
        "avg_finish": _average(finished),
        "finish_count": len(finished),
    }


# --- CSV export (round 1 Q12) -----------------------------------------------------------------


def _cell(value):
    """Spreadsheet-safe text: a leading = + - @ (or tab/CR) cannot start a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def _lines(case):
    return ", ".join(
        f"{line.item.code} ×{line.requested_quantity.normalize():f}"
        if line.requested_quantity is not None
        else line.item.code
        for line in case.lines.all()
    )


def _person(user):
    if user is None:
        return ""
    profile = getattr(user, "staff_profile", None)
    return profile.display_name if profile else user.username


def _when(value):
    if value is None:
        return ""
    local = timezone.localtime(value)
    return f"{local.day:02d}/{local.month:02d}/{local.year + 543} {local:%H:%M}"


HEADER = [
    "เลขเรื่อง",
    "ชนิด",
    "สถานะ",
    "ระดับ",
    "เขต",
    "กองเจ้าภาพ",
    "ผู้รับผิดชอบ",
    "ยื่นเมื่อ",
    "ปิดเมื่อ",
    "กรอบเวลาที่ใกล้สุด",
    "ครบกำหนด",
    "เกินกรอบเวลา",
    "รายการของ",
]


def export_csv(cases, now=None):
    """(text, row count, truncated) for already-scoped, ordered cases; no requester name or text."""
    now = now or timezone.now()
    limit = settings.EXPORT_MAX_ROWS
    chosen = cases[: limit + 1]
    truncated = len(chosen) > limit
    rows = queues.build_rows(chosen[:limit], now)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(HEADER)
    for row in rows:
        case = row.case
        assignee = case.assignee
        writer.writerow(
            [
                _cell(v)
                for v in (
                    case.number,
                    case.get_kind_display(),
                    case.get_status_display(),
                    queues.level_name(row.level),
                    case.district.name,
                    case.owner_division.code,
                    _person(assignee),
                    _when(case.submitted_at),
                    _when(case.ended_at),
                    row.clock.get_kind_display() if row.clock else "",
                    _when(row.clock.due_at) if row.clock else "",
                    "ใช่" if row.overdue else "",
                    _lines(case),
                )
            ]
        )
    if truncated:
        writer.writerow([f"ตัดที่ {limit:,} แถว ใช้ตัวกรองให้แคบลงเพื่อส่งออกส่วนที่เหลือ"])
    return "﻿" + buffer.getvalue(), len(rows), truncated
