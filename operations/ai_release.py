"""รุ่นการตั้งค่า AI (ADR 0038): one model chain and one gate for every AI task.

A release is one AIConfigVersion per task sharing a CalVer label such as 2026.10.31.1. Each
member keeps its own task's checks; the release can be switched on when every task that could
be evaluated passed. A task with nothing to evaluate yet (no duties, no active policy) does not
block it but stays off. Releases are made only where evaluations run; production imports them,
signed, under the same label (ai_evidence).
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import CommandError
from django.db import transaction
from django.utils import timezone

from config.environment import require_demo_database
from operations import ai, ai_eval, identity
from operations.models import AIConfigVersion, AIEvalJob, AIModel

Task = AIConfigVersion.Task
Status = AIConfigVersion.Status
TASKS = tuple(Task.values)
OPEN_JOBS = (AIEvalJob.Status.QUEUED, AIEvalJob.Status.RUNNING, AIEvalJob.Status.WAITING)
STATE_LABELS = {
    "queued": "รอคิว",
    "running": "กำลังประเมิน",
    "waiting": "รอโควตา",
    "not_ready": "ยังไม่พร้อมประเมิน",
    "pending": "ยังไม่ประเมิน",
    "passed": "ผ่าน",
    "failed": "ไม่ผ่าน",
}


def evaluates_here():
    """The evaluating machine: evaluations replay drafts, so only a demo database runs them."""
    try:
        require_demo_database(settings.APP_ENV, settings.DATABASES["default"])
    except CommandError:
        return False
    return True


def _order(label):
    return tuple(int(part) for part in label.split("."))


def labels():
    """Every release, newest first."""
    found = AIConfigVersion.objects.exclude(release="").values_list("release", flat=True)
    return sorted(set(found), key=_order, reverse=True)


def next_label(today=None):
    day = (today or timezone.localdate()).strftime("%Y.%m.%d")
    used = [_order(label)[3] for label in labels() if label.startswith(f"{day}.")]
    return f"{day}.{max(used, default=0) + 1}"


def members(label):
    """{task: version} of one release."""
    return {
        v.task: v
        for v in AIConfigVersion.objects.filter(release=label).prefetch_related(
            "entries__profile__credential", "eval_jobs"
        )
    }


def current():
    """The release whose members serve now, or None."""
    version = (
        AIConfigVersion.objects.filter(status=Status.ACTIVE)
        .exclude(release="")
        .order_by("-activated_at")
        .first()
    )
    return version.release if version else None


def task_is_on(task):
    return ai.active_version(task) is not None


def extract_defaults():
    """ตั้งค่าขั้นสูง starts from the extraction version in use (Q15)."""
    active = ai.active_version(Task.EXTRACT)
    return {
        "use_json_schema": True,
        "pipeline": active.pipeline if active else AIConfigVersion.Pipeline.HARNESS,
        "few_shot": active.few_shot if active else 3,
        "typed_fields": ai.reads_typed(active) if active else True,
        "menu_fields": ai.reads_menu(active) if active else True,
        "slip_facts": ai.reads_slips(active) if active else True,
    }


@transaction.atomic
def create(actor, models, options=None):
    """A new release for every task with this chain, on the evaluating machine only (Q17)."""
    identity.require_ai_editor(actor)
    if not evaluates_here():
        raise ValidationError("สร้างรุ่นได้บนเครื่องที่ประเมินเท่านั้น")
    picked = [m.pk for m in models if m is not None]
    fresh = AIModel.objects.select_related("provider").in_bulk(picked)  # keys as saved now
    models = [fresh[pk] for pk in picked if pk in fresh]
    if not 1 <= len(models) <= ai.MAX_CHAIN or len(set(picked)) != len(models):
        raise ValidationError("เลือกโมเดลหลัก และโมเดลสำรองได้ไม่เกินสามตัวที่ไม่ซ้ำกัน")
    missing = list(dict.fromkeys(m.provider.title for m in models if not m.provider.has_key))
    if missing:
        raise ValidationError(f"ยังไม่มีคีย์ของ {' และ '.join(missing)}")
    chosen = {**extract_defaults(), **(options or {})}
    profiles = [ai.model_profile(actor, m, chosen["use_json_schema"]) for m in models]
    AIConfigVersion.objects.select_for_update().order_by("-number").first()  # one label at a time
    label = next_label()
    versions = []
    for task in TASKS:
        extract = task == Task.EXTRACT
        typed = extract and bool(chosen["typed_fields"])
        menu = typed and bool(chosen["menu_fields"])
        versions.append(
            ai.create_version(
                actor,
                profiles,
                f"รุ่น {label}",
                int(chosen["few_shot"]) if extract else 0,
                chosen["pipeline"] if extract else AIConfigVersion.Pipeline.SINGLE,
                task=task,
                typed_fields=typed,
                menu_fields=menu,
                slip_facts=menu and bool(chosen["slip_facts"]),
            )
        )
    AIConfigVersion.objects.filter(pk__in=[v.pk for v in versions]).update(release=label)
    identity.audit(
        actor,
        "ai.release_created",
        versions[0],
        label=label,
        chain=[m.model_id for m in models],
        **{k: chosen[k] for k in sorted(chosen)},
    )
    return label


def evaluate(actor, label):
    """Queue every task's evaluation; the worker tests the connection first (Q25)."""
    identity.require_ai_editor(actor)
    queued = 0
    for version in members(label).values():
        if version.status == Status.REJECTED:
            continue
        if version.eval_jobs.filter(status__in=OPEN_JOBS).exists():
            continue
        ai_eval.request_eval(actor, version)
        queued += 1
    return queued


def tested(version):
    return bool(version.test_passed) and ai._tested_after_rotation(version)


def test(actor, label):
    """The connection test once for the shared chain, with the extraction member's call; every
    member records the same outcome (they call the same models with the same keys)."""
    found = members(label)
    if Task.EXTRACT not in found:
        raise ValidationError("รุ่นนี้ไม่ครบทุกงาน")
    lead = ai.test_version(actor, found[Task.EXTRACT])
    AIConfigVersion.objects.filter(release=label).exclude(pk=lead.pk).update(
        test_passed=lead.test_passed, test_result=lead.test_result
    )
    return lead.test_passed


def not_ready(version):
    """Why this task had nothing to evaluate, or ''."""
    return (version.eval_result or {}).get("not_ready", "")


def eval_blocker(version):
    """The task's own gate on its evidence; the answer member also needs ตอบอิสระ's (Q13)."""
    problem = ai.eval_blocker(version)
    if problem or version.task != Task.ANSWER:
        return problem
    from operations import free_chat_eval

    if not free_chat_eval.gate_passed((version.eval_result or {}).get("free_chat"), version):
        return "Free chat evidence is missing, stale or failed; evaluate again."
    return None


def state(version):
    """One word for the task's row: queued, running, waiting, not_ready, pending, passed,
    failed."""
    job = next(iter(version.eval_jobs.all()), None)
    if job and job.status in OPEN_JOBS:
        return job.status
    if not_ready(version):
        return "not_ready"
    if version.eval_passed is None:
        return "failed" if job and job.status == AIEvalJob.Status.FAILED else "pending"
    return "failed" if eval_blocker(version) else "passed"


def blocker(label, tasks_on):
    """Why this release cannot serve these tasks now, in Thai, or None."""
    found = members(label)
    if set(found) != set(TASKS):
        return "รุ่นนี้ไม่ครบทุกงาน"
    for task in TASKS:
        version = found[task]
        name = Task(task).label
        if version.status == Status.REJECTED:
            return "รุ่นนี้ถูกตั้งว่าไม่ใช้แล้ว"
        if not_ready(version):
            if task in tasks_on:
                return f"{name} ยังไม่พร้อมประเมิน ต้องปิดงานนี้ไว้"
            continue
        if eval_blocker(version):
            return f"{name} ยังไม่ผ่านการประเมิน"
    if not all(tested(v) for v in found.values()):
        return "ทดสอบการเชื่อมต่อก่อน"
    return None


@transaction.atomic
def activate(actor, label, tasks_on, reason):
    """Switch every task to this release at once: the tasks in tasks_on use it, the others
    are off (Q13, Q14). The earlier versions retire as their task switches."""
    identity.require_ai_editor(actor)
    tasks_on = set(tasks_on) & set(TASKS)
    problem = blocker(label, tasks_on)
    if problem:
        raise ValidationError(problem)
    for task, version in members(label).items():
        if task in tasks_on:
            if version.status != Status.ACTIVE:
                ai.activate(actor, version, reason)
        elif task_is_on(task):
            ai.stop(actor, task, reason)
    identity.audit(
        actor,
        "ai.release_activated",
        members(label)[Task.EXTRACT],
        reason,
        label=label,
        tasks_on=sorted(tasks_on),
    )
    return label


def _switch_target(task):
    """The version that serves a task turned on: the current release's member, or before any
    release the newest earlier version that may still be switched to."""
    label = current()
    if label:
        return members(label).get(task)
    for version in AIConfigVersion.objects.filter(task=task, release="").order_by("-number"):
        if version.status != Status.REJECTED and ai.activation_blocker(version) is None:
            return version
    return None


@transaction.atomic
def switch(actor, task, on, reason):
    """The per-task AI switch (Q14)."""
    identity.require_ai_editor(actor)
    if task not in TASKS:
        raise ValidationError("ไม่รู้จักงานนี้")
    if not on:
        if not task_is_on(task):
            raise ValidationError("งานนี้ปิด AI อยู่แล้ว")
        return ai.stop(actor, task, reason)
    if task_is_on(task):
        raise ValidationError("งานนี้เปิด AI อยู่แล้ว")
    target = _switch_target(task)
    if target is None or not_ready(target):
        raise ValidationError("งานนี้ยังไม่มีรุ่นที่ผ่านการประเมิน")
    if ai.activation_blocker(target):
        raise ValidationError("งานนี้ยังไม่ผ่านการประเมิน")
    return ai.activate(actor, target, reason)


def reject(actor, label, reason):
    identity.require_ai_editor(actor)
    for version in members(label).values():
        if version.status == Status.ACTIVE:
            raise ValidationError("รุ่นนี้ใช้งานอยู่ เปลี่ยนรุ่นก่อน")
    for version in members(label).values():
        ai.reject(actor, version, reason)


def overview():
    """Every release with its members' state, newest first, for the admin page."""
    rows = []
    serving = current()
    for label in labels():
        found = members(label)
        tasks = []
        for t in TASKS:
            if t in found:
                word = state(found[t])
                tasks.append(
                    {
                        "task": t,
                        "name": Task(t).label,
                        "version": found[t],
                        "state": word,
                        "state_label": STATE_LABELS[word],
                        "on": task_is_on(t),
                    }
                )
        lead = found.get(Task.EXTRACT) or next(iter(found.values()))
        states = {row["state"] for row in tasks}
        rows.append(
            {
                "label": label,
                "chain": lead.chain(),
                "created_at": lead.created_at,
                "imported": bool((lead.eval_result or {}).get("provenance")),
                "serving": label == serving,
                "rejected": lead.status == Status.REJECTED,
                "tested": all(tested(row["version"]) for row in tasks),
                "evaluated": not states & {"pending", "queued", "running", "waiting", "failed"},
                "busy": bool(states & set(OPEN_JOBS)),
                "waiting": AIEvalJob.Status.WAITING in states,
                "failed": "failed" in states,
                "tasks": tasks,
                "blocker": blocker(label, {r["task"] for r in tasks if r["state"] != "not_ready"}),
            }
        )
    return rows
