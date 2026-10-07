from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import PasswordChangeView
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connection
from django.db.migrations.executor import MigrationExecutor
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_POST, require_safe

from operations import (
    ai,
    ai_eval,
    attachments,
    forms,
    identity,
    notifications,
    orchestration,
    policy,
)
from operations.models import (
    AIConfigVersion,
    AICredential,
    AIProfile,
    AIRun,
    Attachment,
    AuthorityGrant,
    Division,
    LineQuotaSnapshot,
    NotificationDelivery,
    OutboxEvent,
    PolicyVersion,
    RegistrationRequest,
    Requester,
    Role,
    RoleGrant,
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
    success_url = reverse_lazy("console")

    def form_valid(self, form):
        response = super().form_valid(form)
        identity.password_changed(self.request.user)
        messages.success(self.request, "เปลี่ยนรหัสผ่านแล้ว")
        return response


@login_required
@require_safe
def console(request):
    scope = identity.staff_scope(request.user)
    divisions = Division.objects.filter(pk__in=list(scope.division_roles), is_active=True)
    return render(
        request,
        "operations/console.html",
        {
            "scope": scope,
            "memberships": [
                (d, sorted(Role(r).label for r in scope.division_roles[d.pk])) for d in divisions
            ],
        },
    )


@login_required
@require_safe
def division_detail(request, code):
    scope = identity.staff_scope(request.user)
    division = Division.objects.filter(code=code, is_active=True).first()
    # Unknown and out-of-scope divisions look the same, so URLs reveal nothing.
    if division is None or not identity.can_access_division(scope, division.pk):
        raise Http404
    grants = (
        RoleGrant.objects.filter(division=division, revoked_at__isnull=True, user__is_active=True)
        .select_related("user__staff_profile")
        .order_by("user__username", "role")
    )
    return render(request, "operations/division.html", {"division": division, "grants": grants})


EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "application/pdf": "pdf"}


@login_required
@require_safe
def attachment_download(request, pk):
    """Scanned-clean files of cases in the viewer's divisions only; everything else is 404."""
    attachment = (
        Attachment.objects.select_related("case")
        .filter(pk=pk, case__isnull=False, scan_status=Attachment.ScanStatus.CLEAN)
        .first()
    )
    scope = identity.staff_scope(request.user)
    if attachment is None or not identity.can_access_division(
        scope, attachment.case.owner_division_id
    ):
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
def admin_users(request):
    users = User.objects.select_related("staff_profile").order_by("username")
    return render(request, "operations/admin_users.html", {"users": users})


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
    return render(
        request,
        "operations/admin_user_detail.html",
        {
            "target": target,
            "grants": grants,
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


@admin_required
@require_safe
def admin_registrations(request):
    pending = RegistrationRequest.objects.filter(
        status=RegistrationRequest.Status.PENDING
    ).select_related("requested_district", "identity")
    recent = (
        RegistrationRequest.objects.exclude(status=RegistrationRequest.Status.PENDING)
        .select_related("requested_district", "decided_by")
        .order_by("-decided_at")[:50]
    )
    requesters = Requester.objects.select_related("district").order_by("-approved_at")[:100]
    return render(
        request,
        "operations/admin_registrations.html",
        {
            "pending": pending,
            "recent": recent,
            "requesters": requesters,
            "requester_form": forms.RequesterCreateForm(),
        },
    )


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


@admin_required
@require_POST
def admin_registration_action(request, pk, action):
    registration = get_object_or_404(RegistrationRequest, pk=pk)
    reason = request.POST.get("reason", "")
    if action == "approve":
        _run(
            request,
            lambda: identity.approve_registration(request.user, registration, reason),
            "รับรองผู้แจ้งแล้ว",
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
        request, "operations/admin_divisions.html", {"divisions": divisions, "form": form}
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
                "user__staff_profile", "division", "granted_by", "revoked_by"
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


# --- AI configuration (developer-admin only) ----------------------------------------------------


def developer_admin_required(view):
    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not identity.staff_scope(request.user).is_developer_admin:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapper


@developer_admin_required
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
            "switchable": [v for v in versions if v.blocker is None],
            "presets": ai.preset_status(),
            "gate": ai.EVAL_GATE,
            "preset_form": forms.AIPresetForm(),
            "runs": AIRun.objects.select_related("config_version").order_by("-pk")[:20],
            "ai_enabled": settings.AI_ENABLED,
            "credential_form": forms.AICredentialForm(),
            "profile_form": forms.AIProfileForm(),
            "version_form": forms.AIVersionForm(),
        },
    )


@developer_admin_required
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
