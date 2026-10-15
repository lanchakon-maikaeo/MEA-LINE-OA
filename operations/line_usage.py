"""LINE pushes per case and the package forecast (C.2.3, M13).

The pilot report for the budget owner: how many pushes each month used, how many per case, the
month's forecast, and the forecast once all 18 districts are open. The rule is C.2.3: propose
Basic when the forecast passes 80% of the free quota two months running, or when the 18-district
forecast passes the free quota. Counts only: no names, LINE user IDs or message text.
"""

import calendar
from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from operations import notifications
from operations.models import Case, District, LineQuotaSnapshot, NotificationDelivery

N = NotificationDelivery
COUNTED = (N.Status.SENT, N.Status.UNKNOWN)  # as the monthly budget counts them
# LINE OA packages in Thailand, messages per month, checked 6 Oct 2569 (PROJECT.md C.2.3).
# Check lineforbusiness.com again before proposing one; the prices are in PROJECT.md.
PLANS = (("free", 300), ("basic", 15_000), ("pro", 35_000))
WARN_SHARE = 0.8
ALL_DISTRICTS = 18


def _month_starts(now, count):
    """The first instant of each of the last `count` months, oldest first."""
    starts = [notifications._month_start(now)]
    for _ in range(count - 1):
        starts.insert(0, (starts[0] - timedelta(days=1)).replace(day=1))
    return starts


def _next_month(start):
    days = calendar.monthrange(start.year, start.month)[1]
    return (start + timedelta(days=days)).replace(day=1)


def month(start, now):
    end = _next_month(start)
    complete = now >= end
    pushes = N.objects.filter(
        channel=N.Channel.LINE_PUSH, status__in=COUNTED, sent_at__gte=start, sent_at__lt=end
    )
    kinds = Counter()
    for kind, counted in pushes.values_list("kind", "counted"):
        kinds[kind] += counted
    total = sum(kinds.values())
    staff = sum(n for kind, n in kinds.items() if kind.startswith("staff_"))
    on_cases = (
        pushes.exclude(kind__startswith="staff_")
        .filter(case__isnull=False)
        .aggregate(n=Sum("counted"))["n"]
        or 0
    )
    cases = Case.objects.filter(submitted_at__gte=start, submitted_at__lt=min(end, now))
    submitted = cases.count()
    districts = cases.values("district").distinct().count()
    if complete:
        forecast = total
    else:
        elapsed = max((now - start).total_seconds() / 86400, 1)
        forecast = int(total / elapsed * (end - start).days)
    held = N.objects.filter(channel=N.Channel.LINE_PUSH, send_after__gte=start, send_after__lt=end)
    return {
        "month": notifications.month_key(start),
        "complete": complete,
        "pushes": {
            "total": total,
            "requester": total - staff,
            "staff": staff,
            "on_cases": on_cases,
            "by_kind": dict(sorted(kinds.items())),
        },
        "deferred": held.filter(status=N.Status.DEFERRED).count(),
        "failed": held.filter(status=N.Status.FAILED).count(),
        "cases": {
            "submitted": submitted,
            "line": cases.filter(channel=Case.Channel.LINE).count(),
            "districts": districts,
        },
        "pushes_per_case": round(on_cases / submitted, 2) if submitted else None,
        "forecast": forecast,
        "forecast_all_districts": (
            int(forecast * ALL_DISTRICTS / districts) if districts else None
        ),
    }


def recommend(months):
    """The package C.2.3 proposes from these months, with the reasons in Thai."""
    free = PLANS[0][1]
    reasons = []
    hot = [m["forecast"] > WARN_SHARE * free for m in months]
    if any(a and b for a, b in zip(hot, hot[1:], strict=False)):
        reasons.append(f"คาดการณ์เกิน {int(WARN_SHARE * 100)}% ของโควตาฟรี ({free}) สองเดือนติดกัน")
    projected = max((m["forecast_all_districts"] or 0 for m in months), default=0)
    if projected > free:
        reasons.append(f"คาดการณ์เมื่อเปิดครบ {ALL_DISTRICTS} เขต {projected} ข้อความต่อเดือน เกินโควตาฟรี")
    if not reasons:
        return {"plan": "free", "reasons": ["ยังอยู่ในโควตาฟรีตามเกณฑ์ C.2.3"], "budget": free}
    plan, quota = next(
        ((name, quota) for name, quota in PLANS[1:] if projected <= quota), PLANS[-1]
    )
    if projected > quota:
        reasons.append(f"คาดการณ์เกินโควตาของแพ็กเกจ {plan} ต้องคุมด้วยงบรายเดือน")
    return {"plan": plan, "reasons": reasons, "budget": quota}


def report(now=None, months=2):
    now = now or timezone.now()
    rows = [month(start, now) for start in _month_starts(now, max(1, months))]
    snapshot = LineQuotaSnapshot.objects.order_by("-month").first()
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "environment": settings.APP_ENV,
        "districts_in_system": District.objects.count(),
        "plans": dict(PLANS),
        "budget_setting": settings.LINE_PUSH_MONTHLY_BUDGET,
        "quota_snapshot": (
            {"month": snapshot.month, "quota": snapshot.quota, "usage": snapshot.usage}
            if snapshot
            else None
        ),
        "months": rows,
        "recommendation": recommend(rows),
    }
