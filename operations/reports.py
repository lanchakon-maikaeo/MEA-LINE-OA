"""Division reports and the scoped CSV export (M10 round 1 Q12, Q13).

Reports start from services.visible_cases through queues; queue CSV also accepts scoped work
orders. A report or file never expands the viewer's access. Durations are service minutes on
the active calendar (emergencies counted
continuously), averaged over the last 30 days; all figures come from synthetic data until the
pilot.
"""

import csv
import io
from datetime import date, datetime, time, timedelta

from django.conf import settings
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from operations import queues, workflow
from operations.models import AckClock, Case, CaseLine, CaseTriage, ChildWork, Decision

WINDOW_DAYS = 30
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _average(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values)) if values else None


def _clock_minutes(clock, calendar):
    used = calendar.minutes_between(clock.started_at, clock.stopped_at, clock.priority)
    return max(used - clock.paused_minutes, 0)


PERIODS = (7, 30, 90)
AGE_BUCKETS = (
    ("lt1", "น้อยกว่า 1 วัน", 0, 1),
    ("1to3", "1–3 วัน", 1, 3),
    ("3to7", "3–7 วัน", 3, 7),
    ("7to14", "7–14 วัน", 7, 14),
    ("gt14", "เกิน 14 วัน", 14, None),
)
LEVELS = ("P0", "P1", "P2", "P3", "none")


def period(days):
    """The report window in days (7, 30 or 90; 30 when anything else is asked)."""
    try:
        days = int(days)
    except (TypeError, ValueError):
        return WINDOW_DAYS
    return days if days in PERIODS else WINDOW_DAYS


class _Sets:
    """The case sets one report counts, so a figure and its drill-down list always agree."""

    def __init__(self, user, division, now, days):
        self.user, self.division, self.now, self.days = user, division, now, days
        self.admin_all = division is None
        self.since = now - timedelta(days=days)
        self.visible = queues.visible(user).exclude(is_example=True)  # ตัวอย่าง: never counted
        self.owned = (
            self.visible if division is None else self.visible.filter(owner_division=division)
        )
        self.owned_open = self.owned.filter(status__in=workflow.OPEN)
        self.host = self.visible.filter(
            queues.tab_condition("host", division, user, now, self.admin_all)
        )
        self.district = self.visible.filter(
            queues.tab_condition("district", division, user, now, self.admin_all)
        )

    def open_clocks(self):
        return AckClock.objects.filter(case__in=self.owned_open, stopped_at__isnull=True)

    def overdue(self):
        late = self.open_clocks().filter(due_at__lte=self.now, paused_at__isnull=True)
        return self.owned_open.filter(pk__in=late.values("case"))

    def escalated(self):
        raised = self.open_clocks().filter(escalated_at__isnull=False)
        return self.owned_open.filter(pk__in=raised.values("case"))

    def children(self):
        work = ChildWork.objects.filter(status__in=ChildWork.OPEN)
        if self.division is not None:
            work = work.filter(division=self.division)
        return self.visible.filter(pk__in=work.values("case"))

    def ack_clocks(self):
        clocks = AckClock.objects.filter(
            kind__in=[AckClock.Kind.INTAKE, AckClock.Kind.HANDOFF],
            stopped_at__gte=self.since,
            case__in=self.visible,
        )
        return clocks if self.division is None else clocks.filter(division=self.division)

    def resolve_clocks(self):
        clocks = AckClock.objects.filter(
            kind=AckClock.Kind.RESOLVE, stopped_at__gte=self.since, case__in=self.visible
        )
        return clocks if self.division is None else clocks.filter(division=self.division)

    def finished(self):
        return self.owned.filter(status=Case.Status.CLOSED, ended_at__gte=self.since).exclude(
            kind=Case.Kind.PROBLEM
        )

    def level(self, level):
        if level == "none":
            condition = Q(triage__isnull=True) | Q(
                triage__effective_priority="", triage__suggested_priority=""
            )
        else:
            condition = Q(triage__effective_priority=level) | Q(
                triage__effective_priority="", triage__suggested_priority=level
            )
        return self.owned_open.filter(condition)

    def age(self, key):
        bucket = next((b for b in AGE_BUCKETS if b[0] == key), None)
        if bucket is None:
            return self.owned_open.none()
        _, _, low, high = bucket
        cases = self.owned_open.filter(submitted_at__lte=self.now - timedelta(days=low))
        if high is not None:
            cases = cases.filter(submitted_at__gt=self.now - timedelta(days=high))
        return cases

    def day(self, field, value):
        """Cases whose `field` (submitted_at or ended_at) falls on a Bangkok date; the value
        "window" is the whole period (the overview's เรื่องเข้า/เรื่องจบ totals)."""
        if value == "window":
            return self.owned.filter(**{f"{field}__gte": self.since})
        try:
            day = date.fromisoformat(value)
        except (TypeError, ValueError):
            return self.owned.none()
        start = timezone.make_aware(datetime.combine(day, time.min))
        return self.owned.filter(**{f"{field}__gte": start, f"{field}__lt": start + timedelta(1)})


# metric -> (title; with {value} where the metric takes one, queue tab with the same cases)
METRICS = {
    "open": ("เรื่องที่ต้องทำ", "host"),
    "overdue": ("เรื่องที่เกินกรอบเวลา", None),
    "escalated": ("เรื่องที่ถูกยกระดับ", None),
    "district": ("เรื่องที่รอหน่วยงาน (เขต/ฝ่าย)ตอบ", "district"),
    "children": ("เรื่องที่มีงานย่อยค้าง", "child"),
    "level": ("เรื่องที่เปิดอยู่ ระดับ{value}", None),
    "kind": ("เรื่องที่เปิดอยู่ ชนิด{value}", None),
    "status": ("เรื่องที่เปิดอยู่ สถานะ{value}", None),
    "section": ("เรื่องที่เปิดอยู่ของ{value}", None),
    "age": ("เรื่องที่เปิดอยู่มา{value}", None),
    "item": ("เรื่องที่เปิดอยู่ที่มีรายการ {value}", None),
    "new": ("เรื่องที่เข้ามาวันที่ {value}", None),
    "ended": ("เรื่องที่จบวันที่ {value}", None),
    "ack": ("เรื่องที่นับเวลาตอบรับ", None),
    "resolve": ("เรื่องที่นับเวลาแก้ปัญหา", None),
    "finish": ("เรื่องเบิก/คืนที่ปิดแล้ว", None),
}


def drilldown(user, division, metric, value, now=None, days=WINDOW_DAYS):
    """(title, queue tab or None, cases) behind one figure of the report; None if unknown."""
    if metric not in METRICS:
        return None
    sets = _Sets(user, division, now or timezone.now(), period(days))
    title, tab = METRICS[metric]
    shown = value
    if metric == "level":
        if value not in LEVELS:
            return None
        shown = queues.level_name(value) if value != "none" else queues.UNTRIAGED
        cases = sets.level(value)
    elif metric == "kind":
        if value not in Case.Kind.values:
            return None
        cases, shown = sets.owned_open.filter(kind=value), Case.Kind(value).label
    elif metric == "status":
        if value not in workflow.OPEN:
            return None
        cases, shown = sets.owned_open.filter(status=value), Case.Status(value).label
    elif metric == "section":
        # Real structure Q12: a แผนก of this division, or "none" for cases not given one.
        if value == "none":
            cases, shown = sets.owned_open.filter(section__isnull=True), "ที่ยังไม่ระบุแผนก"
        elif value.isdigit() and division is not None:
            section = division.sections.filter(pk=int(value)).first()
            if section is None:
                return None
            cases, shown = sets.owned_open.filter(section=section), section.name
        else:
            return None
    elif metric == "age":
        bucket = next((b for b in AGE_BUCKETS if b[0] == value), None)
        if bucket is None:
            return None
        cases, shown = sets.age(value), bucket[1]
    elif metric == "item":
        cases = sets.owned_open.filter(lines__item__code=value).distinct()
    elif metric in ("new", "ended"):
        cases = sets.day("submitted_at" if metric == "new" else "ended_at", value)
        shown = f"{sets.days} วันล่าสุด" if value == "window" else _thai_day(value)
    elif metric == "ack":
        cases = sets.visible.filter(pk__in=sets.ack_clocks().values("case"))
    elif metric == "resolve":
        cases = sets.visible.filter(pk__in=sets.resolve_clocks().values("case"))
    else:
        cases = {
            "open": sets.host,
            "overdue": sets.overdue(),
            "escalated": sets.escalated(),
            "district": sets.district,
            "children": sets.children(),
            "finish": sets.finished(),
        }[metric]
    return title.format(value=shown), tab, cases


def _thai_day(value):
    try:
        day = date.fromisoformat(value)
    except (TypeError, ValueError):
        return value
    return f"{day.day}/{day.month}/{day.year + 543}"


def _per_day(cases, field, since, tz):
    rows = (
        cases.filter(**{f"{field}__gte": since})
        .annotate(day=TruncDate(field, tzinfo=tz))
        .values("day")
        .annotate(n=Count("pk"))
    )
    return {row["day"]: row["n"] for row in rows}


def trend(sets):
    """New and ended cases per Bangkok day over the window, oldest first."""
    tz = timezone.get_current_timezone()
    today = timezone.localdate(sets.now)
    first = today - timedelta(days=sets.days - 1)
    since = timezone.make_aware(datetime.combine(first, time.min))
    new = _per_day(sets.owned, "submitted_at", since, tz)
    ended = _per_day(sets.owned.filter(ended_at__isnull=False), "ended_at", since, tz)
    days = [first + timedelta(days=i) for i in range(sets.days)]
    return [(d, new.get(d, 0), ended.get(d, 0)) for d in days]


def aging(sets):
    return [(key, label, sets.age(key).count()) for key, label, _, _ in AGE_BUCKETS]


def top_items(sets, limit=6):
    rows = (
        CaseLine.objects.filter(case__in=sets.owned_open)
        .values("item__code", "item__name")
        .annotate(n=Count("case", distinct=True))
        .order_by("-n", "item__code")[:limit]
    )
    return [(r["item__code"], r["item__name"], r["n"]) for r in rows]


def division_report(user, division, now=None, days=WINDOW_DAYS):
    """One division's numbers (or every division's for `division` None in the admin view)."""
    sets = _Sets(user, division, now or timezone.now(), period(days))
    calendar = queues.calendar_now()
    ack, resolve, closed = sets.ack_clocks(), sets.resolve_clocks(), sets.finished()
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

    owned_open = sets.owned_open
    by_status = dict(owned_open.values_list("status").annotate(n=Count("pk")))
    by_kind = dict(owned_open.values_list("kind").annotate(n=Count("pk")))
    by_section = dict(owned_open.values_list("section_id").annotate(n=Count("pk")))
    sections = list(division.sections.filter(is_active=True)) if division is not None else []
    by_level = {}
    for triage in CaseTriage.objects.filter(case__in=owned_open):
        level = triage.effective_priority or triage.suggested_priority or "none"
        by_level[level] = by_level.get(level, 0) + 1
    untriaged = owned_open.filter(triage__isnull=True).count()
    if untriaged:
        by_level["none"] = by_level.get("none", 0) + untriaged
    return {
        "division": division,
        "days": sets.days,
        "open": sets.host.count(),
        "total": owned_open.count(),
        "overdue": sets.overdue().count(),
        "escalated": sets.escalated().count(),
        "district": sets.district.count(),
        "children": sets.children().count(),
        "by_status": [
            (value, label, by_status.get(value, 0))
            for value, label in Case.Status.choices
            if value in workflow.OPEN and by_status.get(value)
        ],
        "by_kind": [(value, label, by_kind.get(value, 0)) for value, label in Case.Kind.choices],
        "by_section": (
            [(str(s.pk), s.name, by_section.get(s.pk, 0)) for s in sections]
            + [("none", "ยังไม่ระบุแผนก", by_section.get(None, 0))]
            if sections
            else []
        ),
        "by_level": [(key, by_level.get(key, 0)) for key in LEVELS],
        "avg_ack": _average(_clock_minutes(c, calendar) for c in ack),
        "ack_count": ack.count(),
        "avg_resolve": _average(_clock_minutes(c, calendar) for c in resolve),
        "resolve_count": resolve.count(),
        "avg_finish": _average(finished),
        "finish_count": len(finished),
        "trend": trend(sets),
        "aging": aging(sets),
        "items": top_items(sets),
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
    if not isinstance(value, datetime):
        return f"{value.day:02d}/{value.month:02d}/{value.year + 543}"
    local = timezone.localtime(value)
    return f"{local.day:02d}/{local.month:02d}/{local.year + 543} {local:%H:%M}"


HEADER = [
    "เลขเรื่อง",
    "ชนิด",
    "สถานะ",
    "ระดับ",
    "หน่วยงาน (เขต/ฝ่าย)",
    "กองเจ้าภาพ",
    "ผู้รับผิดชอบ",
    "ยื่นเมื่อ",
    "เวลาแจ้งจริง",
    "ปิดเมื่อ",
    "กรอบเวลาที่ใกล้สุด",
    "ครบกำหนด",
    "เกินกรอบเวลา",
    "รายการของ",
    "ผู้สั่ง",
    "ช่องทาง",
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
        if row.order is not None:
            order = row.order
            writer.writerow(
                [
                    _cell(value)
                    for value in (
                        order.number,
                        "งานสั่ง",
                        order.get_status_display(),
                        queues.level_name(row.level),
                        "",
                        order.division.code,
                        _person(order.assignee),
                        _when(order.created_at),
                        "",
                        _when(order.ended_at),
                        "",
                        _when(order.due_on),
                        "ใช่" if row.overdue else "",
                        "",
                        _person(order.issued_by),
                        order.get_channel_display() if order.channel else "ไม่ระบุช่องทาง",
                    )
                ]
            )
            continue
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
                    _when(case.reported_at),
                    _when(case.ended_at),
                    row.clock.get_kind_display() if row.clock else "",
                    _when(row.clock.due_at) if row.clock else "",
                    "ใช่" if row.overdue else "",
                    _lines(case),
                    "",
                    case.get_channel_display(),
                )
            ]
        )
    if truncated:
        writer.writerow([f"ตัดที่ {limit:,} แถว ใช้ตัวกรองให้แคบลงเพื่อส่งออกส่วนที่เหลือ"])
    return "﻿" + buffer.getvalue(), len(rows), truncated
