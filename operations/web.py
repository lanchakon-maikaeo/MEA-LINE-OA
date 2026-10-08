"""Staff workspace of M10: home, division queues, the case page, search and account.

Read pages only call services.visible_cases (through queues) so a division never sees another
division's cases; admins see every division read-only and their visits to other divisions'
cases are audited (round 2 Q3). Actions on a case go through services.perform_action.
"""

import uuid
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST, require_safe

from operations import (
    casepage,
    identity,
    queues,
    registry,
    reports,
    services,
    uploads,
    web_actions,
    workflow,
)
from operations.contracts import Actor, CommandFailed, ErrorCode
from operations.models import Case, District, Division, Priority, Role, RoleGrant

ADMIN_URLS = ("admin-",)


def navigation(request):
    """Top bar data: the viewer's divisions, section and admin rights (hidden otherwise)."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    scope = identity.staff_scope(user)
    match = getattr(request, "resolver_match", None)
    name = match.url_name if match else ""
    if name and name.startswith(ADMIN_URLS):
        section = "admin"
    elif name in {"account", "password_change"}:
        section = "account"
    elif name == "reports":
        section = "reports"
    elif name and name.startswith("registry"):
        section = "registry"
    else:
        section = "queue"
    profile = getattr(user, "staff_profile", None)
    return {
        "nav": {
            "scope": scope,
            "divisions": queues.own_divisions(scope) if scope.has_any_access else [],
            "section": section,
            "url_name": name,
            "current": (match.kwargs.get("code") if match else None)
            or request.session.get("division", ""),
            "name": profile.display_name if profile else user.username,
            "is_intake": _intake_member(scope),
            "registry": registry.may_open(user),
        }
    }


def _scope(request):
    return identity.staff_scope(request.user)


def _division_or_404(scope, code):
    division = Division.objects.filter(code=code, is_active=True).first()
    # Unknown and out-of-scope divisions look the same, so URLs reveal nothing.
    if division is None or not identity.can_access_division(scope, division.pk):
        raise Http404
    return division


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


@login_required
@require_safe
def start(request):
    """Where a sign-in lands (round 2 Q9): one division goes straight to its queue."""
    scope = _scope(request)
    divisions = queues.own_divisions(scope) if scope.has_any_access else []
    if not scope.is_admin and len(divisions) == 1:
        return redirect("division-detail", code=divisions[0].code)
    return redirect("console")


@login_required
@require_safe
def home(request):
    """ภาพรวม: a card of numbers per division the account works in (every division for admins)."""
    scope = _scope(request)
    if not scope.has_any_access:
        return render(request, "operations/console.html", {"scope": scope, "cards": []})
    divisions = queues.own_divisions(scope)
    now = timezone.now()
    cards = []
    for division in divisions:
        tally = queues.counts(request.user, division, now, ["host", "mine"])
        roles = sorted(Role(r).label for r in scope.division_roles.get(division.pk, ()))
        cards.append(
            {
                "division": division,
                "roles": roles,
                "open": tally["host"],
                "mine": tally["mine"],
                "overdue": queues.overdue_count(request.user, division, now),
            }
        )
    return render(request, "operations/console.html", {"scope": scope, "cards": cards})


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
    items, pager = queues.page(cases, tab, number)
    if division:
        request.session["division"] = division.code
    context = {
        "division": division,
        "admin_all": admin_all,
        "tab": tab,
        "tabs": [(key, queues.TAB_LABELS[key], tally[key]) for key in keys],
        "filters": filters,
        "base_query": urlencode(filters.query()),
        "rows": queues.build_rows(items, now),
        "pager": pager,
        "now": now,
        "kinds": Case.Kind.choices,
        "levels": [(p, queues.level_name(p)) for p in Priority.values] + [("none", "รอคัดกรอง")],
        "statuses": Case.Status.choices,
        "districts": District.objects.order_by("name"),
    }
    context["registry_count"] = _registry_count(request.user, division, admin_all)
    if _is_htmx(request):
        return render(request, "operations/_queue_body.html", context)
    if division:
        context["members"] = (
            RoleGrant.objects.filter(
                division=division, revoked_at__isnull=True, user__is_active=True
            )
            .select_related("user__staff_profile")
            .order_by("user__username", "role")
        )
    return render(request, "operations/division.html", context)


def _registry_count(user, division, admin_all):
    """M11 Q6: open ส่วนต่างทะเบียน for the registry unit (or an admin's every-division view),
    and those sent to this division for checking; None hides the chip."""
    from operations.models import RegistryDiscrepancy as RD

    home = registry.registry_division()
    if admin_all or (division is not None and home is not None and division.pk == home.pk):
        return RD.objects.filter(status=RD.Status.OPEN).count()
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
    """The admin's every-division view (read-only for divisions they are not in)."""
    if not _scope(request).is_admin:
        raise PermissionDenied
    return _queue(request, None, admin_all=True)


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
    if not scope.has_any_access:
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
    offers = web_actions.available(request.user, case, st)
    return render(
        request,
        "operations/case_detail.html",
        {
            "case": case,
            "page": data,
            "back_division": back if back in codes else "",
            "offers": offers,
            "primary": next((o for o in offers if o.primary), None),
            "has_irreversible": any(o.irreversible for o in offers),
            "can_upload": uploads.may_upload(st),
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
# English service messages the staff may meet, in plain Thai.
MESSAGE_TEXT = {
    "A reason is required.": "ต้องระบุเหตุผล",
    "Give facts to confirm or names to clear.": "เลือกข้อเท็จจริงที่จะยืนยันหรือล้างอย่างน้อยหนึ่งข้อ",
    "Differs from the proposal: choose a reason category and explain briefly.": (
        "กองที่เลือกต่างจากข้อเสนอ เลือกเหตุผลและอธิบายสั้น ๆ"
    ),
    "A handoff is already waiting for an answer.": "มีการส่งต่อที่รอกองปลายทางตอบอยู่แล้ว",
    "The case already belongs to this division.": "เรื่องนี้อยู่ที่กองนี้อยู่แล้ว",
    "The route is waiting for a coordinator's decision.": "เรื่องนี้รอผู้ประสานตัดสินอยู่",
}


def _error_text(exc):
    message = MESSAGE_TEXT.get(exc.message, exc.message)
    base = ERROR_TEXT.get(exc.code, "")
    if message and not message.isascii():
        return f"{base} ({message})" if base and exc.code != ErrorCode.VALIDATION_ERROR else message
    if exc.code == ErrorCode.VALIDATION_ERROR:
        return f"ข้อมูลยังไม่ครบหรือไม่ถูกต้อง{f' ({message})' if message else ''}"
    return base or "บันทึกไม่สำเร็จ"


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
            services.perform_action(
                Actor.staff(request.user),
                case.pk,
                action,
                expected_revision=revision,
                idempotency_key=request.POST.get("idempotency_key", ""),
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
@require_safe
def case_revision(request, number):
    """Round 1 Q15: polled every 30 s; a banner (never a reload) when the case changed."""
    if not _scope(request).has_any_access:
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
    if not scope.has_any_access:
        raise Http404
    query = (request.GET.get("q") or "").strip().upper()[:20]
    rows = []
    if query:
        cases = queues.visible(request.user).filter(number__icontains=query)
        exact = cases.filter(number=query).first()
        if exact is not None:
            return redirect("case-detail", number=exact.number)
        found = list(queues.prefetch(cases).order_by("-submitted_at")[:30])
        rows = queues.build_rows(found, timezone.now())
    return render(request, "operations/search.html", {"query": query, "rows": rows})


@login_required
@require_safe
def account(request):
    """Round 2 Q10: read-only profile; the email is set by an admin."""
    scope = _scope(request)
    grants = (
        RoleGrant.objects.filter(user=request.user, revoked_at__isnull=True)
        .select_related("division")
        .order_by("division__code", "role")
    )
    return render(request, "operations/account.html", {"scope": scope, "grants": grants})


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
    cases = queues.ordered(queues.apply_filters(queues.prefetch(cases), filters), tab)
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
    if not _scope(request).is_admin:
        raise PermissionDenied
    return _export(request, None, admin_all=True)


@login_required
@require_safe
def report(request):
    """Round 1 Q13: numbers per division; the admin compares every division."""
    scope = _scope(request)
    if not scope.has_any_access:
        raise Http404
    divisions = queues.own_divisions(scope)
    code = request.GET.get("division", "")
    chosen = next((d for d in divisions if d.code == code), None)
    if chosen is None and not scope.is_admin and divisions:
        chosen = divisions[0]
    now = timezone.now()
    main = reports.division_report(request.user, chosen, now)
    table = (
        [reports.division_report(request.user, d, now) for d in divisions]
        if scope.is_admin and chosen is None
        else []
    )
    return render(
        request,
        "operations/reports.html",
        {"report": main, "divisions": divisions, "chosen": chosen, "table": table},
    )


# --- Filing a case for a requester (round 1 Q16) ------------------------------------------------

NEW_CASE_ROWS = 5
KIND_TEXT_FIELDS = {
    Case.Kind.WITHDRAWAL: ["purpose", "needed_by"],
    Case.Kind.RETURN: ["condition", "reason", "source_reference"],
    Case.Kind.PROBLEM: ["description", "impact"],
}


def _intake_member(scope):
    intake = Division.objects.filter(is_intake=True, is_active=True).first()
    return intake is not None and intake.pk in scope.division_roles


@login_required
def case_new(request):
    """Intake staff file a case for an approved requester (phone, letter or counter), no AI."""
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
        if (data.get("requester_id") or "").isdigit():
            payload["requester_id"] = int(data["requester_id"])
        for name in KIND_TEXT_FIELDS.get(kind, []):
            value = (data.get(name) or "").strip()
            if value:
                payload[name] = value
        if (data.get("related_case_number") or "").strip():
            payload["related_case_number"] = data["related_case_number"].strip().upper()
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
            case = Case.objects.get(pk=result.case_id)
            messages.success(request, f"บันทึกเรื่อง {case.number} แทนผู้แจ้งแล้ว เรื่องเข้าคิวคัดกรอง")
            return redirect("case-detail", number=case.number)
    return render(
        request,
        "operations/case_new.html",
        {
            "values": data,
            "errors": errors,
            "requesters": Requester.objects.filter(status=Requester.Status.ACTIVE)
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
            "channels": web_actions.STAFF_CHANNELS,
            "idempotency_key": data.get("idempotency_key") or uuid.uuid4().hex,
        },
    )
