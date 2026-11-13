"""Problem cases in LINE (M09 Q1, Q2, Q4, Q14): the resolution card, accept or dispute, reopen.

Buttons are bound to the requester's LINE identity and the case, and expire after 7 days;
"ติดตามเรื่อง" shows fresh ones. A dispute or a reopening asks for a typed reason. Every answer
goes through services.perform_action, so ownership, status and the 30-day window are checked by
the backend, never by the chat.
"""

import secrets
from datetime import timedelta

from django.utils import timezone

from operations import policy, problems, services, warehouse, workflow
from operations.contracts import TEXT_MAX, Actor, CommandFailed
from operations.models import Case, PostbackToken

BUTTON_TTL = timedelta(days=7)
PROMPTS = {"dispute": "dispute_resolution", "reopen": "reopen"}


def _token(identity, action, case, now):
    token = PostbackToken.objects.create(
        token=secrets.token_urlsafe(16),
        identity=identity,
        action=action,
        value={"case": str(case.pk)},
        expires_at=now + BUTTON_TTL,
    )
    return f"t={token.token}"


def _when(moment):
    local = timezone.localtime(moment)
    return f"{local.day:02d}/{local.month:02d}/{local.year + 543} {local:%H:%M} น."


def _result(case):
    """(title, result category, message) the requester answers, or None. A case followed by its
    number has what staff typed in แจ้งผลให้ผู้แจ้ง and no category (ADR 0057)."""
    if case.kind in (Case.Kind.WITHDRAWAL, Case.Kind.RETURN):
        told = case.decisions.filter(action=warehouse.REPORT).order_by("-id").first()
        said = told and ((told.data or {}).get("notice") or warehouse.DONE_TEXT)
        return told and (f"ผลการดำเนินการ เรื่อง {case.number}", "", said)
    resolution = problems.latest(case)
    if resolution is None:
        return None
    heading = "ผลการพิจารณาข้อเสนอแนะ" if case.topic == Case.Topic.SUGGESTION else "ผลการแก้ปัญหา"
    return f"{heading} เรื่อง {case.number}", resolution.get_category_display(), resolution.message


def resolution_card(case, identity, now=None):
    """(plain text, Flex message) asking the requester to accept or dispute the resolution."""
    result = _result(case)
    if identity is None or result is None:
        return None
    title, category, said = result
    now = now or timezone.now()
    accept = _token(identity, "problem_accept", case, now)
    dispute = _token(identity, "problem_dispute", case, now)
    accept_label, dispute_label = labels(case)
    if case.topic == Case.Topic.SUGGESTION:
        hint = f"กด '{accept_label}' ถ้าไม่มีอะไรเพิ่ม หรือ '{dispute_label}' แล้วพิมพ์ต่อได้"
    else:
        hint = f"กด '{accept_label}' ถ้าเรียบร้อย หรือ '{dispute_label}' แล้วพิมพ์เหตุผล"
    lines = [line for line in (title, category, said, hint) if line]
    text = "\n".join(lines)

    def button(label, data, style):
        return {
            "type": "button",
            "style": style,
            "height": "sm",
            "action": {"type": "postback", "label": label, "data": data, "displayText": label},
        }

    message = {
        "type": "flex",
        "altText": text[:400],
        "contents": {
            "type": "bubble",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [
                    {"type": "text", "text": title, "weight": "bold", "wrap": True},
                    *(
                        {"type": "text", "text": t[:1500], "size": "sm", "wrap": True}
                        for t in lines[1:-1]
                    ),
                    {"type": "text", "text": hint, "size": "xs", "wrap": True, "color": "#555555"},
                ],
            },
            "footer": {
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "contents": [
                    button(accept_label, accept, "primary"),
                    button(dispute_label, dispute, "secondary"),
                ],
            },
        },
    }
    return text, message


def labels(case):
    """(accept, dispute) button labels: a suggestion is acknowledged, a problem accepted."""
    if case.topic == Case.Topic.SUGGESTION:
        return "รับทราบ", "มีข้อมูลเพิ่ม"
    if case.kind in (Case.Kind.WITHDRAWAL, Case.Kind.RETURN):  # ADR 0057
        return warehouse.CONFIRM_LABEL, warehouse.REJECT_LABEL
    return "ยืนยัน", "ยังไม่เรียบร้อย"  # ยอมรับผล until 4 Nov 2569


def _case(turn, case_id):
    return services.visible_cases(Actor.for_requester(turn.requester)).filter(pk=case_id).first()


def _act(turn, case, action, payload=None):
    try:
        return services.perform_action(
            Actor.for_requester(turn.requester),
            case.pk,
            action,
            expected_revision=Case.objects.get(pk=case.pk).revision,
            idempotency_key=f"line-problem-{turn.inbox.pk}",
            payload=payload,
        )
    except CommandFailed as exc:
        turn.set_prompt()
        turn.say(f"บันทึกไม่ได้: {exc.message or exc.code}")
        return None


def on_token(turn, token):
    case = _case(turn, (token.value or {}).get("case"))
    if case is None:
        turn.say("ไม่พบเรื่องนี้ในเรื่องของคุณ")
        return
    if token.action == "problem_accept":
        if case.status != Case.Status.AWAITING_CONFIRMATION:
            turn.say("เรื่องนี้ไม่ได้รอการยืนยันผลแล้ว")
            return
        if _act(turn, case, "confirm") is not None:
            turn.set_prompt()
            turn.say(f"บันทึกแล้ว เรื่อง {case.number} ปิดแล้ว")
        return
    if token.action == "problem_dispute":
        if case.status != Case.Status.AWAITING_CONFIRMATION:
            turn.say("เรื่องนี้ไม่ได้รอการยืนยันผลแล้ว")
            return
        turn.set_prompt("dispute", case=str(case.pk))
        if case.topic == Case.Topic.SUGGESTION:
            turn.say(f"เรื่อง {case.number}: พิมพ์ข้อมูลที่อยากเพิ่มได้เลย")
        else:
            turn.say(f"เรื่อง {case.number}: พิมพ์ว่ายังไม่เรียบร้อยอย่างไร")
        return
    if token.action == "problem_reopen":
        turn.set_prompt("reopen", case=str(case.pk))
        turn.say(f"เรื่อง {case.number}: พิมพ์เหตุผลที่ขอเปิดเรื่องใหม่")


def on_text(turn, text):
    """The typed reason of a dispute or a reopening."""
    prompt = turn.conversation.prompt
    case = _case(turn, (turn.conversation.prompt_data or {}).get("case"))
    if case is None:
        turn.set_prompt()
        turn.say("ไม่พบเรื่องนี้ในเรื่องของคุณ")
        return
    reason = text.strip()
    if not reason or len(reason) > TEXT_MAX:
        turn.say("พิมพ์เหตุผลสั้น ๆ อีกครั้ง")
        return
    result = _act(turn, case, PROMPTS[prompt], {"reason": reason})
    if result is None:
        return
    turn.set_prompt()
    if prompt == "dispute":
        turn.say(f"บันทึกแล้ว: เรื่อง {case.number} กลับไปให้เจ้าหน้าที่ดำเนินการต่อ")
    else:
        turn.say(f"เปิดเรื่อง {case.number} ใหม่แล้ว เจ้าหน้าที่จะดำเนินการต่อ")


def status_text(case):
    """Update due (Q14) and the latest resolution for "ติดตามเรื่อง"."""
    rows = []
    due = policy.update_due(case)
    if due is not None:
        rows.append(f"กำหนดอัปเดต: {_when(due)}")
    resolution = problems.latest(case)
    if resolution is not None and case.status in (
        Case.Status.AWAITING_CONFIRMATION,
        Case.Status.CLOSED,
    ):
        heading = "ผลการพิจารณา" if case.topic == Case.Topic.SUGGESTION else "ผลการแก้ปัญหา"
        rows.append(f"{heading}: {resolution.get_category_display()}\n{resolution.message}")
    return "\n".join(rows)


def buttons(turn, case):
    if case.status == Case.Status.AWAITING_CONFIRMATION:
        accept_label, dispute_label = labels(case)
        return [
            turn.button(accept_label, "problem_accept", {"case": str(case.pk)}),
            turn.button(dispute_label, "problem_dispute", {"case": str(case.pk)}),
        ]
    window = timedelta(days=workflow.REOPEN_WINDOW_DAYS)
    if (
        case.status == Case.Status.CLOSED
        and case.ended_at is not None
        and timezone.now() - case.ended_at <= window
    ):
        return [turn.button("เปิดเรื่องใหม่", "problem_reopen", {"case": str(case.pk)})]
    return []
