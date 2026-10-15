import json
from collections import defaultdict
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import PasswordChangeView
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import Prefetch, Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from operations import (
    ai,
    ai_chat,
    ai_eval,
    answers,
    attachments,
    email_codes,
    employees,
    forms,
    identity,
    inventory,
    notifications,
    options,
    orchestration,
    policy,
    questions,
    queues,
    roster,
    sealing,
)
from operations.models import (
    AIConfigVersion,
    AICredential,
    AIProfile,
    AIRun,
    Allocation,
    Attachment,
    AuditEvent,
    AuthorityGrant,
    ChannelIdentity,
    ChatAnswer,
    ConversationMessage,
    District,
    Division,
    Employee,
    EquipmentType,
    FakeStock,
    LineQuotaSnapshot,
    NotificationDelivery,
    OutboxEvent,
    PolicyVersion,
    RegistrationRequest,
    Requester,
    Role,
    RoleGrant,
    RosterUpload,
    Section,
    StockMovement,
    StockSnapshot,
    UnansweredQuestion,
)

User = get_user_model()


@require_safe
def index(request):
    return render(request, "operations/index.html")


@require_safe
def live(request):
    return JsonResponse({"status": "ok"})


@require_safe
def ready(request):
    try:
        # Connectivity plus a fully migrated schema: an empty DB, or code deployed ahead of its
        # migrations, is not ready to serve traffic.
        executor = MigrationExecutor(connection)
        if executor.migration_plan(executor.loader.graph.leaf_nodes()):
            raise DatabaseError("Unapplied migrations")
    except DatabaseError:
        return JsonResponse({"status": "unavailable"}, status=503)
    # AI/LINE availability is deliberately independent of the core database check.
    return JsonResponse({"status": "ok"})


class StaffPasswordChangeView(PasswordChangeView):
    template_name = "operations/password_change.html"
    success_url = reverse_lazy("start")

    def form_valid(self, form):
        response = super().form_valid(form)
        identity.password_changed(self.request.user)
        messages.success(self.request, "เปลี่ยนรหัสผ่านแล้ว")
        return response


EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "application/pdf": "pdf"}


@login_required
@require_safe
def attachment_download(request, pk):
    """Scanned-clean files of cases the viewer may see; everything else is 404."""
    attachment = (
        Attachment.objects.select_related("case")
        .filter(pk=pk, case__isnull=False, scan_status=Attachment.ScanStatus.CLEAN)
        .first()
    )
    # Same scope as the case page (M10 round 2 Q1/Q2): whoever may see the case.
    if attachment is None or not identity.staff_scope(request.user).has_any_access:
        raise Http404
    if not queues.visible(request.user).filter(pk=attachment.case_id).exists():
        raise Http404
    path = attachments.storage_dir() / attachment.storage_name
    if not attachment.storage_name or not path.is_file():
        raise Http404
    position = list(attachment.case.attachments.values_list("pk", flat=True)).index(attachment.pk)
    filename = f"{attachment.case.number}-{position + 1}.{EXTENSIONS[attachment.content_type]}"
    response = FileResponse(
        path.open("rb"), as_attachment=True, filename=filename, content_type=attachment.content_type
    )
    response["Cache-Control"] = "private, no-store"
    return response


# --- Admin console ---------------------------------------------------------------------------


def admin_required(view):
    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not identity.staff_scope(request.user).is_admin:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapper


def registration_required(view):
    """Admins, or whoever ตัวเลือกระบบ lets approve LINE registrations (ADR 0009)."""

    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not identity.may_decide_registrations(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapper


def _run(request, action, success):
    """Run a service call and report validation errors without leaking internals."""
    try:
        result = action()
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return None
    messages.success(request, success)
    return result


@admin_required
@require_safe
def admin_home(request):
    """หน้าแรกแอดมิน (15 ต.ค. 2569): what waits for an admin, then every settings page by
    group. The groups, hints and badges come from admin_nav through the navigation context."""
    return render(request, "operations/admin_home.html")


@admin_required
@require_safe
def admin_users(request):
    grants = RoleGrant.objects.filter(revoked_at__isnull=True).select_related("division")
    users = list(
        User.objects.select_related("staff_profile")
        .prefetch_related(Prefetch("role_grants", queryset=grants, to_attr="active_grants"))
        .order_by("username")
    )
    for user in users:
        codes = sorted({g.division.code for g in user.active_grants if g.division})
        user.division_codes = codes
        user.roles_label = ", ".join(sorted({Role(g.role).label for g in user.active_grants}))
    divisions = sorted({code for u in users for code in u.division_codes})
    return render(
        request,
        "operations/admin_users.html",
        {
            "users": users,
            "user_filters": [
                ("division", "กอง", [(code, code) for code in divisions]),
                ("status", "สถานะ", [("active", "ใช้งาน"), ("suspended", "ระงับ")]),
            ],
        },
    )


@admin_required
def admin_user_new(request):
    form = forms.StaffCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        result = _run(
            request,
            lambda: identity.create_staff_user(request.user, **form.cleaned_data),
            "สร้างบัญชีแล้ว (ยังไม่มีบทบาท)",
        )
        if result:
            user, password = result
            return render(
                request,
                "operations/admin_password_once.html",
                {"target": user, "password": password},
            )
    return render(request, "operations/admin_user_new.html", {"form": form})


@admin_required
@require_safe
def admin_user_detail(request, pk):
    target = get_object_or_404(User.objects.select_related("staff_profile"), pk=pk)
    grants = RoleGrant.objects.filter(user=target).select_related("division").order_by("-pk")
    held = {g.division_id for g in grants if g.revoked_at is None and g.division_id}
    return render(
        request,
        "operations/admin_user_detail.html",
        {
            "target": target,
            "grants": grants,
            # Q13: a แผนก in a division where this account holds a role.
            "active_divisions": Division.objects.filter(is_active=True).order_by("kind", "code"),
            "line": getattr(target, "line_identity", None),
            "sections": Section.objects.filter(division_id__in=held, is_active=True)
            .select_related("division")
            .order_by("division__code", "order"),
            "grant_form": forms.GrantRoleForm(),
            "move_form": forms.ChangeDivisionForm(),
        },
    )


@admin_required
@require_POST
def admin_user_action(request, pk, action):
    target = get_object_or_404(User, pk=pk)
    actor = request.user
    data = request.POST
    reason = data.get("reason", "")
    if action == "grant":
        form = forms.GrantRoleForm(data)
        if form.is_valid():
            _run(
                request,
                lambda: identity.grant_role(actor, target, **form.cleaned_data),
                "เพิ่มบทบาทแล้ว",
            )
        else:
            messages.error(request, "ข้อมูลบทบาทไม่ถูกต้อง")
    elif action == "revoke":
        grant_id = data.get("grant_id", "")
        if not grant_id.isdigit():
            raise Http404
        grant = get_object_or_404(RoleGrant, pk=int(grant_id), user=target)
        _run(request, lambda: identity.revoke_role(actor, grant, reason), "ถอนบทบาทแล้ว")
    elif action == "move":
        form = forms.ChangeDivisionForm(data)
        if form.is_valid():
            _run(
                request,
                lambda: identity.change_division(actor, target, **form.cleaned_data),
                "ย้ายกองแล้ว",
            )
        else:
            messages.error(request, "ข้อมูลการย้ายกองไม่ถูกต้อง")
    elif action == "full-division":
        # ทำได้ทุกงานในกอง: every role and the division's authority in one step (ADR 0009).
        value = data.get("division", "")
        if not value.isdigit():
            raise Http404
        division = get_object_or_404(Division, pk=int(value))
        result = _run(
            request,
            lambda: identity.grant_full_division(
                actor, target, division, reason, data.get("approval_reference", "")
            ),
            f"ให้ทำได้ทุกงานใน {division.code} แล้ว",
        )
        if result and result == (0, 0):
            messages.info(request, "บัญชีนี้มีทุกบทบาทและอำนาจของกองนี้อยู่แล้ว")
    elif action == "employee-id":
        _run(
            request,
            lambda: identity.set_employee_id(actor, target, data.get("employee_id", ""), reason),
            "บันทึกรหัสพนักงานแล้ว",
        )
    elif action == "line-unlink":
        from operations import staff_line
        from operations.models import ChannelIdentity

        line = ChannelIdentity.objects.filter(staff_user=target).first()
        if line is not None:
            staff_line.unlink(line, actor, reason)
            messages.success(request, "เลิกเชื่อม LINE ของบัญชีนี้แล้ว")
    elif action == "section":
        value = data.get("section", "")
        section = None
        if value:
            if not value.isdigit():
                raise Http404
            section = get_object_or_404(Section, pk=int(value))
        _run(
            request,
            lambda: identity.set_section(actor, target, section, reason),
            "เปลี่ยนแผนกแล้ว",
        )
    elif action in {"suspend", "activate"}:
        _run(
            request,
            lambda: identity.set_staff_active(actor, target, action == "activate", reason),
            "เปลี่ยนสถานะบัญชีแล้ว",
        )
    elif action == "revoke-sessions":
        _run(
            request,
            lambda: identity.revoke_sessions(actor, target, reason),
            "ออกจากระบบทุกเครื่องแล้ว",
        )
    elif action == "reset-password":
        password = _run(
            request,
            lambda: identity.reset_password(actor, target, reason),
            "ตั้งรหัสผ่านชั่วคราวแล้ว",
        )
        if password:
            return render(
                request,
                "operations/admin_password_once.html",
                {"target": target, "password": password},
            )
    else:
        raise Http404
    return redirect("admin-user-detail", pk=target.pk)


@registration_required
@require_safe
def admin_registrations(request):
    pending = RegistrationRequest.objects.filter(
        status=RegistrationRequest.Status.PENDING
    ).select_related("requested_district", "identity", "employee")
    recent = (
        RegistrationRequest.objects.exclude(status=RegistrationRequest.Status.PENDING)
        .select_related("requested_district", "decided_by")
        .order_by("-decided_at")[:50]
    )
    # Anonymous reporters are not approved people, and staff never see who they are (ADR 0010).
    people = (
        Requester.objects.filter(is_anonymous=False)
        .select_related("district", "division", "employee")
        .order_by("-approved_at")
    )
    requesters = _requester_rows(people.exclude(status=Requester.Status.REVOKED)[:100])
    revoked = _requester_rows(people.filter(status=Requester.Status.REVOKED)[:100])
    states = [row["state"] for row in requesters]
    is_admin = identity.staff_scope(request.user).is_admin
    return render(
        request,
        "operations/admin_registrations.html",
        {
            "layout": "operations/admin_base.html" if is_admin else "operations/base.html",
            "is_admin": is_admin,
            "pending": pending,
            "recent": recent,
            "requesters": requesters,
            "revoked": revoked,
            "to_review": states.count("review"),
            "to_confirm": states.count("name"),
            # Who may talk to the bot (user, 14 Oct 2569: "ให้แสดงในหน้ารับรองด้วย").
            "is_production": settings.APP_ENV == "production",
            "audience": options.get(options.LINE_AUDIENCE),
            "audience_option": options.OPTIONS[options.LINE_AUDIENCE],
            "testers": len(settings.LINE_ALLOWED_RECIPIENTS),
            "check_label": options.OPTIONS[options.REGISTRATION_CHECK].label_of(
                options.get(options.REGISTRATION_CHECK)
            ),
            "approver_label": options.OPTIONS[options.REGISTRATION].label_of(
                options.get(options.REGISTRATION)
            ),
            "requester_filters": [
                (
                    "district",
                    "เขต",
                    list(
                        District.objects.filter(is_active=True)
                        .order_by("code")
                        .values_list("code", "name")
                    ),
                ),
                (
                    "status",
                    "สถานะ",
                    [c for c in Requester.Status.choices if c[0] != Requester.Status.REVOKED],
                ),
                ("identity", "ตัวตน", employees.IDENTITY_STATES),
            ],
            "requester_form": forms.RequesterCreateForm(),
        },
    )


def _requester_rows(requesters):
    """What the requester list says about each person (user, 14 Oct 2569): how we know who they
    are, their position from ทะเบียนพนักงาน, and their role in the system."""
    requesters = list(requesters)
    roles = identity.requester_roles(requesters)
    people = employees.name_index()
    lines = defaultdict(list)
    for line in ChannelIdentity.objects.filter(requester__in=requesters):
        lines[line.requester_id].append(line)
    link = Requester.EmployeeLink
    rows = []
    for requester in requesters:
        own = lines[requester.pk]
        if requester.is_internal:
            channel = "LINE ของเจ้าหน้าที่"
        elif any(line.status == ChannelIdentity.Status.ACTIVE for line in own):
            channel = "LINE"
        elif own:
            channel = "LINE ถูกถอน"
        else:
            channel = "ไม่ใช้ LINE (แอดมินเพิ่มโดยตรง)"
        employee = requester.employee
        if employee is None:
            proof = "ยังไม่ผูกทะเบียนพนักงาน"
        elif requester.employee_link == link.NAME:
            proof = f"ชื่อตรงทะเบียน {employee.employee_id} (ยังไม่ได้ยืนยัน)"
        elif requester.employee_link == link.ADMIN:
            proof = f"แอดมินผูกรหัสพนักงาน {employee.employee_id}"
        else:
            proof = f"ยืนยันรหัสพนักงาน {employee.employee_id}"
        found = employees.same_name(requester, people)
        state = employees.identity_state(requester, found)
        rows.append(
            {
                "requester": requester,
                "state": state,
                "note": employees.review_note(requester, found) if state == "review" else "",
                "same_name": found,
                "proof": proof,
                "channel": channel,
                # ตำแหน่ง · ฝ่าย (15 ต.ค. 2569 Q15); a closed roster row is flagged (Q29).
                "position": roster.employee_unit(employee) if employee else "",
                "stale": employee is not None and not employee.is_active,
                "role": " · ".join(
                    [
                        "ผู้ใช้ LINE OA (ภายใน)" if requester.is_internal else "ผู้ใช้ LINE OA",
                        *roles[requester.pk],
                    ]
                ),
                "where": requester.division.code
                if requester.is_internal and requester.division_id
                else requester.district.name,
            }
        )
    return rows


REQUESTER_MESSAGES = 300  # newest messages on one page
REQUESTER_HISTORY = {
    "registration.approved": "รับรองการลงทะเบียน",
    "registration.auto_approved": "ระบบรับรองเอง (ตรงทะเบียนพนักงาน)",
    "requester.created_direct": "แอดมินเพิ่มผู้แจ้งโดยตรง",
    "requester.internal_created": "เริ่มเป็นผู้แจ้งภายใน",
    "requester.suspended": "ระงับ",
    "requester.activated": "เปิดใช้อีกครั้ง",
    "requester.revoked": "ถอนการรับรอง",
    "requester.messages_viewed": "เปิดดูข้อความที่พิมพ์มา",
    "requester.employee_matched": "ระบบผูกทะเบียนพนักงานจากชื่อที่ตรง",
    "requester.employee_unmatched": "ระบบยกเลิกการผูก เพราะชื่อไม่ตรงพนักงานคนเดียวแล้ว",
    "requester.employee_linked": "แอดมินผูกทะเบียนพนักงาน",
    "requester.employee_confirmed": "แอดมินยืนยันว่าเป็นพนักงานที่ชื่อตรง",
    "requester.employee_unlinked": "แอดมินยกเลิกการผูกทะเบียนพนักงาน",
    "requester.district_changed": "แอดมินแก้เขต",
}
ACTOR_LABELS = {"system": "ระบบ", "line": "LINE", "manage.py": "คำสั่งของผู้ดูแลระบบ"}


@admin_required
@require_safe
def admin_requester_detail(request, pk):
    requester = get_object_or_404(
        Requester.objects.select_related(
            "district", "division", "employee", "approved_by__staff_profile"
        ),
        pk=pk,
        is_anonymous=False,
    )
    events = (
        AuditEvent.objects.filter(
            Q(target_type="Requester", target_id=str(requester.pk))
            | Q(
                action__in=["registration.approved", "registration.auto_approved"],
                data__requester_id=requester.pk,
            )
        )
        .select_related("actor__staff_profile")
        .order_by("-pk")[:50]
    )
    row = _requester_rows([requester])[0]
    # Employees with the same name, for an admin to confirm (user, 14 Oct 2569): while the
    # link is not proved, whether the system tied one by name or left it to an admin.
    candidates = [
        {
            "employee": e,
            "place": e.district.name
            if e.district_id
            else (e.division.code if e.division_id else ""),
            "note": _candidate_note(requester, e, row["same_name"]),
        }
        for e in (row["same_name"] if row["state"] != "verified" else [])
    ]
    history = []
    for event in events:
        actor = event.actor
        data = event.data if event.action.startswith("requester.") else {}
        detail = f" · รหัสพนักงาน {data['employee']}" if data.get("employee") else ""
        if data.get("district"):
            detail = f" · {data.get('previous', '')} → {data['district']}"
        history.append(
            {
                "at": event.created_at,
                "what": REQUESTER_HISTORY.get(event.action, event.action) + detail,
                "who": getattr(getattr(actor, "staff_profile", None), "display_name", "")
                or getattr(actor, "username", "")
                or ACTOR_LABELS.get(event.actor_label, event.actor_label or "ระบบ"),
                "why": event.reason,
            }
        )
    return render(
        request,
        "operations/admin_requester_detail.html",
        {
            "requester": requester,
            "row": row,
            "candidates": candidates,
            "districts": District.objects.filter(is_active=True).order_by("code"),
            "lines": ChannelIdentity.objects.filter(requester=requester).select_related(
                "staff_user__staff_profile"
            ),
            "history": history,
            "message_count": ConversationMessage.objects.filter(requester=requester).count(),
            "case_count": requester.cases.count(),
            "managed": requester.status != Requester.Status.REVOKED,
        },
    )


def _candidate_note(requester, employee, found):
    """Why the system did not tie this employee by itself, for the admin who checks."""
    if employee.pk == requester.employee_id:
        return "ผูกจากชื่ออยู่ตอนนี้ ยังไม่ได้ยืนยัน"
    notes = []
    if len(found) > 1:
        notes.append(f"ชื่อนี้มี {len(found)} คนในทะเบียน")
    if employee.district_id not in (None, requester.district_id):
        notes.append(f"ทะเบียนระบุ{employee.district.name}")
    if requester.employee_link == Requester.EmployeeLink.UNLINKED:
        notes.append("แอดมินเคยยกเลิกการผูก")
    return " · ".join(notes)


@admin_required
@require_safe
def admin_requester_messages(request, pk):
    """What the person typed in LINE as this requester, never what the same LINE account typed
    while reporting anonymously (ADR 0010). Admins only, and every opening is audited."""
    requester = get_object_or_404(Requester, pk=pk, is_anonymous=False)
    rows = ConversationMessage.objects.filter(requester=requester)
    total = rows.count()
    shown = list(
        rows.select_related("case", "draft__case").order_by("-created_at", "-pk")[
            :REQUESTER_MESSAGES
        ]
    )
    for message in shown:
        # A case or draft of another requester (an anonymous one of the same LINE account) is
        # never named here, so this page cannot tell who reported it.
        draft = message.draft if message.draft and message.draft.requester_id == pk else None
        if message.case and message.case.requester_id == pk:
            message.about, message.about_case = "ตอบที่ขอข้อมูลเพิ่มของเรื่อง", message.case
        elif draft is not None and draft.case is not None:
            message.about, message.about_case = "ร่างที่ยื่นเป็นเรื่อง", draft.case
        elif draft is not None:
            kind = f" {draft.get_kind_display()}" if draft.kind else ""
            message.about = f"ร่างเรื่อง{kind} ({draft.get_status_display()})"
            message.about_case = None
        else:
            message.about, message.about_case = "ข้อความทั่วไป", None
    identity.audit(request.user, "requester.messages_viewed", requester, shown=len(shown))
    return render(
        request,
        "operations/admin_requester_messages.html",
        {"requester": requester, "rows": shown, "total": total},
    )


@admin_required
@require_POST
def admin_requester_action(request, pk, action):
    requester = get_object_or_404(Requester, pk=pk, is_anonymous=False)
    actor, data = request.user, request.POST
    reason = data.get("reason", "")
    if action in {"suspend", "activate"}:
        _run(
            request,
            lambda: identity.set_requester_active(actor, requester, action == "activate", reason),
            "เปิดใช้ผู้ใช้แล้ว" if action == "activate" else "ระงับผู้ใช้แล้ว",
        )
    elif action == "revoke":
        if data.get("confirm") != "1":
            messages.error(request, "ติ๊กยืนยันก่อนถอนการรับรอง")
        else:
            _run(
                request,
                lambda: identity.revoke_requester(actor, requester, reason),
                "ถอนการรับรองแล้ว ถ้าจะใช้ LINE ต่อต้องลงทะเบียนใหม่",
            )
    elif action == "link-employee":
        _run(
            request,
            lambda: identity.link_requester_employee(
                actor, requester, data.get("employee_id", ""), reason
            ),
            "ผูกกับทะเบียนพนักงานแล้ว",
        )
    elif action == "unlink-employee":
        _run(
            request,
            lambda: identity.unlink_requester_employee(actor, requester, reason),
            "ยกเลิกการผูกกับทะเบียนพนักงานแล้ว",
        )
    elif action == "district":
        _run(
            request,
            lambda: identity.set_requester_district(
                actor, requester, data.get("district", ""), reason
            ),
            "แก้เขตแล้ว เรื่องเดิมยังอยู่เขตเดิม เรื่องใหม่ใช้เขตนี้",
        )
    else:
        raise Http404
    return redirect("admin-requester-detail", pk=requester.pk)


@admin_required
@require_POST
def admin_requester_new(request):
    form = forms.RequesterCreateForm(request.POST)
    if form.is_valid():
        data = form.cleaned_data
        _run(
            request,
            lambda: identity.create_requester(
                request.user, data["display_name"], data["district"].code, data["reason"]
            ),
            "รับรองผู้แจ้งโดยตรงแล้ว",
        )
    else:
        messages.error(request, "ข้อมูลผู้แจ้งไม่ถูกต้อง")
    return redirect("admin-registrations")


@registration_required
@require_POST
def admin_registration_action(request, pk, action):
    registration = get_object_or_404(RegistrationRequest, pk=pk)
    reason = request.POST.get("reason", "")
    if action == "approve":
        _run(
            request,
            lambda: identity.approve_registration(request.user, registration, reason),
            "รับรองผู้ใช้แล้ว",
        )
    elif action == "reject":
        _run(
            request,
            lambda: identity.reject_registration(request.user, registration, reason),
            "ปฏิเสธคำขอแล้ว",
        )
    else:
        raise Http404
    return redirect("admin-registrations")


@admin_required
def admin_divisions(request):
    form = forms.DivisionCreateForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            _run(
                request,
                lambda: identity.create_division(request.user, **form.cleaned_data),
                "เพิ่มกองแล้ว",
            )
            return redirect("admin-divisions")
    divisions = Division.objects.all()
    return render(
        request,
        "operations/admin_divisions.html",
        {
            "divisions": divisions,
            "form": form,
            "division_filters": [
                ("status", "สถานะ", [("active", "ใช้งาน"), ("closed", "ปิด")]),
                ("kind", "ประเภท", Division.Kind.choices),
            ],
        },
    )


@admin_required
@require_POST
def admin_division_action(request, pk, action):
    division = get_object_or_404(Division, pk=pk)
    reason = request.POST.get("reason", "")
    if action == "set-intake":
        _run(
            request,
            lambda: identity.set_intake_division(request.user, division, reason),
            "ตั้งกองรับเรื่องกลางแล้ว",
        )
    elif action == "clear-intake":
        _run(
            request,
            lambda: identity.clear_intake_division(request.user, reason),
            "ยกเลิกกองรับเรื่องกลางแล้ว เรื่องใหม่ส่งตรงถึงกองตามกติกา",
        )
    elif action in {"activate", "deactivate"}:
        _run(
            request,
            lambda: identity.set_division_active(
                request.user, division, action == "activate", reason
            ),
            "เปลี่ยนสถานะกองแล้ว",
        )
    else:
        raise Http404
    return redirect("admin-divisions")


@admin_required
@require_safe
def admin_line(request):
    """Message budget, deliveries that did not go out, and the outbox dead-letter view."""
    state = notifications.budget_state()
    return render(
        request,
        "operations/admin_line.html",
        {
            "state": state,
            "snapshot": LineQuotaSnapshot.objects.filter(
                month=notifications.month_key(timezone.now())
            ).first(),
            "push_enabled": notifications.push_enabled(),
            "held": NotificationDelivery.objects.filter(
                status__in=["deferred", "unknown", "failed"]
            ).select_related("case")[:50],
            "dead": OutboxEvent.objects.filter(status=OutboxEvent.Status.FAILED).order_by("-id")[
                :50
            ],
            "health": orchestration.health(),
        },
    )


@admin_required
@require_POST
def admin_requeue(request, pk):
    event = get_object_or_404(OutboxEvent, pk=pk)
    reason = request.POST.get("reason", "").strip()
    if not reason:
        messages.error(request, "ต้องระบุเหตุผล")
    elif orchestration.requeue(event):
        identity.audit(request.user, "outbox.requeued", event, reason, event_type=event.event_type)
        messages.success(request, "ส่งงานกลับเข้าคิวแล้ว")
    else:
        messages.error(request, "งานนี้ไม่ได้อยู่ใน dead-letter แล้ว")
    return redirect("admin-line")


# --- Rules and authority (M06) ----------------------------------------------------------------


@admin_required
def admin_policy(request):
    """Rule versions (imported by command, activated here) and authority grants (Q7/Q16)."""
    grant_form = forms.AuthorityGrantForm(request.POST or None)
    if request.method == "POST":
        if grant_form.is_valid():
            d = grant_form.cleaned_data
            _run(
                request,
                lambda: policy.grant_authority(
                    request.user,
                    d["user"],
                    d["activity"],
                    d["reason"],
                    d["approval_reference"],
                    d["division"],
                    d["valid_from"],
                    d["valid_until"],
                    category=d["category"],
                    item=d["item"],
                    max_quantity=d["max_quantity"],
                ),
                "บันทึกอำนาจแล้ว",
            )
            return redirect("admin-policy")
        messages.error(request, "ข้อมูลอำนาจไม่ครบหรือไม่ถูกต้อง")
    return render(
        request,
        "operations/admin_policy.html",
        {
            "versions": PolicyVersion.objects.select_related("activated_by"),
            "grants": AuthorityGrant.objects.select_related(
                "user__staff_profile", "division", "item", "granted_by", "revoked_by"
            ),
            "activate_form": forms.PolicyActivateForm(),
            "grant_form": grant_form,
            "now": timezone.now(),
        },
    )


@admin_required
@require_POST
def admin_policy_action(request, action, pk):
    if action == "activate":
        version = get_object_or_404(PolicyVersion, pk=pk)
        form = forms.PolicyActivateForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            _run(
                request,
                lambda: policy.activate_policy(
                    request.user,
                    version,
                    d["approved_by_name"],
                    d["approval_reference"],
                    d["reason"],
                ),
                f"เปิดใช้ {version.code} แล้ว ข้อเสนอของเรื่องที่เปิดอยู่ถูกคำนวณใหม่ "
                "(กองเจ้าภาพและระดับที่ยืนยันแล้วไม่เปลี่ยน)",
            )
        else:
            messages.error(request, "ระบุผู้รับรอง เลขอ้างอิง และเหตุผล")
    elif action == "revoke":
        grant = get_object_or_404(AuthorityGrant, pk=pk)
        _run(
            request,
            lambda: policy.revoke_authority(request.user, grant, request.POST.get("reason", "")),
            "ถอนอำนาจแล้ว",
        )
    else:
        raise Http404
    return redirect("admin-policy")


# --- Inventory seam (M07) ----------------------------------------------------------------------


@admin_required
@require_safe
def admin_inventory(request):
    """Ready figures per item, who waits for them, allocations, and the fake store's history."""
    now = timezone.now()
    rows = []
    for item in EquipmentType.objects.filter(is_active=True).order_by("code"):
        figures = inventory.availability(item, now)
        rows.append(
            {
                "item": item,
                "figures": figures,
                "demand": inventory.demand(item),
                "contended": inventory.contended(item, now),
                "allocated": inventory.active_allocated(item),
                "fake": FakeStock.objects.filter(item=item).first(),
                "serials": inventory.available_serials(item),
                "snapshot": StockSnapshot.objects.filter(item=item).order_by("-as_of").first(),
            }
        )
    return render(
        request,
        "operations/admin_inventory.html",
        {
            "mode": inventory.mode(),
            "stale_hours": settings.INVENTORY_STALE_HOURS,
            "rows": rows,
            "allocations": Allocation.objects.filter(ended_at__isnull=True).select_related(
                "case", "item", "allocated_by"
            ),
            "movements": StockMovement.objects.select_related("item", "case").order_by("-id")[:30],
            "adjust_form": forms.InventoryAdjustForm(),
            "import_form": forms.SnapshotImportForm(),
        },
    )


@admin_required
@require_POST
def admin_inventory_action(request, action):
    if action == "adjust":
        form = forms.InventoryAdjustForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            _run(
                request,
                lambda: inventory.adjust_fake(
                    request.user,
                    d["item"],
                    d["delta"],
                    d["reason"],
                    d["add_serials"].split(),
                    d["remove_serials"].split(),
                    bucket=d["bucket"] or "available",
                ),
                "ปรับคลังจำลองแล้ว",
            )
        else:
            messages.error(request, "ข้อมูลการปรับไม่ครบ")
    elif action == "import":
        form = forms.SnapshotImportForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            try:
                rows = json.loads(d["rows"])
            except ValueError:
                rows = None
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                messages.error(request, "ยอดต้องเป็นรายการ JSON ของ object")
            else:
                _run(
                    request,
                    lambda: inventory.import_snapshot(
                        request.user, rows, d["as_of"], d["source"], d["is_synthetic"]
                    ),
                    "นำเข้ายอดแล้ว",
                )
        else:
            messages.error(request, "ข้อมูลนำเข้าไม่ครบ")
    else:
        raise Http404
    return redirect("admin-inventory")


# --- AI configuration (every admin looks; ตัวเลือกระบบ ai_editors decides who changes) -------------


def ai_editor_required(view):
    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not identity.may_change_ai(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapper


@admin_required
@require_safe
def admin_ai(request):
    versions = list(
        AIConfigVersion.objects.select_related("screen_profile").prefetch_related(
            "entries__profile__credential", "eval_jobs"
        )
    )
    for version in versions:
        version.blocker = ai.activation_blocker(version)
        version.last_job = next(iter(version.eval_jobs.all()), None)
    # The last 50 messages that went to the AI, with what it did (ADR 0012 review).
    runs = list(AIRun.objects.select_related("config_version").order_by("-pk")[:50])
    titles = dict(ChatAnswer.objects.values_list("key", "title"))
    for run in runs:
        run.review, run.refused = ai_chat.review(run, titles)
    return render(
        request,
        "operations/admin_ai.html",
        {
            "credentials": AICredential.objects.all(),
            "profiles": AIProfile.objects.select_related("credential").order_by("-pk"),
            "versions": versions,
            "active": next(
                (v for v in versions if v.status == "active" and v.task == "extract"), None
            ),
            "active_triage": next(
                (v for v in versions if v.status == "active" and v.task == "triage"), None
            ),
            "active_answer": next(
                (v for v in versions if v.status == "active" and v.task == "answer"), None
            ),
            "switchable": [v for v in versions if v.blocker is None],
            "stoppable": [v for v in versions if v.status == "active"],
            "presets": ai.preset_status(),
            "gate": ai.EVAL_GATE,
            "preset_form": forms.AIPresetForm(),
            "runs": runs,
            "run_filters": [
                ("status", "ผล", AIRun.Status.choices),
                ("refused", "ถ้อยคำของ AI", [("yes", "มีที่ถูกตีกลับ"), ("no", "ไม่มีที่ถูกตีกลับ")]),
            ],
            "ai_enabled": settings.AI_ENABLED,
            "can_change": identity.may_change_ai(request.user),
            "credential_form": forms.AICredentialForm(),
            "profile_form": forms.AIProfileForm(),
            "version_form": forms.AIVersionForm(),
        },
    )


@ai_editor_required
@require_POST
def admin_ai_action(request, action, pk=None):
    actor, data = request.user, request.POST
    reason = data.get("reason", "")
    if action == "credential":
        form = forms.AICredentialForm(data)
        if form.is_valid():
            d = form.cleaned_data
            _run(
                request,
                lambda: ai.save_credential(
                    actor, d["name"], d["base_url"], d["api_key"], d["reason"]
                ),
                "บันทึก credential แล้ว (เข้ารหัส)",
            )
        else:
            messages.error(request, "ข้อมูล credential ไม่ครบ")
    elif action == "profile":
        form = forms.AIProfileForm(data)
        if form.is_valid():
            d = form.cleaned_data
            _run(
                request,
                lambda: ai.create_profile(
                    actor,
                    d["credential"],
                    d["model_id"],
                    d["timeout_seconds"],
                    d["max_tokens"],
                    d["temperature"],
                    d["use_json_schema"],
                ),
                "เพิ่มโปรไฟล์แล้ว",
            )
        else:
            messages.error(request, "ข้อมูลโปรไฟล์ไม่ถูกต้อง")
    elif action == "version":
        form = forms.AIVersionForm(data)
        if form.is_valid():
            d = form.cleaned_data
            _run(
                request,
                lambda: ai.create_version(
                    actor,
                    form.profiles(),
                    d["note"],
                    d["few_shot"],
                    d["pipeline"],
                    d["screen_profile"],
                    d["task"] or AIConfigVersion.Task.EXTRACT,
                ),
                "สร้างรุ่นการตั้งค่าแล้ว (รอทดสอบและประเมิน)",
            )
        else:
            messages.error(request, "เลือกโมเดลหลักและรูปแบบการทำงานก่อน")
    elif action == "preset":
        form = forms.AIPresetForm(data)
        key = data.get("preset", "")
        if form.is_valid():
            d = form.cleaned_data
            version = _run(
                request,
                lambda: ai.create_preset_version(actor, key, d["pipeline"], d["use_json_schema"]),
                "สร้างรุ่นใหม่จากโมเดลสำเร็จรูปแล้ว ขั้นต่อไป: ทดสอบการเชื่อมต่อ → ประเมิน → เปลี่ยน",
            )
            if version is not None:
                return redirect(f"{reverse('admin-ai')}#v{version.number}")
        else:
            messages.error(request, "เลือกรูปแบบการทำงานก่อน")
    elif action == "stop":
        task = data.get("task", "")
        if task not in AIConfigVersion.Task.values:
            messages.error(request, "เลือกงานที่จะหยุดใช้ AI")
        else:
            stopped = _run(request, lambda: ai.stop(actor, task, reason), "หยุดใช้ AI แล้ว")
            if stopped and stopped[1]:
                messages.info(request, f"ปิดงานคัดกรองที่รอ AI อยู่ {stopped[1]} เรื่อง")
    elif action == "switch":
        version = AIConfigVersion.objects.filter(pk=data.get("version") or None).first()
        if version is None:
            messages.error(request, "เลือกรุ่นที่จะเปลี่ยนไปใช้")
        else:
            _run(
                request,
                lambda: ai.activate(actor, version, reason),
                f"เปลี่ยนไปใช้ v{version.number} แล้ว ({version.get_task_display()}: งานใหม่ใช้รุ่นนี้)",
            )
    elif action in {"test", "evaluate", "activate", "reject"}:
        version = get_object_or_404(AIConfigVersion, pk=pk)
        operation = {
            "test": lambda: ai.test_version(actor, version),
            "evaluate": lambda: ai_eval.request_eval(actor, version),
            "activate": lambda: ai.activate(actor, version, reason),
            "reject": lambda: ai.reject(actor, version, reason),
        }[action]
        _run(
            request,
            operation,
            {
                "test": "ทดสอบแล้ว",
                "evaluate": (
                    "เข้าคิวประเมินแล้ว (worker ประเมินบน heldout-v1 + redteam-v1 ใช้เวลาหลายนาที)"
                ),
                "activate": "เปิดใช้แล้ว",
                "reject": "ทำเครื่องหมายไม่ใช้แล้ว",
            }[action],
        )
    else:
        raise Http404
    return redirect("admin-ai")


@admin_required
def admin_options(request):
    """ตัวเลือกระบบ (ADR 0009): choices the organisation changes without code, each with a
    reason and in the audit trail; the first choice of each is the safe default."""
    if request.method == "POST":
        key = request.POST.get("key", "")
        _run(
            request,
            lambda: options.change(
                request.user, key, request.POST.get("value", ""), request.POST.get("reason", "")
            ),
            "บันทึกตัวเลือกแล้ว",
        )
        # The requester approval page shows and changes who may talk to the bot as well.
        return redirect(
            "admin-registrations"
            if request.POST.get("next") == "registrations"
            else "admin-options"
        )
    history = (
        AuditEvent.objects.filter(action="option.changed")
        .select_related("actor")
        .order_by("-created_at")[:20]
    )
    return render(
        request,
        "operations/admin_options.html",
        {
            "rows": options.rows(),
            "history": history,
            "option_filters": [
                ("state", "สถานะ", [("changed", "เปลี่ยนจากค่าเริ่มต้น"), ("default", "ค่าเริ่มต้น")])
            ],
        },
    )


@admin_required
def admin_answers(request):
    """คลังคำตอบ (ADR 0012): what the bot may answer in LINE. People write and approve every
    answer; the AI only chooses one or writes from them, and only when ตัวเลือกระบบ allows."""
    if request.method == "POST":
        data = request.POST
        key = (data.get("key") or "").strip()
        if data.get("op") == "retire":
            _run(
                request,
                lambda: answers.retire(request.user, key, data.get("reason", "")),
                "เลิกใช้คำตอบแล้ว",
            )
        else:
            saved = _run(
                request,
                lambda: answers.save(
                    request.user,
                    key,
                    data.get("title", ""),
                    [w for w in data.get("keywords", "").replace("\r", "").split("\n")],
                    data.get("text", ""),
                    data.get("action", ChatAnswer.Action.TEXT),
                    data.get("reason", ""),
                    approve=data.get("approve") == "1",
                    short_only=data.get("short_only") == "1",
                ),
                "บันทึกคำตอบแล้ว",
            )
            asked = UnansweredQuestion.objects.filter(
                pk=data.get("question") or 0, status=UnansweredQuestion.Status.OPEN
            ).first()
            if saved is not None and asked is not None:
                # คำถามที่บอทตอบไม่ได้ (Q11): the question now has an answer in the bank.
                questions.handle(
                    request.user,
                    asked,
                    UnansweredQuestion.Status.ANSWERED,
                    "เพิ่มเป็นคำตอบในคลัง",
                    answer_key=key,
                )
        return redirect(f"{reverse('admin-answers')}#a-{key}")
    from operations import ai_answer

    active = ai.active_version(AIConfigVersion.Task.ANSWER)
    asked = UnansweredQuestion.objects.filter(
        pk=request.GET.get("question") or 0, status=UnansweredQuestion.Status.OPEN
    ).first()
    return render(
        request,
        "operations/admin_answers.html",
        {
            "answers": ChatAnswer.objects.select_related("updated_by", "approved_by"),
            "actions": ChatAnswer.Action.choices,
            "answer_filters": [
                ("status", "สถานะ", ChatAnswer.Status.choices),
                ("action", "ต่อท้าย", ChatAnswer.Action.choices),
            ],
            "mode": options.OPTIONS[options.CHAT_ANSWERS].label_of(answers.mode()),
            "active": active,
            "usable": ai_answer.active_version(),
            "history": AuditEvent.objects.filter(action__startswith="chat_answer.")
            .select_related("actor")
            .order_by("-created_at")[:20],
            "question": asked,
        },
    )


@admin_required
@require_http_methods(["GET", "HEAD", "POST"])
def admin_questions(request):
    """คำถามที่บอทตอบไม่ได้ (14 Oct 2569 Q11): what people asked that คลังคำตอบ did not know,
    masked and without names; an admin adds an answer or marks it not to be answered."""
    if request.method == "POST":
        question = get_object_or_404(UnansweredQuestion, pk=request.POST.get("question") or 0)
        _run(
            request,
            lambda: questions.handle(
                request.user,
                question,
                request.POST.get("status", ""),
                request.POST.get("reason", ""),
            ),
            "บันทึกแล้ว",
        )
        return redirect("admin-questions")
    rows = UnansweredQuestion.objects.select_related("handled_by")
    return render(
        request,
        "operations/admin_questions.html",
        {
            "waiting": rows.filter(status=UnansweredQuestion.Status.OPEN).order_by(
                "-count", "-last_at"
            )[:200],
            "handled": rows.exclude(status=UnansweredQuestion.Status.OPEN).order_by("-handled_at")[
                :100
            ],
        },
    )


def _staged(value):
    try:
        return RosterUpload.objects.filter(pk=value).first() if value else None
    except ValidationError:  # not a UUID
        return None


@admin_required
def admin_employees(request):
    """ทะเบียนพนักงาน (15 ต.ค. 2569 Q10, Q27–Q29): the owner uploads the organisation's roster
    (.xlsx or .csv); a preview shows which column is which field and which เขต/หน่วยงาน each ฝ่าย
    is, then the import. Names and e-mails never leave this page; no AI reads the file."""
    upload, columns = None, None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "upload":
            file = request.FILES.get("file")
            if file is None or file.size > roster.MAX_BYTES:
                messages.error(request, "เลือกไฟล์ .xlsx หรือ .csv ขนาดไม่เกิน 10 MB")
                return redirect("admin-employees")
            staged = _run(
                request,
                lambda: roster.stage(request.user, file.name, file.read()),
                "อ่านไฟล์แล้ว ตรวจการจับคู่ด้านล่างก่อนนำเข้า",
            )
            if staged is None:
                return redirect("admin-employees")
            return redirect(f"{reverse('admin-employees')}?upload={staged.pk}")
        upload = _staged(request.POST.get("upload"))
        if upload is None:
            messages.error(request, "ไม่พบไฟล์ที่ตรวจไว้ อัปโหลดใหม่")
            return redirect("admin-employees")
        columns = {field: request.POST.get(f"col_{field}", "") for field in roster.FIELDS}
        if action == "import":
            keys = request.POST.getlist("unit_key")
            choices = dict(zip(keys, request.POST.getlist("unit_choice"), strict=False))
            summary = _run(
                request,
                lambda: roster.import_upload(
                    request.user,
                    upload,
                    columns,
                    choices,
                    request.POST.get("deactivate_missing") == "1",
                    request.POST.get("reason", ""),
                ),
                "นำเข้าทะเบียนพนักงานแล้ว",
            )
            if summary:
                messages.info(
                    request,
                    f"เพิ่ม {summary['created']} แก้ {summary['updated']} ปิด {summary['closed']} คน · "
                    f"มีอีเมล {summary['emails']} คน · ผูกผู้ใช้ LINE จากชื่อ {summary['matched']} · "
                    f"ชื่อตรงแต่ต้องให้แอดมินตรวจ {summary['review']} ราย",
                )
                return redirect("admin-employees")
    elif request.GET.get("upload"):
        upload = _staged(request.GET["upload"])
    preview = None
    if upload is not None:
        try:
            headers, rows = roster.opened(upload)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            upload.delete()
            return redirect("admin-employees")
        columns = columns or roster.guess_columns(headers)
        preview = roster.preview(headers, rows, columns)
        preview["headers"] = headers
        preview["fields"] = [
            (field, label, columns.get(field, "")) for field, (label, _) in roster.FIELDS.items()
        ]
    active = Employee.objects.filter(is_active=True)
    return render(
        request,
        "operations/admin_employees.html",
        {
            "active": active.count(),
            "inactive": Employee.objects.filter(is_active=False).count(),
            "with_email": active.exclude(email_hash="").count(),
            "synthetic": active.filter(is_synthetic=True).exists(),
            "latest": Employee.objects.order_by("-imported_at").first(),
            "sealing": sealing.available(),
            "mail": email_codes.sending_ready(),
            "check": options.OPTIONS[options.REGISTRATION_CHECK].label_of(
                options.get(options.REGISTRATION_CHECK)
            ),
            "methods": options.OPTIONS[options.EMAIL_METHODS].label_of(
                options.get(options.EMAIL_METHODS)
            ),
            "upload": upload,
            "preview": preview,
            "districts": District.objects.filter(is_active=True).order_by("code"),
            "new_unit": roster.NEW_UNIT,
        },
    )
