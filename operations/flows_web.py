"""หน้าแอดมิน flow LINE และ requirement ของกอง (ADR 0023, grill กคว. 27 ต.ค. 2569).

The list of flows, the canvas editor of one flow's draft (saved as JSON, checked on every save),
publishing (closed until the flow runner is ready), and the divisions' requirements, each of
which opens the box it belongs to in the editor.
"""

import json

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST, require_safe

from operations import line_flows
from operations.models import Division, DivisionRequirement, LineFlow, LineFlowVersion
from operations.views import admin_required

V = LineFlowVersion.Status


def _versions(flow):
    published = flow.versions.filter(status=V.PUBLISHED).first()
    draft = line_flows.draft_of(flow)
    return published, draft


@admin_required
@require_safe
def admin_flows(request):
    rows = []
    for flow in LineFlow.objects.select_related("division"):
        published, draft = _versions(flow)
        checks = (draft or published).checks if (draft or published) else []
        rows.append(
            {
                "flow": flow,
                "published": published,
                "draft": draft,
                "errors": sum(1 for c in checks if c["level"] == "error"),
                "warnings": sum(1 for c in checks if c["level"] == "warn"),
                "requirements": flow.requirements.count(),
            }
        )
    return render(
        request,
        "operations/admin_flows.html",
        {"rows": rows, "runner_ready": line_flows.RUNNER_READY},
    )


@admin_required
@require_POST
def admin_flow_start(request, key):
    flow = get_object_or_404(LineFlow, key=key)
    line_flows.start_draft(request.user, flow)
    return redirect("admin-flow", key=flow.key)


@admin_required
@require_safe
def admin_flow(request, key):
    flow = get_object_or_404(LineFlow, key=key)
    published, draft = _versions(flow)
    version = draft or published
    if version is None:
        messages.info(request, "flow นี้ยังไม่มีรุ่น กดเริ่มร่างก่อน")
        return redirect("admin-flows")
    requirements = [
        {
            "id": r.pk,
            "node": r.node_id,
            "text": r.text,
            "status": r.get_status_display(),
            "division": r.division.code,
        }
        for r in flow.requirements.select_related("division")
    ]
    config = {
        "flow": {"key": flow.key, "name": flow.name},
        "version": {
            "id": version.pk,
            "number": version.number,
            "status": version.status,
            "revision": version.revision,
            "editable": version.status == V.DRAFT,
        },
        "graph": version.graph,
        "checks": version.checks,
        "kinds": {k: v[0] for k, v in line_flows.KINDS.items()},
        "roles": {k: v[1] for k, v in line_flows.KINDS.items()},
        "rules": line_flows.RULES,
        "limits": {"buttons": line_flows.MAX_BUTTONS, "label": line_flows.MAX_LABEL},
        "requirements": requirements,
        "saveUrl": reverse("admin-flow-save", args=[flow.key]),
        "requirementsUrl": reverse("admin-requirements"),
        "focus": request.GET.get("node", ""),
        "runnerReady": line_flows.RUNNER_READY,
    }
    return render(
        request,
        "operations/admin_flow.html",
        {
            "flow": flow,
            "version": version,
            "published": published,
            "draft": draft,
            "config": config,
            "runner_ready": line_flows.RUNNER_READY,
            "blocked": line_flows.blocked(version.checks),
        },
    )


@admin_required
@require_POST
def admin_flow_save(request, key):
    flow = get_object_or_404(LineFlow, key=key)
    draft = line_flows.draft_of(flow)
    if draft is None:
        return JsonResponse({"error": "flow นี้ไม่มีร่าง เริ่มร่างก่อน"}, status=409)
    try:
        body = json.loads(request.body or b"{}")
        version = line_flows.save_draft(
            request.user, draft, body.get("graph"), body.get("revision"), body.get("note")
        )
    except (ValueError, TypeError):
        return JsonResponse({"error": "ข้อมูลที่ส่งมาไม่ถูกต้อง"}, status=400)
    except ValidationError as exc:
        return JsonResponse({"error": " ".join(exc.messages)}, status=409)
    return JsonResponse({"revision": version.revision, "checks": version.checks})


@admin_required
@require_POST
def admin_flow_publish(request, key):
    flow = get_object_or_404(LineFlow, key=key)
    draft = line_flows.draft_of(flow)
    if draft is None:
        messages.error(request, "ไม่มีร่างให้เผยแพร่")
        return redirect("admin-flow", key=flow.key)
    try:
        line_flows.publish(request.user, draft, request.POST.get("reason", ""))
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, f"เผยแพร่ {flow.name} รุ่น {draft.number} แล้ว")
    return redirect("admin-flow", key=flow.key)


@admin_required
def admin_requirements(request):
    divisions = list(Division.objects.filter(requirements__isnull=False).distinct())
    code = request.GET.get("division") or (divisions[0].code if divisions else "")
    division = next((d for d in divisions if d.code == code), None)
    if request.method == "POST":
        req = get_object_or_404(DivisionRequirement, pk=request.POST.get("id"))
        flow = LineFlow.objects.filter(key=request.POST.get("flow", "")).first()
        try:
            line_flows.update_requirement(
                request.user,
                req,
                status=request.POST.get("status"),
                remark=request.POST.get("remark"),
                flow=flow,
                node_id=request.POST.get("node", "") if flow else "",
            )
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, "บันทึก requirement แล้ว")
        return redirect(f"{reverse('admin-requirements')}?division={req.division.code}#r{req.pk}")
    rows = []
    flows = list(LineFlow.objects.all())
    if division is not None:
        for req in DivisionRequirement.objects.filter(division=division).select_related("flow"):
            rows.append({"req": req, "box": line_flows.node_title(req.flow, req.node_id)})
    boxes = {}
    for flow in flows:
        published, draft = _versions(flow)
        version = draft or published
        boxes[flow.key] = [
            {"id": n["id"], "title": n["title"]}
            for n in (version.graph.get("nodes", []) if version else [])
        ]
    return render(
        request,
        "operations/admin_requirements.html",
        {
            "divisions": divisions,
            "division": division,
            "rows": rows,
            "topics": DivisionRequirement.Topic.choices,
            "statuses": DivisionRequirement.Status.choices,
            "flows": flows,
            "boxes": boxes,
        },
    )
