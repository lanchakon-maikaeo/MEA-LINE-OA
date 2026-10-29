"""Monitoring (15 ต.ค. 2569 Q40–Q47): how the AI tasks, the chat answers (RAG) and the system
are doing, day by day, for the admin page.

Each day becomes one MonitoringDay of counts and rates; nothing in it names a person or carries
text anyone typed. The worker computes yesterday after the hour ตัวเลือกระบบ sets (02:00 by
default) and keeps a year; an admin with the AI right can compute now. Evidently (a library in
this app; no service, no cloud, telemetry off) compares the messages of the last 7 days with
the 28 days before on numbers taken from the masked text (length, masked items, digits, hour),
never the text itself; การใช้ไฟฟ้า messages are left out entirely.

16 ต.ค. 2569 Q1: the alerts are Evidently's own tests, pass or fail, on the day's data: AI
errors and speed per task, questions the bot could not answer against the week before, message
drift, and (when its option is on) the LLM judge's scores of the chat answers (monitor_judge).
A failed test raises an alert: a red dot on the admin menu and an item on the admin home, and
an e-mail to admins when the option is on. A stuck outbox is the one plain system check.
"""

import logging
import os
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from operations import identity, notifications, options, pii
from operations.models import (
    AIEvalJob,
    AIKeywordRun,
    AIRoutingRun,
    AIRun,
    AITriageRun,
    Case,
    ConversationMessage,
    Handoff,
    MaskCount,
    MonitoringDay,
    NotificationDelivery,
    OutboxEvent,
    RoleGrant,
    RoutingWord,
    UnansweredQuestion,
    WebhookInbox,
)

os.environ.setdefault("DO_NOT_TRACK", "1")  # Evidently's telemetry stays off (Q42)
os.environ.setdefault("EVIDENTLY_DISABLE_TELEMETRY", "1")

logger = logging.getLogger(__name__)

TASKS = {
    "chat": "แชต LINE (สกัดร่างและตอบแชต)",
    "triage": "คัดกรองเรื่อง",
    "routing": "เสนอกองและคำสำคัญ",
    "keywords": "ยกคำสำคัญ",
}
FEATURES = {
    "length": "ความยาวข้อความ",
    "masked": "ข้อมูลส่วนบุคคลที่ถูกปิด",
    "digits": "สัดส่วนตัวเลข",
    "hour": "ชั่วโมงที่ส่ง",
}
CURRENT_DAYS, REFERENCE_DAYS, MIN_ROWS = 7, 28, 20
LABEL = re.compile(r"\[[^\]\s]+-\d{1,4}\]")
OK = {"ok", "accepted", "kept first answer"}
_last_check = None


# --- One day's numbers --------------------------------------------------------------------------


def _bounds(day):
    start = timezone.make_aware(datetime.combine(day, time.min))
    return start, start + timedelta(days=1)


def _percentile(values, share):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(share * len(ordered)))]


def _calls(day):
    """Every AI run of the day as (task, model, outcome kind, latency ms, fell back)."""
    start, end = _bounds(day)
    rows = []
    sources = [
        (AIRun.objects.filter(created_at__gte=start, created_at__lt=end), "chat"),
        (AITriageRun.objects.filter(created_at__gte=start, created_at__lt=end), "triage"),
        (AIRoutingRun.objects.filter(created_at__gte=start, created_at__lt=end), "routing"),
        (AIKeywordRun.objects.filter(created_at__gte=start, created_at__lt=end), "keywords"),
    ]
    for queryset, kind in sources:
        for run in queryset.only("attempts"):
            attempts = [a for a in (run.attempts or []) if isinstance(a, dict)]
            tried = [a for a in attempts if a.get("model")]
            good = [a for a in tried if str(a.get("outcome", "")) in OK]
            if not tried:
                continue  # nothing reached a model (no version, blocked before the provider)
            last = good[-1] if good else tried[-1]
            outcome = str(last.get("outcome", ""))
            if good:
                result = "ok"
            elif "timeout" in outcome.lower() or "time" in outcome.lower():
                result = "timeout"
            else:
                result = "error"
            fell_back = bool(good) and tried.index(good[0]) > 0
            latency = sum(int(a.get("latency_ms") or 0) for a in tried) or None
            rows.append((kind, last.get("model", ""), result, latency, fell_back))
    return rows


def _ai(day):
    by_task = defaultdict(list)
    for row in _calls(day):
        by_task[row[0]].append(row)
    out = {}
    for task, rows in by_task.items():
        latencies = [r[3] for r in rows if r[3]]
        models = defaultdict(lambda: {"calls": 0, "ok": 0})
        for _, model, result, _, _ in rows:
            models[model]["calls"] += 1
            models[model]["ok"] += result == "ok"
        out[task] = {
            "calls": len(rows),
            "ok": sum(r[2] == "ok" for r in rows),
            "fallback": sum(r[4] for r in rows),
            "error": sum(r[2] == "error" for r in rows),
            "timeout": sum(r[2] == "timeout" for r in rows),
            "p50_ms": _percentile(latencies, 0.5),
            "p95_ms": _percentile(latencies, 0.95),
            "models": dict(models),
        }
    return out


def _rag(day):
    """The chat answers: answered by the bank, or a question the bot could not answer."""
    start, end = _bounds(day)
    keys, answered, written, refused, repeats = Counter(), 0, 0, 0, 0
    seen = defaultdict(list)
    runs = AIRun.objects.filter(created_at__gte=start, created_at__lt=end).only(
        "result", "identity_id", "created_at"
    )
    for run in runs:
        result = run.result if isinstance(run.result, dict) else {}
        answer = result.get("answer") if isinstance(result.get("answer"), dict) else None
        if answer is None:
            continue
        key = answer.get("key") or ""
        if key and key != "none":
            keys[key] += 1
            answered += 1
            written += answer.get("by") == "model"
        refused += bool(answer.get("refused"))
        if run.identity_id:
            seen[run.identity_id].append(run.created_at)
    for moments in seen.values():
        moments.sort()
        repeats += sum(
            1 for a, b in zip(moments, moments[1:], strict=False) if b - a <= timedelta(minutes=10)
        )
    new_questions = UnansweredQuestion.objects.filter(first_at__gte=start, first_at__lt=end)
    asked_again = UnansweredQuestion.objects.filter(last_at__gte=start, last_at__lt=end)
    return {
        "answered": answered,
        "written": written,
        "unanswered": asked_again.count(),
        "new_questions": new_questions.count(),
        "open_questions": UnansweredQuestion.objects.filter(status="open").count(),
        "refused": refused,
        "repeats": repeats,
        "keys": dict(keys.most_common(10)),
    }


def _quality(day):
    start, end = _bounds(day)
    extract = AIRun.objects.filter(created_at__gte=start, created_at__lt=end, status="applied")
    corrections = [r.corrections for r in extract.only("corrections") if r.corrections is not None]
    routing = AIRoutingRun.objects.filter(created_at__gte=start, created_at__lt=end)
    handoffs = Handoff.objects.filter(decided_at__gte=start, decided_at__lt=end)
    return {
        "extract_runs": extract.count(),
        "corrections_avg": round(statistics.mean(corrections), 2) if corrections else None,
        "triage_runs": AITriageRun.objects.filter(
            created_at__gte=start, created_at__lt=end
        ).count(),
        "routing_runs": routing.count(),
        "routing_applied": routing.filter(applied=True).count(),
        "words_learned": RoutingWord.objects.filter(
            created_at__gte=start, created_at__lt=end
        ).count(),
        "words_by_ai": RoutingWord.objects.filter(
            created_at__gte=start, created_at__lt=end, source="ai"
        ).count(),
        "wrong_division": handoffs.filter(
            status=Handoff.Status.ACCEPTED, reason_category="wrong_division"
        ).count(),
    }


def _messages(start, end):
    """Messages people typed in LINE, without การใช้ไฟฟ้า (never analysed, Q42)."""
    return (
        ConversationMessage.objects.filter(created_at__gte=start, created_at__lt=end)
        .exclude(case__topic=Case.Topic.USAGE)
        .exclude(draft__data__topic=Case.Topic.USAGE)
        .only("text", "created_at")
    )


def _features(start, end):
    """Numbers from each masked message; the text itself goes nowhere."""
    rows = []
    stops = pii.domain_words()
    lists = (pii.known_names(stops), pii.masked_words(), pii.item_codes(), stops)
    for message in _messages(start, end).iterator():
        masked = pii.Masker(*lists).mask(message.text or "") or ""  # fresh labels each time
        plain = LABEL.sub("", masked)
        digits = sum(ch.isdigit() for ch in plain)
        rows.append(
            {
                "length": len(masked),
                "masked": len(LABEL.findall(masked)),
                "digits": round(digits / len(plain), 3) if plain else 0.0,
                "hour": timezone.localtime(message.created_at).hour,
            }
        )
    return rows


def _inputs(day):
    start, end = _bounds(day)
    rows = _features(start, end)
    lengths = [r["length"] for r in rows]
    masked = dict(
        MaskCount.objects.filter(day=day)
        .exclude(task="eval")
        .values_list("kind")
        .annotate(n=Sum("count"))
    )
    return {
        "messages": len(rows),
        "avg_length": round(statistics.mean(lengths), 1) if lengths else None,
        "p95_length": _percentile(lengths, 0.95),
        "masked": masked,
    }


def _evals(day):
    start, end = _bounds(day)
    jobs = AIEvalJob.objects.filter(finished_at__gte=start, finished_at__lt=end).select_related(
        "version"
    )
    return [
        {
            "version": j.version.number,
            "task": j.version.task,
            "passed": j.passed,
            "status": j.status,
        }
        for j in jobs
    ]


def _system(day, now):
    start, end = _bounds(day)
    state = notifications.budget_state(now)
    pending = OutboxEvent.objects.filter(status__in=["pending", "leased"])
    oldest = pending.order_by("occurred_at").values_list("occurred_at", flat=True).first()
    inbox = WebhookInbox.objects.filter(
        received_at__gte=start, received_at__lt=end, processed_at__isnull=False
    )
    waits = [
        (row.processed_at - row.received_at).total_seconds()
        for row in inbox.only("received_at", "processed_at")
    ]
    done = OutboxEvent.objects.filter(completed_at__isnull=False).order_by("-completed_at")
    last_done = done.values_list("completed_at", flat=True).first()
    return {
        "push_used": state.used,
        "push_budget": state.budget,
        "push_tier": state.tier,
        "failed_sends": NotificationDelivery.objects.filter(
            created_at__gte=start, created_at__lt=end, status__in=["failed", "unknown"]
        ).count(),
        "outbox_pending": pending.count(),
        "outbox_failed": OutboxEvent.objects.filter(status="failed").count(),
        "oldest_pending_s": int((now - oldest).total_seconds()) if oldest else 0,
        "webhook_p95_s": round(_percentile(waits, 0.95), 1) if waits else None,
        "worker_seen": last_done.isoformat() if last_done else "",
    }


# --- Evidently ----------------------------------------------------------------------------------


def _frames(day):
    import pandas as pd

    _, end = _bounds(day)
    current_start = end - timedelta(days=CURRENT_DAYS)
    reference_start = current_start - timedelta(days=REFERENCE_DAYS)
    current = pd.DataFrame(_features(current_start, end), columns=list(FEATURES))
    reference = pd.DataFrame(_features(reference_start, current_start), columns=list(FEATURES))
    return current, reference


def _report(current, reference):
    from evidently import DataDefinition, Dataset, Report
    from evidently.metrics import DriftedColumnsCount
    from evidently.presets import DataDriftPreset, DataSummaryPreset
    from evidently.tests import lt

    definition = DataDefinition(numerical_columns=list(FEATURES))
    # Evidently's test: fewer than half of the measured columns drifted.
    drifted = DriftedColumnsCount(tests=[lt(len(FEATURES) / 2)])
    return Report([DataSummaryPreset(), DataDriftPreset(), drifted]).run(
        Dataset.from_pandas(current, data_definition=definition),
        Dataset.from_pandas(reference, data_definition=definition),
    )


def _status(test):
    return "pass" if str(test.get("status", "")).endswith("SUCCESS") else "fail"


ACTUAL = re.compile(r"Actual value (-?[0-9.]+)")


def _actual(test):
    match = ACTUAL.search(str(test.get("description", "")))
    return float(match.group(1)) if match else None


def run_tests(frame, checks):
    """Evidently's tests on one table: `checks` are (metric with its test, Thai name, how to
    show the value). Returns one row per test: name, pass or fail, the value shown, and
    Evidently's own wording."""
    from evidently import DataDefinition, Dataset, Report

    definition = DataDefinition(numerical_columns=list(frame.columns))
    snapshot = Report([metric for metric, _, _ in checks]).run(
        Dataset.from_pandas(frame, data_definition=definition), None
    )
    tests = snapshot.dict().get("tests", [])
    out = []
    for (_, name, shown), test in zip(checks, tests, strict=False):
        value = _actual(test)
        out.append(
            {
                "name": name,
                "status": _status(test),
                "value": shown(value) if value is not None else "",
                "evidently": str(test.get("description", ""))[:200],
            }
        )
    return out


def drift(day):
    """Evidently's drift check of the last 7 days against the 28 before, as plain numbers."""
    current, reference = _frames(day)
    if len(current) < MIN_ROWS or len(reference) < MIN_ROWS:
        return {"status": "few", "current": len(current), "reference": len(reference)}
    try:
        result = _report(current, reference).dict()
    except Exception as exc:  # noqa: BLE001 - a library failure must not stop the day's numbers
        logger.warning("Evidently drift failed: %s", type(exc).__name__)
        return {"status": "error", "error": type(exc).__name__}
    columns, share = {}, None
    test = next(iter(result.get("tests", [])), None)
    for metric in result.get("metrics", []):
        config = metric.get("config", {})
        kind = str(config.get("type", ""))
        if kind.endswith("DriftedColumnsCount"):
            share = (metric.get("value") or {}).get("share")
        elif kind.endswith("ValueDrift") and config.get("column") in FEATURES:
            p_value = metric.get("value")
            threshold = config.get("threshold", 0.05)
            columns[config["column"]] = {
                "p_value": round(float(p_value), 4) if p_value is not None else None,
                "drifted": p_value is not None and float(p_value) < float(threshold),
                "method": config.get("method", ""),
            }
    return {
        "status": "ok",
        "current": len(current),
        "reference": len(reference),
        "share": round(float(share), 3) if share is not None else None,
        "columns": columns,
        "test": _status(test) if test else "",
        "evidently": str(test.get("description", ""))[:200] if test else "",
    }


def full_report_html(day=None):
    """Evidently's own report of the message numbers (the admin's 'เปิดรายงานเต็ม')."""
    day = day or timezone.localdate()
    current, reference = _frames(day)
    if len(current) < MIN_ROWS or len(reference) < MIN_ROWS:
        return None
    return _report(current, reference).get_html_str(as_iframe=False)


# --- Alerts -------------------------------------------------------------------------------------


def _tests(day, metrics):
    """Evidently's tests of the day (16 ต.ค. 2569 Q1); the thresholds stay options."""
    import pandas as pd
    from evidently.metrics import MeanValue, QuantileValue, RowCount
    from evidently.tests import gte, lte

    limit_error = float(options.get(options.MONITOR_ERROR))
    limit_p95 = int(options.get(options.MONITOR_P95)) / 1000
    times = float(options.get(options.MONITOR_UNANSWERED))
    out = []
    by_task = defaultdict(list)
    for task, _, result, latency, _ in _calls(day):
        by_task[task].append((float(result != "ok"), (latency or 0) / 1000))
    for task, rows in sorted(by_task.items()):
        if len(rows) < 5:
            continue  # too few calls to judge a rate
        name, count = TASKS.get(task, task), len(rows)
        frame = pd.DataFrame(rows, columns=["failed", "latency_s"])
        out += run_tests(
            frame,
            [
                (
                    MeanValue(column="failed", tests=[lte(limit_error)]),
                    f"AI {name}: ผิดพลาดไม่เกิน {limit_error:.0%}",
                    lambda v, n=count: f"ผิดพลาด {v:.0%} ของ {n} ครั้ง",
                ),
                (
                    QuantileValue(column="latency_s", quantile=0.95, tests=[lte(limit_p95)]),
                    f"AI {name}: p95 ไม่เกิน {limit_p95:g} วินาที",
                    lambda v: f"ตอบช้า p95 {v:.1f} วินาที",
                ),
            ],
        )
    before = MonitoringDay.objects.filter(day__lt=day, day__gte=day - timedelta(days=7))
    history = [d.metrics.get("rag", {}).get("unanswered", 0) for d in before]
    today = metrics["rag"]["unanswered"]
    average = sum(history) / len(history) if history else 0
    if today >= 3 and average:
        out += run_tests(
            pd.DataFrame({"asked": [1.0] * today}),
            [
                (
                    RowCount(tests=[lte(times * average)]),
                    f"คำถามที่บอทตอบไม่ได้ไม่เกิน {times:g} เท่าของค่าเฉลี่ย 7 วัน ({average:.1f})",
                    lambda v: f"{v:.0f} ครั้ง",
                ),
            ],
        )
    drift = metrics.get("drift", {})
    if drift.get("test"):
        out.append(
            {
                "name": "ข้อความที่เข้ามาไม่เปลี่ยนไปจากช่วงก่อนหน้า (น้อยกว่าครึ่งของตัววัด)",
                "status": drift["test"],
                "value": f"เปลี่ยน {round((drift.get('share') or 0) * 100)}% ของตัววัด",
                "evidently": drift.get("evidently", ""),
            }
        )
    judge = metrics.get("judge", {})
    if judge.get("status") == "ok":
        from operations import monitor_judge

        frame = pd.DataFrame(judge["scores"], columns=["faithful", "relevant"])
        out += run_tests(
            frame,
            [
                (
                    MeanValue(column="faithful", tests=[gte(monitor_judge.ALERT)]),
                    f"คำตอบแชตตรงกับคลังคำตอบอย่างน้อย {monitor_judge.ALERT:.0%}",
                    lambda v: f"ตรง {v:.0%} จาก {len(frame)} คำตอบ",
                ),
                (
                    MeanValue(column="relevant", tests=[gte(monitor_judge.ALERT)]),
                    f"คำตอบแชตตรงกับคำถามอย่างน้อย {monitor_judge.ALERT:.0%}",
                    lambda v: f"ตรงคำถาม {v:.0%} จาก {len(frame)} คำตอบ",
                ),
            ],
        )
    return out


def _alerts(metrics):
    """A failed Evidently test is an alert; so is work stuck in the outbox."""
    out = [f"{t['name']}: ไม่ผ่าน ({t['value']})" for t in metrics["tests"] if t["status"] == "fail"]
    if metrics["system"]["outbox_failed"]:
        out.append(f"งานที่ส่งไม่สำเร็จค้าง {metrics['system']['outbox_failed']} งาน")
    return out


# --- Computing and keeping ----------------------------------------------------------------------


@transaction.atomic
def compute(day, now=None):
    """One day's numbers, stored (replacing what was there)."""
    now = now or timezone.now()
    metrics = {
        "ai": _ai(day),
        "rag": _rag(day),
        "quality": _quality(day),
        "inputs": _inputs(day),
        "evals": _evals(day),
        "system": _system(day, now),
        "drift": drift(day),
    }
    from operations import monitor_judge  # local: the judge loads Evidently's LLM parts

    metrics["judge"] = monitor_judge.nightly(day)
    try:
        metrics["tests"] = _tests(day, metrics)
    except Exception as exc:  # noqa: BLE001 - a library failure must not lose the day's numbers
        logger.warning("Evidently tests failed: %s", type(exc).__name__)
        metrics["tests"] = [
            {"name": "Evidently ตรวจไม่สำเร็จ", "status": "fail", "value": type(exc).__name__}
        ]
    alerts = _alerts(metrics)
    row, _ = MonitoringDay.objects.update_or_create(
        day=day, defaults={"metrics": metrics, "alerts": alerts, "computed_at": now}
    )
    return row


def refresh(actor, days=30, now=None):
    """'คำนวณตอนนี้': today so far, plus any of the last `days` days not computed yet."""
    identity.require_ai_editor(actor)
    now = now or timezone.now()
    today = timezone.localdate(now)
    done = set(
        MonitoringDay.objects.filter(day__gte=today - timedelta(days=days)).values_list(
            "day", flat=True
        )
    )
    for back in range(days, 0, -1):
        day = today - timedelta(days=back)
        if day not in done:
            compute(day, now)
    row = compute(today, now)
    identity.audit(actor, "monitoring.refreshed", row, day=today.isoformat())
    return row


def maybe_run(now=None):
    """The worker's nightly job: after the set hour, yesterday once; then prune (Q43)."""
    global _last_check
    now = now or timezone.now()
    if _last_check and now - _last_check < timedelta(minutes=1):
        return None
    _last_check = now
    local = timezone.localtime(now)
    hour, minute = (int(x) for x in options.get(options.MONITOR_TIME).split(":"))
    if (local.hour, local.minute) < (hour, minute):
        return None
    yesterday = local.date() - timedelta(days=1)
    if MonitoringDay.objects.filter(day=yesterday, computed_at__date__gt=yesterday).exists():
        return None
    try:
        row = compute(yesterday, now)
    except Exception:  # noqa: BLE001 - the worker goes on; tomorrow tries again
        logger.exception("Monitoring for %s failed", yesterday)
        return None
    keep = int(options.get(options.MONITOR_KEEP))
    MonitoringDay.objects.filter(day__lt=local.date() - timedelta(days=keep)).delete()
    if row.alerts and options.get(options.MONITOR_EMAIL) == "on":
        _mail(row)
    return row


def _mail(row):
    admins = (
        get_user_model()
        .objects.filter(
            is_active=True,
            pk__in=RoleGrant.objects.filter(role="admin", revoked_at__isnull=True).values("user"),
        )
        .exclude(email="")
        .values_list("email", flat=True)
    )
    if not admins:
        return
    body = "\n".join(
        [f"Monitoring วันที่ {row.day:%d/%m/%Y} มีเรื่องที่ควรดู", "", *row.alerts, ""]
        + [f"ดูที่ {settings.STAFF_WEB_BASE_URL}/console/admin/monitoring/"]
    )
    send_mail("Monitoring DED Center", body, settings.DEFAULT_FROM_EMAIL, list(admins))


def latest_alerts():
    row = MonitoringDay.objects.order_by("-day").first()
    return len(row.alerts) if row and row.day >= timezone.localdate() - timedelta(days=1) else 0


# --- The page -----------------------------------------------------------------------------------


def dashboard(days=14):
    """What the admin page shows: the latest day, then one row per day for each section."""
    rows = list(MonitoringDay.objects.order_by("-day")[:days])
    rows.reverse()
    latest = rows[-1] if rows else None
    ai_rows, daily, rag_rows, quality_rows, input_rows, evals = [], [], [], [], [], []
    keys = Counter()
    for row in rows:
        metrics = row.metrics
        calls = errors = 0
        for task, data in metrics.get("ai", {}).items():
            n = data.get("calls") or 0
            bad = (data.get("error") or 0) + (data.get("timeout") or 0)
            calls, errors = calls + n, errors + bad
            ai_rows.append(
                {
                    "day": row.day,
                    "task": TASKS.get(task, task),
                    "calls": n,
                    "ok": _share(data.get("ok"), n),
                    "fallback": _share(data.get("fallback"), n),
                    "error": _share(bad, n),
                    "p50": data.get("p50_ms"),
                    "p95": data.get("p95_ms"),
                }
            )
        rag = metrics.get("rag", {})
        keys.update(rag.get("keys", {}))
        daily.append(
            {
                "day": row.day,
                "calls": calls,
                "error": _share(errors, calls),
                "unanswered": rag.get("unanswered", 0),
                "alerts": len(row.alerts),
            }
        )
        rag_rows.append({"day": row.day, **rag})
        quality_rows.append({"day": row.day, **metrics.get("quality", {})})
        input_rows.append({"day": row.day, **metrics.get("inputs", {})})
        evals += [{"day": row.day, **e} for e in metrics.get("evals", [])]
    drift = latest.metrics.get("drift", {}) if latest else {}
    return {
        "rows": rows,
        "latest": latest,
        "daily": daily,
        "most_calls": max([d["calls"] for d in daily] + [1]),
        "most_unanswered": max([d["unanswered"] for d in daily] + [1]),
        "ai_rows": ai_rows,
        "rag_rows": rag_rows,
        "top_keys": keys.most_common(8),
        "quality_rows": quality_rows,
        "input_rows": input_rows,
        "evals": evals,
        "drift": drift,
        "drifted": sum(1 for d in (drift.get("columns") or {}).values() if d.get("drifted")),
        "drift_rows": [
            {"name": FEATURES.get(name, name), **data}
            for name, data in (drift.get("columns") or {}).items()
        ],
        "system": latest.metrics.get("system", {}) if latest else {},
        "tests": latest.metrics.get("tests", []) if latest else [],
        "judge": latest.metrics.get("judge", {}) if latest else {},
        "tasks": TASKS,
    }


def _share(part, whole):
    return round(100 * (part or 0) / whole) if whole else None
