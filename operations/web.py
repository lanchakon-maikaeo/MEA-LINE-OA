"""Staff workspace of M10: home, division queues, the case page, search and account.

Read pages only call services.visible_cases (through queues) so a division never sees another
division's cases; admins see every division read-only and their visits to other divisions'
cases are audited (round 2 Q3). Actions on a case go through services.perform_action.
"""

import uuid
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from operations import (
    admin_nav,
    approvals,
    case_menu,
    case_numbers,
    casepage,
    charts,
    employees,
    identity,
    numbering,
    options,
    queues,
    registry,
    reports,
    services,
    sheet_access,
    support,
    units,
    uploads,
    warehouse,
    warehouse_web,
    web_actions,
    wording,
    work_orders,
    workflow,
)
from operations.contracts import Actor, CommandFailed, ErrorCode
from operations.models import (
    VIEWER_ROLES,
    Case,
    District,
    Division,
    Priority,
    Role,
    RoleGrant,
    WorkOrder,
)

ADMIN_URLS = ("admin-",)
REFERENCE_URLS = {"pii", "routing-words", "duties", "knowledge"}
ALL_QUEUE = "*"  # the remembered queue is ทุกกอง (an admin's every-division queue)
ALL_QUEUE_URLS = {"queue-all", "queue-all-export"}
# ADR 0030 Q12: ปัญหาและข้อเสนอแนะ is a tab of its own after คิวงาน, for admins only.
# The queue's kind filter and the one-division pie: the LINE short names, then งานสั่ง.
KINDS = [*units.KIND_SHORT.items(), (queues.ORDER_KIND, "งานสั่ง")]
PROBLEM_URLS = {"admin-problems", "admin-problems-csv", "admin-problems-send"}


def environment(request):
    """Which kind of server this is, for the footer (15 ต.ค. 2569: production holds real data)."""
    return {"app_env": settings.APP_ENV}


def navigation(request):
    """Top bar data: the viewer's divisions, section and admin rights (hidden otherwise)."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    scope = identity.staff_scope(user)
    match = getattr(request, "resolver_match", None)
    name = match.url_name if match else ""
    # An admin on a settings page gets the grouped admin menu (15 ต.ค. 2569); staff opening the
    # pages they share (คำที่ระบบจำ, ปิดข้อมูลก่อนส่ง AI, หน้าที่ของกอง) are on a reference page.
    admin_menu = (
        admin_nav.menu(scope, name) if scope.is_admin and name in admin_nav.URL_NAMES else None
    )
    if name in PROBLEM_URLS:
        section = "problems"
    elif admin_menu or (name and name.startswith(ADMIN_URLS)):
        section = "admin"
    elif name in REFERENCE_URLS:
        section = "reference"
    elif name in {"account", "password_change"}:
        section = "account"
    elif name in {"reports", "report-cases"}:
        section = "reports"
    elif name and name.startswith("registry"):
        section = "registry"
    elif name in {"orders", "order-new", "order-detail"}:
        section = "queue"
    else:
        section = "queue"
    profile = getattr(user, "staff_profile", None)
    # The division switch shows the queue open now, else the one chosen last (15 ต.ค. 2569: on
    # ทุกกอง it kept showing the division opened before).
    code = match.kwargs.get("code") if match else None
    remembered = request.session.get("division", "")
    all_queue = scope.sees_department and (
        name in ALL_QUEUE_URLS or (not code and remembered == ALL_QUEUE)
    )
    current = "" if all_queue or remembered == ALL_QUEUE else remembered
    return {
        "nav": {
            "scope": scope,
            "divisions": queues.own_divisions(scope) if scope.may_read else [],
            "section": section,
            "url_name": name,
            "current": "" if name in ALL_QUEUE_URLS else (code or current),
            "all_queue": all_queue,
            "name": profile.display_name if profile else user.username,
            "is_intake": _intake_member(scope),
            "has_intake": identity.intake_division() is not None,
            # ตัวเลือกระบบ (ADR 0009): a non-admin who may approve LINE registrations.
            "registrations": not scope.is_admin and identity.may_decide_registrations(user),
            "registry": registry.may_open(user),
            # ADR 0033: a ผู้ดูแลชีท who is not an admin reaches the sheets from the top menu.
            "sheets": not scope.is_admin and sheet_access.may_open(user),
            # ADR 0050 Q8, Q12: only แอดมินระบบ keep the ตารางเวร; a แอดมินฝ่าย sees their shift.
            "duty_until": _duty_until(user) if scope.is_department_supervisor else "",
            "admin": admin_menu,
            "admin_waiting": _admin_waiting() if scope.sees_department else 0,
        }
    }


def _duty_until(user):
    """อยู่เวรถึง: the end of this แอดมินฝ่าย's shift now, "" off duty."""
    from operations import duty

    end = duty.until(user)
    if end is None:
        return ""
    tomorrow = end.date() > timezone.localdate()
    return f"{'พรุ่งนี้ ' if tomorrow else ''}{end:%H:%M} น."


def _admin_waiting():
    """The number on the ปัญหาและข้อเสนอแนะ tab: open cases the admins hold (ADR 0030)."""
    from operations import problems_web  # local: problems_web imports views

    return problems_web._waiting().count()


def _scope(request):
    return identity.staff_scope(request.user)


def _division_or_404(scope, code):
    division = Division.objects.filter(code=code, is_active=True).first()
    # Unknown and out-of-scope divisions look the same, so URLs reveal nothing.
    if division is None or not identity.may_open_division(scope, division.pk):
        raise Http404
    return division


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


@login_required
@require_safe
def start(request):
    """Where a sign-in lands (round 2 Q9): one division goes straight to its queue."""
    scope = _scope(request)
    divisions = queues.own_divisions(scope) if scope.may_read else []
    if not scope.is_admin and len(divisions) == 1:
        return redirect("division-detail", code=divisions[0].code)
    return redirect("console")


@login_required
@require_safe
def home(request):
    """ภาพรวม (redesigned 15 ต.ค. 2569, the user: modern, easy, nothing twice): what needs
    attention now, four headline figures, the daily trend, each division's work in one table,
    and where the open cases sit. Every figure still opens the cases behind it."""
    scope = _scope(request)
    if not scope.may_read:
        return render(request, "operations/console.html", {"scope": scope, "cards": []})
    context = _report_context(request)
    link = _report_link(context["chosen"], context["days"])
    compared = {row["division"].code: row for row in context["table"]}
    now = timezone.now()
    cards = []
    for division in queues.own_divisions(scope):
        tally = queues.counts(request.user, division, now, ["host", "mine"])
        row = compared.get(division.code, {})
        cards.append(
            {
                "division": division,
                "roles": sorted(Role(r).label for r in scope.division_roles.get(division.pk, ())),
                "open": tally["host"],
                "mine": tally["mine"],
                "overdue": queues.overdue_count(request.user, division, now),
                "escalated": row.get("escalated"),
                "avg_ack": row.get("avg_ack"),
                "report_url": row.get("report_url")
                or f"?{urlencode({'division': division.code, 'days': context['days']})}",
                "escalated_url": row.get("links", {}).get("escalated", ""),
            }
        )
    busy = [c for c in cards if c["open"] or c["mine"] or c["overdue"] or c["escalated"]]
    open_count = sum(
        card["open"]
        for card in cards
        if context["chosen"] is None or card["division"] == context["chosen"]
    )
    queue_link = (
        reverse("division-detail", args=[context["chosen"].code])
        if context["chosen"]
        else reverse("queue-all")
    )
    context.update(
        scope=scope,
        cards=cards,
        busy_cards=sorted(busy, key=lambda c: (-c["overdue"], -c["open"])),
        quiet_cards=[c for c in cards if c not in busy],
        attention=_attention(request.user, scope, context["report"], link),
        open_link=queue_link,
        open_count=open_count,
        new_link=link("new", "window"),
        ended_link=link("ended", "window"),
        # Each division/centre's share of the open work, above สถานะ (user, 3 Nov 2569); one
        # division's share by kind, each kind in its own colour (user, 4 Nov 2569).
        share=_kind_share(request.user, context["chosen"], now)
        if context["chosen"]
        else charts.pie(
            [
                (
                    c["division"].code,
                    c["open"],
                    reverse("division-detail", args=[c["division"].code]) + "?tab=host",
                )
                for c in cards
            ],
            other_href=reverse("queue-all") if scope.sees_department else "",
        ),
        today=now,
    )
    return render(request, "operations/console.html", context)


def _kind_share(user, division, now):
    names = dict(KINDS)
    queue = reverse("division-detail", args=[division.code])
    return charts.pie(
        [
            (names[kind], n, f"{queue}?{urlencode({'tab': 'host', 'kind': kind})}", f"c{i + 1}")
            for i, (kind, n) in enumerate(queues.host_by_kind(user, division, now))
        ]
    )


def _attention(user, scope, report, link):
    """Only what is waiting now, most urgent first; nothing at zero (no wall of zeros)."""
    items = []

    def add(label, value, href, alert=False):
        if value:
            items.append({"label": label, "value": value, "href": href, "alert": alert})

    approver = scope.is_admin or identity.may_decide_registrations(user)
    waiting = admin_nav.badges() if approver else {}
    add("เรื่องเกินกรอบเวลา", report["overdue"], link("overdue"), alert=True)
    add("เรื่องถูกยกระดับ", report["escalated"], link("escalated"), alert=True)
    if approver:
        add("ผู้ใช้ LINE OA รอรับรอง", waiting["registrations"], reverse("admin-registrations"), True)
    submitted = next(
        (n for value, _, n in report["by_status"] if value == Case.Status.SUBMITTED), 0
    )
    add("เรื่องใหม่รอคัดกรอง", submitted, link("status", Case.Status.SUBMITTED))
    add("รอหน่วยงาน (เขต/ฝ่าย)ตอบ", report["district"], link("district"))
    add("งานย่อยค้าง", report["children"], link("children"))
    if scope.is_admin:
        add("การแจ้งเตือนที่ส่งไม่สำเร็จ", waiting["line"], reverse("admin-line"), alert=True)
        add("คำถามที่บอทตอบไม่ได้", waiting["questions"], reverse("admin-questions"))
        add("ตัวเลขติดตามระบบมีเรื่องที่ควรดู", waiting["monitoring"], reverse("admin-monitoring"), True)
    return items


def _queue(request, division, admin_all=False):
    now = timezone.now()
    scope = _scope(request)
    keys = queues.tabs_for(request.user, division, now, admin_all)
    tab = request.GET.get("tab", "host")
    if tab not in keys:
        tab = "host"
    roles = scope.division_roles.get(division.pk, frozenset()) if division else frozenset()
    filters = queues.read_filters(request.GET, roles)
    tally = queues.counts(request.user, division, now, keys, admin_all)
    cases = queues.visible(request.user).filter(
        queues.tab_condition(tab, division, request.user, now, admin_all)
    )
    cases = queues.apply_filters(queues.prefetch(cases), filters)
    try:
        number = int(request.GET.get("page", "1"))
    except ValueError:
        number = 1
    shown = request.GET.get("shown", "")
    shown = min(int(shown), 2000) if shown.isdigit() else 0
    orders = queues.order_queryset(request.user, tab, division, now, filters)
    sort = request.GET.get("sort", "newest")
    sort = sort if sort in queues.SORTS else "newest"
    items, pager = queues.page(cases, tab, number, orders, sort, shown)
    if division or admin_all:
        request.session["division"] = division.code if division else ALL_QUEUE
    context = {
        "division": division,
        "has_warehouse": bool(division) and warehouse.has_slips(division),  # ADR 0056
        "admin_all": admin_all,
        "tab": tab,
        "tabs": [(key, queues.TAB_LABELS[key], tally[key]) for key in keys],
        "filters": filters,
        "base_query": urlencode(filters.query()),
        "today_query": urlencode(filters.query(today="" if filters.today else "1")),
        "rows": queues.build_rows(items, now),
        "pager": pager,
        "shown": shown,
        "now": now,
        "kinds": KINDS,
        "levels": [(p, queues.level_name(p)) for p in Priority.values] + [("none", "ยังไม่ตั้งระดับ")],
        "sort": sort,
        "sorts": queues.SORTS.items(),
        "may_approve": bool(division)
        and approvals.no_screening()
        and (
            approvals.may_start(request.user, division.pk)
            # ADR 0052: the queue already holds only their unit's cases; each is checked again.
            or division.pk in identity.staff_scope(request.user).unit_divisions
        ),
        "statuses": [
            *Case.Status.choices,
            *(
                (value, label)
                for value, label in WorkOrder.Status.choices
                if value not in Case.Status.values
            ),
        ],
        "districts": District.objects.order_by("name"),
        "sections": list(division.sections.filter(is_active=True)) if division else [],
        "may_order": scope.is_department_supervisor,
    }
    context["registry_count"] = _registry_count(request.user, division, admin_all)
    if _is_htmx(request):
        return render(request, "operations/_queue_body.html", context)
    if division:
        context["members"] = _members(division)
    return render(request, "operations/division.html", context)


def _members(division):
    """One row per person (16 Oct 2569 Q3): แอดมินกอง or สมาชิกกอง, or each duty when
    the organisation splits the division's work."""
    grants = (
        RoleGrant.objects.filter(division=division, revoked_at__isnull=True, user__is_active=True)
        .exclude(role__in=VIEWER_ROLES)  # ADR 0039: viewers are not members
        .select_related("user__staff_profile")
        .order_by("user__username", "role")
    )
    people = {}
    for grant in grants:
        people.setdefault(grant.user, set()).add(grant.role)
    split = identity.work_is_split()
    rows = []
    for user, roles in people.items():
        if split:
            label = ", ".join(sorted(Role(r).label for r in roles))
        else:
            heads = (Role.SUPERVISOR, Role.SECTION_ADMIN, Role.WORK_GROUP_ADMIN)  # ADR 0052
            label = Role(next((r for r in heads if r in roles), Role.STAFF)).label
        profile = getattr(user, "staff_profile", None)
        rows.append({"name": profile.display_name if profile else user.username, "label": label})
    return rows


def _registry_count(user, division, admin_all):
    """M11 Q6: open ส่วนต่างทะเบียน for the registry unit (or an admin's every-division view),
    and those sent to this division for checking; None hides the chip."""
    from operations.models import RegistryDiscrepancy as RD

    if not registry.enabled():
        return None
    home = registry.registry_division()
    if admin_all or (division is not None and home is not None and division.pk == home.pk):
        return RD.objects.filter(status=RD.Status.OPEN).count()
    if division is not None and home is None:
        # ADR 0008: each division decides the discrepancies of its own facts and cases.
        mine = RD.objects.filter(status__in=[RD.Status.OPEN, RD.Status.WITH_DIVISION]).filter(
            Q(division=division) | Q(record__division=division) | Q(case__owner_division=division)
        )
        return mine.distinct().count() or None
    if division is None:
        return None
    sent = RD.objects.filter(status=RD.Status.WITH_DIVISION, division=division).count()
    return sent or None


@login_required
@require_safe
def division_queue(request, code):
    division = _division_or_404(_scope(request), code)
    return _queue(request, division)


@login_required
@require_safe
def all_queue(request):
    """The admin's every-division view (read-only for divisions they are not in; ผู้ช่วยเหลือระบบ
    acts in all of them). ADR 0039: ฝ่าย has it too."""
    scope = _scope(request)
    if not scope.sees_department:
        raise PermissionDenied
    return _queue(request, None, admin_all=True)


@login_required
@require_POST
def division_approve(request, code):
    """ADR 0025 Q26: approve the ticked cases at the level the system proposed."""
    division = _division_or_404(_scope(request), code)
    numbers = request.POST.getlist("case")[:100]
    cases = list(queues.visible(request.user).filter(owner_division=division, number__in=numbers))
    done, skipped = approvals.bulk_approve(request.user, cases)
    if done:
        messages.success(request, f"อนุมัติแล้ว {len(done)} เรื่อง")
    for case, why in skipped[:20]:
        messages.error(request, f"{case.number}: {why}")
    if not numbers:
        messages.error(request, "ติ๊กเรื่องที่จะอนุมัติก่อน")
    return redirect("division-detail", code=division.code)


# Queue tabs the tour may take a case from, in order of preference.
TOUR_TABS = ("host", "incoming", "district", "mine", "closed")


@login_required
@require_safe
def tour_where(request):
    """สาธิตการใช้งาน: a queue and a case of one kind this account can open, so the tour shows
    real screens even when the queue in view has none (e.g. an admin on a quiet division).
    The division in view comes first; with no queue holding one, the newest visible case."""
    scope = _scope(request)
    kind = request.GET.get("kind", "")
    if kind not in Case.Kind.values:
        kind = ""
    divisions = queues.own_divisions(scope) if scope.may_read else []
    current = request.session.get("division", "")
    divisions.sort(key=lambda d: d.code != current)
    visible = queues.visible(request.user)
    if kind:
        visible = visible.filter(kind=kind)
    now = timezone.now()
    for tab in TOUR_TABS:
        for division in divisions:
            cases = visible.filter(queues.tab_condition(tab, division, request.user, now))
            if not cases.exists():
                continue
            first = queues.ordered(cases, tab)[0]
            query = urlencode({"tab": tab, **({"kind": kind} if kind else {})})
            return JsonResponse(
                {
                    "queue": f"{reverse('division-detail', args=[division.code])}?{query}",
                    "case": reverse("case-detail", args=[first.number]),
                }
            )
    latest = visible.order_by("-submitted_at").first()
    return JsonResponse(
        {"queue": None, "case": reverse("case-detail", args=[latest.number]) if latest else None}
    )


def _visible_case(request, number):
    case = (
        queues.visible(request.user)
        .select_related("requester", "district", "owner_division", "assignee__staff_profile")
        .filter(number=number)
        .first()
    )
    if case is None:
        raise Http404
    return case


@login_required
@require_safe
def case_detail(request, number):
    scope = _scope(request)
    if not scope.may_read:
        raise Http404
    case = _visible_case(request, number)
    if scope.is_admin and case.owner_division_id not in scope.division_roles:
        identity.audit(request.user, "case.viewed", case, number=case.number)
    data = casepage.build(case)
    visible_ids = set(
        queues.visible(request.user)
        .filter(pk__in=[c.pk for c in data["related"]])
        .values_list("pk", flat=True)
    )
    data["related"] = [(c, c.pk in visible_ids) for c in data["related"]]
    back = request.session.get("division", "")
    codes = {d.code for d in queues.own_divisions(scope)}
    st = web_actions.situation(request.user, case)
    panel = warehouse_web.case_panel(request.user, case)
    menu = case_menu.build(request.user, case, st, panel)  # ADR 0055: the LINE card's list
    return render(
        request,
        "operations/case_detail.html",
        {
            "case": case,
            "page": data,
            "back_division": back if back in codes else "",
            "menu": menu,
            "hints": web_actions.hints(st, menu.offers),
            "can_upload": uploads.may_upload(st),
            "panel": panel,
            "tracking_numbers": case_numbers.panel(request.user, case),
            # Zero trust (31 ต.ค. 2569 Q2): a typed name is not checked against the roster.
            "unverified": not (case.requester.is_anonymous or case.requester.is_internal)
            and employees.identity_state(case.requester) != "verified",
            # ADR 0054 Q15: the รหัสพนักงาน the reporter proved, as the LINE card shows it.
            "reporter_id": case.requester.employee.employee_id
            if not case.requester.is_anonymous
            and employees.identity_state(case.requester) == "verified"
            else "",
        },
    )


# --- Actions (round 1 Q9; round 2 Q6, Q8) -------------------------------------------------------

ERROR_TEXT = {
    ErrorCode.FORBIDDEN: "บัญชีนี้ทำรายการนี้ไม่ได้",
    ErrorCode.INVALID_TRANSITION: "สถานะของเรื่องเปลี่ยนไปแล้ว ทำรายการนี้ไม่ได้ในตอนนี้",
    ErrorCode.ACTION_NOT_ENABLED: "ยังไม่เปิดใช้รายการนี้กับเรื่องชนิดนี้",
    ErrorCode.IDEMPOTENCY_CONFLICT: "ฟอร์มนี้ถูกส่งไปแล้วด้วยข้อมูลอื่น กรุณาเปิดฟอร์มใหม่",
    ErrorCode.DEPENDENCY_UNAVAILABLE: "ระบบส่วนหลังยังไม่พร้อม ลองใหม่อีกครั้งในอีกสักครู่",
    ErrorCode.EXTERNAL_OUTCOME_UNKNOWN: "ยังไม่ทราบผลจากระบบภายนอก ตรวจสอบก่อนบันทึกซ้ำ",
}


def _error_text(exc):
    """What a person reads when a step is refused: Thai, with what to do next; never the
    services' English (wording.py holds the Thai for each)."""
    text = wording.thai(exc.message)
    if text is None and exc.message and not exc.message.isascii():
        text = exc.message  # the service already speaks Thai here
    return text or ERROR_TEXT.get(exc.code) or "ข้อมูลยังไม่ครบหรือไม่ถูกต้อง ตรวจช่องที่กรอกแล้วลองอีกครั้ง"


def _form_page(request, case, form, st, *, errors=None, conflict=None, status=200, demo=False):
    web_actions.bind(form, request.POST if request.method == "POST" else None)
    context = {
        "case": case,
        "form": form,
        "values": request.POST if request.method == "POST" else {},
        "revision": int(request.POST.get("revision", case.revision))
        if request.method == "POST" and not conflict
        else case.revision,
        "idempotency_key": uuid.uuid4().hex,
        "errors": errors or [],
        "conflict": conflict,
        "query": request.GET.urlencode(),
        "demo": demo,
    }
    template = (
        "operations/_action_form.html" if _is_htmx(request) else "operations/case_action.html"
    )
    return render(request, template, context, status=200 if _is_htmx(request) else status)


@login_required
def case_action(request, number, action):
    """GET: the form of one action (a partial for HTMX). POST: perform it through the service."""
    scope = _scope(request)
    if not scope.has_any_access or action not in web_actions.RULES:
        raise Http404
    if action in web_actions.STOCK and not options.old_stock():
        raise Http404  # ADR 0032
    case = _visible_case(request, number)
    st = web_actions.situation(request.user, case)
    offered = {o.name for o in web_actions.available(request.user, case, st)}
    if request.method == "GET":
        if request.GET.get("demo") == "1":
            # สาธิตการใช้งาน: an example of the form for any action of this kind of case, shown
            # to whoever may see the case; it cannot be sent (no revision, no key, no button).
            if str(case.kind) not in workflow.ACTIONS[action].enabled_kinds:
                raise Http404
            form = web_actions.build_form(action, st, request.GET, demo=True)
            return _form_page(request, case, form, st, demo=True)
        if action not in offered:
            raise PermissionDenied
        return _form_page(request, case, web_actions.build_form(action, st, request.GET), st)
    if request.method != "POST":
        return HttpResponse(status=405)
    form = web_actions.build_form(action, st, request.POST)
    try:
        payload = web_actions.payload_from(form, request.POST)
    except web_actions.MissingConfirmation:
        return _form_page(request, case, form, st, errors=["ติ๊กยืนยันก่อนบันทึก เพราะรายการนี้ย้อนกลับไม่ได้"])
    files = request.FILES.getlist("files")
    evidence = request.FILES.getlist("evidence")
    problems = uploads.check(files) + uploads.check(evidence)
    if problems:
        return _form_page(request, case, form, st, errors=problems)
    try:
        revision = int(request.POST.get("revision", ""))
    except ValueError:
        revision = 0
    stored = []
    try:
        # M11: evidence is stored with the fact it proves; a refused action keeps neither.
        with transaction.atomic():
            if evidence and action in registry.EVIDENCE_ACTIONS:
                stored = uploads.store(
                    request.user, case, evidence, note=f"หลักฐานทะเบียน: {form.label}"
                )
                payload["evidence"] = [str(a.pk) for a in stored]
            key = request.POST.get("idempotency_key", "")
            services.perform_form_action(
                Actor.staff(request.user),
                case.pk,
                action,
                expected_revision=revision,
                idempotency_key=key,
                payload=payload,
            )
    except CommandFailed as exc:
        uploads.discard(stored)
        if exc.code == ErrorCode.NOT_FOUND:
            raise Http404 from exc
        if exc.code == ErrorCode.REVISION_CONFLICT:
            fresh = Case.objects.get(pk=case.pk)
            changes = [
                casepage.action_label(d.action)
                for d in fresh.decisions.filter(revision__gt=revision).order_by("id")
            ]
            return _form_page(request, fresh, form, st, conflict=changes, status=409)
        status = 403 if exc.code == ErrorCode.FORBIDDEN else 400
        return _form_page(request, case, form, st, errors=[_error_text(exc)], status=status)
    if files:
        uploads.store(request.user, case, files, note=form.label)
    messages.success(request, f"บันทึกแล้ว: {form.label}")
    target = reverse("case-detail", args=[case.number])
    if _is_htmx(request):
        response = HttpResponse(status=204)
        response["HX-Redirect"] = target
        return response
    return redirect(target)


@login_required
@require_POST
def case_number_edit(request, number):
    """ADR 0047: add the numbers the requester typed in the details, or remove one with a reason."""
    case = _visible_case(request, number)
    try:
        if request.POST.get("remove"):
            case_numbers.remove(
                request.user, case, int(request.POST["remove"]), request.POST.get("reason")
            )
            messages.success(request, "ลบเลขแล้ว")
        else:
            kind = request.POST.get("kind", "")
            typed = case_numbers.split(request.POST.get("typed", ""), kind)
            typed += request.POST.getlist("found")
            added = case_numbers.add(request.user, case, kind, typed)
            messages.success(request, "เพิ่มเลข " + ", ".join(n for n, _ in added) + " แล้ว")
            for n, others in added:
                if others:
                    messages.warning(request, f"เลข {n} ซ้ำกับเรื่อง {', '.join(others)} ที่ยังไม่ปิด")
    except (ValidationError, PermissionDenied, ValueError) as exc:
        messages.error(request, " ".join(getattr(exc, "messages", ["ไม่มีสิทธิ์ทำรายการนี้"])))
    return redirect(f"{reverse('case-detail', args=[case.number])}#s-numbers")


@login_required
@require_safe
def case_revision(request, number):
    """Round 1 Q15: polled every 30 s; a banner (never a reload) when the case changed."""
    if not _scope(request).may_read:
        raise Http404
    case = _visible_case(request, number)
    try:
        seen = int(request.GET.get("r", "0"))
    except ValueError:
        seen = 0
    if case.revision == seen:
        return HttpResponse(status=204)
    return render(request, "operations/_revision.html", {"case": case})


@login_required
@require_safe
def case_search(request):
    scope = _scope(request)
    if not scope.may_read:
        raise Http404
    typed = (request.GET.get("q") or "").strip()[:48]
    # ADR 0045: กคว69-142 reads as กคว.69-000142, and 69-142 finds every number ending so.
    query = numbering.read(typed) or numbering.tail(typed) or typed.upper()
    rows = []
    order = work_orders.visible(request.user).filter(number=query).first() if query else None
    if order is not None:  # a งานสั่ง number opens the order
        return redirect("order-detail", number=order.number)
    if query:
        # 23 Oct 2569: a SAP slip number finds its case too.
        visible = queues.visible(request.user)
        # ADR 0047: a number staff added finds the case too.
        cited = Q(sap_number__iexact=typed) | Q(extra_numbers__number__iexact=typed)
        cases = visible.filter(Q(number__icontains=query) | cited).distinct()
        exact = cases.filter(number=query).first()
        citing = visible.filter(cited).distinct()
        if exact is None and citing.count() == 1:
            exact = citing.get()
        if exact is not None:
            return redirect("case-detail", number=exact.number)
        orders = (
            work_orders.visible(request.user)
            .filter(number__icontains=query)
            .select_related("division", "assignee__staff_profile", "issued_by__staff_profile")
        )
        found = sorted(
            [
                *queues.prefetch(cases).order_by("-submitted_at")[:30],
                *orders.order_by("-created_at")[:30],
            ],
            key=lambda item: item.created_at if isinstance(item, WorkOrder) else item.submitted_at,
            reverse=True,
        )[:30]
        rows = queues.build_rows(found, timezone.now())
    return render(request, "operations/search.html", {"query": query, "rows": rows})


@login_required
@require_http_methods(["GET", "HEAD", "POST"])
def account(request):
    """Round 2 Q10: read-only profile; the email is set by an admin. ADR 0010: link or unlink
    LINE with a one-time code shown once here."""
    from operations import staff_line
    from operations.models import ChannelIdentity

    scope = _scope(request)
    code = None
    if request.method == "POST" and scope.has_any_access:
        if request.POST.get("action") == "line-code":
            code = staff_line.issue_code(request.user)
        elif request.POST.get("action") == "line-unlink":
            line = ChannelIdentity.objects.filter(staff_user=request.user).first()
            if line is not None and staff_line.unlink(line, request.user, "เลิกเชื่อมจากหน้าเว็บ"):
                messages.success(request, "เลิกเชื่อม LINE แล้ว")
            return redirect("account")
    grants = (
        RoleGrant.objects.filter(user=request.user, revoked_at__isnull=True)
        .select_related("division")
        .order_by("division__code", "role")
    )
    return render(
        request,
        "operations/account.html",
        {
            "scope": scope,
            "grants": grants,
            "line": ChannelIdentity.objects.filter(staff_user=request.user).first(),
            "code": code,
            "code_minutes": staff_line.CODE_MINUTES,
            "otp": options.otp_on(),
        },
    )


@login_required
@require_POST
def case_upload(request, number):
    """Round 1 Q11: staff add files to a case; they wait for the virus scan before download."""
    if not _scope(request).has_any_access:
        raise Http404
    case = _visible_case(request, number)
    if not uploads.may_upload(web_actions.situation(request.user, case)):
        raise PermissionDenied
    files = request.FILES.getlist("files")
    problems = uploads.check(files) if files else ["เลือกไฟล์ก่อน"]
    if problems:
        for problem in problems:
            messages.error(request, problem)
    else:
        note = (request.POST.get("note") or "").strip()[:200]
        stored = uploads.store(request.user, case, files, note=note)
        messages.success(request, f"แนบไฟล์แล้ว {len(stored)} ไฟล์ (รอสแกนไวรัสก่อนเปิดได้)")
    return redirect(f"{reverse('case-detail', args=[case.number])}#s-files")


# --- CSV export and reports (round 1 Q12, Q13) ------------------------------------------------


def _export(request, division, admin_all=False):
    now = timezone.now()
    scope = _scope(request)
    keys = queues.tabs_for(request.user, division, now, admin_all)
    tab = request.GET.get("tab", "host")
    if tab not in keys:
        tab = "host"
    roles = scope.division_roles.get(division.pk, frozenset()) if division else frozenset()
    filters = queues.read_filters(request.GET, roles)
    cases = queues.visible(request.user).filter(
        queues.tab_condition(tab, division, request.user, now, admin_all)
    )
    orders = queues.order_queryset(request.user, tab, division, now, filters)
    sort = request.GET.get("sort", "newest")  # the CSV keeps the order on screen
    sort = sort if sort in queues.SORTS else "newest"
    cases = queues.ordered(queues.apply_filters(queues.prefetch(cases), filters), tab, orders, sort)
    text, count, truncated = reports.export_csv(cases, now)
    identity.audit(
        request.user,
        "cases.exported",
        division or request.user,
        actor_label=f"staff:{request.user.pk}",
        division=division.code if division else "ALL",
        tab=tab,
        filters=filters.query(),
        rows=count,
        truncated=truncated,
    )
    code = division.code if division else "ALL"
    stamp = timezone.localtime(now).strftime("%Y%m%d-%H%M")
    response = HttpResponse(text, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="faoj-{code}-{tab}-{stamp}.csv"'
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@require_safe
def division_export(request, code):
    return _export(request, _division_or_404(_scope(request), code))


@login_required
@require_safe
def all_export(request):
    if not _scope(request).sees_department:
        raise PermissionDenied
    return _export(request, None, admin_all=True)


def _report_scope(request):
    """(scope, divisions, chosen division or None for every division, days) of a report page."""
    scope = _scope(request)
    if not scope.may_read:
        raise Http404
    divisions = queues.own_divisions(scope)
    code = request.GET.get("division", "")
    chosen = next((d for d in divisions if d.code == code), None)
    if code and chosen is None:
        raise Http404
    if chosen is None and not scope.sees_department and divisions:
        chosen = divisions[0]
    return scope, divisions, chosen, reports.period(request.GET.get("days"))


def _report_link(division, days):
    """URL of the cases behind one report figure (/reports/cases/)."""
    base = reverse("report-cases")

    def link(metric, value=""):
        params = {"metric": metric}
        if value:
            params["value"] = value
        if division is not None:
            params["division"] = division.code
        if days != reports.WINDOW_DAYS:
            params["days"] = days
        return f"{base}?{urlencode(params)}"

    return link


def _dashboard(report, link):
    """The report's figures with the drill-down link of each (M10 report, 9 ตุลาคม 2569)."""
    total = report["total"] or 1
    level_steps = {"P0": "o5", "P1": "o4", "P2": "o3", "P3": "o1", "none": "muted"}
    return {
        "kpis": [
            ("เรื่องที่ต้องทำ", report["open"], link("open"), "dark"),
            (
                "เกินกรอบเวลา",
                report["overdue"],
                link("overdue"),
                "alert" if report["overdue"] else "",
            ),
            (
                "ถูกยกระดับ",
                report["escalated"],
                link("escalated"),
                "alert" if report["escalated"] else "",
            ),
            ("รอหน่วยงาน (เขต/ฝ่าย)ตอบ", report["district"], link("district"), ""),
            ("งานย่อยค้าง", report["children"], link("children"), ""),
        ],
        "trend": charts.trend(report["trend"], link),
        "levels": [
            (key, n, total, link("level", key), level_steps[key]) for key, n in report["by_level"]
        ],
        "kinds": [(label, n, total, link("kind", value)) for value, label, n in report["by_kind"]],
        "sections": [
            (label, n, total, link("section", value)) for value, label, n in report["by_section"]
        ],
        "statuses": [
            (label, n, total, link("status", value)) for value, label, n in report["by_status"]
        ],
        "ages": [
            (label, n, total, link("age", key), f"o{i + 1}")
            for i, (key, label, n) in enumerate(report["aging"])
        ],
        "items": [
            (
                code,
                name,
                n,
                max(report["items"][0][2], 1) if report["items"] else 1,
                link("item", code),
            )
            for code, name, n in report["items"]
        ],
        "times": {
            "ack": link("ack"),
            "resolve": link("resolve"),
            "finish": link("finish"),
        },
    }


@login_required
@require_safe
def report(request):
    """รายงาน is part of ภาพรวม now; the old address shows the same page so links keep working."""
    return home(request)


def _report_context(request):
    """Round 1 Q13: numbers per division; the admin compares every division. Every figure links
    to the cases behind it and the dashboard draws the window (7/30/90 days)."""
    scope, divisions, chosen, days = _report_scope(request)
    now = timezone.now()
    main = reports.division_report(request.user, chosen, now, days)
    table = []
    if scope.is_admin and chosen is None:
        for division in divisions:
            row = reports.division_report(request.user, division, now, days)
            row["links"] = {
                metric: _report_link(division, days)(metric)
                for metric in ("open", "overdue", "escalated", "district", "children")
            }
            row["report_url"] = f"?{urlencode({'division': division.code, 'days': days})}"
            table.append(row)
    # ADR 0007: การทำแทนกอง, for every division (admin, ผู้ช่วยเหลือระบบ) or the viewer's own.
    everywhere = scope.is_admin or scope.is_support
    acted = support.actions(None if everywhere else scope.division_roles, days=days)[:200]
    return {
        "report": main,
        "dash": _dashboard(main, _report_link(chosen, days)),
        "divisions": divisions,
        "chosen": chosen,
        "days": days,
        "periods": reports.PERIODS,
        "table": table,
        "support_actions": acted,
    }


@login_required
@require_safe
def report_cases(request):
    """The cases behind one report figure, counted the same way as the report."""
    scope, divisions, chosen, days = _report_scope(request)
    now = timezone.now()
    found = reports.drilldown(
        request.user, chosen, request.GET.get("metric", ""), request.GET.get("value", ""), now, days
    )
    if found is None:
        raise Http404
    title, tab, cases = found
    try:
        number = int(request.GET.get("page", "1"))
    except ValueError:
        number = 1
    items, pager = queues.page(queues.prefetch(cases), "host", number)
    query = {k: v for k, v in request.GET.items() if k != "page"}
    back = {"division": chosen.code} if chosen else {}
    if days != reports.WINDOW_DAYS:
        back["days"] = days
    return render(
        request,
        "operations/report_cases.html",
        {
            "title": title,
            "chosen": chosen,
            "days": days,
            "rows": queues.build_rows(items, now),
            "pager": pager,
            "query": urlencode(query),
            "back": f"{reverse('console')}?{urlencode(back)}",
            "queue": f"{reverse('division-detail', args=[chosen.code])}?tab={tab}"
            if tab and chosen
            else "",
        },
    )


# --- Filing a case for a requester (round 1 Q16) ------------------------------------------------

NEW_CASE_ROWS = 5
KIND_TEXT_FIELDS = {
    Case.Kind.WITHDRAWAL: ["purpose", "needed_by"],
    Case.Kind.RETURN: ["condition", "reason", "source_reference"],
    Case.Kind.PROBLEM: ["description", "impact"],
}


def _intake_member(scope):
    """May file a case for a requester: intake staff, or (ADR 0008, no intake desk) staff of
    any division; the rules then route the case."""
    intake = identity.intake_division()
    if intake is None:
        return scope.is_support or bool(scope.division_roles)
    return intake.pk in scope.division_roles or scope.is_support


@login_required
def case_new(request):
    """Intake staff file a case for an approved requester (phone, letter, counter or the fallback
    form while the system was down), no AI."""
    scope = _scope(request)
    if not _intake_member(scope):
        raise PermissionDenied
    from operations.models import EquipmentType, Requester

    data = request.POST if request.method == "POST" else {}
    errors = []
    if request.method == "POST":
        kind = data.get("kind", "")
        payload = {
            "kind": kind,
            "channel": data.get("channel", ""),
            "intake_note": data.get("intake_note", "").strip(),
        }
        if (data.get("reported_at") or "").strip():
            payload["reported_at"] = data["reported_at"].strip()
        if (data.get("requester_id") or "").isdigit():
            payload["requester_id"] = int(data["requester_id"])
        for name in KIND_TEXT_FIELDS.get(kind, []):
            value = (data.get(name) or "").strip()
            if value:
                payload[name] = value
        if (data.get("related_case_number") or "").strip():
            typed = data["related_case_number"]
            payload["related_case_number"] = numbering.read(typed) or typed.strip().upper()
        if kind == Case.Kind.PROBLEM and data.get("topic"):
            payload["topic"] = data["topic"]
        lines = []
        for i in range(NEW_CASE_ROWS):
            code = (data.get(f"row-{i}-item") or "").strip()
            if not code:
                continue
            line = {"item_code": code}
            quantity = (data.get(f"row-{i}-quantity") or "").strip()
            if quantity:
                line["quantity"] = quantity
            serials = web_actions._serials(data.get(f"row-{i}-serials"))
            if serials:
                line["serials"] = serials
            lines.append(line)
        if lines:
            payload["lines"] = lines
        try:
            result = services.submit_case(
                Actor.staff(request.user),
                idempotency_key=data.get("idempotency_key", ""),
                payload=payload,
            )
        except CommandFailed as exc:
            errors.append(_error_text(exc))
        else:
            numbers = [result.case_number, *(s["case_number"] for s in result.split)]
            owners = dict(
                Case.objects.filter(number__in=numbers).values_list(
                    "number", "owner_division__code"
                )
            )
            if result.split:
                # ADR 0008: one request with items of several divisions, one case each.
                text = f"บันทึกแทนผู้แจ้งแล้ว แยกเป็น {len(numbers)} เรื่องตามกอง: " + ", ".join(
                    f"{number} ({owners[number]})" for number in numbers
                )
            else:
                text = f"บันทึกเรื่อง {numbers[0]} แทนผู้แจ้งแล้ว ส่งถึง {owners[numbers[0]]}"
            # K15: staff of any division may file, but open only their own division's cases.
            # Show the first case this account can open; otherwise stay on a new form.
            visible = set(
                queues.visible(request.user)
                .filter(number__in=numbers)
                .values_list("number", flat=True)
            )
            shown = next((number for number in numbers if number in visible), None)
            if shown is None:
                where = "เรื่องนี้" if len(numbers) == 1 else "ทุกเรื่อง"
                messages.success(request, f"{text} {where}อยู่ในคิวของกองอื่น บัญชีนี้จึงเปิดดูไม่ได้")
                return redirect("case-new")
            messages.success(request, text)
            return redirect("case-detail", number=shown)
    return render(
        request,
        "operations/case_new.html",
        {
            "values": data,
            "errors": errors,
            "requesters": Requester.objects.filter(
                status=Requester.Status.ACTIVE, is_anonymous=False
            )
            .select_related("district")
            .order_by("district__code", "display_name"),
            "items": EquipmentType.objects.filter(is_active=True).order_by("code"),
            "rows": [
                {
                    "i": i,
                    "item": data.get(f"row-{i}-item", "") if data else "",
                    "quantity": data.get(f"row-{i}-quantity", "") if data else "",
                    "serials": data.get(f"row-{i}-serials", "") if data else "",
                }
                for i in range(NEW_CASE_ROWS)
            ],
            "kinds": Case.Kind.choices,
            "topics": Case.Topic.choices,
            "channels": web_actions.STAFF_CHANNELS,
            "idempotency_key": data.get("idempotency_key") or uuid.uuid4().hex,
        },
    )
