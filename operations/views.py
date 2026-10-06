from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import PasswordChangeView
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connection
from django.db.migrations.executor import MigrationExecutor
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views.decorators.http import require_POST, require_safe

from operations import forms, identity
from operations.models import Division, RegistrationRequest, Role, RoleGrant

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
    return render(
        request, "operations/admin_registrations.html", {"pending": pending, "recent": recent}
    )


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
    if action not in {"activate", "deactivate"}:
        raise Http404
    _run(
        request,
        lambda: identity.set_division_active(
            request.user, division, action == "activate", request.POST.get("reason", "")
        ),
        "เปลี่ยนสถานะกองแล้ว",
    )
    return redirect("admin-divisions")
