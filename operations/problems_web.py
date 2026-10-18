"""ปัญหาและข้อเสนอแนะ: the admin's list of every problem and suggestion (user, 14 Oct 2569).

The user asked for one admin page with every problem and suggestion, who reported it, the channel
and when, with a CSV download. Their answers:
- Q1: an anonymous reporter appears only as their random รหัสแทนตัว, never who it is (ADR 0010).
- Q2: every topic, status and channel, newest first, 50 a page, filtered by topic, status,
  channel, division and dates.
- Q5: the code appears on this page only.
- Q4, Q6 and Q7: the CSV carries no personal data (no name, code, text, location or staff name),
  only the kind of reporter, and every download is audited.
"""

import csv
import io
from datetime import date

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.http import require_safe

from operations import identity, reports
from operations.models import Case, Division, RoleGrant
from operations.views import admin_required

PER_PAGE = 50
NO_TOPIC = "none"
RECORDED_LATER = 60  # seconds between the real report time and the record worth showing
HEADER = (
    "เลขเรื่อง",
    "หัวข้อ",
    "ประเภทผู้แจ้ง",
    "หน่วยงาน (เขต/ฝ่าย)",
    "ช่องทาง",
    "เจ้าหน้าที่บันทึกแทน",
    "กองผู้บันทึก",
    "วันเวลาแจ้งจริง",
    "วันเวลาบันทึก",
    "กองเจ้าภาพ",
    "สถานะ",
    "วันที่ปิด",
)


def _chosen(query):
    """The filters of the list; anything that is not a known value is ignored."""
    chosen = {}
    topics = {value for value, _ in Case.Topic.choices} | {NO_TOPIC}
    for key, allowed in (
        ("topic", topics),
        ("status", {value for value, _ in Case.Status.choices}),
        ("channel", {value for value, _ in Case.Channel.choices}),
    ):
        if query.get(key) in allowed:
            chosen[key] = query[key]
    division = (query.get("division") or "").strip()
    if division and Division.objects.filter(code=division).exists():
        chosen["division"] = division
    for key in ("from", "to"):
        try:
            chosen[key] = date.fromisoformat(query.get(key) or "").isoformat()
        except ValueError:
            pass
    return chosen


def _cases(chosen):
    """Problem and suggestion cases matching the filters, newest real report time first."""
    cases = Case.objects.filter(kind=Case.Kind.PROBLEM).annotate(
        reported=Coalesce("reported_at", "submitted_at")
    )
    if chosen.get("topic") == NO_TOPIC:
        cases = cases.filter(topic="")
    elif "topic" in chosen:
        cases = cases.filter(topic=chosen["topic"])
    if "status" in chosen:
        cases = cases.filter(status=chosen["status"])
    if "channel" in chosen:
        cases = cases.filter(channel=chosen["channel"])
    if "division" in chosen:
        cases = cases.filter(owner_division__code=chosen["division"])
    if "from" in chosen:
        cases = cases.filter(reported__date__gte=chosen["from"])
    if "to" in chosen:
        cases = cases.filter(reported__date__lte=chosen["to"])
    return cases.select_related(
        "district",
        "owner_division",
        "requester__district",
        "requester__division",
        "submitted_by__staff_profile",
    ).order_by("-reported", "-submitted_at")


def _topic(case):
    return case.get_topic_display() if case.topic else "ไม่ระบุหัวข้อ"


def _recorded_later(case):
    return case.reported_at is not None and (
        abs((case.submitted_at - case.reported_at).total_seconds()) > RECORDED_LATER
    )


def _staff_name(user):
    profile = getattr(user, "staff_profile", None)
    return (profile.display_name if profile else "") or user.username


def _row(case):
    requester = case.requester
    link = ""
    if requester.is_anonymous:
        reporter = f"ไม่ระบุตัวตน #{requester.alias}" if requester.alias else "ไม่ระบุตัวตน"
    elif requester.is_internal:
        reporter = f"{requester.display_name} · ผู้แจ้งภายใน"
    else:
        reporter = f"{requester.display_name} · {requester.district.name}"
        link = reverse("admin-requester-detail", args=[requester.pk])
    return {
        "case": case,
        "topic": _topic(case),
        "reporter": reporter,
        "reporter_link": link,
        "on_behalf": _staff_name(case.submitted_by) if case.submitted_by_id else "",
        "reported": case.reported_at or case.submitted_at,
        "recorded": case.submitted_at if _recorded_later(case) else None,
    }


def _filters():
    topics = [*Case.Topic.choices, (NO_TOPIC, "ไม่ระบุหัวข้อ")]
    divisions = Division.objects.filter(
        pk__in=Case.objects.filter(kind=Case.Kind.PROBLEM).values("owner_division")
    ).order_by("code")
    return [
        ("topic", "หัวข้อ", topics),
        ("status", "สถานะ", Case.Status.choices),
        ("channel", "ช่องทาง", Case.Channel.choices),
        ("division", "กองเจ้าภาพ", [(d.code, d.code) for d in divisions]),
    ]


@admin_required
@require_safe
def problems(request):
    chosen = _chosen(request.GET)
    page = Paginator(_cases(chosen), PER_PAGE).get_page(request.GET.get("page"))
    query = urlencode(chosen)
    return render(
        request,
        "operations/admin_problems.html",
        {
            "page": page,
            "rows": [_row(case) for case in page],
            "chosen": chosen,
            "filters": _filters(),
            "query": query,
            "page_query": f"{query}&" if query else "",
        },
    )


def _reporter_kind(requester):
    if requester.is_anonymous:
        return "ไม่ระบุตัวตน"
    return "ผู้แจ้งภายใน" if requester.is_internal else "ผู้แจ้งหน่วยงาน (เขต/ฝ่าย)"


def _staff_divisions(users):
    """The division codes each staff member works in today (global roles: ระดับระบบ)."""
    found = {}
    grants = RoleGrant.objects.filter(user_id__in=users, revoked_at__isnull=True).select_related(
        "division"
    )
    for grant in grants:
        found.setdefault(grant.user_id, set()).add(
            grant.division.code if grant.division_id else "ระดับระบบ"
        )
    return {user: " ".join(sorted(codes)) for user, codes in found.items()}


@admin_required
@require_safe
def problems_csv(request):
    """The list as CSV without personal data (Q4, Q6, Q7): no name, code, typed text,
    location or staff name. Formula-safe, with a BOM for Excel, and audited."""
    now = timezone.now()
    chosen = _chosen(request.GET)
    limit = settings.EXPORT_MAX_ROWS
    cases = list(_cases(chosen)[: limit + 1])
    truncated = len(cases) > limit
    cases = cases[:limit]
    divisions = _staff_divisions({c.submitted_by_id for c in cases if c.submitted_by_id})
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(HEADER)
    for case in cases:
        on_behalf = case.submitted_by_id is not None
        writer.writerow(
            [
                reports._cell(value)
                for value in (
                    case.number,
                    _topic(case),
                    _reporter_kind(case.requester),
                    case.district.name,
                    case.get_channel_display(),
                    "ใช่" if on_behalf else "ไม่ใช่",
                    divisions.get(case.submitted_by_id, "") if on_behalf else "",
                    reports._when(case.reported_at or case.submitted_at),
                    reports._when(case.submitted_at),
                    case.owner_division.code,
                    case.get_status_display(),
                    reports._when(case.ended_at),
                )
            ]
        )
    if truncated:
        writer.writerow([f"ตัดที่ {limit:,} แถว ใช้ตัวกรองให้แคบลงเพื่อส่งออกส่วนที่เหลือ"])
    identity.audit(
        request.user,
        "cases.problems_exported",
        request.user,
        filters=chosen,
        rows=len(cases),
        truncated=truncated,
    )
    stamp = timezone.localtime(now).strftime("%Y%m%d-%H%M")
    response = HttpResponse("﻿" + buffer.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="faoj-problems-{stamp}.csv"'
    response["Cache-Control"] = "private, no-store"
    return response
