"""การตั้งค่า AI (ADR 0038): providers and keys, the model chain, releases and the AI's runs.

Four tabs on one page. Every admin may look; changes need the AI right. Saving, changing or
removing a key, and removing a provider, ask for the admin's own password again (Q4).
"""

from django.conf import settings
from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST, require_safe

from operations import ai, ai_chat, ai_eval, ai_evidence, ai_release, identity
from operations.models import (
    AIConfigVersion,
    AICredential,
    AIModel,
    AIRun,
    ChatAnswer,
)
from operations.views import _run, admin_required, ai_editor_required

Task = AIConfigVersion.Task
TABS = (
    ("providers", "ผู้ให้บริการ AI"),
    ("chain", "ลำดับโมเดล"),
    ("releases", "รุ่นและผลประเมิน"),
    ("runs", "ประวัติการเรียก AI"),
)
GEMINI_ORDER = ("gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite")
WRONG_PASSWORD = "รหัสผ่านไม่ถูกต้อง"


def _tab_url(tab, anchor=""):
    return f"{reverse('admin-ai')}?tab={tab}{f'#{anchor}' if anchor else ''}"


def _choosable():
    """Visible models of providers that have a key, in the providers' order."""
    return [
        m
        for m in AIModel.objects.select_related("provider")
        .filter(is_hidden=False, provider__is_hidden=False)
        .order_by("provider_id", "position", "pk")
        if m.provider.has_key
    ]


def _suggested(choosable):
    """What the chain form starts with: the release in use, else Gemini's order then Typhoon
    (Q5), else the extraction chain in use."""
    by_key = {(m.provider_id, m.model_id): m for m in choosable}
    label = ai_release.current()
    if label:
        chain = ai_release.members(label)[Task.EXTRACT].chain()
    else:
        gemini = [m for m in choosable if m.model_id in GEMINI_ORDER]
        typhoon = [m for m in choosable if m.provider.name == "opentyphoon"]
        if gemini:
            gemini.sort(key=lambda m: GEMINI_ORDER.index(m.model_id))
            return [*gemini, *typhoon][: ai.MAX_CHAIN]
        active = ai.active_version(Task.EXTRACT)
        chain = active.chain() if active else []
    picked = [by_key.get((p.credential_id, p.model_id)) for p in chain]
    return list(dict.fromkeys(m for m in picked if m))[: ai.MAX_CHAIN]


def _switches():
    """The per-task AI switches (Q14)."""
    return [{"task": t, "name": Task(t).label, "on": ai_release.task_is_on(t)} for t in Task.values]


def _old_versions():
    versions = list(
        AIConfigVersion.objects.filter(release="")
        .select_related("screen_profile")
        .prefetch_related("entries__profile__credential", "eval_jobs")
    )
    for version in versions:
        version.blocker = ai.activation_blocker(version)
    return versions


@admin_required
@require_safe
def admin_ai(request):
    tab = request.GET.get("tab", "providers")
    if tab not in dict(TABS):
        tab = "providers"
    context = {
        "tab": tab,
        "tabs": TABS,
        "can_change": identity.may_change_ai(request.user),
        "ai_enabled": settings.AI_ENABLED,
        "evaluates_here": ai_release.evaluates_here(),
    }
    if tab == "providers":
        providers = list(ai.providers())
        for provider in providers:
            provider.serving = ai.in_use(provider)
            provider.values = {**ai.PROFILE_DEFAULTS, **(provider.defaults or {})}
            provider.model_rows = [
                {"model": m, "used": ai.model_used(m)} for m in provider.models.all()
            ]
        context["providers"] = providers
    elif tab == "chain":
        choosable = _choosable()
        suggested = _suggested(choosable)
        overview = ai_release.overview()
        switches = _switches()
        context.update(
            {
                "choosable": choosable,
                "slots": [
                    ("primary", "โมเดลหลัก", suggested[0] if suggested else None),
                    *(
                        (
                            f"fallback_{n}",
                            f"สำรอง {n}",
                            suggested[n] if len(suggested) > n else None,
                        )
                        for n in (1, 2, 3)
                    ),
                ],
                "advanced": ai_release.extract_defaults(),
                "pipelines": AIConfigVersion.Pipeline.choices,
                "serving": next((r for r in overview if r["serving"]), None),
                # Before the first release, the extraction version in use stands for the chain.
                "legacy": ai.active_version(Task.EXTRACT),
                "pending": [r for r in overview if not r["serving"] and not r["rejected"]][:3],
                "switches": switches,
                "any_on": any(row["on"] for row in switches),
            }
        )
    elif tab == "releases":
        context.update({"releases": ai_release.overview(), "old_versions": _old_versions()})
    else:
        runs = list(AIRun.objects.select_related("config_version").order_by("-pk")[:50])
        titles = dict(ChatAnswer.objects.values_list("key", "title"))
        for run in runs:
            run.review, run.refused = ai_chat.review(run, titles)
        context.update(
            {
                "runs": runs,
                "run_filters": [
                    ("status", "ผล", AIRun.Status.choices),
                    ("refused", "ถ้อยคำของ AI", [("yes", "มีที่ถูกตีกลับ"), ("no", "ไม่มีที่ถูกตีกลับ")]),
                ],
            }
        )
    return render(request, "operations/admin_ai.html", context)


def _password_ok(request):
    """Q4: the admin's own password again before anything touches a key."""
    if request.user.check_password(request.POST.get("password", "")):
        return True
    messages.error(request, WRONG_PASSWORD)
    identity.audit(request.user, "ai.password_check_failed", request.user)
    return False


def _report_check(request, credential):
    ok, detail = ai.check_key(request.user, credential)
    (messages.success if ok else messages.warning)(request, f"{credential.title}: {detail}")


def _models_from(data):
    picked = [data.get(name) for name in ("primary", "fallback_1", "fallback_2", "fallback_3")]
    found = {str(m.pk): m for m in AIModel.objects.filter(pk__in=[p for p in picked if p])}
    return [found.get(p) for p in picked if p]


def _advanced_from(data):
    try:
        few_shot = int(data.get("few_shot", 0))
    except ValueError:
        few_shot = 0
    pipeline = data.get("pipeline", "")
    return {
        "use_json_schema": data.get("use_json_schema") == "on",
        "pipeline": pipeline if pipeline in AIConfigVersion.Pipeline.values else "harness",
        "few_shot": max(0, min(few_shot, 5)),
        "typed_fields": data.get("typed_fields") == "on",
        "menu_fields": data.get("menu_fields") == "on",
        "slip_facts": data.get("slip_facts") == "on",
    }


def _provider_action(request, action, pk):
    actor, data = request.user, request.POST
    reason = data.get("reason", "")
    if action == "provider_add":
        if not _password_ok(request):
            return _tab_url("providers", "add")
        created = _run(
            request,
            lambda: ai.add_provider(
                actor, data.get("label"), data.get("base_url"), data.get("api_key"), reason
            ),
            "เพิ่มผู้ให้บริการแล้ว",
        )
        return _tab_url("providers", f"p{created.pk}" if created else "add")
    if action == "model_add":
        provider = get_object_or_404(AICredential, pk=pk)
        _run(
            request,
            lambda: ai.add_model(actor, provider, data.get("model_id"), data.get("label")),
            "เพิ่มโมเดลแล้ว",
        )
        return _tab_url("providers", f"p{provider.pk}")
    if action in {"model_hide", "model_show", "model_delete"}:
        model = get_object_or_404(AIModel, pk=pk)
        _run(
            request,
            {
                "model_hide": lambda: ai.set_model_hidden(actor, model, True),
                "model_show": lambda: ai.set_model_hidden(actor, model, False),
                "model_delete": lambda: ai.delete_model(actor, model),
            }[action],
            {
                "model_hide": "ซ่อนโมเดลแล้ว",
                "model_show": "แสดงโมเดลแล้ว",
                "model_delete": "ลบโมเดลแล้ว",
            }[action],
        )
        return _tab_url("providers", f"p{model.provider_id}")
    provider = get_object_or_404(AICredential, pk=pk)
    anchor = f"p{provider.pk}"
    if action == "key":
        if _password_ok(request):
            saved = _run(
                request,
                lambda: ai.save_credential(
                    actor, provider.name, provider.base_url, data.get("api_key"), reason
                ),
                "บันทึกคีย์แล้ว",
            )
            if saved:
                _report_check(request, saved)
    elif action == "key_check":
        _report_check(request, provider)
    elif action == "key_remove":
        if _password_ok(request):
            _run(request, lambda: ai.remove_key(actor, provider, reason), "ลบคีย์แล้ว")
    elif action == "provider_remove":
        if _password_ok(request):
            _run(request, lambda: ai.remove_provider(actor, provider, reason), "ลบผู้ให้บริการแล้ว")
            anchor = ""
    elif action == "provider_update":
        _run(
            request,
            lambda: ai.update_provider(
                actor,
                provider,
                data.get("label"),
                {
                    "timeout_seconds": data.get("timeout_seconds"),
                    "max_tokens": data.get("max_tokens"),
                    "temperature": data.get("temperature"),
                },
                data.get("enforces_schema") == "on",
                reason,
            ),
            "บันทึกค่าขั้นสูงแล้ว",
        )
    else:
        raise Http404
    return _tab_url("providers", anchor)


def _release_action(request, action):
    actor, data = request.user, request.POST
    label, reason = data.get("release", ""), data.get("reason", "")
    if action == "release_create":
        created = _run(
            request,
            lambda: ai_release.create(actor, _models_from(data), _advanced_from(data)),
            "สร้างรุ่นแล้ว กำลังทดสอบและประเมินทุกงาน",
        )
        if created:
            ai_release.evaluate(actor, created)
        return _tab_url("chain")
    if not ai_release.members(label):
        raise Http404
    if action == "release_evaluate":
        _run(request, lambda: ai_release.evaluate(actor, label), "เข้าคิวประเมินแล้ว")
    elif action == "release_test":
        passed = _run(request, lambda: ai_release.test(actor, label), "ทดสอบการเชื่อมต่อแล้ว")
        if passed is False:
            messages.warning(request, "การเชื่อมต่อไม่ผ่าน ตรวจคีย์ในแท็บผู้ให้บริการ AI")
    elif action == "release_activate":
        _run(
            request,
            lambda: ai_release.activate(actor, label, data.getlist("tasks"), reason),
            f"เปิดใช้รุ่น {label} แล้ว",
        )
    elif action == "release_reject":
        _run(request, lambda: ai_release.reject(actor, label, reason), f"ตั้งรุ่น {label} ว่าไม่ใช้แล้ว")
        return _tab_url("releases")
    else:
        raise Http404
    return _tab_url("chain")


@ai_editor_required
@require_POST
def admin_ai_action(request, action, pk=None):
    actor, data = request.user, request.POST
    reason = data.get("reason", "")
    if action.startswith(("provider_", "key", "model_")):
        return redirect(_provider_action(request, action, pk))
    if action.startswith("release_"):
        return redirect(_release_action(request, action))
    if action == "task_switch":
        on = data.get("on") == "1"
        _run(
            request,
            lambda: ai_release.switch(actor, data.get("task", ""), on, reason),
            "เปิด AI ของงานนี้แล้ว" if on else "ปิด AI ของงานนี้แล้ว",
        )
        return redirect(_tab_url("chain", "switches"))
    if action == "evidence":
        upload = request.FILES.get("evidence")
        if upload is None or upload.size > ai_evidence.MAX_BYTES:
            messages.error(request, "เลือกไฟล์ผลประเมินจากเครื่องที่ประเมิน")
            return redirect(_tab_url("chain"))
        raw = upload.read()
        if ai_evidence.is_release(raw):
            label = _run(
                request,
                lambda: ai_evidence.import_release(actor, raw, reason),
                "นำเข้าผลประเมินแล้ว",
            )
            return redirect(_tab_url("chain", f"r{label}" if label else ""))
        imported = _run(
            request, lambda: ai_evidence.import_evidence(actor, raw, reason), "นำเข้าผลประเมินแล้ว"
        )
        if imported:
            source = imported[0].eval_result["imported"]["bundle"]["source_version"]
            # An earlier version is numbered here, so say which one the evidence went to.
            messages.info(request, f"ผลประเมินของ v{source} อยู่ที่ v{imported[0].number} บนเครื่องนี้")
        if imported and imported[1]:
            messages.warning(request, imported[1])
        return redirect(_tab_url("releases", f"v{imported[0].number}" if imported else ""))
    # The earlier per-task versions: test, evaluate, switch back to or set aside (Q16).
    if action == "switch":
        version = AIConfigVersion.objects.filter(pk=data.get("version") or None).first()
        if version is None:
            messages.error(request, "เลือกรุ่นที่จะเปลี่ยนไปใช้")
        else:
            _run(
                request,
                lambda: ai.activate(actor, version, reason),
                f"เปลี่ยนไปใช้ {version.label} แล้ว",
            )
        return redirect(_tab_url("releases"))
    if action in {"test", "evaluate", "activate", "reject"}:
        version = get_object_or_404(AIConfigVersion, pk=pk)
        _run(
            request,
            {
                "test": lambda: ai.test_version(actor, version),
                "evaluate": lambda: ai_eval.request_eval(actor, version),
                "activate": lambda: ai.activate(actor, version, reason),
                "reject": lambda: ai.reject(actor, version, reason),
            }[action],
            {
                "test": "ทดสอบแล้ว",
                "evaluate": "เข้าคิวประเมินแล้ว",
                "activate": "เปิดใช้แล้ว",
                "reject": "ตั้งว่าไม่ใช้แล้ว",
            }[action],
        )
        return redirect(_tab_url("releases", f"v{version.number}"))
    raise Http404
