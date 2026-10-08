"""การยืนยันรับของ in LINE (M07 Q6, Q15): a card per delivery, then guided answers.

The card's buttons are bound to the requester's LINE identity, the case and the delivery, and
expire after 7 days; afterwards "เรื่องของฉัน" shows fresh ones. "ไม่ตรง" asks line by line with
buttons or a typed number/serials, never AI. Every answer goes through services.perform_action
("confirm"), so ownership, the delivery state and idempotency are checked by the backend.
"""

import re
import secrets
from datetime import timedelta

from django.utils import timezone

from operations import fulfilment, services
from operations.contracts import Actor, CommandFailed, ErrorCode
from operations.models import Case, Delivery, PostbackToken

BUTTON_TTL = timedelta(days=7)
THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
NUMBER = re.compile(r"^\d{1,10}(\.\d{1,2})?$")
OPEN = (Delivery.Status.AWAITING, Delivery.Status.MISMATCH)


def _token(identity, action, value, now):
    token = PostbackToken.objects.create(
        token=secrets.token_urlsafe(16),
        identity=identity,
        action=action,
        value=value,
        expires_at=now + BUTTON_TTL,
    )
    return f"t={token.token}"


def _delivery_lines(case, seq):
    delivery = Delivery.objects.filter(case=case, seq=seq).first()
    if delivery is None:
        return None, []
    rows = []
    for dl, quantity, serials in fulfilment._effective(delivery).values():
        item = dl.line.item
        text = f"{dl.line.line_no}. {item.name} {fulfilment._number(quantity)} {item.unit}"
        if serials:
            text += f"\nเลขเครื่อง {', '.join(serials)}"
        rows.append(text)
    return delivery, rows


def delivery_card(case, seq, identity, now=None):
    """(plain text, Flex message) asking the requester to confirm one delivery."""
    if identity is None or seq is None:
        return None
    now = now or timezone.now()
    delivery, rows = _delivery_lines(case, seq)
    if delivery is None:
        return None
    value = {"case": str(case.pk), "delivery": seq}
    full = _token(identity, "receipt_full", value, now)
    mismatch = _token(identity, "receipt_mismatch", value, now)
    title = f"ของพร้อมรับ เรื่อง {case.number}"
    meta = f"การส่งมอบครั้งที่ {seq} · เอกสาร {delivery.document_number}"
    hint = "กด 'ได้รับครบ' เมื่อได้ของครบ ถ้าจำนวนหรือเลขเครื่องไม่ตรงให้กด 'ไม่ตรง'"
    text = "\n".join([title, meta, *rows, hint])
    body = [
        {"type": "text", "text": title, "weight": "bold", "wrap": True},
        {"type": "text", "text": meta, "size": "sm", "wrap": True},
        *({"type": "text", "text": row, "size": "sm", "wrap": True} for row in rows),
        {"type": "text", "text": hint, "size": "xs", "wrap": True, "color": "#555555"},
    ]
    buttons = [
        {
            "type": "button",
            "style": "primary",
            "height": "sm",
            "action": {
                "type": "postback",
                "label": "ได้รับครบ",
                "data": full,
                "displayText": "ได้รับครบ",
            },
        },
        {
            "type": "button",
            "style": "secondary",
            "height": "sm",
            "action": {
                "type": "postback",
                "label": "ไม่ตรง",
                "data": mismatch,
                "displayText": "ไม่ตรง",
            },
        },
    ]
    message = {
        "type": "flex",
        "altText": text[:400],
        "contents": {
            "type": "bubble",
            "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": body},
            "footer": {
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "contents": buttons,
            },
        },
    }
    return text, message


# --- Chat turns ---------------------------------------------------------------------------------


def _case(turn, value):
    actor = Actor.for_requester(turn.requester)
    return services.visible_cases(actor).filter(pk=value.get("case")).first()


def _submit(turn, case, seq, result, lines=None):
    payload = {"delivery": seq, "result": result}
    if lines is not None:
        payload["lines"] = lines
    try:
        outcome = services.perform_action(
            Actor.for_requester(turn.requester),
            case.pk,
            "confirm",
            expected_revision=Case.objects.get(pk=case.pk).revision,
            idempotency_key=f"line-receipt-{turn.inbox.pk}",
            payload=payload,
        )
    except CommandFailed as exc:
        turn.set_prompt()
        if exc.code == ErrorCode.INVALID_TRANSITION:
            turn.say("ตอนนี้ยังยืนยันรับไม่ได้ (เรื่องพักหรือรอข้อมูลอยู่) ลองอีกครั้งภายหลัง")
        else:
            turn.say(f"บันทึกไม่ได้: {exc.message or exc.code}")
        return
    turn.set_prompt()
    delivery = Delivery.objects.get(case=case, seq=seq)
    if delivery.status == Delivery.Status.CONFIRMED:
        text = f"บันทึกแล้ว: ได้รับครบ (เรื่อง {case.number} ครั้งที่ {seq})"
    else:
        text = f"บันทึกแล้ว: ของไม่ตรง (เรื่อง {case.number} ครั้งที่ {seq}) เจ้าหน้าที่จะตรวจและแจ้งผล"
    if outcome.status == Case.Status.CLOSED:
        text += "\nเรื่องนี้ปิดแล้ว"
    turn.say(text)


def _ask(turn, case, delivery, index, answers):
    figures = list(fulfilment._effective(delivery).values())
    dl, quantity, serials = figures[index]
    item = dl.line.item
    state = {"case": str(case.pk), "delivery": delivery.seq, "index": index}
    turn.set_prompt("receipt", **state, answers=answers)
    head = f"ครั้งที่ {delivery.seq} รายการ {index + 1}/{len(figures)}: {item.name}"
    if item.is_serialized:
        turn.say(
            f"{head}\nจ่ายเลขเครื่อง {', '.join(serials)}\nพิมพ์เลขเครื่องที่ได้รับจริง คั่นด้วยเว้นวรรคหรือจุลภาค",
            [
                turn.button("ได้ครบทุกเครื่อง", "receipt_answer", {**state, "serials": serials}),
                turn.button("ไม่ได้รับเลย", "receipt_answer", {**state, "serials": []}),
            ],
        )
        return
    number = fulfilment._number(quantity)
    turn.say(
        f"{head}\nจ่าย {number} {item.unit} ได้รับจริงเท่าไร พิมพ์ตัวเลข",
        [
            turn.button(f"ได้ครบ {number}", "receipt_answer", {**state, "quantity": number}),
            turn.button("ไม่ได้รับเลย", "receipt_answer", {**state, "quantity": "0"}),
        ],
    )


def _answer(turn, case, delivery, index, answers, quantity=None, serials=None):
    figures = list(fulfilment._effective(delivery).values())
    dl = figures[index][0]
    answer = {"line_no": dl.line.line_no, "serials": serials or []}
    if not dl.line.item.is_serialized:
        answer["quantity"] = quantity
    answers = [*answers, answer]
    if index + 1 < len(figures):
        return _ask(turn, case, delivery, index + 1, answers)
    return _submit(turn, case, delivery.seq, "mismatch", answers)


def _open_delivery(turn, value):
    case = _case(turn, value)
    if case is None:
        turn.say("ไม่พบเรื่องนี้ในเรื่องของคุณ")
        return None, None
    delivery = Delivery.objects.filter(case=case, seq=value.get("delivery")).first()
    if delivery is None or delivery.status not in OPEN:
        turn.set_prompt()
        turn.say("การส่งมอบนี้ยืนยันหรือปิดแล้ว")
        return None, None
    return case, delivery


def on_token(turn, token):
    value = token.value or {}
    case, delivery = _open_delivery(turn, value)
    if case is None:
        return
    if token.action == "receipt_full":
        return _submit(turn, case, delivery.seq, "full")
    if token.action == "receipt_mismatch":
        return _ask(turn, case, delivery, 0, [])
    prompt = turn.conversation.prompt_data or {}
    if (
        turn.conversation.prompt != "receipt"
        or prompt.get("case") != value.get("case")
        or prompt.get("delivery") != value.get("delivery")
        or prompt.get("index") != value.get("index")
    ):
        turn.say("ปุ่มนี้เป็นของคำถามก่อนหน้า ใช้คำถามล่าสุดแทน")
        return
    return _answer(
        turn,
        case,
        delivery,
        prompt["index"],
        prompt.get("answers", []),
        quantity=value.get("quantity"),
        serials=value.get("serials"),
    )


def on_text(turn, text):
    """A typed answer to the current receipt question."""
    prompt = turn.conversation.prompt_data or {}
    case, delivery = _open_delivery(turn, prompt)
    if case is None:
        return
    index = prompt.get("index", 0)
    figures = list(fulfilment._effective(delivery).values())
    if index >= len(figures):
        turn.set_prompt()
        return
    item = figures[index][0].line.item
    cleaned = text.translate(THAI_DIGITS).strip()
    if item.is_serialized:
        serials = [s for s in re.split(r"[\s,]+", cleaned) if s]
        if len(set(serials)) != len(serials) or any(len(s) > 64 for s in serials):
            turn.say("เลขเครื่องซ้ำหรือยาวเกินไป พิมพ์ใหม่อีกครั้ง")
            return _ask(turn, case, delivery, index, prompt.get("answers", []))
        return _answer(turn, case, delivery, index, prompt.get("answers", []), serials=serials)
    if not NUMBER.match(cleaned):
        turn.say("พิมพ์เป็นตัวเลข เช่น 38")
        return _ask(turn, case, delivery, index, prompt.get("answers", []))
    return _answer(turn, case, delivery, index, prompt.get("answers", []), quantity=cleaned)


def status_text(case):
    """Per-line counts and deliveries for "ติดตามเรื่อง" (E.5 derived labels)."""
    data = fulfilment.summary(case)
    rows = []
    for line in data["lines"]:
        row = (
            f"{line['line_no']}. {line['name']}: ขอ {line['requested']}"
            f" อนุมัติ {line['approved']} จ่าย {line['issued']} รับ {line['received']}"
            f" ค้าง {line['outstanding']} {line['unit']}"
        )
        if line["approved"] not in ("-",) and line["not_approved"] not in ("0", "-"):
            row += f"\n   ไม่อนุมัติ {line['not_approved']}: {line['approval_reason'] or '-'}"
        if line["terminated"] != "0":
            row += f"\n   ยุติส่วนค้าง {line['terminated']}"
        rows.append(row)
    for delivery in data["deliveries"]:
        rows.append(
            f"ส่งมอบครั้งที่ {delivery['seq']} ({delivery['document_number']}): "
            f"{delivery['status_label']}"
        )
    if data["partially_issued"]:
        rows.insert(0, "จ่ายบางส่วน ยังมีส่วนค้างจ่าย")
    return "\n".join(rows)


def pending_buttons(turn, case):
    """Fresh confirm buttons for deliveries still waiting (after the card's buttons expired)."""
    buttons = []
    for delivery in Delivery.objects.filter(case=case, status__in=OPEN).order_by("seq")[:3]:
        value = {"case": str(case.pk), "delivery": delivery.seq}
        buttons.append(turn.button(f"ได้รับครบ ครั้งที่{delivery.seq}", "receipt_full", value))
        buttons.append(turn.button(f"ไม่ตรง ครั้งที่{delivery.seq}", "receipt_mismatch", value))
    return buttons
