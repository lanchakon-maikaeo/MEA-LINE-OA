"""หน้าที่ของกอง: the admin page that writes it and the staff page that reads it (ADR 0013).

Admins write each division's overview and งานที่ดูแล with example messages, and ask for the
routing AI's evaluation (Q25). Staff read the duties: admins and ส่วนกลาง every division's, the
others their own division's (Q21).
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_safe

from operations import ai, ai_routing, duties, identity, options
from operations.models import AIConfigVersion, AIEvalJob, Division, DutyEntry
from operations.views import admin_required


@admin_required
@require_http_methods(["GET", "HEAD", "POST"])
def admin_page(request):
    if request.method == "POST":
        _act(request)
        return redirect("admin-duties")
    version = ai.active_version(AIConfigVersion.Task.ROUTING)
    usable = ai_routing.usable()
    sent, wrong = ai_routing.misroutes()
    return render(
        request,
        "operations/admin_duties.html",
        {
            "divisions": duties.divisions().prefetch_related("duties"),
            "version": version,
            "usable": usable,
            "stale": version is not None and usable is None,
            "job": AIEvalJob.objects.filter(version__task=AIConfigVersion.Task.ROUTING)
            .select_related("version")
            .order_by("-pk")
            .first(),
            "readiness": ai_routing.readiness(),
            "mode": options.OPTIONS[options.ROUTING_AI].label_of(ai_routing.mode()),
            "threshold": options.OPTIONS[options.ROUTING_AI_ACCURACY].label_of(
                options.get(options.ROUTING_AI_ACCURACY)
            ),
            "sent": sent,
            "wrong": wrong,
            "min_examples": duties.MIN_EXAMPLES,
        },
    )


def _act(request):
    data, user = request.POST, request.user
    action = data.get("action")
    reason = data.get("reason", "")
    before = ai_routing.usable()
    try:
        if action == "evaluate":
            ai_routing.request_eval(user)
            messages.success(request, "เข้าคิวประเมินแล้ว ใช้เวลาหลายนาที ผลจะขึ้นที่หน้านี้")
            return
        if action in ("overview", "add"):
            division = get_object_or_404(Division, code=data.get("division", ""))
            if action == "overview":
                duties.set_overview(
                    user, division, data.get("overview"), data.get("examples"), reason
                )
            else:
                duties.add_entry(
                    user,
                    division,
                    data.get("title"),
                    data.get("description"),
                    data.get("examples"),
                    reason,
                )
        else:
            entry = get_object_or_404(DutyEntry, pk=data.get("entry") or 0)
            if action == "change":
                duties.change_entry(
                    user,
                    entry,
                    data.get("title"),
                    data.get("description"),
                    data.get("examples"),
                    reason,
                )
            elif action == "retire":
                duties.retire_entry(user, entry, reason)
            else:
                raise ValidationError("ไม่รู้จักคำสั่ง")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return
    messages.success(request, "บันทึกแล้ว")
    if before is not None and ai_routing.usable() is None:
        messages.info(request, "AI ส่งกองหยุดเสนอจนกว่าจะประเมินกับหน้าที่ชุดใหม่ (กดประเมินด้านบน)")


@login_required
@require_safe
def staff_page(request):
    scope = identity.staff_scope(request.user)
    if not scope.has_any_access:
        raise PermissionDenied
    return render(
        request,
        "operations/duties.html",
        {
            "divisions": duties.visible_divisions(request.user).prefetch_related("duties"),
            "all": duties.may_see_all(request.user),
        },
    )
