"""M09 problem cases: resolution, acceptance, dispute, closing and reopening (M09 Q1–Q5, Q15).

Handlers run inside services.perform_action. The owner division's resolver records what was done
(a message for the requester and an internal note); the requester accepts or disputes it, never
by silence; disputes and reopenings go back to the same owner division. Nothing here creates a
withdrawal or a return (A.4.3).
"""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from operations import coordination, identity, workflow
from operations.contracts import STAFF_CHANNELS, ErrorCode, clean_text, fail
from operations.models import Case, ProblemResolution, Role

S = Case.Status
R = ProblemResolution.Category


def _member(ctx, division_id, roles=None):
    return identity.holds(ctx.scope, division_id, roles)


SUGGESTION_RESULTS = (R.CONSIDERED, R.ADOPTED, R.NOT_FEASIBLE)
RESULT_ACTIONS = ("resolve", "admin_answer")  # the result a requester answers (ADR 0030)


def categories_for(case, kind=""):
    """A suggestion is answered (considered/adopted/not feasible); a problem is resolved. A case
    from the division carousel (ADR 0030) is either, as staff say when they record the result."""
    kind = kind or ("suggestion" if case.topic == Case.Topic.SUGGESTION else "problem")
    if kind == "suggestion":
        return list(SUGGESTION_RESULTS)
    return [c for c in R.values if c not in SUGGESTION_RESULTS]


def _kind_of(case, payload):
    """ADR 0030 Q13: ปัญหา or ข้อเสนอแนะ, chosen with the result while the topic is unset."""
    if case.topic != Case.Topic.GENERAL:
        return ""
    kind = payload.get("as")
    if kind not in ("problem", "suggestion"):
        fail(ErrorCode.VALIDATION_ERROR, "เลือกว่าเรื่องนี้เป็นปัญหาหรือข้อเสนอแนะ")
    return kind


def _record_result(ctx, case, payload, target):
    kind = _kind_of(case, payload)
    category = payload.get("category")
    if category not in categories_for(case, kind):
        fail(ErrorCode.VALIDATION_ERROR, "Choose a result category that fits this case.")
    if coordination.blocks_closing(case):
        fail(ErrorCode.VALIDATION_ERROR, "ยังมีงานย่อยค้าง บันทึกผลไม่ได้ (Q1/Q6)")
    resolution = ProblemResolution.objects.create(
        case=case,
        category=category,
        message=clean_text(payload, "message", True),
        internal_note=clean_text(payload, "internal_note", False),
        resolved_by=ctx.user,
    )
    data = {"resolution_id": resolution.pk, "category": category}
    if kind:
        case.topic = Case.Topic.SUGGESTION if kind == "suggestion" else Case.Topic.PROBLEM
        data["topic"] = case.topic
    case.status = target
    return data


def latest(case):
    return case.resolutions.order_by("-id").first()


def resolve(ctx, case, payload, reason, target):
    """ผลการแก้ปัญหา by a resolver of the owner division; child work must be finished (Q1)."""
    if ctx.requester is not None or not _member(ctx, case.owner_division_id, [Role.RESOLVER]):
        fail(ErrorCode.FORBIDDEN, "เฉพาะผู้แก้ปัญหาของกองเจ้าภาพบันทึกผล (Q1)")
    if case.held_by_admin:
        fail(ErrorCode.FORBIDDEN, "เรื่องนี้รอแอดมิน")
    return _record_result(ctx, case, payload, target)


def admin_answer(ctx, case, payload, reason, target):
    """ADR 0030 Q10, Q19: an admin answers a case the admins hold; the owner stays แอดมิน."""
    if ctx.requester is not None or not (ctx.scope.is_admin or ctx.scope.is_support):
        fail(ErrorCode.FORBIDDEN, "เฉพาะแอดมินตอบเรื่องที่รอแอดมิน")
    if not case.held_by_admin:
        fail(ErrorCode.VALIDATION_ERROR, "เรื่องนี้อยู่ที่กองแล้ว ให้กองบันทึกผล")
    return _record_result(ctx, case, payload, target)


def _requester_or_intake(ctx, case, payload):
    """The requester themself, or staff recording their answer with channel and note: intake
    staff, or (ADR 0008, no intake) the owner division's staff other than who resolved it."""
    if ctx.requester is not None:
        return {"by": "requester"}
    intake = identity.intake_division()
    if intake is not None and not _member(ctx, intake.pk):
        fail(ErrorCode.FORBIDDEN, "เจ้าหน้าที่กองรับเรื่องกลางบันทึกคำตอบแทนผู้แจ้งได้")
    if intake is None:
        if not _member(ctx, case.owner_division_id):
            fail(ErrorCode.FORBIDDEN, "เจ้าหน้าที่กองเจ้าภาพบันทึกคำตอบแทนผู้แจ้งได้")
        resolved = case.decisions.filter(action__in=RESULT_ACTIONS).order_by("-id").first()
        if identity.separated(ctx) and resolved and resolved.actor_id == ctx.user.pk:
            fail(
                ErrorCode.FORBIDDEN,
                "ผู้บันทึกผลการแก้ปัญหาบันทึกคำตอบแทนหน่วยงาน (เขต/ฝ่าย)ของเรื่องเดียวกันไม่ได้ "
                "ให้เจ้าหน้าที่อีกคนในกองบันทึก",
            )
    channel = payload.get("channel")
    if channel not in STAFF_CHANNELS:
        fail(ErrorCode.VALIDATION_ERROR, "channel must be phone, letter or counter.")
    return {
        "by": "intake" if intake else "owner",
        "channel": channel,
        "intake_note": clean_text(payload, "intake_note", True),
    }


def confirm(ctx, case, payload, reason, target):
    """ยืนยัน (ยอมรับผล until 4 Nov 2569): the requester accepts the resolution; the case closes."""
    data = _requester_or_intake(ctx, case, payload)
    resolution = latest(case)
    case.status = target
    return {**data, "resolution_id": resolution.pk if resolution else None}


def dispute(ctx, case, payload, reason, target):
    """การโต้แย้งผล: back to the same owner division; the second time flags supervisors (Q2)."""
    data = _requester_or_intake(ctx, case, payload)
    count = case.decisions.filter(action="dispute_resolution").count() + 1
    case.status = target
    if count >= 2:
        identity.audit(
            ctx.user,
            "case.repeat_dispute",
            case,
            reason,
            actor_label=ctx.label,
            disputes=count,
            division=case.owner_division.code,
        )
    return {**data, "dispute": count, "repeat": count >= 2}


def close(ctx, case, payload, reason, target):
    """Q3: no answer for 7 days after the resolution; the owner division closes with a reason."""
    if ctx.requester is not None or not _member(ctx, case.owner_division_id):
        fail(ErrorCode.FORBIDDEN, "สมาชิกกองเจ้าภาพเป็นผู้ปิดเรื่อง")
    asked = case.decisions.filter(action__in=RESULT_ACTIONS).order_by("-id").first()
    cutoff = timezone.now() - timedelta(days=settings.RECEIPT_NO_RESPONSE_DAYS)
    if asked is None or asked.created_at > cutoff:
        fail(
            ErrorCode.VALIDATION_ERROR,
            f"รอผู้แจ้งตอบผลครบ {settings.RECEIPT_NO_RESPONSE_DAYS} วันก่อนปิดโดยไม่มีคำตอบ (Q3)",
        )
    case.status = target
    return {"unanswered_since": asked.created_at.isoformat()}


def reopen(ctx, case, payload, reason, target):
    """Q4: within 30 days, the requester or the owner division; back to the same owner with no
    responsible person; later problems are a new problem case referring to this one."""
    if ctx.requester is None and not _member(ctx, case.owner_division_id):
        fail(ErrorCode.FORBIDDEN, "ผู้แจ้งเจ้าของเรื่องหรือกองเจ้าภาพเดิมเปิดเรื่องใหม่ได้")
    window = timedelta(days=workflow.REOPEN_WINDOW_DAYS)
    if case.ended_at is None or timezone.now() - case.ended_at > window:
        fail(
            ErrorCode.VALIDATION_ERROR,
            f"เกิน {workflow.REOPEN_WINDOW_DAYS} วันหลังปิด ให้ยื่นเรื่องแจ้งปัญหา/ข้อเสนอแนะใหม่อ้างเรื่องนี้ (Q4)",
        )
    count = case.decisions.filter(action="reopen").count() + 1
    case.status, case.assignee, case.ended_at = target, None, None
    return {"reopen": count}


HANDLERS = {
    "resolve": resolve,
    "admin_answer": admin_answer,
    "dispute_resolution": dispute,
}


def summary(case):
    """Resolution, disputes and reopenings for the card and LINE (the internal note stays here,
    staff only)."""
    resolution = latest(case)
    disputes = list(
        case.decisions.filter(action="dispute_resolution")
        .order_by("id")
        .values("reason", "created_at")
    )
    return {
        "resolution": {
            "category": resolution.category,
            "category_label": resolution.get_category_display(),
            "message": resolution.message,
            "internal_note": resolution.internal_note,
            "at": resolution.created_at.isoformat(),
        }
        if resolution
        else None,
        "disputes": [{"reason": d["reason"], "at": d["created_at"].isoformat()} for d in disputes],
        "repeat_dispute": len(disputes) >= 2,
        "reopens": case.decisions.filter(action="reopen").count(),
    }
