"""ปัญหาและข้อเสนอแนะ: the admin's list of every problem and suggestion (user, 14 Oct 2569).

The user asked for one admin page with every problem and suggestion, who reported it, the channel
and when, with a CSV download. Their answers:
- Q1: an anonymous reporter appears only as their random รหัสแทนตัว, never who it is (ADR 0010).
- Q2: every topic, status and channel, newest first, 50 a page, filtered by topic, status,
  channel, division and dates.
- Q5: the code appears on this page only.
- Q4, Q6 and Q7: the CSV carries no personal data (no name, code, text, location or staff name),
  only the kind of reporter, and every download is audited.

ADR 0030 (28 Oct 2569): the page is a tab after คิวงาน for admins. Its first sub-tab holds the
cases nobody picked a division for (ปัญหาการใช้งานหรืออื่นๆ); admins tick cases, or every case
the filters match, and send them to one division with one reason after confirming the count.

ADR 0039 (31 Oct 2569): a แอดมินฝ่าย opens the page and sends as admins do; a
ผู้ดูข้อมูลระดับฝ่าย only reads it. The CSV carries every row the filters match and what the
page shows, with the full text; an anonymous reporter is still only their รหัสแทนตัว.
"""

import csv
import io
import uuid
from datetime import date
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.http import require_POST, require_safe

from operations import identity, problem_pick, reports, services, workflow
from operations.contracts import Actor, CommandFailed
from operations.models import Case, Division, RoleGrant

PER_PAGE = 50
NO_TOPIC = "none"
ADMIN_OWNER = "admin"  # the กองเจ้าภาพ filter's value for cases the admins hold
TABS = (("waiting", "รอแอดมิน"), ("all", "ทั้งหมด"))
SEND_MAX = 500  # cases one ส่งให้กอง may move
RECORDED_LATER = 60  # seconds between the real report time and the record worth showing
HEADER = (
    "เลขเรื่อง",
    "หัวข้อ",
    "ข้อความที่แจ้ง",
    "ผู้แจ้ง",
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


def _department_required(may_send=False):
    """ADR 0039: ฝ่าย reads the page; admins and แอดมินฝ่าย also send."""

    def decorate(view):
        @login_required
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            scope = identity.staff_scope(request.user)
            if not (_may_send(scope) if may_send else scope.sees_department):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapper

    return decorate


def _may_send(scope):
    return scope.is_admin or scope.is_support or scope.is_department_supervisor


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
    if division == ADMIN_OWNER or (division and Division.objects.filter(code=division).exists()):
        chosen["division"] = division
    for key in ("from", "to"):
        try:
            chosen[key] = date.fromisoformat(query.get(key) or "").isoformat()
        except ValueError:
            pass
    return chosen


def _waiting():
    """Open cases the admins hold (ADR 0030), for the sub-tab and the count on the tab."""
    return Case.objects.filter(held_by_admin=True).exclude(status__in=workflow.TERMINAL)


def _cases(chosen, tab="all"):
    """Cases from the ปัญหา/ข้อเสนอแนะ tile matching the filters, newest real report first."""
    cases = (_waiting() if tab == "waiting" else Case.objects).filter(kind=Case.Kind.PROBLEM)
    cases = cases.filter(problem_pick.from_tile())  # 31 Oct 2569 Q12: the tile's cases only
    cases = cases.annotate(reported=Coalesce("reported_at", "submitted_at"))
    if chosen.get("topic") == NO_TOPIC:
        cases = cases.filter(topic="")
    elif "topic" in chosen:
        cases = cases.filter(topic=chosen["topic"])
    if "status" in chosen:
        cases = cases.filter(status=chosen["status"])
    if "channel" in chosen:
        cases = cases.filter(channel=chosen["channel"])
    if chosen.get("division") == ADMIN_OWNER:
        cases = cases.filter(held_by_admin=True)
    elif "division" in chosen:
        cases = cases.filter(owner_division__code=chosen["division"], held_by_admin=False)
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


def _reporter(requester):
    if requester.is_anonymous:
        return f"ไม่ระบุตัวตน #{requester.alias}" if requester.alias else "ไม่ระบุตัวตน"
    if requester.is_internal:
        return f"{requester.display_name} · ผู้แจ้งภายใน"
    return f"{requester.display_name} · {requester.district.name}"


def _row(case, admin=True):
    requester = case.requester
    named = not (requester.is_anonymous or requester.is_internal)
    link = reverse("admin-requester-detail", args=[requester.pk]) if admin and named else ""
    return {
        "case": case,
        "topic": _topic(case),
        "reporter": _reporter(requester),
        "reporter_link": link,
        "on_behalf": _staff_name(case.submitted_by) if case.submitted_by_id else "",
        "reported": case.reported_at or case.submitted_at,
        "recorded": case.submitted_at if _recorded_later(case) else None,
        "ended": case.status in workflow.TERMINAL,
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
        ("division", "กองเจ้าภาพ", [(ADMIN_OWNER, "แอดมิน"), *[(d.code, d.code) for d in divisions]]),
    ]


def _targets():
    """Where ส่งให้กอง may send: the real active divisions."""
    divisions = Division.objects.filter(is_active=True, is_synthetic=False).order_by("code")
    return [(d.code, f"{d.code} {d.name}") for d in divisions]


def _tab(query):
    """The sub-tab asked for; otherwise รอแอดมิน while anything waits there, else ทั้งหมด."""
    tab = query.get("tab", "")
    if tab in dict(TABS):
        return tab
    return "waiting" if _waiting().exists() else "all"


def _page(request, query, pending=None):
    scope = identity.staff_scope(request.user)
    chosen, tab = _chosen(query), _tab(query)
    cases = _cases(chosen, tab)
    page = Paginator(cases, PER_PAGE).get_page(query.get("page"))
    filters = urlencode(chosen)
    here = urlencode({"tab": tab, **chosen})
    return render(
        request,
        "operations/admin_problems.html",
        {
            "page": page,
            "rows": [_row(case, scope.is_admin) for case in page],
            "may_send": _may_send(scope),
            "chosen": chosen,
            "filters": _filters(),
            "query": filters,
            "page_query": f"{here}&",
            "tab": tab,
            "tabs": [
                (key, label, urlencode({"tab": key, **chosen}), _cases(chosen, key).count())
                for key, label in TABS
            ],
            "matching": page.paginator.count,
            "open_matching": cases.exclude(status__in=workflow.TERMINAL).count(),
            "targets": _targets(),
            "pending": pending,
        },
    )


@_department_required()
@require_safe
def problems(request):
    return _page(request, request.GET)


def _to_send(post):
    """The cases one ส่งให้กอง names: the ticked numbers, or every open case the filters
    match (ADR 0030 Q18), at most SEND_MAX."""
    if post.get("all_filtered") == "1":
        cases = _cases(_chosen(post), _tab(post)).exclude(status__in=workflow.TERMINAL)
    else:
        numbers = post.getlist("case")[:SEND_MAX]
        cases = Case.objects.filter(kind=Case.Kind.PROBLEM, number__in=numbers)
    return list(cases.select_related("owner_division")[: SEND_MAX + 1])


@_department_required(may_send=True)
@require_POST
def problems_send(request):
    """ส่งให้กอง for many cases: the first post shows the count to confirm, the second sends
    each case with the same reason; cases that cannot move are listed with why."""
    post = request.POST
    back = f"{reverse('admin-problems')}?{urlencode({'tab': _tab(post), **_chosen(post)})}"
    division = Division.objects.filter(
        code=post.get("to_division", ""), is_active=True, is_synthetic=False
    ).first()
    reason = " ".join((post.get("reason") or "").split())
    cases = _to_send(post)
    if division is None or not reason or not cases:
        messages.error(request, "เลือกเรื่อง เลือกกอง และใส่เหตุผลก่อนส่ง")
        return redirect(back)
    capped = len(cases) > SEND_MAX
    cases = cases[:SEND_MAX]
    if post.get("confirm") != "1":
        pending = {
            "count": len(cases),
            "capped": capped,
            "division": division,
            "reason": reason,
            "numbers": [c.number for c in cases],
            "all_filtered": post.get("all_filtered") == "1",
        }
        return _page(request, post, pending)
    batch = uuid.uuid4().hex[:12]
    done, failed = 0, []
    for case in cases:
        try:
            services.perform_action(
                Actor.staff(request.user),
                case.pk,
                "admin_send",
                expected_revision=case.revision,
                idempotency_key=f"admin-send-{batch}-{case.pk}",
                payload={"division": division.code, "reason": reason},
            )
        except CommandFailed as exc:
            failed.append(f"{case.number}: {exc.message or exc.code}")
        else:
            done += 1
    if done:
        messages.success(request, f"ส่ง {done} เรื่องให้ {division.code} แล้ว")
    for line in failed[:20]:
        messages.error(request, line)
    if capped:
        messages.info(request, f"ส่งได้ครั้งละไม่เกิน {SEND_MAX} เรื่อง เรื่องที่เหลือยังอยู่ในรายการ")
    return redirect(back)


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


@_department_required()
@require_safe
def problems_csv(request):
    """Every row the filters match, with what the page shows (ADR 0039, replacing the 14 Oct
    Q4, Q6, Q7 list without personal data); an anonymous reporter stays a รหัสแทนตัว.
    Formula-safe, with a BOM for Excel, and audited."""
    now = timezone.now()
    chosen = _chosen(request.GET)
    cases = list(_cases(chosen))
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
                    case.description,
                    _reporter(case.requester),
                    _reporter_kind(case.requester),
                    case.district.name,
                    case.get_channel_display(),
                    _staff_name(case.submitted_by) if on_behalf else "",
                    divisions.get(case.submitted_by_id, "") if on_behalf else "",
                    reports._when(case.reported_at or case.submitted_at),
                    reports._when(case.submitted_at),
                    case.owner_label,
                    case.get_status_display(),
                    reports._when(case.ended_at),
                )
            ]
        )
    identity.audit(
        request.user, "cases.problems_exported", request.user, filters=chosen, rows=len(cases)
    )
    stamp = timezone.localtime(now).strftime("%Y%m%d-%H%M")
    response = HttpResponse("﻿" + buffer.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="faoj-problems-{stamp}.csv"'
    response["Cache-Control"] = "private, no-store"
    return response
