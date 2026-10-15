"""LINE conversation turns (M03): registration, guided drafts, status replies and attachments.

`handle_inbox` turns one stored webhook event into draft changes plus reply messages inside a
transaction that locks the LINE identity, so turns of one user never interleave. Replies are sent
afterwards by `send_reply` (one reply token, used once, before its deadline). The local runner
calls both today; M04's n8n workflow will call the same functions through claim/lease APIs.

Until AI extraction (M05) exists the chat asks one field at a time with buttons; free text that
does not answer the current question is kept verbatim and the question is asked again.
"""

import re
import secrets
from dataclasses import dataclass, field
from datetime import date, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q, Subquery
from django.utils import timezone

from operations import identity as identity_services
from operations import line_api, services, workflow
from operations.contracts import (
    Actor,
    CommandFailed,
    ErrorCode,
    check_quantity_for_item,
    parse_quantity,
)
from operations.models import (
    Attachment,
    Case,
    ChannelIdentity,
    Conversation,
    ConversationDraft,
    ConversationMessage,
    Decision,
    District,
    EquipmentType,
    PostbackToken,
    RegistrationRequest,
    Requester,
    WebhookInbox,
)

LINE = ChannelIdentity.Channel.LINE
D = ConversationDraft.Status
K = Case.Kind

DRAFT_LIFETIME = timedelta(days=7)
CONFIRM_TTL = timedelta(minutes=30)
BUTTON_TTL = timedelta(days=7)
MAX_QUICK = 13
MAX_LINES = 50
CASE_NUMBER = re.compile(r"^[WRP]\d{2}-\d{6}$")
THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")

KIND_LABELS = {K.WITHDRAWAL: "เบิก", K.RETURN: "ส่งคืน", K.PROBLEM: "แจ้งปัญหา/ข้อเสนอแนะ"}
KIND_SHORT = {K.WITHDRAWAL: "เบิก", K.RETURN: "คืน", K.PROBLEM: "ปัญหา/ข้อเสนอแนะ"}
KIND_ORDER = [K.WITHDRAWAL, K.RETURN, K.PROBLEM]
# หมวดเรื่อง of แจ้งปัญหา/ข้อเสนอแนะ (real structure Q3; 10 Oct 2569 Q1): equipment topics lead to
# the items, การใช้ไฟฟ้า and ข้อเสนอแนะ straight to the story. A topic shows only while the
# policy names an owner for it, so the list follows the rules (ADR 0009).
TOPIC_LABELS = {
    "meter": "มิเตอร์",
    "transformer": "หม้อแปลง",
    "other": "อุปกรณ์อื่น",
    "usage": "การใช้ไฟฟ้า",
    "suggestion": "ข้อเสนอแนะ",
}
TOPIC_NOTES = {"usage": " (ลักใช้ไฟ ไฟแพง ร้องเรียน)"}
SUGGESTION_FIELDS = [
    ("description", "พิมพ์ข้อเสนอแนะได้เลย", True),
    ("impact", "ถ้ามีรายละเอียดเพิ่ม เช่น สถานที่หรืองานที่เกี่ยวข้อง พิมพ์ต่อได้ หรือกดข้าม", False),
    ("related_case_number", "เกี่ยวกับเรื่องเดิมไหม เลือกเรื่อง หรือกดข้าม", False),
]
USAGE_FIELDS = [
    ("description", "เล่าเรื่องที่พบ เช่น สถานที่ สิ่งที่เห็น ช่วงเวลา", True),
    ("impact", "ข้อมูลเพิ่มเติม เช่น เลขผู้ใช้ไฟ (CA) ถ้ามี หรือกดข้าม", False),
    ("related_case_number", "เกี่ยวกับเรื่องเดิมไหม เลือกเรื่อง หรือกดข้าม", False),
]
CATEGORY_LABELS = {
    EquipmentType.Category.METER: "มิเตอร์",
    EquipmentType.Category.TRANSFORMER: "หม้อแปลง",
    EquipmentType.Category.OTHER: "อุปกรณ์อื่น",
}
CATEGORY_ORDER = list(CATEGORY_LABELS)
# Per-kind fields after the item lines: (name, question, required).
FIELDS = {
    K.WITHDRAWAL: [
        ("purpose", "ขอเบิกเพื่อใช้ทำอะไร (วัตถุประสงค์)", True),
        ("needed_by", "ต้องการใช้ภายในวันที่เท่าไร (เช่น 31/12/2569) หรือกดข้าม", False),
    ],
    K.RETURN: [
        ("condition", "สภาพของที่ส่งคืน (เช่น ชำรุด ใช้งานได้ ไม่ครบชุด)", True),
        ("reason", "เหตุผลที่ส่งคืน", True),
        ("source_reference", "เลขเอกสารเบิกเดิม (ถ้ามี) หรือกดข้าม", False),
    ],
    K.PROBLEM: [
        ("description", "อธิบายปัญหาที่พบ หรือข้อเสนอแนะ", True),
        ("impact", "ผลกระทบต่อการทำงานหรือผู้ใช้ไฟ (ถ้ามี) หรือกดข้าม", False),
        ("related_case_number", "เกี่ยวกับเรื่องเดิมไหม เลือกเรื่อง หรือกดข้าม", False),
    ],
}
FIELD_LABELS = {
    "purpose": "วัตถุประสงค์",
    "needed_by": "ต้องการภายใน",
    "condition": "สภาพ",
    "reason": "เหตุผล",
    "source_reference": "เอกสารเบิกเดิม",
    "description": "ปัญหา/ข้อเสนอแนะ",
    "impact": "ผลกระทบ",
    "related_case_number": "เรื่องที่เกี่ยวข้อง",
}
# Story topics ask different questions, so their answers carry their own labels.
TOPIC_FIELD_LABELS = {
    "suggestion": {"description": "ข้อเสนอแนะ", "impact": "รายละเอียดเพิ่ม"},
    "usage": {"description": "เรื่องที่พบ", "impact": "ข้อมูลเพิ่มเติม"},
}
MENU = [("ส่งเรื่องใหม่", "m=new"), ("เรื่องของฉัน", "m=mine"), ("ติดตามเรื่อง", "m=track")]
MENU_WORDS = {label: data for label, data in MENU}
# What typed text reads as besides an answer to the bot's question (ADR 0012): a question to the
# bot, or a new request. Question words that also end plain descriptions ("ไม่รู้เป็นอะไร",
# "ทำไม") are left out on purpose; "ไหม้" (burnt) is not "ไหม".
_ASK = (
    r"(?:ไหม|มั้ย|มั๊ย|หรือเปล่า|รึเปล่า|หรือยัง|รึยัง|หรือไม่|ยังไง|อย่างไร|ไง|เมื่อไร|"
    r"เมื่อไหร่|ไหน|ได้บ้าง|กี่\S*)"
)
_TAIL = r"(?:\s*(?:แล้ว|ครับ|ค่ะ|คะ|คับ|ฮะ|จ้ะ|จ้า|นะ|หน่อย|ล่ะ))*\s*[?？]*\s*$"
# Words that ask wherever they stand ("ต้องรอนานแค่ไหนกว่าจะได้ของ"; found with the live model).
_ASK_ANYWHERE = (
    r"[?？]|(?:แค่ไหน|เมื่อไร|เมื่อไหร่|กี่โมง|ถึงไหน|ได้ไหม|ได้มั้ย|หรือยัง|หรือเปล่า|รึเปล่า)"
    r"(?![\u0e48-\u0e4b])"  # "ได้ไหม้" is burnt, not a question
)
QUESTION = re.compile(rf"{_ASK}{_TAIL}|^\s*ขอ\S*.*หน่อย{_TAIL}|{_ASK_ANYWHERE}")
REQUEST = re.compile(r"^\s*(?:(?:ขอ|อยาก|ต้องการ)\s*(?:เบิก|คืน|ส่งคืน|แจ้ง)|เบิก|ส่งคืน|แจ้งปัญหา|แจ้งเรื่อง)")
NAME_EXAMPLE = "เช่น สมชาย ใจดี"
SELF_INTRODUCTION = re.compile(r"^(?:ผม|ดิฉัน|หนู|ฉัน|กระผม)?\s*ชื่อ\s*")  # "ผมชื่อ สมชาย ใจดี"


# --- Message building --------------------------------------------------------------------------


def _fit(label, limit):
    """LINE rejects longer labels; dynamic names are cut with an ellipsis."""
    return label if len(label) <= limit else label[: limit - 1] + "…"


LOCATION = "@location"  # a quick reply that opens LINE's location picker


def _postback(label, data):
    if data == LOCATION:
        return {"type": "action", "action": {"type": "location", "label": _fit(label, 20)}}
    if data.startswith("https://") or data.startswith("http://"):
        return {"type": "action", "action": {"type": "uri", "label": _fit(label, 20), "uri": data}}
    return {
        "type": "action",
        "action": {
            "type": "postback",
            "label": _fit(label, 20),
            "data": data,
            "displayText": label,
        },
    }


def text_message(text, quick=None):
    message = {"type": "text", "text": text[:5000]}
    if quick:
        message["quickReply"] = {"items": [_postback(lbl, data) for lbl, data in quick][:MAX_QUICK]}
    return message


def button_list(title, buttons):
    """A Flex carousel for choices that do not fit quick reply (e.g. 18 districts)."""
    bubbles = []
    for start in range(0, len(buttons), 9):
        contents = [
            {
                "type": "button",
                "style": "secondary",
                "height": "sm",
                "action": {"type": "postback", "label": _fit(label, 40), "data": data},
            }
            for label, data in buttons[start : start + 9]
        ]
        bubbles.append(
            {
                "type": "bubble",
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "spacing": "sm",
                    "contents": contents,
                },
            }
        )
    return {"type": "flex", "altText": title, "contents": {"type": "carousel", "contents": bubbles}}


# --- Turn context -------------------------------------------------------------------------------


@dataclass
class Turn:
    inbox: WebhookInbox
    event: dict
    identity: ChannelIdentity
    conversation: Conversation
    now: object
    replies: list = field(default_factory=list)
    requester: Requester | None = None
    anonymous: bool = False  # ผู้แจ้งไม่ระบุตัวตน (ADR 0010): problems and suggestions only
    internal: bool = False  # ผู้แจ้งภายใน (Q8): staff reporting in their division's name

    def say(self, text, quick=None):
        self.replies.append(text_message(text, quick))

    def button(self, label, action, value=None, draft=None, ttl=BUTTON_TTL):
        token = PostbackToken.objects.create(
            token=secrets.token_urlsafe(16),
            identity=self.identity,
            draft=draft,
            draft_revision=draft.revision if draft else None,
            action=action,
            value=value or {},
            expires_at=self.now + ttl,
        )
        return (label, f"t={token.token}")

    def set_prompt(self, prompt="", **data):
        self.conversation.prompt = prompt
        self.conversation.prompt_data = data
        self.conversation.save(update_fields=["prompt", "prompt_data", "updated_at"])


def handle_inbox(inbox_id):
    """Process one stored event; returns reply messages (sent later by `send_reply`)."""
    with transaction.atomic():
        inbox = WebhookInbox.objects.select_for_update().get(pk=inbox_id)
        if inbox.status != WebhookInbox.Status.PENDING:
            return []
        replies, handled = _dispatch(inbox)
        inbox.status = WebhookInbox.Status.DONE if handled else WebhookInbox.Status.IGNORED
        inbox.processed_at = timezone.now()
        inbox.save(update_fields=["status", "processed_at"])
    return replies[:5]


def _dispatch(inbox):
    event = inbox.payload
    if inbox.source_type != "user":
        # Private intake only: group/room content is never processed.
        if inbox.event_type == "join":
            return [text_message("บัญชีนี้รับเรื่องเฉพาะแชตส่วนตัว (1:1) เท่านั้น")], False
        return [], False
    if not inbox.source_user_id:
        return [], False
    identity, _ = ChannelIdentity.objects.select_for_update().get_or_create(
        channel=LINE, external_id=inbox.source_user_id
    )
    conversation, _ = Conversation.objects.get_or_create(identity=identity)
    turn = Turn(inbox, event, identity, conversation, timezone.now())
    if inbox.event_type == "unfollow":
        ChannelIdentity.objects.filter(pk=identity.pk).update(is_following=False)
        return [], True
    if inbox.event_type == "follow":
        ChannelIdentity.objects.filter(pk=identity.pk).update(is_following=True)
        turn.say("ช่องทางนี้รับเรื่องเบิก ส่งคืน แจ้งปัญหา และข้อเสนอแนะถึง ฝอจ.")
    if _staff_turn(turn):
        return turn.replies, True
    if identity.status != ChannelIdentity.Status.ACTIVE:
        anonymous = _anonymous_requester(turn)
        if anonymous is None:
            _registration(turn)
            return turn.replies, True
        turn.requester, turn.anonymous = anonymous, True
        _requester_turn(turn)
        conversation.updates_seen_at = timezone.now()
        conversation.save(update_fields=["updates_seen_at", "updated_at"])
        return turn.replies, True
    requester = identity.requester
    if requester.status != Requester.Status.ACTIVE:
        turn.say("บัญชีผู้แจ้งของคุณถูกระงับ กรุณาติดต่อ ฝอจ.")
        return turn.replies, True
    turn.requester = requester
    _requester_turn(turn)
    # Everything up to the end of this turn (including the user's own actions) has been seen.
    conversation.updates_seen_at = timezone.now()
    conversation.save(update_fields=["updates_seen_at", "updated_at"])
    return turn.replies, True


# --- Registration (pending users get only this) -------------------------------------------------


def _event_text(turn):
    message = turn.event.get("message") or {}
    if turn.inbox.event_type == "message" and message.get("type") == "text":
        return str(message.get("text", ""))
    return None


def _event_location(turn):
    """(latitude, longitude) of a location message the user chose to send, else None."""
    message = turn.event.get("message") or {}
    if turn.inbox.event_type != "message" or message.get("type") != "location":
        return None
    try:
        lat, lng = float(message["latitude"]), float(message["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return round(lat, 6), round(lng, 6)


STAFF_WORDS = {"งานของกอง", "เจ้าหน้าที่"}
LINK_CODE = re.compile(r"^เชื่อม\s*([0-9๐-๙]{6})$")


def _staff_turn(turn):
    """เจ้าหน้าที่ใน LINE (ADR 0010): linking, the staff menu and staff buttons. Returns True when
    this event belonged to the staff side; a LINE that is also a requester keeps that side."""
    from operations import employees, options, staff_line

    text = _event_text(turn)
    command = " ".join(text.split()) if text is not None else ""
    prompt = turn.conversation.prompt
    match = LINK_CODE.match(command)
    if match:
        user, reason = staff_line.link_with_code(
            turn.identity, match.group(1).translate(THAI_DIGITS)
        )
        if user is None:
            turn.say(
                "ลองรหัสผิดครบจำนวนของวันนี้แล้ว"
                if reason == "locked"
                else "รหัสไม่ถูกต้องหรือหมดอายุ ขอรหัสใหม่จากหน้าเว็บ บัญชีของฉัน"
            )
        else:
            turn.say("เชื่อม LINE กับบัญชีเจ้าหน้าที่แล้ว พิมพ์ 'งานของกอง' เพื่อดูงาน")
        return True
    if command == "เชื่อมบัญชีเจ้าหน้าที่":
        if options.get(options.STAFF_LINK) == "code_or_employee" and employees.has_directory():
            turn.set_prompt("staff_emp")
            turn.say("พิมพ์รหัสพนักงานของคุณ หรือพิมพ์ เชื่อม ตามด้วยรหัส 6 หลักจากหน้าเว็บ")
        else:
            turn.say("เข้าเว็บ บัญชีของฉัน กด 'เชื่อม LINE' แล้วพิมพ์ เชื่อม ตามด้วยรหัส 6 หลักที่ได้")
        return True
    if prompt == "staff_emp" and command:
        turn.set_prompt("staff_check", employee_id=command[:32])
        turn.say(f"พิมพ์{employees.check_label()}เพื่อยืนยัน")
        return True
    if prompt == "staff_check" and command:
        employee_id = turn.conversation.prompt_data.get("employee_id", "")
        user, reason = staff_line.link_with_employee(turn.identity, employee_id, command)
        turn.set_prompt()
        if user is not None:
            turn.say("เชื่อม LINE กับบัญชีเจ้าหน้าที่แล้ว พิมพ์ 'งานของกอง' เพื่อดูงาน")
        elif reason == "no_account":
            turn.say("รหัสพนักงานนี้ยังไม่ได้ผูกกับบัญชีเจ้าหน้าที่ ให้แอดมินใส่รหัสพนักงานในบัญชีก่อน")
        else:
            turn.say("ข้อมูลไม่ตรงทะเบียนพนักงาน ใช้รหัส 6 หลักจากหน้าเว็บแทนได้")
        return True
    user = staff_line.staff_of(turn.identity)
    if user is None:
        return False
    if command == "เลิกเชื่อมบัญชีเจ้าหน้าที่":
        staff_line.unlink(turn.identity)
        turn.say("เลิกเชื่อม LINE กับบัญชีเจ้าหน้าที่แล้ว")
        return True
    token, _ = _token(turn)
    if token is not None and token.action == "staff_menu":
        staff_line.menu(turn, user)
        return True
    if token is not None and token.action == "staff_case":
        case = staff_line._case_for(user, case_id=token.value.get("case"))
        if case is None:
            turn.say("ไม่พบเรื่องนี้ในงานของคุณแล้ว")
        else:
            staff_line.case_detail(turn, user, case)
        return True
    if token is not None and token.action == "staff_act":
        case = staff_line._case_for(user, case_id=token.value.get("case"))
        if case is None:
            turn.say("ไม่พบเรื่องนี้ในงานของคุณแล้ว")
        else:
            staff_line.act(turn, user, case, token.value.get("action", ""))
        return True
    if token is not None and token.action == "staff_report":
        turn.requester = staff_line.internal_requester(turn.identity, user)
        turn.anonymous = False
        turn.internal = True
        draft = _new_draft(turn)
        _ask(turn, draft)
        return True
    if prompt == "staff_info" and command:
        case = staff_line._case_for(user, case_id=turn.conversation.prompt_data.get("case"))
        if case is not None:
            staff_line.act(turn, user, case, "request_info", command[:2000])
        else:
            turn.set_prompt()
            turn.say("ไม่พบเรื่องนี้ในงานของคุณแล้ว")
        return True
    if command in STAFF_WORDS:
        staff_line.menu(turn, user)
        return True
    if command and CASE_NUMBER.fullmatch(command.upper()) and turn.identity.requester_id is None:
        case = staff_line._case_for(user, number=command.upper())
        if case is not None:
            staff_line.case_detail(turn, user, case)
            return True
    if turn.identity.status != ChannelIdentity.Status.ACTIVE:
        # A staff-only LINE: everything else shows the staff menu.
        staff_line.menu(turn, user)
        return True
    return False


def _anonymous_requester(turn):
    """The anonymous reporter of this LINE account while ตัวเลือกระบบ allows it (ADR 0010),
    unless the user is registering or asks to start over (registration offers both again)."""
    from operations import options

    if options.get(options.ANONYMOUS) != "on":
        return None
    requester = Requester.objects.filter(anonymous_identity=turn.identity).first()
    if requester is None or turn.conversation.prompt.startswith("reg_"):
        return None
    token, _ = _token(turn)
    if token is not None and (token.action.startswith("reg_") or token.action == "anon_start"):
        return None
    if (turn.event.get("postback") or {}).get("data") == "m=new":
        return None
    return requester


def _token(turn):
    """Resolve a postback token of this identity; returns (token, problem message)."""
    data = (turn.event.get("postback") or {}).get("data", "")
    if turn.inbox.event_type != "postback" or not data.startswith("t="):
        return None, None
    token = PostbackToken.objects.select_related("draft").filter(token=data[2:]).first()
    if token is None or token.identity_id != turn.identity.pk:
        return None, "ปุ่มนี้ใช้ไม่ได้"
    if token.expires_at < turn.now:
        return None, "ปุ่มนี้หมดเวลาแล้ว"
    return token, None


def _registration(turn):
    identity = turn.identity
    token, problem = _token(turn)
    latest = identity_services.registration_status(LINE, identity.external_id)
    if latest is not None and latest.status == RegistrationRequest.Status.PENDING:
        if token is not None and token.action == "reg_cancel":
            identity_services.cancel_registration(LINE, identity.external_id)
            turn.set_prompt("reg_name")
            turn.say("ยกเลิกคำขอแล้ว พิมพ์ชื่อ-นามสกุลเพื่อลงทะเบียนใหม่")
            return
        turn.say(
            f"คำขอลงทะเบียนของคุณ ({latest.submitted_name}, "
            f"{latest.requested_district.name}) รอเจ้าหน้าที่รับรอง\n"
            "ระหว่างนี้ยังส่งหรือดูเรื่องไม่ได้",
            [turn.button("ยกเลิกคำขอ", "reg_cancel")],
        )
        return
    prompt = turn.conversation.prompt
    data = dict(turn.conversation.prompt_data or {})
    if token is not None and token.action == "anon_start":
        return _start_anonymous(turn)
    if prompt == "reg_district" and token is not None and token.action == "reg_district":
        data["district"] = token.value["code"]
        return _registration_next(turn, data)
    if prompt == "reg_district" and token is not None and token.action == "reg_rename":
        turn.set_prompt("reg_name")
        turn.say(f"พิมพ์ชื่อและนามสกุลใหม่ เว้นวรรคระหว่างกัน {NAME_EXAMPLE}")
        return
    text = _event_text(turn)
    if prompt == "reg_district" and text is not None and data.get("name"):
        return _ask_district(turn, data["name"])  # typed instead of choosing: the list again
    if prompt == "reg_employee":
        if token is not None and token.action == "reg_other":
            return _registration_next(turn, {**data, "checked": True})
        if text and text.strip():
            data["employee_id"] = text.strip()[:32]
            turn.set_prompt("reg_check", **data)
            from operations import employees

            turn.say(f"พิมพ์{employees.check_label()}เพื่อยืนยัน")
            return
        return _registration_next(turn, data)
    if prompt == "reg_check":
        if text and text.strip():
            return _registration_check(turn, data, text)
        from operations import employees

        turn.say(f"พิมพ์{employees.check_label()}เพื่อยืนยัน")
        return
    if prompt == "reg_location":
        location = _event_location(turn)
        if location is not None:
            return _registration_next(turn, {**data, "location": list(location)})
        if token is not None and token.action == "reg_skip_location":
            return _registration_next(turn, {**data, "location": None})
        return _registration_next(turn, data)
    if prompt == "reg_name" and text:
        text = " ".join(text.split())
        if not 2 <= len(text) <= 200:
            turn.say("กรุณาพิมพ์ชื่อ-นามสกุล (2-200 ตัวอักษร)")
            return
        name = SELF_INTRODUCTION.sub("", text) or text
        if _not_a_name(turn, name):
            return
        return _ask_district(turn, name)
    if problem:
        turn.say(problem)
    lead = "ยังไม่ได้ลงทะเบียนเป็นผู้แจ้ง"
    if latest is not None and latest.status == RegistrationRequest.Status.REJECTED:
        lead = "คำขอก่อนหน้าไม่ได้รับการรับรอง"
        if latest.decision_reason:
            lead += f" ({latest.decision_reason})"
    elif identity.status == ChannelIdentity.Status.REVOKED:
        lead = "บัญชี LINE นี้ต้องลงทะเบียนและรับรองใหม่"
    turn.set_prompt("reg_name")
    _ask_name(turn, f"{lead}\nพิมพ์ชื่อ-นามสกุลเพื่อลงทะเบียน")


def _ask_name(turn, text):
    from operations import options

    if options.get(options.ANONYMOUS) == "on":
        turn.say(
            f"{text}\nถ้าจะแจ้งปัญหาหรือข้อเสนอแนะโดยไม่ลงทะเบียน กด 'แจ้งแบบไม่ระบุตัวตน'",
            [turn.button("แจ้งแบบไม่ระบุตัวตน", "anon_start")],
        )
        return
    turn.say(text)


def _not_a_name(turn, text):
    """Typed text that is not a full name (ตัวเลือกระบบ registration_name "full"): a greeting,
    a question or a request is answered and the name asked again; one word or digits get the
    example. Returns True when the text was not taken."""
    from operations import answers, options  # local: answers builds on this module

    if options.get(options.REGISTRATION_NAME) != "full":
        return False
    ask = f"พิมพ์เฉพาะชื่อและนามสกุลเพื่อลงทะเบียน เว้นวรรคระหว่างกัน {NAME_EXAMPLE}"
    shape = reads_as(text)
    if answers.small_talk(text) or answers.greets(text) or shape == "question":
        found = answers.match(text) if shape == "question" and answers.mode() != "off" else None
        if found is not None and found.action in ("text", "hours") and not found.short_only:
            turn.say(answers.compose(found))
        _ask_name(turn, f"ยังไม่ได้ลงทะเบียนเป็นผู้แจ้ง\n{ask}")
        return True
    if shape == "request":
        _ask_name(turn, f"ต้องลงทะเบียนก่อนจึงเบิก คืน หรือแจ้งเรื่องได้\n{ask}")
        return True
    if re.search(r"[0-9๐-๙]", text) or len(text.split()) < 2:
        _ask_name(turn, f"พิมพ์ทั้งชื่อและนามสกุล เว้นวรรคระหว่างกัน ไม่มีตัวเลข {NAME_EXAMPLE}")
        return True
    return False


def _ask_district(turn, name):
    """The district list, with 'แก้ชื่อ' in case the name was typed wrong."""
    turn.set_prompt("reg_district", name=name)
    districts = District.objects.filter(is_active=True).order_by("code")
    turn.say(f"ชื่อ: {name}\nเลือกเขตการไฟฟ้าที่คุณสังกัด")
    message = button_list(
        "เลือกเขตการไฟฟ้า",
        [turn.button(d.name, "reg_district", {"code": d.code}) for d in districts],
    )
    message["quickReply"] = {"items": [_postback(*turn.button("แก้ชื่อ", "reg_rename"))]}
    turn.replies.append(message)


def _registration_next(turn, data):
    """The next registration step (ADR 0010): an employee check and a location when
    ตัวเลือกระบบ asks for them, then the request, approved at once when the check allows."""
    from operations import employees, options

    check = options.get(options.REGISTRATION_CHECK)
    if check != "approval" and not data.get("checked") and employees.has_directory():
        turn.set_prompt("reg_employee", **data)
        turn.say(
            "ยืนยันตัวตนด้วยรหัสพนักงาน\nพิมพ์รหัสพนักงานของคุณ หรือกด 'ใช้วิธีอื่น' ให้เจ้าหน้าที่ตรวจแทน",
            [turn.button("ใช้วิธีอื่น", "reg_other")],
        )
        return
    where = options.get(options.REGISTRATION_LOCATION)
    if where != "off" and "location" not in data:
        turn.set_prompt("reg_location", **data)
        quick = [("ส่งตำแหน่ง", LOCATION)]
        if where == "optional":
            quick.append(turn.button("ข้าม", "reg_skip_location"))
        turn.say("ส่งตำแหน่งที่คุณอยู่ตอนนี้ ใช้ประกอบการยืนยันตัวตนเท่านั้น", quick)
        return
    from operations.models import Employee

    employee = (
        Employee.objects.filter(pk=data.get("employee")).first() if data.get("employee") else None
    )
    registration = identity_services.submit_registration(
        LINE,
        turn.identity.external_id,
        data.get("name", ""),
        data.get("district", ""),
        employee=employee,
        location=tuple(data["location"]) if data.get("location") else None,
    )
    turn.set_prompt()
    if employee is not None and check == "employee_auto":
        identity_services.auto_approve_registration(registration)
        _menu(turn, "ยืนยันตัวตนแล้ว ใช้งานได้เลย กด 'ส่งเรื่องใหม่' เพื่อเริ่ม")
        return
    note = "ตรงทะเบียนพนักงานแล้ว " if employee is not None else ""
    turn.say(f"ส่งคำขอลงทะเบียนแล้ว {note}รอเจ้าหน้าที่ ฝอจ. รับรอง\nกด 'เรื่องของฉัน' เพื่อดูสถานะได้ทุกเมื่อ")


def _registration_check(turn, data, proof):
    from operations import employees

    district = District.objects.filter(code=data.get("district")).first()
    employee, reason = employees.verify(turn.identity, data.get("employee_id", ""), proof, district)
    data.pop("employee_id", None)
    if employee is not None:
        return _registration_next(turn, {**data, "checked": True, "employee": employee.pk})
    if reason == "locked":
        turn.say("ลองครบจำนวนครั้งของวันนี้แล้ว จะส่งให้เจ้าหน้าที่ตรวจแทน")
        return _registration_next(turn, {**data, "checked": True})
    left = employees.MAX_FAILED - employees.failed_today(turn.identity)
    if left <= 0:
        turn.say("ข้อมูลไม่ตรงทะเบียนพนักงาน จะส่งให้เจ้าหน้าที่ตรวจแทน")
        return _registration_next(turn, {**data, "checked": True})
    turn.set_prompt("reg_employee", **data)
    turn.say(
        f"ข้อมูลไม่ตรงทะเบียนพนักงาน ลองได้อีก {left} ครั้ง\nพิมพ์รหัสพนักงานใหม่ หรือกด 'ใช้วิธีอื่น'",
        [turn.button("ใช้วิธีอื่น", "reg_other")],
    )


def _start_anonymous(turn):
    """แจ้งแบบไม่ระบุตัวตน: a problem or suggestion draft that ends with a location."""
    from operations import options

    if options.get(options.ANONYMOUS) != "on":
        turn.say("ตอนนี้ต้องลงทะเบียนก่อนแจ้งเรื่อง")
        return
    turn.set_prompt()
    turn.requester = identity_services.anonymous_requester(turn.identity)
    turn.anonymous = True
    draft = _new_draft(turn)
    turn.say("แจ้งได้โดยไม่ต้องบอกชื่อ เจ้าหน้าที่จะไม่เห็นว่าใครแจ้ง แต่ต้องส่งตำแหน่งที่พบเรื่อง")
    _ask(turn, draft)


# --- Approved requester -----------------------------------------------------------------------


def _open_drafts(turn):
    return ConversationDraft.objects.filter(
        identity=turn.identity, requester=turn.requester, status=D.OPEN
    ).order_by("-updated_at")


def expire_drafts(now=None, identity=None):
    """Drafts idle for 7 days expire; returns how many."""
    now = now or timezone.now()
    stale = ConversationDraft.objects.filter(status=D.OPEN, updated_at__lt=now - DRAFT_LIFETIME)
    if identity is not None:
        stale = stale.filter(identity=identity)
    ids = list(stale.values_list("pk", flat=True))
    if ids:
        ConversationDraft.objects.filter(pk__in=ids).update(status=D.EXPIRED)
        Conversation.objects.filter(active_draft_id__in=ids).update(active_draft=None)
    return len(ids)


def _requester_turn(turn):
    expire_drafts(turn.now, turn.identity)
    expired = ConversationDraft.objects.filter(
        identity=turn.identity, status=D.EXPIRED, expiry_notified=False
    )
    if expired.update(expiry_notified=True):
        turn.conversation.refresh_from_db()
        turn.say("ร่างเรื่องที่ไม่ได้ทำต่อเกิน 7 วันหมดอายุแล้ว")
    _updates(turn)
    draft = turn.conversation.active_draft
    if draft is not None and draft.status != D.OPEN:
        draft = None
    text = _event_text(turn)
    message = None
    if text is not None:
        message = ConversationMessage.objects.create(
            identity=turn.identity,
            requester=turn.requester,
            inbox=turn.inbox,
            draft=draft,
            text=text,
        )
        from operations import answers  # local: answers builds on this module (ADR 0012)

        answers.emergency(turn, text)  # hazard words: the guidance at once, then carry on
    message_type = (turn.event.get("message") or {}).get("type")
    if turn.inbox.event_type == "message" and message_type in {"image", "file"}:
        return _attachment(turn, draft)
    location = _event_location(turn)
    if location is not None:
        if draft is not None and draft.step == "location":
            draft.data = dict(draft.data, location=list(location))
            return _set(turn, draft, "summary")
        turn.say("ได้รับตำแหน่งแล้ว แต่ตอนนี้ยังไม่ได้ขอ")
        return _reprompt(turn, draft)

    data = (turn.event.get("postback") or {}).get("data", "")
    if turn.inbox.event_type == "postback" and not data.startswith("t="):
        return _static_postback(turn, data, draft)
    token, problem = _token(turn)
    if problem:
        turn.say(problem)
        return _reprompt(turn, draft)
    if token is not None:
        return _token_action(turn, token, draft)

    command = " ".join(text.split()) if text is not None else None
    if command in MENU_WORDS:
        return _static_postback(turn, MENU_WORDS[command], draft)
    if text is not None and turn.conversation.prompt == "receipt":
        from operations import receipts  # local: receipts builds on this module (M07)

        return receipts.on_text(turn, text)
    if text is not None and turn.conversation.prompt in {"dispute", "reopen"}:
        from operations import problem_chat  # local: problem_chat builds on services (M09)

        return problem_chat.on_text(turn, text)
    if command and CASE_NUMBER.fullmatch(command.upper()) and not _expects_text(draft):
        return _case_detail(turn, command.upper())
    if text is not None and turn.conversation.prompt in ("", "info_reply"):
        # "ตอบกลับในแชตนี้ได้เลย": an answer to staff who asked for more, never a new draft.
        waiting = _awaiting_info(turn, draft)
        if waiting:
            return _info_reply(turn, waiting, message, text)
    return _free_text(turn, draft, text, message)


def _free_text(turn, draft, text, message=None):
    """Typed text that is no command: คลังคำตอบ, then the AI, then the draft's current step."""
    if text is not None and turn.conversation.prompt != "track":
        from operations import ai_chat, answers  # local: both build on this module

        found = answers.keyword_answer(turn, draft, text)  # คลังคำตอบ ก (ADR 0012)
        if found is not None:
            return answers.respond(turn, draft, found)
        if ai_chat.wants_ai(turn, draft, text):
            return ai_chat.queue(turn, draft, text, message)
    if draft is not None and text is not None:
        return _draft_text(turn, draft, text)
    if turn.conversation.prompt == "track" and text:
        turn.say("ไม่พบเลขเรื่องในรูปแบบที่ถูกต้อง เช่น W69-000001")
        return
    if text and draft is None and not turn.conversation.prompt and not _open_drafts(turn).exists():
        from operations import questions  # local: questions reads answers, which read this module

        questions.record(turn, text)  # คำถามที่บอทตอบไม่ได้ (Q11)
    _reprompt(turn, draft)


def _updates(turn):
    """Changes not pushed (C.2.3 'มีอัปเดตใหม่'): shown once, at the requester's next turn.

    Only status changes count: handoffs, priorities and facts are internal (M06 Q10).
    """
    since = turn.conversation.updates_seen_at
    if since is None:
        return
    status_changed = Decision.objects.filter(case=OuterRef("pk"), created_at__gt=since).exclude(
        from_status=F("to_status")
    )
    changed = list(
        services.visible_cases(_actor(turn))
        .filter(Exists(status_changed))
        .order_by("-updated_at")[:3]
    )
    lines = []
    for case in changed:
        line = f"{case.number} · {case.get_status_display()}"
        if case.status == Case.Status.AWAITING_INFO:
            asked = case.decisions.filter(action="request_info").order_by("-id").first()
            if asked is not None:
                line += f"\nเจ้าหน้าที่ขอข้อมูล: {asked.reason}"
        lines.append(line)
    if lines:
        turn.say("มีอัปเดตใหม่\n" + "\n".join(lines))


def _awaiting_info(turn, draft):
    """This requester's cases waiting for their information, newest request first. Once they
    have worked on a draft since the request, typing belongs to the draft instead."""
    asked = (
        Decision.objects.filter(case=OuterRef("pk"), action="request_info")
        .order_by("-created_at")
        .values("created_at")[:1]
    )
    cases = (
        services.visible_cases(_actor(turn))
        .filter(status=Case.Status.AWAITING_INFO)
        .annotate(asked_at=Subquery(asked))
        .order_by("-asked_at")
    )
    if draft is not None:
        cases = cases.filter(asked_at__gt=draft.updated_at)
    return list(cases[: MAX_QUICK - 1])


def _info_reply(turn, cases, message, text):
    """Typed text while staff wait for this requester's information (ตัวเลือกระบบ info_reply).

    A greeting or thanks is not information: the bot says what staff wait for. Text that reads
    as information goes to the one waiting case; a question, a new request, very short text, or
    every text when the option says so, is offered first, and 'ไม่ใช่ข้อมูลเพิ่ม' handles it like
    any other message."""
    from operations import answers, options  # local: answers builds on this module

    if answers.small_talk(text):
        return _info_waiting(turn, cases)
    plain = (
        options.get(options.INFO_REPLY) == "check"
        and not reads_as(text)
        and len(answers.rest(text)) >= 4
    )
    if len(cases) == 1 and plain:
        return _attach_reply(turn, cases[0], message)
    turn.set_prompt("info_reply", message=message.pk)
    other = turn.button("ไม่ใช่ข้อมูลเพิ่ม", "info_none", {"message": message.pk})
    if len(cases) == 1:
        case = cases[0]
        asked = "" if _shown(turn, case) else f"\nเจ้าหน้าที่ขอ: {_info_asked(case)}"
        turn.say(
            f"ข้อความนี้เป็นข้อมูลที่เจ้าหน้าที่ขอสำหรับเรื่อง {case.number} ไหม{asked}",
            [
                turn.button(
                    "ส่งถึงเจ้าหน้าที่", "info_reply", {"case": str(case.pk), "message": message.pk}
                ),
                other,
            ],
        )
        return
    buttons = [
        turn.button(c.number, "info_reply", {"case": str(c.pk), "message": message.pk})
        for c in cases
    ]
    turn.say(
        f"มี {len(cases)} เรื่องที่เจ้าหน้าที่ขอข้อมูลเพิ่ม ข้อความนี้เป็นข้อมูลของเรื่องไหน",
        [*buttons, other],
    )


def _info_asked(case):
    asked = case.decisions.filter(action="request_info").order_by("-id").first()
    return asked.reason if asked is not None else ""


def _shown(turn, case):
    """'มีอัปเดตใหม่' of this turn already says what staff asked on this case."""
    return any(case.number in m.get("text", "") for m in turn.replies)


def _info_waiting(turn, cases):
    lines = [f"{c.number}: {_info_asked(c)}" for c in cases[:3] if not _shown(turn, c)]
    head = "เจ้าหน้าที่รอข้อมูลเพิ่ม\n" + "\n".join(lines) + "\n" if lines else ""
    turn.say(f"{head}พิมพ์ตอบในแชตนี้ได้เลย", MENU)


def _attach_reply(turn, case, message):
    ConversationMessage.objects.filter(pk=message.pk, case__isnull=True).update(case=case)
    identity_services.audit(
        None,
        "case.info_replied",
        case,
        actor_label=f"requester:{turn.requester.pk}",
        number=case.number,
        message=message.pk,
    )
    turn.set_prompt()
    turn.say(
        f"ส่งข้อมูลถึงเจ้าหน้าที่แล้ว (เรื่อง {case.number})\n"
        "เจ้าหน้าที่จะตรวจแล้วดำเนินการต่อ มีรูปหรือไฟล์เพิ่มส่งต่อได้เลย",
        MENU,
    )


def _info_token(turn, token, draft):
    """The requester chose which waiting case a typed answer (or file) belongs to, or said it
    is not information: then the message is handled like any other (คลังคำตอบ, AI, draft)."""
    value = token.value or {}
    if token.action == "info_none":
        turn.set_prompt()
        message = ConversationMessage.objects.filter(
            pk=value.get("message"), identity=turn.identity, case__isnull=True
        ).first()
        if message is None:
            return _menu(turn, "ข้อความนี้ไม่ได้ส่งถึงเจ้าหน้าที่ เลือกเมนูด้านล่างเพื่อทำต่อ")
        turn.say("ข้อความนี้ไม่ได้ส่งถึงเจ้าหน้าที่")
        return _free_text(turn, draft, message.text, message)
    case = (
        services.visible_cases(_actor(turn))
        .filter(pk=value.get("case"), status=Case.Status.AWAITING_INFO)
        .first()
    )
    message = ConversationMessage.objects.filter(
        pk=value.get("message"), identity=turn.identity, case__isnull=True
    ).first()
    if case is None or message is None:
        turn.set_prompt()
        turn.say("เรื่องนี้ไม่ได้รอข้อมูลจากคุณแล้ว หรือส่งข้อความนี้ไปแล้ว", MENU)
        return
    return _attach_reply(turn, case, message)


def _expects_text(draft):
    """Steps whose answer is typed; elsewhere a typed case number means 'show this case'."""
    if draft is None:
        return False
    return draft.step in {"item_search", "quantity", "serials"} or draft.step.startswith("field:")


def reads_as(text):
    """'question', 'request' or '' (an answer) for typed text; see QUESTION and REQUEST."""
    text = " ".join(str(text or "").split())
    if QUESTION.search(text):
        return "question"
    if REQUEST.search(text):
        return "request"
    return ""


def off_step(draft, text):
    """'question' or 'request' for text typed at a step that takes typed answers, when
    ตัวเลือกระบบ chat_typed_steps asks to check (ADR 0012); '' when the step takes it.

    The caller lets คลังคำตอบ (questions only) and the AI look first; when they find nothing, the
    step takes the text as before. A count or serial numbers are never a new request."""
    from operations import options

    if not _expects_text(draft) or options.get(options.CHAT_TYPED) != "check":
        return ""
    found = reads_as(text)
    if found == "request" and draft.step in ("quantity", "serials"):
        return ""
    return found


def _static_postback(turn, data, draft):
    if data == "m=new":
        return _start_new(turn)
    if data == "m=mine":
        return _my_cases(turn)
    if data == "m=track":
        return _track_prompt(turn)
    if data.startswith("c="):
        return _case_detail(turn, data[2:])
    return _reprompt(turn, draft)


def _menu(turn, text):
    from operations import staff_line

    extra = [("งานของกอง", "งานของกอง")] if staff_line.staff_of(turn.identity) else []
    turn.say(text, [*MENU, *[turn.button(label, "staff_menu") for label, _ in extra]])


def _reprompt(turn, draft):
    """Ask the current question again (free text is kept, never guessed into a field)."""
    if draft is not None:
        return _ask(turn, draft)
    drafts = list(_open_drafts(turn)[:10])
    if drafts:
        return _choose_draft(turn, drafts)
    _menu(turn, "เลือกเมนูด้านล่าง: ส่งเรื่องใหม่ ดูเรื่องของฉัน หรือติดตามเรื่อง")


# --- Status replies ---------------------------------------------------------------------------


def _actor(turn):
    return Actor.for_requester(turn.requester)


def _when(moment):
    local = timezone.localtime(moment)
    return f"{local.day:02d}/{local.month:02d}/{local.year + 543} {local:%H:%M} น."


def _status_label(case):
    label = case.get_status_display()
    if case.kind == K.WITHDRAWAL:
        from operations import fulfilment

        if fulfilment.summary(case)["partially_issued"]:
            label += " (จ่ายบางส่วน)"
    elif case.kind == K.RETURN:
        from operations import returns

        if returns.summary(case)["partially_returned"]:
            label += " (รับคืนบางส่วน)"
    return label


def _my_cases(turn):
    cases = list(
        services.visible_cases(_actor(turn))
        .exclude(status__in=workflow.TERMINAL)
        .order_by("-submitted_at")[:5]
    )
    drafts = _open_drafts(turn).count()
    lines = [
        f"{c.number} · {_kind_label(c.kind, c.topic)} · {_status_label(c)}" for c in cases
    ] or ["ยังไม่มีเรื่องที่เปิดอยู่"]
    if drafts:
        lines.append(f"ร่างเรื่องที่ยังไม่ยื่น {drafts} ร่าง (กดส่งเรื่องใหม่เพื่อทำต่อ)")
    turn.set_prompt()
    turn.say("เรื่องของฉัน\n" + "\n".join(lines), [(c.number, f"c={c.number}") for c in cases])


def _track_prompt(turn):
    recent = list(services.visible_cases(_actor(turn)).order_by("-submitted_at")[:10])
    turn.set_prompt("track")
    turn.say("พิมพ์เลขเรื่องที่ต้องการติดตาม เช่น W69-000001", [(c.number, f"c={c.number}") for c in recent])


def _case_detail(turn, number):
    case = services.visible_cases(_actor(turn)).filter(number=number).first()
    turn.set_prompt()
    if case is None:
        # Same answer for other people's cases and numbers that do not exist.
        turn.say(f"ไม่พบเรื่อง {number} ในเรื่องของคุณ", MENU)
        return
    files = case.attachments.count()
    text = (
        f"เรื่อง {case.number}\nประเภท: {_kind_label(case.kind, case.topic)}\n"
        f"สถานะ: {_status_label(case)}\nกองเจ้าภาพ: {case.owner_division.name}\n"
        f"อัปเดตล่าสุด: {_when(case.updated_at)}" + (f"\nไฟล์แนบ: {files} ไฟล์" if files else "")
    )
    buttons = []
    if case.kind == K.WITHDRAWAL:
        from operations import receipts  # local: receipts builds on this module (M07)

        detail = receipts.status_text(case)
        if detail:
            text += "\n" + detail
        buttons = receipts.pending_buttons(turn, case)
    elif case.kind == K.RETURN:
        from operations import returns  # local: returns builds on services (M08)

        text += "\n" + returns.status_text(case)
    else:
        from operations import problem_chat  # local: problem_chat builds on services (M09)

        detail = problem_chat.status_text(case)
        if detail:
            text += "\n" + detail
        buttons = problem_chat.buttons(turn, case)
    turn.say(text, [*buttons, *MENU][:MAX_QUICK])


# --- Drafts -------------------------------------------------------------------------------------


def _touch(draft, now, **changes):
    """Every draft change bumps the revision, which invalidates older buttons."""
    for name, value in changes.items():
        setattr(draft, name, value)
    draft.revision += 1
    draft.updated_at = now
    draft.save()


def _activate(turn, draft):
    turn.conversation.active_draft = draft
    turn.conversation.save(update_fields=["active_draft", "updated_at"])
    turn.set_prompt()
    # Files sent before a draft was chosen join the draft the user picked.
    Attachment.objects.filter(identity=turn.identity, draft__isnull=True, case__isnull=True).update(
        draft=draft
    )


def _new_draft(turn):
    anonymous = turn.anonymous
    # Anonymous and internal reporters send problems and suggestions only.
    only_problems = anonymous or turn.internal or getattr(turn.requester, "is_internal", False)
    draft = ConversationDraft.objects.create(
        identity=turn.identity,
        requester=turn.requester,
        kind=K.PROBLEM if only_problems else "",
        step="topic" if only_problems else "kind",
        data={"lines": [], "fields": {}, **({"anonymous": True} if anonymous else {})},
        updated_at=turn.now,
    )
    _activate(turn, draft)
    return draft


def _start_new(turn):
    drafts = list(_open_drafts(turn)[:10])
    if not drafts:
        return _ask(turn, _new_draft(turn))
    buttons = [turn.button(_draft_title(d), "select_draft", {"id": str(d.pk)}) for d in drafts[:3]]
    if len(drafts) == 1:
        buttons.append(turn.button("ทิ้งเดิม เริ่มใหม่", "discard_and_new", {"id": str(drafts[0].pk)}))
    buttons.append(turn.button("เริ่มใหม่ เก็บเดิม", "new_draft"))
    turn.say(
        f"คุณมีร่างเรื่องที่ยังไม่ยื่น {len(drafts)} ร่าง\n"
        "กดร่างที่ต้องการทำต่อ ทิ้งร่างเดิมแล้วเริ่มใหม่ หรือเริ่มใหม่โดยเก็บร่างเดิมไว้",
        buttons,
    )


def _draft_title(draft):
    kind = KIND_SHORT.get(draft.kind, "ร่าง")
    return f"{kind} {timezone.localtime(draft.updated_at):%d/%m %H:%M}"


def _choose_draft(turn, drafts):
    buttons = [turn.button(_draft_title(d), "select_draft", {"id": str(d.pk)}) for d in drafts]
    buttons.append(turn.button("เริ่มเรื่องใหม่", "new_draft"))
    turn.say("เลือกร่างเรื่องที่จะทำต่อก่อน", buttons)


def _owned_draft(turn, draft_id):
    return _open_drafts(turn).filter(pk=draft_id).first()


def _token_action(turn, token, draft):
    action = token.action
    if action in {"info_reply", "info_none"}:
        return _info_token(turn, token, draft)
    if action.startswith("receipt_"):
        from operations import receipts  # local: receipts builds on this module (M07)

        return receipts.on_token(turn, token)
    if action.startswith("problem_"):
        from operations import problem_chat  # local: problem_chat builds on services (M09)

        return problem_chat.on_token(turn, token)
    if action in {"new_draft", "select_draft", "discard_and_new"}:
        if action == "new_draft":
            return _ask(turn, _new_draft(turn))
        chosen = _owned_draft(turn, token.value.get("id"))
        if chosen is None:
            turn.say("ร่างนี้ไม่อยู่แล้ว")
            return _reprompt(turn, None)
        if action == "discard_and_new":
            _touch(chosen, turn.now, status=D.DISCARDED)
            return _ask(turn, _new_draft(turn))
        _activate(turn, chosen)
        return _ask(turn, chosen)
    bound = token.draft
    if bound is None:
        return _reprompt(turn, draft)
    if bound.identity_id != turn.identity.pk:
        turn.say("ปุ่มนี้ใช้ไม่ได้")
        return _reprompt(turn, draft)
    if bound.status == D.SUBMITTED and action == "confirm":
        # A repeated confirmation returns the original case, never a second one.
        turn.say(f"ยื่นเรื่องนี้แล้ว เลขเรื่อง {bound.case.number}", MENU)
        return
    if bound.status != D.OPEN or bound.revision != token.draft_revision:
        turn.say("ปุ่มนี้เป็นของขั้นตอนก่อนหน้า ใช้คำถามล่าสุดแทน")
        return _reprompt(turn, draft)
    if draft is None or draft.pk != bound.pk:
        _activate(turn, bound)
    return _draft_button(turn, bound, action, token.value)


def _items(category):
    return EquipmentType.objects.filter(category=category, is_active=True).order_by("name")


def _ask(turn, draft):
    """Send the question for the draft's current step with fresh, revision-bound buttons."""
    step = draft.step
    data = draft.data
    b = turn.button
    if step == "kind":
        turn.say(
            "ต้องการแจ้งเรื่องประเภทใด\n1. เบิก\n2. ส่งคืน\n"
            "3. แจ้งปัญหา/ข้อเสนอแนะ (ของชำรุด ใช้งานไม่ได้ ไฟแพง ลักใช้ไฟ หรือข้อเสนอแนะ)",
            [b(KIND_LABELS[k], "kind", {"kind": k}, draft) for k in KIND_ORDER]
            + [b("ยกเลิกร่าง", "discard", draft=draft)],
        )
    elif step == "topic":
        topics = _topics()
        lines = [f"{n}. {TOPIC_LABELS[t]}{TOPIC_NOTES.get(t, '')}" for n, t in enumerate(topics, 1)]
        turn.say(
            "เรื่องนี้เกี่ยวกับอะไร\n" + "\n".join(lines),
            [b(TOPIC_LABELS[t], "topic", {"topic": t}, draft) for t in topics]
            + [b("ไม่แน่ใจ", "lines_done", draft=draft)]  # the text and the rules decide
            + [b("ยกเลิกร่าง", "discard", draft=draft)],
        )
    elif step == "category":
        buttons = [
            b(CATEGORY_LABELS[c], "category", {"category": c}, draft) for c in CATEGORY_ORDER
        ]
        if draft.kind == K.PROBLEM or data["lines"]:
            buttons.append(
                b("ข้าม (ไม่ระบุของ)" if not data["lines"] else "ไปต่อ", "lines_done", draft=draft)
            )
        turn.say("เลือกหมวดของ\n1. มิเตอร์\n2. หม้อแปลง\n3. อุปกรณ์อื่น", buttons)
    elif step == "item":
        items = list(_items(data["line"]["category"]))
        skip = _skip_model(turn, draft)
        if len(items) > MAX_QUICK - 1 - len(skip):
            _touch(draft, turn.now, step="item_search")
            return _ask(turn, draft)
        turn.say(
            "เลือกรายการของ",
            [b(i.name, "item", {"code": i.code}, draft) for i in items]
            + skip
            + [b("ย้อนกลับ", "back_category", draft=draft)],
        )
    elif step == "item_search":
        turn.say(
            "พิมพ์ชื่อหรือรหัสของบางส่วนเพื่อค้นหา",
            _skip_model(turn, draft) + [b("ย้อนกลับ", "back_category", draft=draft)],
        )
    elif step == "quantity":
        item = EquipmentType.objects.get(code=data["line"]["item_code"])
        turn.say(f"{item.name}\nจำนวนกี่{item.unit}", [b("ย้อนกลับ", "back_category", draft=draft)])
    elif step == "serials":
        line = data["line"]
        if draft.kind == K.RETURN:
            turn.say(f"พิมพ์หมายเลขเครื่องทั้ง {line['quantity']} เครื่อง คั่นด้วยเว้นวรรคหรือขึ้นบรรทัดใหม่")
        else:
            turn.say("พิมพ์หมายเลขเครื่องที่มีปัญหา หรือกดข้าม", [b("ข้าม", "skip_serials", draft=draft)])
    elif step == "more":
        turn.say(
            "เพิ่มรายการของอีกไหม",
            [b("เพิ่มรายการ", "add_line", draft=draft), b("ไปต่อ", "lines_done", draft=draft)],
        )
    elif step.startswith("field:"):
        name = step.split(":", 1)[1]
        question, required = next((q, r) for n, q, r in _fields(draft) if n == name)
        buttons = []
        if name == "related_case_number":
            recent = services.visible_cases(_actor(turn)).order_by("-submitted_at")[:5]
            buttons = [b(c.number, "related", {"number": c.number}, draft) for c in recent]
        if not required:
            buttons.append(b("ข้าม", "skip_field", draft=draft))
        turn.say(question, buttons or None)
    elif step == "location":
        turn.say("กด 'ส่งตำแหน่ง' แล้วเลือกจุดที่พบเรื่องนี้", [("ส่งตำแหน่ง", LOCATION)])
    elif step == "summary":
        turn.say(
            _summary(draft),
            [
                b("ยืนยันส่งเรื่อง", "confirm", draft=draft, ttl=CONFIRM_TTL),
                b("กรอกใหม่", "restart", draft=draft),
                b("ยกเลิกร่าง", "discard", draft=draft),
            ],
        )


def _skip_model(turn, draft):
    """A problem may name no model, also when the catalogue is long enough to need a search
    (found by the dialogue simulator: search mode had dropped this button)."""
    if draft.kind == K.PROBLEM and not draft.data["lines"]:
        return [turn.button("ไม่ระบุรุ่น", "lines_done", draft=draft)]
    return []


def _summary(draft):
    data = draft.data
    lines = [f"สรุปเรื่อง: {_kind_label(draft.kind, data.get('topic'))}"]
    items = {
        i.code: i
        for i in EquipmentType.objects.filter(code__in=[x["item_code"] for x in data["lines"]])
    }
    for number, line in enumerate(data["lines"], start=1):
        item = items.get(line["item_code"])
        text = f"{number}. {item.name if item else line['item_code']}"
        if line.get("quantity"):
            text += f" {line['quantity']} {item.unit if item else ''}".rstrip()
        if line.get("serials"):
            text += f" (หมายเลข {', '.join(line['serials'])})"
        lines.append(text)
    # In the order the questions were asked: the stored JSON does not keep key order.
    labels = {**FIELD_LABELS, **TOPIC_FIELD_LABELS.get(data.get("topic"), {})}
    asked = [name for name, _, _ in _fields(draft)]
    for name in sorted(data["fields"], key=lambda n: asked.index(n) if n in asked else len(asked)):
        if data["fields"][name]:
            lines.append(f"{labels[name]}: {data['fields'][name]}")
    if data.get("location"):
        lines.append("ตำแหน่ง: ส่งแล้ว")
    if data.get("anonymous"):
        lines.append("แจ้งแบบไม่ระบุตัวตน")
    files = list(draft.attachments.all())
    if files:
        waiting = sum(f.scan_status in {"pending_fetch", "pending_scan"} for f in files)
        bad = sum(f.scan_status in {"rejected", "infected"} for f in files)
        status = f"ไฟล์แนบ {len(files)} ไฟล์"
        if waiting:
            status += f" (กำลังตรวจ {waiting})"
        if bad:
            status += f" (ไม่ผ่าน {bad} ไฟล์ ส่งใหม่ได้)"
        lines.append(status)
    lines.append("กดยืนยันภายใน 30 นาที")
    return "\n".join(lines)


def _offered(topic):
    """Whether the policy names an owner for a problem of this topic."""
    from operations import policy  # local: policy builds on workflow, not on this module

    current = policy.current_policy()
    rules = current.doc.get("responsibility", []) if current else []
    return any(r.get("category") == topic and K.PROBLEM in r.get("kinds", []) for r in rules)


def _topics():
    """Equipment topics always (unknown ones go to people), story topics while owned."""
    return [t for t in TOPIC_LABELS if t in CATEGORY_LABELS or _offered(t)]


def _kind_label(kind, topic=""):
    if topic in (Case.Topic.USAGE, Case.Topic.SUGGESTION):
        return TOPIC_LABELS[topic]
    return KIND_LABELS[kind]


def _fields(draft):
    topic = draft.data.get("topic")
    if topic == Case.Topic.USAGE:
        return USAGE_FIELDS
    if topic == Case.Topic.SUGGESTION:
        return SUGGESTION_FIELDS
    return FIELDS[draft.kind]


def _next_field(draft):
    filled = draft.data["fields"]
    for name, _, _ in _fields(draft):
        if name not in filled:
            return f"field:{name}"
    if draft.data.get("anonymous") and not draft.data.get("location"):
        return "location"
    return "summary"


def _set(turn, draft, step, **data_changes):
    data = dict(draft.data)
    data.update(data_changes)
    _touch(draft, turn.now, step=step, data=data)
    return _ask(turn, draft)


def _draft_button(turn, draft, action, value):
    data = draft.data
    if action == "discard":
        _touch(draft, turn.now, status=D.DISCARDED)
        turn.conversation.active_draft = None
        turn.conversation.save(update_fields=["active_draft", "updated_at"])
        return _menu(turn, "ยกเลิกร่างแล้ว")
    if action == "restart":
        if data.get("anonymous") or getattr(draft.requester, "is_internal", False):
            fresh = {
                "lines": [],
                "fields": {},
                **({"anonymous": True} if data.get("anonymous") else {}),
            }
            _touch(draft, turn.now, kind=K.PROBLEM, step="topic", data=fresh)
        else:
            _touch(draft, turn.now, kind="", step="kind", data={"lines": [], "fields": {}})
        return _ask(turn, draft)
    if action == "kind":
        step = "topic" if value["kind"] == K.PROBLEM else "category"
        _touch(draft, turn.now, kind=value["kind"], step=step)
        return _ask(turn, draft)
    if action == "topic":
        topic = value.get("topic")
        if topic not in _topics():
            return _ask(turn, draft)
        if topic in (Case.Topic.USAGE, Case.Topic.SUGGESTION):
            keep = {"anonymous": True} if data.get("anonymous") else {}
            draft.data = {"lines": [], "fields": {}, "topic": topic, **keep}
            return _set(turn, draft, _next_field(draft))
        draft.data = dict(data, topic=topic)
        return _set(turn, draft, "item", line={"category": topic})
    if action == "category":
        return _set(turn, draft, "item", line={"category": value["category"]})
    if action == "back_category":
        back = "topic" if draft.kind == K.PROBLEM and not data["lines"] else "category"
        return _set(turn, draft, back, line={})
    if action == "item":
        return _choose_item(turn, draft, value["code"])
    if action == "skip_serials":
        return _finish_line(turn, draft, [])
    if action == "add_line":
        return _set(turn, draft, "category", line={})
    if action == "lines_done":
        if draft.kind != K.PROBLEM and not data["lines"]:
            return _set(turn, draft, "category")
        return _set(turn, draft, _next_field(draft), line={})
    if action == "skip_field":
        name = draft.step.split(":", 1)[1]
        fields = dict(data["fields"], **{name: ""})
        draft.data = dict(data, fields=fields)
        return _set(turn, draft, _next_field(draft))
    if action == "related":
        fields = dict(data["fields"], related_case_number=value["number"])
        draft.data = dict(data, fields=fields)
        return _set(turn, draft, _next_field(draft))
    if action == "confirm":
        return _confirm(turn, draft)
    return _ask(turn, draft)


def _choose_item(turn, draft, code):
    item = EquipmentType.objects.filter(code=code, is_active=True).first()
    if item is None:
        return _set(turn, draft, "category", line={})
    if any(line["item_code"] == code for line in draft.data["lines"]):
        turn.say("มีรายการนี้แล้ว ถ้าต้องการเปลี่ยนจำนวนให้กดกรอกใหม่ที่หน้าสรุป")
        return _set(turn, draft, "more", line={})
    line = {"category": item.category, "item_code": code}
    if draft.kind == K.PROBLEM:
        step = "serials" if item.is_serialized else None
        if step is None:
            draft.data = dict(draft.data, line=line)
            return _finish_line(turn, draft, [])
        return _set(turn, draft, step, line=line)
    return _set(turn, draft, "quantity", line=line)


def _finish_line(turn, draft, serials):
    data = draft.data
    line = {k: v for k, v in data["line"].items() if k in {"item_code", "quantity"}}
    if serials:
        line["serials"] = serials
    lines = data["lines"] + [line]
    step = "more" if len(lines) < MAX_LINES else _next_field(draft)
    return _set(turn, draft, step, lines=lines, line={})


def _parse_date(text):
    text = text.translate(THAI_DIGITS).strip()
    try:
        if "/" in text:
            day, month, year = (int(p) for p in text.split("/"))
            if year > 2400:
                year -= 543
            return date(year, month, day)
        return date.fromisoformat(text)
    except ValueError:
        return None


def _draft_text(turn, draft, text):
    text = text.strip()
    step = draft.step
    data = draft.data
    if step == "item_search":
        query = text.strip()
        matches = list(
            EquipmentType.objects.filter(category=data["line"]["category"], is_active=True)
            .filter(Q(name__icontains=query) | Q(code__icontains=query))
            .order_by("name")[:11]
        )
        if not matches:
            turn.say("ไม่พบรายการที่ตรงกัน ลองพิมพ์คำอื่น")
            return _ask(turn, draft)
        # Ten buttons at most: when more match, say so, so the person narrows the search.
        more = len(matches) > 10
        matches = matches[:10]
        turn.say(
            "พบหลายรายการ นี่คือ 10 รายการแรก ถ้าไม่มีที่ต้องการ พิมพ์ชื่อหรือรหัสให้ยาวขึ้น"
            if more
            else "เลือกรายการของ",
            [turn.button(i.name, "item", {"code": i.code}, draft) for i in matches]
            + _skip_model(turn, draft)
            + [turn.button("ย้อนกลับ", "back_category", draft=draft)],
        )
        return
    if step == "quantity":
        item = EquipmentType.objects.get(code=data["line"]["item_code"])
        raw = text.translate(THAI_DIGITS)
        number = re.fullmatch(
            rf"\s*(\d+(?:,\d{{3}})*(?:\.\d+)?)\s*(?:{re.escape(item.unit)})?"
            r"\s*(?:ครับ|ค่ะ|คะ)?\s*",
            raw,
        )
        try:
            if number is None:
                raise CommandFailed(ErrorCode.VALIDATION_ERROR)
            quantity = check_quantity_for_item(
                parse_quantity(number.group(1).replace(",", "")), item
            )
        except CommandFailed:
            unit_rule = "ทศนิยมไม่เกิน 2 ตำแหน่ง" if item.is_divisible else "จำนวนเต็ม"
            turn.say(f"จำนวนไม่ถูกต้อง กรุณาพิมพ์ตัวเลขมากกว่า 0 เป็น{unit_rule}")
            return _ask(turn, draft)
        quantity = format(quantity.normalize(), "f")
        line = dict(data["line"], quantity=quantity)
        if draft.kind == K.RETURN and item.is_serialized:
            return _set(turn, draft, "serials", line=line)
        draft.data = dict(data, line=line)
        return _finish_line(turn, draft, [])
    if step == "serials":
        serials = [s for s in re.split(r"[\s,]+", text.strip()) if s]
        unique = len(set(serials)) == len(serials) and all(len(s) <= 64 for s in serials)
        if draft.kind == K.RETURN:
            expected = int(float(data["line"]["quantity"]))
            if len(serials) != expected or not unique:
                turn.say(f"ต้องมีหมายเลขเครื่องไม่ซ้ำกันครบ {expected} หมายเลข")
                return _ask(turn, draft)
        elif not serials or not unique:
            return _ask(turn, draft)
        return _finish_line(turn, draft, serials)
    if step.startswith("field:"):
        name = step.split(":", 1)[1]
        if name == "related_case_number":
            return _ask(turn, draft)  # chosen with buttons only
        value = text.strip()
        if name == "needed_by":
            parsed = _parse_date(value)
            if parsed is None or parsed < timezone.localdate():
                turn.say("วันที่ไม่ถูกต้อง พิมพ์เช่น 31/12/2569 (ต้องไม่ใช่วันที่ผ่านมาแล้ว)")
                return _ask(turn, draft)
            value = parsed.isoformat()
        limit = 100 if name == "source_reference" else 2000
        if not value or len(value) > limit:
            turn.say(f"กรุณาพิมพ์ไม่เกิน {limit} ตัวอักษร")
            return _ask(turn, draft)
        fields = dict(data["fields"], **{name: value})
        draft.data = dict(data, fields=fields)
        return _set(turn, draft, _next_field(draft))
    if step == "kind" and text.translate(THAI_DIGITS) in {"1", "2", "3"}:
        kind = KIND_ORDER[int(text.translate(THAI_DIGITS)) - 1]
        return _draft_button(turn, draft, "kind", {"kind": kind})
    if step == "topic" and text.translate(THAI_DIGITS).isdigit():
        topics = _topics()
        number = int(text.translate(THAI_DIGITS))
        if 1 <= number <= len(topics):
            return _draft_button(turn, draft, "topic", {"topic": topics[number - 1]})
    if step == "category" and text.translate(THAI_DIGITS) in {"1", "2", "3"}:
        category = CATEGORY_ORDER[int(text.translate(THAI_DIGITS)) - 1]
        return _draft_button(turn, draft, "category", {"category": category})
    # Free text where a button is expected: how to answer, then the question again.
    turn.say(CHOICE_HINTS.get(step, "เลือกจากปุ่มด้านล่าง"))
    return _ask(turn, draft)


CHOICE_HINTS = {
    "kind": "เลือกจากปุ่มด้านล่าง หรือพิมพ์เลขข้อ",
    "topic": "เลือกจากปุ่มด้านล่าง หรือพิมพ์เลขข้อ ถ้าไม่แน่ใจกด 'ไม่แน่ใจ'",
    "category": "เลือกจากปุ่มด้านล่าง หรือพิมพ์เลขข้อ",
    "location": "กด 'ส่งตำแหน่ง' ด้านล่าง",
}


def _payload(draft):
    data = draft.data
    payload = {"kind": draft.kind, "lines": data["lines"]}
    if data.get("topic"):
        payload["topic"] = data["topic"]
    if data.get("location"):
        payload["latitude"], payload["longitude"] = data["location"]
    for name, value in data["fields"].items():
        if value:
            payload[name] = value
    return payload


def _confirm(turn, draft):
    try:
        result = services.submit_case(
            _actor(turn), idempotency_key=f"draft-{draft.pk}", payload=_payload(draft)
        )
    except CommandFailed as exc:
        if exc.code == ErrorCode.DEPENDENCY_UNAVAILABLE:
            turn.say("ระบบยังรับเรื่องไม่ได้ชั่วคราว กรุณากดยืนยันอีกครั้งในภายหลัง")
        else:
            turn.say("ข้อมูลยังไม่ครบหรือไม่ถูกต้อง กรุณากรอกใหม่")
        return _ask(turn, draft)
    case = Case.objects.get(pk=result.case_id)
    from operations import ai_chat  # local: ai_chat builds on this module

    ai_chat.record_corrections(draft)
    _touch(draft, turn.now, status=D.SUBMITTED, case=case)
    Attachment.objects.filter(draft=draft).update(case=case)
    turn.conversation.active_draft = None
    turn.conversation.save(update_fields=["active_draft", "updated_at"])
    remaining = _open_drafts(turn).count()
    numbers = [case.number, *(s["case_number"] for s in result.split)]
    if len(numbers) > 1:
        # ADR 0008: one request with items of several divisions becomes one case per division.
        text = (
            f"ยื่นเรื่องแล้ว แยกเป็น {len(numbers)} เรื่องตามกองที่ดูแลของ\n"
            f"เลขเรื่อง: {', '.join(numbers)}\nสถานะ: {case.get_status_display()}\n"
            "ยืนยันรับของแยกตามเรื่อง ติดตามได้จากเมนู 'ติดตามเรื่อง'"
        )
    else:
        text = (
            f"ยื่นเรื่องแล้ว\nเลขเรื่อง: {case.number}\nสถานะ: {case.get_status_display()}\n"
            "ติดตามได้จากเมนู 'ติดตามเรื่อง'"
        )
    if remaining:
        text += f"\nยังมีร่างที่ยังไม่ยื่น {remaining} ร่าง"
    from operations import policy  # local: policy builds on workflow, not on this module

    guidance = policy.hazard_guidance(case)  # M09 Q10: at once, never waiting for AI
    if guidance:
        text += f"\n\nคำแนะนำเร่งด่วน: {guidance}"
    _menu(turn, text)


# --- Attachments ---------------------------------------------------------------------------


def _attachment(turn, draft):
    message = turn.event.get("message") or {}
    image_set = message.get("imageSet") or {}
    quiet = image_set and image_set.get("index", 1) < image_set.get("total", 1)
    if message["type"] == "image" and (message.get("contentProvider") or {}).get("type") != "line":
        turn.say("รับเฉพาะรูปที่ส่งจาก LINE โดยตรง")
        return
    if message["type"] == "file":
        name = str(message.get("fileName", ""))
        extension = name[name.rfind(".") :].lower() if "." in name else ""
        size = message.get("fileSize") or 0
        if extension not in {".pdf", ".jpg", ".jpeg", ".png"}:
            turn.say("รับเฉพาะไฟล์ PDF และรูป JPEG/PNG")
            return
        if size > settings.ATTACHMENT_MAX_BYTES:
            turn.say("ไฟล์ใหญ่เกิน 10 MB")
            return
    if draft is not None and draft.attachments.count() >= settings.ATTACHMENTS_PER_DRAFT:
        turn.say(f"แนบไฟล์ได้ไม่เกิน {settings.ATTACHMENTS_PER_DRAFT} ไฟล์ต่อเรื่อง")
        return
    waiting = _awaiting_info(turn, draft)
    attachment = Attachment.objects.create(
        identity=turn.identity,
        inbox=turn.inbox,
        line_message_id=str(message.get("id", "")),
        draft=None if waiting else draft,
        case=waiting[0] if waiting else None,  # a file sent after an information request
        declared_name=str(message.get("fileName", ""))[:200],
    )
    if quiet:
        return
    if waiting:
        case = waiting[0]
        identity_services.audit(
            None,
            "case.info_file_received",
            case,
            actor_label=f"requester:{turn.requester.pk}",
            number=case.number,
            attachment=str(attachment.pk),
        )
        more = (
            f"\nมี {len(waiting)} เรื่องที่รอข้อมูล ไฟล์นี้แนบกับเรื่องที่เจ้าหน้าที่ขอล่าสุด"
            if len(waiting) > 1
            else ""
        )
        turn.say(
            f"ได้รับไฟล์แล้ว แนบกับเรื่อง {case.number}\n"
            f"เจ้าหน้าที่จะเห็นเมื่อระบบตรวจความปลอดภัยของไฟล์เสร็จ{more}",
            MENU,
        )
        return
    if draft is None:
        drafts = list(_open_drafts(turn)[:10])
        if drafts:
            turn.say("ได้รับไฟล์แล้ว")
            return _choose_draft(turn, drafts)
        turn.say(
            "ได้รับไฟล์แล้ว ต้องการเริ่มเรื่องใหม่เพื่อแนบไฟล์นี้ไหม",
            [turn.button("เริ่มเรื่องใหม่", "new_draft")] + MENU[1:],
        )
        return
    turn.say("ได้รับไฟล์แล้ว ระบบกำลังตรวจความปลอดภัยของไฟล์")
    _ask(turn, draft)


# --- Reply delivery ------------------------------------------------------------------------


def _may_send(user_id):
    from operations import options

    if not settings.OUTBOUND_ENABLED or not settings.LINE_CHANNEL_ACCESS_TOKEN:
        return False, "outbound disabled"
    if (
        settings.APP_ENV != "production"
        and user_id not in settings.LINE_ALLOWED_RECIPIENTS
        and options.get(options.LINE_AUDIENCE) != "everyone"
    ):
        return False, "not in allowlist"
    return True, ""


def send_reply(inbox_id, messages):
    """Use the event's reply token once, before its deadline. A reply that can no longer go
    (deadline passed, or LINE refused the token of a redelivered event) is handed to
    notifications.plan_catch_up (ADR 0015 Q12), which decides whether a push may carry it."""
    if not messages:
        return
    S = WebhookInbox.ReplyStatus
    with transaction.atomic():
        inbox = WebhookInbox.objects.select_for_update().get(pk=inbox_id)
        if not inbox.reply_token or inbox.reply_used_at is not None:
            return
        allowed, why = _may_send(inbox.source_user_id)
        if timezone.now() > inbox.reply_deadline:
            inbox.reply_status, inbox.reply_detail = S.EXPIRED, "deadline passed"
        elif not allowed:
            inbox.reply_status, inbox.reply_detail = S.SKIPPED, why
        else:
            inbox.reply_status = S.UNKNOWN
        inbox.reply_used_at = timezone.now()
        inbox.save(update_fields=["reply_used_at", "reply_status", "reply_detail"])
        outcome, token = inbox.reply_status, inbox.reply_token
    if outcome == S.EXPIRED:
        _catch_up(inbox_id, messages)
    if outcome != S.UNKNOWN:
        return
    token_refused = False
    try:
        line_api.reply(token, messages)
        status, detail = S.SENT, ""
    except line_api.LineApiError as exc:
        status, detail = S.FAILED, f"{exc.status}"
        token_refused = exc.status == 400 and "reply token" in (exc.detail or "").lower()
    except OSError as exc:
        # Timeout or connection loss: LINE may or may not have delivered it.
        status, detail = S.UNKNOWN, type(exc).__name__
    WebhookInbox.objects.filter(pk=inbox_id).update(reply_status=status, reply_detail=detail)
    if status == S.FAILED and token_refused:  # the expired token of a redelivered event
        _catch_up(inbox_id, messages)


def _catch_up(inbox_id, messages):
    from operations import notifications  # local: notifications imports this module's peers

    inbox = WebhookInbox.objects.get(pk=inbox_id)
    if notifications.plan_catch_up(inbox, messages):
        WebhookInbox.objects.filter(pk=inbox_id).update(reply_detail="catch-up push planned")
