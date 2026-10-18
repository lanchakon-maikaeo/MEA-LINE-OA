"""ตัวตัดสินคำตอบแชต (16 ต.ค. 2569 Q1, Q12): Evidently's LLM judges on the chat answers.

Each night, when ตัวเลือกระบบ monitoring_judge is on, up to 50 of yesterday's chat answers are
judged by Evidently's own evaluators: faithfulness (the answer keeps to the approved answer it
came from) and relevance (it answers what was asked). The judge is the main model of the active
answer version, called through our own provider (the same encrypted key and endpoint checks as
every AI call). It reads masked text only, never a message about การใช้ไฟฟ้า, and the day keeps
only the scores. A share below 80% fails Evidently's test and raises an alert.

The option can be switched on only after the judge scored at least 90% on a frozen set of
synthetic good and bad answers (a calibration the worker runs), for that same model.
"""

import asyncio
import json
import random
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from operations import ai, identity, options, pii
from operations.models import AIConfigVersion, AIRun, Case, JudgeCalibration

PROVIDER = "faoj"
SAMPLE = 50  # answers judged per night at most
ALERT = 0.8  # Evidently's test: at least this share faithful and relevant
PASS = 0.9  # the calibration a model must reach before the option opens
DATASET = Path(settings.BASE_DIR) / "operations" / "fixtures" / "monitor_judge_v1.json"
RELEVANCE = (
    "A RELEVANT answer responds to what the person asked in the QUESTION below, even if only "
    "in part. An IRRELEVANT answer talks about something other than the QUESTION. The texts "
    "are in Thai; judge their meaning.\nQUESTION: {question}"
)
_calls = {}  # model token -> how to reach the provider (filled before Evidently runs)


# --- Evidently's provider -----------------------------------------------------------------------


def _register():
    """Evidently asks a provider for each judgement; ours answers through ai._post."""
    from evidently.llm.utils.wrapper import LLMResult, LLMWrapper, _wrappers, llm_provider

    if (PROVIDER, None) in _wrappers:
        return

    @llm_provider(PROVIDER, None)
    class Judge(LLMWrapper):
        __used_options__ = []

        def __init__(self, model, options):
            self.model = model

        async def complete(self, messages, seed=None):
            sent = [{"role": m.role, "content": m.content} for m in messages]
            content = await asyncio.to_thread(_ask, self.model, sent)
            return LLMResult(content, 0, 0)

        def get_batch_size(self):
            return 2  # gentle on the provider


def _ask(token, messages):
    """One judgement over HTTP; no database here (Evidently runs it on its own thread). The
    texts were masked before they reached Evidently."""
    call = _calls[token]
    body = {
        "model": call["model"],
        "messages": messages,
        "max_tokens": call["max_tokens"],
        "temperature": 0,
    }
    return ai._content(ai._post(call["url"], call["key"], body, call["timeout"]))


def _prepare(profile):
    token = f"profile-{profile.pk}"
    credential = profile.credential
    _calls[token] = {
        "url": f"{credential.base_url}/chat/completions",
        "key": ai.decrypt_key(credential),
        "model": profile.model_id,
        "max_tokens": min(profile.max_tokens, 500),
        "timeout": max(profile.timeout_seconds, 30),
    }
    return token


def main_profile():
    """The primary model of the active chat-answer version, or None."""
    version = ai.active_version(AIConfigVersion.Task.ANSWER)
    if version is None:
        return None, None
    entry = version.entries.select_related("profile__credential").order_by("position").first()
    return version, entry.profile if entry else None


def judge(rows, profile):
    """[(faithful, relevant)] for rows of question, answer and context; None when the judge
    could not tell."""
    import pandas as pd
    from evidently import DataDefinition, Dataset
    from evidently.descriptors import FaithfulnessLLMEval, LLMEval
    from evidently.llm.templates import BinaryClassificationPromptTemplate

    _register()
    model = _prepare(profile)
    frame = pd.DataFrame(rows, columns=["question", "answer", "context"])
    relevance = BinaryClassificationPromptTemplate(
        criteria=RELEVANCE, target_category="RELEVANT", non_target_category="IRRELEVANT"
    )
    dataset = Dataset.from_pandas(
        frame,
        data_definition=DataDefinition(text_columns=["question", "answer", "context"]),
        descriptors=[
            FaithfulnessLLMEval("answer", context="context", provider=PROVIDER, model=model,
                                alias="faithful"),
            LLMEval("answer", provider=PROVIDER, model=model, template=relevance,
                    additional_columns={"question": "question"}, alias="relevant"),
        ],
    )  # fmt: skip
    out = []
    for record in dataset.as_dataframe().to_dict("records"):
        faithful = str(record.get("faithful", "")).upper()
        relevant = str(record.get("relevant", "")).upper()
        out.append(
            (
                {"FAITHFUL": True, "UNFAITHFUL": False}.get(faithful),
                {"RELEVANT": True, "IRRELEVANT": False}.get(relevant),
            )
        )
    return out


# --- Calibration ----------------------------------------------------------------------------------


def load_dataset():
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    return data["examples"]


def request_calibration(actor):
    """'ทดสอบตัวตัดสิน': the worker runs it on the frozen synthetic set."""
    identity.require_ai_editor(actor)
    version, profile = main_profile()
    if profile is None:
        raise ValidationError("ยังไม่มีรุ่น AI ตอบแชตที่ใช้งานอยู่ จึงยังไม่มีโมเดลตัดสิน")
    calibration = JudgeCalibration.objects.create(
        version=version, profile=profile, requested_by=actor
    )
    identity.audit(actor, "monitoring.judge_requested", calibration, profile=profile.pk)
    return calibration


def calibrate(calibration):
    """Judge the synthetic answers and compare with their labels."""
    examples = load_dataset()
    rows = [(e["question"], e["answer"], e["context"]) for e in examples]
    results = judge(rows, calibration.profile)
    details, faithful_hits, relevant_hits = [], 0, 0
    for example, (faithful, relevant) in zip(examples, results, strict=True):
        faithful_hits += faithful == example["faithful"]
        relevant_hits += relevant == example["relevant"]
        details.append(
            {
                "faithful": [example["faithful"], faithful],
                "relevant": [example["relevant"], relevant],
            }
        )
    total = len(examples)
    calibration.faithful = round(faithful_hits / total, 3)
    calibration.relevant = round(relevant_hits / total, 3)
    calibration.passed = calibration.faithful >= PASS and calibration.relevant >= PASS
    calibration.details = details
    return calibration


def process_queued():
    """The worker's turn: run waiting calibrations one at a time."""
    calibration = JudgeCalibration.objects.filter(status=JudgeCalibration.Status.QUEUED).first()
    if calibration is None:
        return None
    JudgeCalibration.objects.filter(pk=calibration.pk).update(
        status=JudgeCalibration.Status.RUNNING
    )
    try:
        calibrate(calibration)
        calibration.status = JudgeCalibration.Status.DONE
    except Exception as exc:  # noqa: BLE001 - the worker goes on; the page shows the failure
        calibration.status, calibration.error = JudgeCalibration.Status.FAILED, type(exc).__name__
    calibration.finished_at = timezone.now()
    calibration.save()
    identity.audit(
        None,
        "monitoring.judge_calibrated",
        calibration,
        actor_label="worker",
        passed=calibration.passed,
        faithful=calibration.faithful,
        relevant=calibration.relevant,
    )
    return calibration


def latest_calibration(profile=None):
    calibrations = JudgeCalibration.objects.select_related("profile", "version")
    if profile is not None:
        calibrations = calibrations.filter(profile=profile)
    return calibrations.order_by("-created_at").first()


def blocker():
    """Why the judge cannot be switched on yet ("" when it can)."""
    _, profile = main_profile()
    if profile is None:
        return "ยังไม่มีรุ่น AI ตอบแชตที่ใช้งานอยู่"
    latest = latest_calibration(profile)
    if latest is None or latest.status != JudgeCalibration.Status.DONE:
        return "ทดสอบตัวตัดสินกับชุดตัวอย่างในหน้า Monitoring ก่อน"
    if not latest.passed:
        return f"ตัวตัดสินยังได้ไม่ถึง {PASS:.0%} ในชุดตัวอย่าง จึงยังเปิดไม่ได้"
    return ""


# --- The nightly judgement ------------------------------------------------------------------------


def _question(run):
    message = (run.inbox.payload or {}).get("message") or {}
    return str(message.get("text", "")) if message.get("type") == "text" else ""


def sample(day):
    """Up to SAMPLE of the day's chat answers as masked (question, answer, approved answer),
    never one about การใช้ไฟฟ้า."""
    from operations import answers, monitoring

    start, end = monitoring._bounds(day)
    runs = (
        AIRun.objects.filter(created_at__gte=start, created_at__lt=end, result__has_key="answer")
        .exclude(draft__data__topic=Case.Topic.USAGE)
        .select_related("inbox")
        .order_by("pk")
    )
    bank = {a.key: a.text for a in answers.approved()}
    masker = pii.Masker.for_ai()
    rows = []
    for run in runs:
        answer = run.result.get("answer") or {}
        context = bank.get(answer.get("key") or "")
        question = _question(run)
        if not context or not question or answer.get("by") not in ("model", "template"):
            continue
        reply = answer.get("reply") if answer.get("by") == "model" else context
        rows.append((masker.mask(question), masker.mask(reply or ""), context))
    random.Random(day.toordinal()).shuffle(rows)
    return rows[:SAMPLE]


def nightly(day):
    """The day's scores for Monitoring, or why there are none."""
    if options.get(options.MONITOR_JUDGE) != "on":
        return {"status": "off"}
    if blocker():
        return {"status": "not_ready"}
    _, profile = main_profile()
    rows = sample(day)
    if not rows:
        return {"status": "none"}
    try:
        results = judge(rows, profile)
    except Exception as exc:  # noqa: BLE001 - a judge failure must not lose the day's numbers
        return {"status": "error", "error": type(exc).__name__}
    scores = [(float(f), float(r)) for f, r in results if f is not None and r is not None]
    return {
        "status": "ok" if scores else "none",
        "sampled": len(rows),
        "judged": len(scores),
        "faithful": round(sum(f for f, _ in scores) / len(scores), 3) if scores else None,
        "relevant": round(sum(r for _, r in scores) / len(scores), 3) if scores else None,
        "scores": scores,
        "model": profile.model_id,
    }
