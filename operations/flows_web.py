"""หน้าแอดมิน flow LINE (ADR 0023, grill กคว. 27 ต.ค. 2569).

The list of flows, the canvas editor of one flow's draft (saved as JSON, checked on every save),
ลองคุย (the editor plays the draft and asks the runner's own rules what typed text reads as)
and publishing. The divisions' requirement page went away in the กคอ. grill (Q41): the menu
designer on Vercel keeps them.
"""

import json

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST, require_safe

from operations import flow_runner, line_flows, typed_input
from operations.models import LineFlow, LineFlowVersion, SheetSource
from operations.views import admin_required

V = LineFlowVersion.Status


def _versions(flow):
    published = flow.versions.filter(status=V.PUBLISHED).first()
    draft = line_flows.draft_of(flow)
    return published, draft


def _row(flow):
    published, draft = _versions(flow)
    checks = (draft or published).checks if (draft or published) else []
    return {
        "flow": flow,
        "published": published,
        "draft": draft,
        "errors": sum(1 for c in checks if c["level"] == "error"),
        "warnings": sum(1 for c in checks if c["level"] == "warn"),
    }


@admin_required
def admin_flows(request):
    """One tab per button of the real rich menu (27 ต.ค. 2569): its flow, or a starter."""
    from operations.models import LineMenuItem

    if request.method == "POST":
        item = get_object_or_404(LineMenuItem, pk=request.POST.get("tile"))
        try:
            flow = line_flows.flow_for_tile(request.user, item)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return redirect(f"{reverse('admin-flows')}?tile={item.pk}")
        return redirect("admin-flow", key=flow.key)
    tabs = line_flows.menu_tabs()
    wanted = request.GET.get("tile", "")
    current = next((t for t in tabs if str(t["item"].pk) == wanted), tabs[0] if tabs else None)
    if current is not None:
        current = {
            **current,
            "row": _row(current["flow"]) if current["flow"] else None,
            "choices": list(
                current["item"].choices.filter(is_active=True).order_by("position", "pk")
            ),
        }
    on_menu = {t["flow"].pk for t in tabs if t["flow"]}
    others = [_row(f) for f in LineFlow.objects.select_related("division") if f.pk not in on_menu]
    return render(
        request,
        "operations/admin_flows.html",
        {
            "tabs": tabs,
            "current": current,
            "others": others,
            "runner_ready": line_flows.RUNNER_READY,
        },
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
        "sheets": ":เลือกแหล่งข้อมูล|"
        + "|".join(
            f"{s.key}:{s.name.replace('|', ' ').replace(':', ' ')}"
            for s in SheetSource.objects.all()
        ),
        "saveUrl": reverse("admin-flow-save", args=[flow.key]),
        "tryUrl": reverse("admin-flow-try", args=[flow.key]),
        "focus": request.GET.get("node", ""),
        "runnerReady": line_flows.RUNNER_READY,
    }
    return render(
        request,
        "operations/admin_flow.html",
        {
            "tabs": line_flows.menu_tabs(),
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
def admin_flow_try(request, key):
    """What the chat would read from typed text at an ask box: nothing is saved or sent."""
    get_object_or_404(LineFlow, key=key)
    try:
        body = json.loads(request.body or b"{}")
        rule, field, text = str(body["rule"]), str(body.get("field", "")), str(body["text"])[:5000]
        kind = str(body.get("kind", ""))[:16]
        values = body.get("values") if isinstance(body.get("values"), dict) else {}
    except (ValueError, TypeError, KeyError):
        return JsonResponse({"error": "ข้อมูลที่ส่งมาไม่ถูกต้อง"}, status=400)
    if rule not in line_flows.RULES:
        return JsonResponse({"error": "ไม่รู้จักกฎนี้"}, status=400)
    value, shown, problem = flow_runner.read(rule, field, text, kind)
    answer = {"value": value, "shown": shown, "problem": problem}
    if not problem and rule == "multiplier":
        vt = str(values.get("vt_size", "")) if values.get("set_kind") in flow_runner.WITH_VT else ""
        computed = typed_input.multiplier_from(str(values.get("ct_size", "")), vt)
        answer["computed"] = None if computed is None else f"{computed:g}"
        answer["ok"] = computed is not None and abs(float(value) - computed) < 0.001
    if not problem and field.startswith("rows."):
        answer["rows"] = [{**r, "text": flow_runner.row_text(r)} for r in value]
    return JsonResponse(answer)


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
