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
from operations import (
    line_api,
    problem_pick,
    requester_steps,
    services,
    typed_lines,
    usage_meters,
    workflow,
)
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
    LineMenuItem,
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
SUMMARY_HEAD = "สรุปเรื่อง:"
CONFIRM_LABEL = "ยืนยันส่งเรื่อง"
THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")

KIND_LABELS = {K.WITHDRAWAL: "เบิก", K.RETURN: "ส่งคืน", K.PROBLEM: "แจ้งปัญหา/ข้อเสนอแนะ"}
KIND_SHORT = {K.WITHDRAWAL: "เบิก", K.RETURN: "คืน", K.PROBLEM: "ปัญหา/ข้อเสนอแนะ"}
KIND_ORDER = [K.WITHDRAWAL, K.RETURN, K.PROBLEM]
# หมวดเรื่องจริงของ Case ใช้ส่งต่อและแสดงผลภายหลัง ส่วนเมนูเริ่มต้นของ LINE ใช้
# METER_GROUPS ด้านล่างเพื่อให้ทุกบริการอุปกรณ์มีตัวเลือกชุดเดียวกัน.
TOPIC_LABELS = {
    "meter": "มิเตอร์",
    "transformer": "หม้อแปลง",
    "other": "อุปกรณ์อื่น",
    "usage": "การใช้ไฟฟ้า",
    "suggestion": "ข้อเสนอแนะ",
    "repair": "ติดตามสถานะการซ่อม",
}
SUGGESTION_FIELDS = [
    ("description", "กรุณาแจ้งข้อเสนอแนะ", True),
    ("impact", "ถ้ามีรายละเอียดเพิ่ม เช่น สถานที่หรืองานที่เกี่ยวข้อง พิมพ์ต่อได้ หรือกดข้าม", False),
    ("related_case_number", "หากเกี่ยวข้องกับเรื่องเดิม กรุณาเลือกเรื่อง หรือเลือกข้าม", False),
]
# ADR 0030 Q6: a division picked from the carousel, then the details in one message.
GENERAL_FIELDS = [("description", problem_pick.DETAIL, True)]
# 25 Oct 2569 Q11: after the meters and their districts; the details may be skipped.
USAGE_FIELDS = [
    (
        "description",
        "รายละเอียดเพิ่มเติม เช่น สิ่งที่พบ ช่วงเวลา หรือเลขผู้ใช้ไฟ (CA) หรือกดข้าม",
        False,
    ),
    ("related_case_number", "หากเกี่ยวข้องกับเรื่องเดิม กรุณาเลือกเรื่อง หรือเลือกข้าม", False),
]
# เมนู LINE (15 ต.ค. 2569 Q18, Q20): a question to the division the menu named, and a problem
# with a work system (SMG/AMR); neither lists items.
INQUIRY_FIELDS = [
    ("description", "แจ้งรายละเอียดที่ต้องการสอบถาม เช่น เลขใบเบิก เลขเครื่อง หรือเลขเรื่อง", True),
]
SYSTEM_FIELDS = [
    ("description", "แจ้งปัญหาที่พบ เช่น หน้าจอที่ใช้ เลขมิเตอร์ หรือช่วงเวลา", True),
    ("impact", "ผลกระทบหรือข้อมูลเพิ่มเติม พิมพ์ต่อได้ หรือกดข้าม", False),
]
# ติดตามสถานะการซ่อม (user, 24 Oct 2569): after the SAP number and the equipment rows.
REPAIR_FIELDS = [
    ("description", "พิมพ์สิ่งที่ต้องการติดตาม เช่น ส่งซ่อมเมื่อไร หรืออาการที่ส่งซ่อม", True),
    ("related_case_number", "หากเกี่ยวข้องกับเรื่องเดิม กรุณาเลือกเรื่อง หรือเลือกข้าม", False),
]
CATEGORY_LABELS = {
    EquipmentType.Category.METER: "มิเตอร์",
    EquipmentType.Category.TRANSFORMER: "หม้อแปลง",
    EquipmentType.Category.OTHER: "อุปกรณ์อื่น",
}
METER_GROUPS = {"meter": "มิเตอร์", "ct": "CT", "vt": "VT", "other": "อุปกรณ์อื่น"}
# Every group a tile may ask for before the action (typed_lines; กอจ. since 24 Oct 2569).
TYPED_GROUPS = dict(EquipmentType.LineGroup.choices)
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
        ("related_case_number", "หากเกี่ยวข้องกับเรื่องเดิม กรุณาเลือกเรื่อง หรือเลือกข้าม", False),
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
    "general": {"description": "รายละเอียด"},
    "usage": {"description": "เรื่องที่พบ", "impact": "ข้อมูลเพิ่มเติม"},
    "inquiry": {"description": "สิ่งที่สอบถาม"},
    "repair": {"description": "สิ่งที่ต้องการติดตาม"},
    "system": {"description": "ปัญหาที่พบ", "impact": "ข้อมูลเพิ่มเติม"},
}
MENU = [("ส่งเรื่องใหม่", "m=new"), ("เรื่องของฉัน", "m=mine"), ("ติดตามเรื่อง", "m=track")]
MENU_WORDS = {label: data for label, data in MENU}
MENU_WORDS.update({"บริการ": "m=home", "ติดตามเรื่องของฉัน": "m=mine", "เมนูตามสิทธิ์": "m=roles"})
MENU_WORDS.update(
    {
        "เบิกอุปกรณ์": "m=equipment&kind=withdrawal",
        "ส่งคืนอุปกรณ์": "m=equipment&kind=return",
        "แจ้งปัญหา": "m=problems",
        "เมนูเพิ่มเติม": "m=roles",
    }
)
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


def _postback(label, data, display=None):
    """A quick reply; `display` is what shows in the chat when the label had to be shortened
    (25 Oct 2569 Q3: "ทบทวนหน่วยไฟฟ้า" for ทบทวนหน่วยไฟฟ้าจากการร้องเรียน)."""
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
            "displayText": display or label,
        },
    }


def text_message(text, quick=None):
    message = {"type": "text", "text": text[:5000]}
    if quick:
        message["quickReply"] = {"items": [_postback(*button) for button in quick][:MAX_QUICK]}
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
    allow_agent: bool = True
    anonymous: bool = False  # ผู้แจ้งไม่ระบุตัวตน (ADR 0010): problems and suggestions only
    internal: bool = False  # ผู้แจ้งภายใน (Q8): staff reporting in their division's name

    def say(self, text, quick=None):
        self.replies.append(text_message(text, quick))

    def button(self, label, action, value=None, draft=None, ttl=BUTTON_TTL):
        value = dict(value or {})
        if action in {
            "staff_ow",
            "staff_step_pick",
            "staff_step_go",
            "staff_step_cancel",
            "staff_order_confirm",
            "staff_order_edit",
            "staff_order_cancel",
            "staff_order_value",
            "staff_order_people",
            "staff_step_back",
            "admin_confirm",
        }:
            value["prompt_revision"] = self.conversation.prompt_data.get("prompt_revision")
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
        if prompt in {"staff_ow", "staff_step", "staff_order_chat", "admin_choice"}:
            data = {**data, "prompt_revision": secrets.token_urlsafe(16)}
        self.conversation.prompt_data = data
        self.conversation.save(update_fields=["prompt", "prompt_data", "updated_at"])


def _remember_choices(inbox, messages):
    choices = {}
    ambiguous = set()

    def visit(node):
        if isinstance(node, list):
            for child in node:
                visit(child)
        elif isinstance(node, dict):
            if node.get("type") == "postback" and node.get("data"):
                for label in (node.get("label"), node.get("displayText")):
                    if label:
                        label = " ".join(label.split())
                        if label in choices and choices[label] != node["data"]:
                            ambiguous.add(label)
                        choices[label] = node["data"]
            for child in node.values():
                if isinstance(child, (dict, list)):
                    visit(child)

    visit(messages)
    for label in ambiguous:
        choices.pop(label, None)
    Conversation.objects.filter(
        identity__channel=inbox.channel, identity__external_id=inbox.source_user_id
    ).filter(Q(reply_choices__inbox__lte=inbox.pk) | Q(reply_choices={})).update(
        reply_choices={
            "inbox": inbox.pk,
            "actions": choices,
            "summary": "\n".join(m.get("text", "") for m in messages)[-3000:],
        }
    )


def handle_inbox(inbox_id):
    """Process one stored event; returns reply messages (sent later by `send_reply`)."""
    with transaction.atomic():
        inbox = WebhookInbox.objects.select_for_update().get(pk=inbox_id)
        if inbox.status != WebhookInbox.Status.PENDING:
            return []
        replies, handled = _dispatch(inbox)
        if replies:
            _remember_choices(inbox, replies[:5])
        inbox.status = WebhookInbox.Status.DONE if handled else WebhookInbox.Status.IGNORED
        inbox.processed_at = timezone.now()
        inbox.save(update_fields=["status", "processed_at"])
    return replies[:5]


def _dispatch(inbox, event_override=None, allow_agent=True):
    event = inbox.payload if event_override is None else event_override
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
    if (
        event_override is None
        and inbox.event_type == "message"
        and (event.get("message") or {}).get("type") == "text"
    ):
        typed = " ".join(str(event["message"].get("text", "")).split())
        actions = conversation.reply_choices.get("actions", {})
        selected = actions.get(typed)
        if not selected and CONFIRM_LABEL in actions:
            from operations import agent_intake  # local: it reads AI versions

            # ADR 0022 Q13: "ยืนยัน" or "ส่งเลย" typed right after the latest summary is its
            # confirm button; the button's token still checks that the draft is unchanged.
            summary = conversation.reply_choices.get("summary", "")
            if SUMMARY_HEAD in summary and agent_intake.confirming(typed) and agent_intake.active():
                selected = actions[CONFIRM_LABEL]
        if not selected and typed not in MENU_WORDS:
            # Rich-menu labels are typable even before the bot has suggested them in chat.
            matches = list(
                LineMenuItem.objects.filter(is_active=True, label=typed).filter(
                    Q(parent__isnull=True) | Q(parent__is_active=True)
                )[:2]
            )
            if len(matches) == 1:
                selected = f"m=item&id={matches[0].pk}"
        if selected and selected.startswith("t="):
            choice = PostbackToken.objects.filter(token=selected[2:]).first()
            if choice and choice.draft_id and choice.draft_id != conversation.active_draft_id:
                selected = None  # Typing must not silently select one of several drafts.
        if selected:
            # Treat typing a displayed option exactly like tapping it; stored webhook stays raw.
            event = {**event, "postback": {"data": selected}}
            inbox.event_type = "postback"
    turn = Turn(inbox, event, identity, conversation, timezone.now(), allow_agent=allow_agent)
    from operations import agent_chat

    if allow_agent and agent_chat.handle_confirmation(turn):
        return turn.replies, True
    if inbox.event_type == "unfollow":
        ChannelIdentity.objects.filter(pk=identity.pk).update(is_following=False)
        return [], True
    if inbox.event_type == "follow":
        ChannelIdentity.objects.filter(pk=identity.pk).update(is_following=True)
        turn.say("ช่องทางนี้รับเรื่องเบิก ส่งคืน แจ้งปัญหา และข้อเสนอแนะถึง ฝอจ.")
    command = (_event_text(turn) or "").strip()
    external = identity.requester_id and identity.requester.is_external
    if command in {"ลงทะเบียนพนักงาน", "ยืนยันพนักงาน"} and (
        identity.status != ChannelIdentity.Status.ACTIVE or external
    ):
        _ask_employee_id(turn, {})
        return turn.replies, True
    if external and (
        conversation.prompt.startswith("reg_")
        or command in {"สถานะลงทะเบียน", "ยกเลิกคำขอ"}
        or (_token(turn)[0] is not None and _token(turn)[0].action == "reg_cancel")
    ):
        _registration(turn)
        return turn.replies, True
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
    from operations import employees, options, staff_line, staff_work, work_orders

    text = _event_text(turn)
    command = " ".join(text.split()) if text is not None else ""
    prompt = turn.conversation.prompt
    token, _ = _token(turn)
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
    if command == "เชื่อมบัญชีเจ้าหน้าที่" or (token is not None and token.action == "staff_otp_new"):
        if options.get(options.STAFF_LINK) == "code_or_otp":
            turn.set_prompt("staff_otp_emp")
            turn.say(
                "พิมพ์รหัสพนักงานของคุณ ระบบจะส่งรหัส 6 หลักไปที่อีเมลในทะเบียนพนักงาน\n"
                "หรือพิมพ์ เชื่อม ตามด้วยรหัส 6 หลักจากหน้าเว็บ บัญชีของฉัน"
            )
        elif options.get(options.STAFF_LINK) == "code_or_employee" and employees.has_directory():
            turn.set_prompt("staff_emp")
            turn.say("พิมพ์รหัสพนักงานของคุณ หรือพิมพ์ เชื่อม ตามด้วยรหัส 6 หลักจากหน้าเว็บ")
        else:
            turn.say("เข้าเว็บ บัญชีของฉัน กด 'เชื่อม LINE' แล้วพิมพ์ เชื่อม ตามด้วยรหัส 6 หลักที่ได้")
        return True
    if prompt == "staff_otp_emp" and command:
        _staff_send_code(turn, command[:32])
        return True
    if token is not None and token.action == "staff_otp_again":
        _staff_send_code(turn, token.value.get("employee_id", ""))
        return True
    if prompt == "staff_otp" and command:
        employee_id = turn.conversation.prompt_data.get("employee_id", "")
        user, reason, left = staff_line.link_with_otp(turn.identity, employee_id, command)
        if user is not None:
            turn.set_prompt()
            turn.say("เชื่อม LINE กับบัญชีเจ้าหน้าที่แล้ว")
            staff_line.menu(turn, user)
        elif reason == "wrong":
            turn.say(f"รหัสไม่ถูก ลองได้อีก {left} ครั้ง")
        else:
            turn.set_prompt()
            turn.say(
                "รหัสนี้ใช้ไม่ได้แล้ว ขอรหัสใหม่ได้",
                [turn.button("ขอรหัสใหม่", "staff_otp_new")],
            )
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
    data = (turn.event.get("postback") or {}).get("data", "")
    if data.startswith("tab="):
        return True  # a rich menu tab switch (richmenuswitch): LINE changes the menu itself
    user = staff_line.staff_of(turn.identity)
    if user is None:
        return False
    if (
        data.startswith("m=koj")
        or (token is not None and token.action.startswith("koj_"))
        or (prompt.startswith("koj_") and text is not None and not data)
    ):
        from operations import koj_chat

        turn.requester = turn.identity.requester
        if data.startswith("m=koj"):
            koj_chat.static(turn, data)
        elif token is not None:
            koj_chat.on_token(turn, token)
        elif text is not None:
            koj_chat.on_text(turn, text)
        return True
    if data == "m=roles" or command in {"เมนูตามสิทธิ์", "เมนูเพิ่มเติม"}:
        turn.set_prompt()
        staff_work.navigation(turn, user)
        return True
    navigation = data or MENU_WORDS.get(command) or ""
    if navigation in {"m=mine", "m=home", "m=problems"} or navigation.startswith(
        ("m=equipment&", "m=problem&")
    ):
        if turn.identity.requester_id is not None:
            return False
        turn.requester = staff_line.internal_requester(turn.identity, user)
        turn.internal = True
        _static_postback(turn, navigation, turn.conversation.active_draft)
        return True
    from operations import admin_line

    if admin_line.handle(turn, user, command, token):
        return True
    if command == "เลิกเชื่อมบัญชีเจ้าหน้าที่":
        staff_line.unlink(turn.identity)
        turn.say("เลิกเชื่อม LINE กับบัญชีเจ้าหน้าที่แล้ว")
        return True
    from operations import staff_orders_chat

    if staff_orders_chat.handle(turn, user, command, token):
        return True
    if _staff_work_turn(turn, user, token, data, command, prompt):
        return True
    if token is not None and token.action == "staff_menu":
        staff_line.menu(turn, user)
        return True
    if token is not None and token.action == "staff_case":
        case = staff_line._case_for(user, case_id=token.value.get("case"))
        if case is None:
            turn.say("ไม่พบเรื่องนี้ในงานของคุณแล้ว")
        else:
            staff_work.case_detail(turn, user, case)
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
            staff_work.case_detail(turn, user, case)
            return True
    if command and staff_work.ORDER_NUMBER.fullmatch(command.upper()):
        order = work_orders.visible(user).filter(number=command.upper()).first()
        if order is not None:
            staff_work.order_detail(turn, user, order)
            return True
    from operations import agent_chat

    if command and not prompt and agent_chat.queue_staff(turn, command):
        return True
    if turn.identity.status != ChannelIdentity.Status.ACTIVE:
        # A staff-only LINE: everything else shows the staff menu.
        staff_line.menu(turn, user)
        return True
    return False


def _staff_work_turn(turn, user, token, data, command, prompt):
    """16 Oct 2569 Q8, Q15: the งานของกอง and สั่งงาน tabs, งานสั่ง and case steps in chat."""
    from django.contrib.auth import get_user_model

    from operations import staff_line, staff_work, work_orders

    User = get_user_model()

    if data.startswith("m=item&id="):
        item = _menu_entry(data.split("=", 2)[2])
        if item is not None and item.tab not in {LineMenuItem.Tab.REPORT, LineMenuItem.Tab.GUEST}:
            staff_work.handle_item(turn, user, item)
            return True
        if item is not None and turn.identity.status != ChannelIdentity.Status.ACTIVE:
            # A staff-only LINE reports in its division's name (Q8).
            turn.requester = staff_line.internal_requester(turn.identity, user)
            turn.anonymous, turn.internal = False, True
            _menu_item(turn, item.pk)
            return True
        return False
    action = token.action if token is not None else ""
    value = token.value if token is not None else {}
    if action in {
        "staff_ow",
        "staff_step_pick",
        "staff_step_go",
        "staff_step_cancel",
        "staff_step_back",
        "staff_order_confirm",
    }:
        revision = value.get("prompt_revision")
        if not revision or revision != turn.conversation.prompt_data.get("prompt_revision"):
            turn.say("ปุ่มนี้เป็นขั้นตอนเก่า ใช้ข้อความหรือปุ่มล่าสุดแทน")
            return True
    if action == "staff_list":
        staff_work.list_page(turn, user, value.get("kind", "group"), value.get("page", 1))
        return True
    if action == "staff_assign_pick":
        case = staff_line._case_for(user, case_id=value.get("case"))
        if case is not None:
            staff_work.step_start(turn, user, case, "assign")
        else:
            turn.say("ไม่พบเรื่องนี้ในงานของคุณแล้ว")
        return True
    if action == "staff_step_back" and prompt == "staff_step":
        staff_work.step_back(turn, user)
        return True
    if action == "staff_order":
        order = work_orders.visible(user).filter(pk=value.get("order")).first()
        if order is None:
            turn.say("ไม่พบงานนี้ในงานของคุณแล้ว")
        else:
            staff_work.order_detail(turn, user, order)
        return True
    if action == "staff_os":
        order = work_orders.visible(user).filter(pk=value.get("order")).first()
        person = None
        if value.get("user"):
            person = User.objects.filter(pk=value["user"], is_active=True).first()
        if order is None:
            turn.say("ไม่พบงานนี้ในงานของคุณแล้ว")
        else:
            staff_work.order_step(
                turn,
                user,
                order,
                value.get("step", ""),
                person=person,
                skip_reason=value.get("skip_reason", False),
                page=value.get("page", 1),
            )
        return True
    if action == "staff_ow_start":
        staff_work.order_start(turn, user)
        return True
    if action == "staff_ow" and prompt == "staff_ow":
        turn.set_prompt()
        turn.say("ขั้นตอนสั่งงานเปลี่ยนแล้ว พิมพ์ สั่งงาน ตามด้วยงานและผู้รับ")
        return True
    if action == "staff_step":
        case = staff_line._case_for(user, case_id=value.get("case"))
        if case is None:
            turn.say("ไม่พบเรื่องนี้ในงานของคุณแล้ว")
        else:
            staff_work.step_start(turn, user, case, value.get("action", ""))
        return True
    if action == "staff_step_pick" and prompt == "staff_step":
        staff_work.step_answer(turn, user, value.get("v", ""))
        return True
    if action == "staff_step_go" and prompt == "staff_step":
        staff_work.step_go(turn, user)
        return True
    if action == "staff_step_cancel":
        turn.set_prompt()
        turn.say("ยกเลิกแล้ว ไม่ได้บันทึกอะไร")
        return True
    if not command:
        return False
    if prompt == "staff_find":
        staff_work.find(turn, user, command)
        return True
    if prompt == "staff_assign":
        staff_work.assign_case(turn, user, command)
        return True
    if prompt == "staff_ow":
        from operations import staff_orders_chat

        staff_orders_chat.start(turn, user)
        staff_orders_chat.handle(turn, user, command, None)
        return True
    if prompt == "staff_step":
        staff_work.step_answer(turn, user, command)
        return True
    if prompt == "staff_os_text":
        order = work_orders.visible(user).filter(pk=turn.conversation.prompt_data.get("order"))
        order = order.first()
        if order is None:
            turn.set_prompt()
            turn.say("ไม่พบงานนี้ในงานของคุณแล้ว")
        else:
            step = turn.conversation.prompt_data.get("step", "")
            staff_work.order_step(turn, user, order, step, text=command[:2000])
        return True
    return False


def _staff_send_code(turn, employee_id):
    """16 Oct 2569 Q10: the employee ID typed for linking; a code goes to the roster e-mail."""
    from operations import options, staff_line

    reason = staff_line.send_link_code(turn.identity, employee_id)
    if reason == "":
        turn.set_prompt("staff_otp", employee_id=employee_id)
        turn.say(
            "ส่งรหัส 6 หลักไปที่อีเมลของคุณในทะเบียนพนักงานแล้ว "
            f"ใช้ได้ {options.get(options.CODE_MINUTES)} นาที\nพิมพ์รหัสที่นี่",
            [turn.button("ส่งรหัสใหม่", "staff_otp_again", {"employee_id": employee_id})],
        )
        return
    turn.set_prompt()
    turn.say(
        {
            "locked": "ลองครบจำนวนครั้งของวันนี้แล้ว ใช้รหัส 6 หลักจากหน้าเว็บ บัญชีของฉัน แทน",
            "limit": "ขอรหัสทางอีเมลครบจำนวนของวันนี้แล้ว ใช้รหัส 6 หลักจากหน้าเว็บแทน",
            "unavailable": "ตอนนี้ส่งอีเมลไม่ได้ ใช้รหัส 6 หลักจากหน้าเว็บ บัญชีของฉัน แทน",
        }.get(
            reason,
            "ไม่พบบัญชีเจ้าหน้าที่ที่ใช้รหัสพนักงานนี้ หรือทะเบียนพนักงานไม่มีอีเมล "
            "ให้แอดมินตรวจรหัสพนักงานในบัญชีของคุณ หรือใช้รหัส 6 หลักจากหน้าเว็บแทน",
        )
    )


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
        if (token is not None and token.action == "reg_cancel") or _event_text(turn) == "ยกเลิกคำขอ":
            identity_services.cancel_registration(LINE, identity.external_id)
            turn.set_prompt("reg_name")
            turn.say("ยกเลิกคำขอแล้ว พิมพ์ชื่อ-นามสกุลเพื่อลงทะเบียนใหม่")
            return
        turn.say(
            f"คำขอลงทะเบียนของคุณ ({latest.submitted_name}, "
            f"{latest.requested_district.name}) รอเจ้าหน้าที่รับรอง\n"
            "ระหว่างรอแจ้งปัญหาหรือข้อเสนอแนะ และติดตามเรื่องของคุณได้",
            [turn.button("ยกเลิกคำขอ", "reg_cancel")],
        )
        return
    prompt = turn.conversation.prompt
    data = dict(turn.conversation.prompt_data or {})
    if token is not None and token.action == "anon_start":
        return _start_anonymous(turn)
    if token is not None and token.action == "reg_as":
        if token.value.get("who") == "employee":
            return _ask_employee_id(turn, {})
        turn.set_prompt("reg_external_name")
        turn.say("พิมพ์ชื่อของคุณ แล้วสามารถแจ้งปัญหาหรือข้อเสนอแนะได้")
        return
    if prompt in ("reg_employee", "reg_method", "reg_code", "reg_email") and token is not None:
        if token.action == "reg_other":
            return _other_path(turn, data)
        if token.action == "reg_otp":
            return _send_code(turn, data)
        if token.action == "reg_typed":
            return _ask_email(turn, data)
    if prompt == "reg_district" and token is not None and token.action == "reg_district":
        data["district"] = token.value["code"]
        return _registration_next(turn, data)
    if prompt == "reg_district" and token is not None and token.action == "reg_rename":
        if data.get("employee"):
            return _ask_district(turn, data.pop("name", ""), **data)
        turn.set_prompt("reg_name")
        turn.say(f"พิมพ์ชื่อและนามสกุลใหม่ เว้นวรรคระหว่างกัน {NAME_EXAMPLE}")
        return
    text = _event_text(turn)
    if prompt == "reg_district" and text is not None and data.get("name"):
        # Typed instead of choosing: the list again, keeping what the steps so far gave.
        return _ask_district(turn, data.pop("name"), **data)
    if prompt == "reg_external_name":
        if not text or not 2 <= len(text.strip()) <= 200:
            turn.say("พิมพ์ชื่อ 2–200 ตัวอักษร")
            return
        from django.core.exceptions import ValidationError

        try:
            turn.requester = identity_services.external_requester(turn.identity, text)
        except ValidationError as exc:
            turn.say("; ".join(exc.messages))
            return
        turn.identity.refresh_from_db()
        turn.set_prompt()
        return _menu(turn, "ลงทะเบียนแล้ว สามารถแจ้งปัญหาหรือข้อเสนอแนะได้")
    if prompt == "reg_employee":
        if text and text.strip():
            return _employee_id(turn, data, text.strip()[:32])
        return _ask_employee_id(turn, data)
    if prompt == "reg_code" and text and text.strip():
        return _check_code(turn, data, text)
    if prompt == "reg_email" and text and text.strip():
        return _check_email(turn, data, text)
    if prompt in ("reg_method", "reg_code", "reg_email"):
        return _ways(turn, data, "เลือกวิธียืนยันจากปุ่มด้านล่าง")
    if prompt == "reg_kind":
        if text and text.strip() in {"พนักงาน", "พนักงาน กฟน."}:
            return _ask_employee_id(turn, {})
        if text and text.strip() in {"อื่น ๆ", "อื่นๆ", "อื่น"}:
            turn.set_prompt("reg_external_name")
            turn.say("พิมพ์ชื่อของคุณ แล้วสามารถแจ้งปัญหาหรือข้อเสนอแนะได้")
            return
        if text and text.strip() == "ไม่ระบุตัวตน":
            return _start_anonymous(turn)
        return _ask_kind(turn, "ยังไม่ได้ลงทะเบียน")
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
        return _ask_district(turn, name, **{k: v for k, v in data.items() if k == "checked"})
    if problem:
        turn.say(problem)
    lead = "ยังไม่ได้ลงทะเบียนเป็นผู้แจ้ง"
    if latest is not None and latest.status == RegistrationRequest.Status.REJECTED:
        lead = "คำขอก่อนหน้าไม่ได้รับการรับรอง"
        if latest.decision_reason:
            lead += f" ({latest.decision_reason})"
    elif identity.status == ChannelIdentity.Status.REVOKED:
        lead = "บัญชี LINE นี้ต้องลงทะเบียนและรับรองใหม่"
    if _employee_path_on():
        return _ask_kind(turn, lead)
    turn.set_prompt("reg_name")
    _ask_name(turn, f"{lead}\nพิมพ์ชื่อ-นามสกุลเพื่อลงทะเบียน")


def _employee_path_on():
    return True


def _ask_kind(turn, lead):
    turn.set_prompt("reg_kind")
    turn.say(
        lead + "\nเลือกประเภทผู้ใช้ กดปุ่มหรือพิมพ์ พนักงาน / อื่น ๆ / ไม่ระบุตัวตน",
        [
            turn.button("พนักงาน กฟน.", "reg_as", {"who": "employee"}),
            turn.button("อื่น ๆ", "reg_as", {"who": "other"}),
            turn.button("ไม่ระบุตัวตน", "anon_start"),
        ],
    )


def _other_path(turn, data):
    """อื่น ๆ or ใช้วิธีอื่น: the name (when not given yet) and the unit, for a person to approve."""
    data = {k: v for k, v in data.items() if k in ("name", "district", "location")}
    if data.get("name") and data.get("district"):
        return _registration_next(turn, {**data, "checked": True})
    turn.set_prompt("reg_name", checked=True)
    _ask_name(turn, f"พิมพ์ชื่อ-นามสกุลเพื่อลงทะเบียน {NAME_EXAMPLE}")


def _ask_employee_id(turn, data):
    turn.set_prompt("reg_employee", **data)
    turn.say(
        "พิมพ์รหัสพนักงานของคุณ หรือกด 'ใช้วิธีอื่น' ให้เจ้าหน้าที่ตรวจแทน",
        [turn.button("ใช้วิธีอื่น", "reg_other")],
    )


def _trying(data):
    from operations.models import Employee

    pk = data.get("employee_try")
    return Employee.objects.filter(pk=pk, is_active=True).first() if pk else None


def _employee_id(turn, data, typed):
    """The roster has this ID: offer the ways to prove it (15 ต.ค. 2569 Q24). The name is never
    shown before that, so typing someone's ID tells nothing about them."""
    from operations import email_codes, employees

    employee, reason = employees.lookup(turn.identity, typed)
    if employee is None:
        return _failed(turn, data, reason, "ไม่พบรหัสพนักงานนี้ในทะเบียน", _ask_employee_id)
    data = {**data, "employee_try": employee.pk}
    ways = email_codes.methods(employee)
    if not ways and employee.check_hash:  # an older list: its proof value
        turn.set_prompt("reg_check", **data, employee_id=typed)
        turn.say(f"พิมพ์{employees.check_label()}เพื่อยืนยัน")
        return
    if ways == ["otp"]:
        return _send_code(turn, data)
    if ways == ["email"]:
        return _ask_email(turn, data)
    if not ways:
        turn.say("รหัสนี้ยังยืนยันทาง LINE ไม่ได้ จะให้เจ้าหน้าที่ตรวจแทน")
        return _other_path(turn, data)
    _ways(turn, data, "ยืนยันว่าเป็นคุณ: รับรหัส 6 หลักทางอีเมลในทะเบียนพนักงาน หรือพิมพ์อีเมลของคุณเอง")


def _ways(turn, data, text):
    from operations import email_codes

    employee = _trying(data)
    if employee is None:
        return _other_path(turn, data)
    turn.set_prompt("reg_method", **data)
    buttons = []
    if "otp" in email_codes.methods(employee):
        buttons.append(turn.button("ส่งรหัสทางอีเมล", "reg_otp"))
    if "email" in email_codes.methods(employee):
        buttons.append(turn.button("พิมพ์อีเมลเอง", "reg_typed"))
    buttons.append(turn.button("ใช้วิธีอื่น", "reg_other"))
    turn.say(text, buttons)


def _send_code(turn, data):
    from operations import email_codes, options

    employee = _trying(data)
    if employee is None:
        return _other_path(turn, data)
    reason = email_codes.issue(turn.identity, employee)
    if reason == "limit":
        return _ways(turn, data, "ขอรหัสทางอีเมลครบจำนวนของวันนี้แล้ว เลือกวิธีอื่น")
    if reason:
        return _ways(turn, data, "ตอนนี้ส่งรหัสทางอีเมลไม่ได้ เลือกวิธีอื่น")
    turn.set_prompt("reg_code", **data)
    buttons = [turn.button("ส่งรหัสใหม่", "reg_otp")]
    if "email" in email_codes.methods(employee):
        buttons.append(turn.button("พิมพ์อีเมลเอง", "reg_typed"))
    buttons.append(turn.button("ใช้วิธีอื่น", "reg_other"))
    turn.say(
        "ส่งรหัส 6 หลักไปที่อีเมลของคุณในทะเบียนพนักงานแล้ว "
        f"ใช้ได้ {options.get(options.CODE_MINUTES)} นาที\nพิมพ์รหัสที่นี่",
        buttons,
    )


def _check_code(turn, data, text):
    from operations import email_codes, employees

    employee = _trying(data)
    if employee is None:
        return _other_path(turn, data)
    ok, reason, left = email_codes.check(turn.identity, employee, text)
    if ok:
        return _employee_proved(turn, data, employee, "otp")
    if employees.failed_today(turn.identity) >= employees.MAX_FAILED:
        turn.say("ลองครบจำนวนครั้งของวันนี้แล้ว จะให้เจ้าหน้าที่ตรวจแทน")
        return _other_path(turn, data)
    if reason == "wrong":
        turn.say(f"รหัสไม่ถูก ลองได้อีก {left} ครั้ง")
        return
    _ways(turn, data, "รหัสนี้ใช้ไม่ได้แล้ว ขอรหัสใหม่หรือเลือกวิธีอื่น")


def _ask_email(turn, data):
    from operations import email_codes

    employee = _trying(data)
    if employee is None or "email" not in email_codes.methods(employee):
        return _ways(turn, data, "เลือกวิธียืนยันจากปุ่มด้านล่าง")
    turn.set_prompt("reg_email", **data)
    buttons = []
    if "otp" in email_codes.methods(employee):
        buttons.append(turn.button("ส่งรหัสทางอีเมล", "reg_otp"))
    buttons.append(turn.button("ใช้วิธีอื่น", "reg_other"))
    turn.say("พิมพ์อีเมลของคุณที่อยู่ในทะเบียนพนักงาน", buttons)


def _check_email(turn, data, text):
    import hmac

    from operations import roster

    employee = _trying(data)
    if employee is None:
        return _other_path(turn, data)
    if employee.email_hash and hmac.compare_digest(employee.email_hash, roster.email_digest(text)):
        identity_services.audit(
            None,
            "identity.employee_check_passed",
            turn.identity,
            actor_label="line",
            employee=employee.employee_id,
            method="email",
        )
        return _employee_proved(turn, data, employee, "email")
    identity_services.audit(
        None, "identity.employee_check_failed", turn.identity, actor_label="line"
    )
    return _failed(turn, data, "", "อีเมลไม่ตรงทะเบียนพนักงาน", _ask_email)


def _failed(turn, data, reason, message, again):
    from operations import employees

    left = employees.MAX_FAILED - employees.failed_today(turn.identity)
    if reason == "locked" or left <= 0:
        turn.say("ลองครบจำนวนครั้งของวันนี้แล้ว จะให้เจ้าหน้าที่ตรวจแทน")
        return _other_path(turn, data)
    turn.say(f"{message} ลองได้อีก {left} ครั้ง")
    again(turn, data)


def _employee_proved(turn, data, employee, method):
    """The employee ID stands for the name (the roster has none, 23 Oct 2569) and the unit
    comes from the roster; the unit is asked only when the roster gives none."""
    from operations import employees

    data = {k: v for k, v in data.items() if k not in ("employee_try", "employee_id", "name")}
    data.update(checked=True, employee=employee.pk, method=method)
    name = employees.label(employee)
    if employee.district_id and employee.district.is_active:
        return _registration_next(turn, {**data, "name": name, "district": employee.district.code})
    return _ask_district(turn, name, **data)


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


def _ask_district(turn, name, **data):
    """The district list, with 'แก้ชื่อ' in case the name was typed wrong. A proved employee
    typed no name (the employee ID stands for it), so there is nothing to correct."""
    turn.set_prompt("reg_district", **data, name=name)
    districts = District.objects.filter(is_active=True).order_by("code")
    employee = bool(data.get("employee"))
    turn.say(f"{name if employee else f'ชื่อ: {name}'}\nเลือกหน่วยงาน (เขต/ฝ่าย)การไฟฟ้าที่คุณสังกัด")
    message = button_list(
        "เลือกหน่วยงาน (เขต/ฝ่าย)การไฟฟ้า",
        [turn.button(d.name, "reg_district", {"code": d.code}) for d in districts],
    )
    if not employee:
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
    method = data.get("method", "proof" if employee is not None else "")
    registration = identity_services.submit_registration(
        LINE,
        turn.identity.external_id,
        data.get("name", ""),
        data.get("district", ""),
        employee=employee,
        location=tuple(data["location"]) if data.get("location") else None,
        verified_by=method,
    )
    turn.set_prompt()
    if employee is not None:
        from django.core.exceptions import ValidationError

        try:
            identity_services.auto_approve_registration(registration)
        except ValidationError:
            turn.requester = identity_services.external_requester(
                turn.identity, data.get("name", "")
            )
            turn.identity.refresh_from_db()
            return _menu(turn, "ยืนยันตัวตนแล้ว แต่ข้อมูลกองหรือบัญชีต้องให้แอดมินตรวจ ระหว่างรอแจ้งปัญหาได้")
        turn.identity.refresh_from_db()
        turn.requester = turn.identity.requester
        _menu(turn, "ยืนยันตัวตนแล้ว สามารถใช้งานได้")
        return
    turn.requester = identity_services.external_requester(turn.identity, data.get("name", ""))
    turn.identity.refresh_from_db()
    _menu(turn, "ส่งคำขอให้แอดมินตรวจแล้ว ระหว่างรอแจ้งปัญหาหรือข้อเสนอแนะได้")


def _registration_check(turn, data, proof):
    from operations import employees

    district = District.objects.filter(code=data.get("district")).first()
    employee, reason = employees.verify(turn.identity, data.get("employee_id", ""), proof, district)
    data.pop("employee_id", None)
    if employee is not None and not data.get("name"):
        return _employee_proved(turn, data, employee, "proof")
    if employee is not None:
        return _registration_next(
            turn, {**data, "checked": True, "employee": employee.pk, "method": "proof"}
        )
    if reason == "locked":
        turn.say("ลองครบจำนวนครั้งของวันนี้แล้ว จะส่งให้เจ้าหน้าที่ตรวจแทน")
        return _other_path(turn, data)
    left = employees.MAX_FAILED - employees.failed_today(turn.identity)
    if left <= 0:
        turn.say("ข้อมูลไม่ตรงทะเบียนพนักงาน จะส่งให้เจ้าหน้าที่ตรวจแทน")
        return _other_path(turn, data)
    turn.set_prompt("reg_employee", **data)
    turn.say(
        f"ข้อมูลไม่ตรงทะเบียนพนักงาน ลองได้อีก {left} ครั้ง\nพิมพ์รหัสพนักงานใหม่ หรือกด 'ใช้วิธีอื่น'",
        [turn.button("ใช้วิธีอื่น", "reg_other")],
    )


def _start_anonymous(turn):
    """แจ้งแบบไม่ระบุตัวตน: a problem or suggestion draft that ends with a location."""
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
    from operations import case_chat  # local: case_chat builds on this module (ADR 0024)

    chat = case_chat.chatting(turn)  # read before held messages open a new chat
    case_chat.deliver(turn)
    from operations import koj_chat  # local: koj_chat builds on this module

    koj_chat.deliver_updates(turn)  # กอจ. jobs followed (owner, 28 Oct 2569: no push)
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
    if command in MENU_WORDS and command not in turn.conversation.reply_choices.get("actions", {}):
        return _static_postback(turn, MENU_WORDS[command], draft)
    if text is not None and turn.conversation.prompt.startswith("koj_"):
        from operations import koj_chat

        return koj_chat.on_text(turn, text)
    in_chat = chat is not None and turn.conversation.prompt == "case_chat"
    if text is not None and in_chat and not CASE_NUMBER.fullmatch((command or "").upper()):
        return case_chat.on_text(turn, chat, text, message)
    if text is not None and turn.conversation.prompt in ("track_slip", "track_meter"):
        from operations import case_search  # local: case_search builds on this module

        return case_search.on_text(turn, text)
    if text is not None and turn.conversation.prompt == "receipt":
        from operations import receipts  # local: receipts builds on this module (M07)

        return receipts.on_text(turn, text)
    if text is not None and turn.conversation.prompt in {"dispute", "reopen"}:
        from operations import problem_chat  # local: problem_chat builds on services (M09)

        return problem_chat.on_text(turn, text)
    if text is not None and draft is None and turn.conversation.prompt == "equipment_name":
        from operations import ai_chat  # local: ai_chat builds on this module

        # Q8: a whole request typed at the name prompt goes to the AI; a name is a name.
        if not (
            reads_as(text) == "request" and turn.allow_agent and ai_chat.wants_ai(turn, None, text)
        ):
            return _equipment_named(turn, text)
        turn.set_prompt()
    if command and CASE_NUMBER.fullmatch(command.upper()) and not _expects_text(draft):
        return _case_detail(turn, command.upper())
    if text is not None and turn.conversation.prompt in ("", "info_reply", "info_target"):
        # "ตอบกลับในแชตนี้ได้เลย": an answer to staff who asked for more, never a new draft.
        waiting = _awaiting_info(turn, draft)
        if waiting:
            return _info_reply(turn, waiting, message, text)
    return _free_text(turn, draft, text, message)


def _free_text(turn, draft, text, message=None):
    """Agent first when enabled; otherwise approved answers, extraction and the current step."""
    from operations import flow_runner  # local: flow_runner builds on this module

    if text is not None and flow_runner.is_flow(draft) and turn.conversation.prompt != "track":
        # ADR 0023 Q44: a published flow asks step by step with its own rules, without AI.
        return flow_runner.on_text(turn, draft, text)
    if (
        text is not None
        and draft is None
        and not turn.conversation.prompt
        and flow_runner.sheet_hit(turn, text)
    ):
        return None  # ADR 0029: a piece in a repair sheet, answered from the sheet
    if text is not None and turn.conversation.prompt != "track":
        from operations import agent_chat, ai_chat, answers  # local: build on this module

        use_ai = turn.allow_agent and ai_chat.wants_ai(turn, draft, text)
        if use_ai and agent_chat.enabled():
            return ai_chat.queue(turn, draft, text, message)
        found = answers.keyword_answer(turn, draft, text)  # คลังคำตอบ ก (ADR 0012)
        if found is not None:
            return answers.respond(turn, draft, found)
        if use_ai:
            return ai_chat.queue(turn, draft, text, message)
    if turn.conversation.prompt == "track" and text and _sap_cases(turn, text):
        return
    if draft is not None and text is not None:
        return _draft_text(turn, draft, text)
    if turn.conversation.prompt == "track" and text:
        turn.say("ไม่พบเรื่องที่มีเลขนี้ในเรื่องของคุณ พิมพ์เลขเรื่อง เช่น W69-000001 หรือเลข SAP")
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
    changed = (
        services.visible_cases(_actor(turn))
        .filter(Exists(status_changed))
        .order_by("-updated_at")[:10]
    )
    lines, opening = [], _opening_case(turn)
    for case in changed:
        if case.number == opening:
            continue  # its card shows the step and the notice in this same reply
        if not _news(case, since):
            continue  # ADR 0028: a move inside the same step is not news for the requester
        if len(lines) == 3:
            break
        line = f"{case.number} · {requester_steps.label(case)}"
        if case.status == Case.Status.AWAITING_INFO:
            asked = case.decisions.filter(action="request_info").order_by("-id").first()
            if asked is not None:
                line += f"\nเจ้าหน้าที่ขอข้อมูล: {asked.reason}"
        else:
            latest = next(iter(requester_steps.notices(case)), None)
            if latest is not None and latest[0].created_at > since:
                line += f"\nข้อความจากเจ้าหน้าที่: {latest[1]}"
        lines.append(line)
    if lines:
        turn.say("มีอัปเดตใหม่\n" + "\n".join(lines))


def _news(case, since):
    """The requester's step changed since they last looked, or staff wrote them a notice."""
    moves = list(
        case.decisions.filter(created_at__gt=since)
        .exclude(from_status=F("to_status"))
        .order_by("id")
    )
    if any(isinstance(d.data, dict) and d.data.get("notice") for d in moves):
        return True
    return bool(moves) and (
        requester_steps.key(moves[0].from_status) != requester_steps.key(case.status)
    )


def _opening_case(turn):
    """The case number this turn opens with its card (a tapped or typed number), else ''."""
    event = getattr(turn, "event", None) or {}
    data = (event.get("postback") or {}).get("data", "")
    if data.startswith("c="):
        return data[2:].upper()
    text = ((event.get("message") or {}).get("text") or "").strip().upper()
    return text if CASE_NUMBER.fullmatch(text) else ""


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
    if turn.conversation.prompt == "info_target":
        return list(cases.filter(pk=turn.conversation.prompt_data.get("case")))
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
            f"ต้องการใช้ข้อความนี้ตอบข้อมูลที่เจ้าหน้าที่ขอสำหรับเรื่อง {case.number} หรือไม่{asked}",
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
    turn.say(f"{head}กรุณาแจ้งข้อมูลในแชทนี้")


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
        "เจ้าหน้าที่จะตรวจแล้วดำเนินการต่อ สามารถแนบรูปภาพหรือไฟล์เพิ่มเติมได้",
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
        turn.say("เรื่องนี้ไม่ได้รอข้อมูลจากคุณแล้ว หรือส่งข้อความนี้ไปแล้ว")
        return
    return _attach_reply(turn, case, message)


def _expects_text(draft):
    """Steps whose answer is typed; elsewhere a typed case number means 'show this case'."""
    if draft is None:
        return False
    if draft.step == "flow":
        from operations import flow_runner  # local: flow_runner builds on this module

        return flow_runner.expects_text(draft)
    return draft.step in {
        "item_search",
        "quantity",
        "serials",
        "sap",
        "entry",
        "meter",
        "district",
    } or (draft.step.startswith("field:"))


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
    if found == "request" and draft.step in ("quantity", "serials", "sap", "entry"):
        return ""
    return found


def _static_postback(turn, data, draft):
    if data.startswith("m=koj"):
        from operations import koj_chat

        return koj_chat.static(turn, data)
    if data in ("m=find_slip", "m=find_meter") and turn.requester:
        from operations import case_search  # local: case_search builds on this module

        return case_search.ask(turn, data.removeprefix("m=find_"))
    if data == "m=chat_end":
        from operations import case_chat  # local: case_chat builds on this module

        return case_chat.end(turn)
    if data == "m=register":
        return _ask_kind(turn, "ลงทะเบียนหรือยืนยันตัวตน")
    if data == "m=problems":
        return _general_draft(turn)  # ADR 0030 Q14: the division carousel, typed or tapped
    if data in {"m=equipment&kind=withdrawal", "m=equipment&kind=return"}:
        if turn.internal or any(
            getattr(turn.requester, flag, False)
            for flag in ("is_internal", "is_external", "is_anonymous")
        ):
            return _menu(turn, "กรุณายืนยันตัวตนพนักงานก่อนเบิกหรือส่งคืนอุปกรณ์")
        kind = K.WITHDRAWAL if data.endswith("=withdrawal") else K.RETURN
        return _equipment_draft(turn, kind)
    if data == "m=problem&topic=equipment":
        return _equipment_draft(turn, K.PROBLEM)
    if data == "m=problem&topic=suggestion":
        draft = _new_draft(turn)
        _touch(draft, turn.now, kind=K.PROBLEM, data=dict(draft.data, topic="suggestion"))
        return _set(turn, draft, _next_field(draft))
    if data in {"m=problem&topic=system", "m=problem&topic=usage"}:
        choices = LineMenuItem.objects.filter(
            place=LineMenuItem.Place.CHOICE,
            topic=data.rsplit("=", 1)[1],
            action=LineMenuItem.Action.PROBLEM,
            is_active=True,
            parent__is_active=True,
        ).order_by("position", "pk")
        return turn.say(
            "เลือกหัวข้อที่ต้องการแจ้ง",
            [(item.label, f"m=item&id={item.pk}") for item in choices[:10]]
            + [("ย้อนกลับ", "m=problems")],
        )
    if data in {"m=home", "m=roles"}:
        turn.set_prompt()
        return _menu(turn, "เลือกบริการจากเมนูด้านล่าง")
    if data.startswith("m=item&id="):
        return _menu_item(turn, data.split("=", 2)[2])
    if data.startswith("m=go&id="):
        return _menu_item(turn, data.split("=", 2)[2], go=True)
    if data.startswith("m=act&"):
        return _menu_act(turn, data)
    if data.startswith("m=ask&id="):
        return _menu_ask(turn, data.split("=", 2)[2])
    if data == "m=new":
        return _start_new(turn)
    if data == "m=mine":
        return _my_cases(turn)
    if data == "m=track":
        return _track_prompt(turn)
    if data.startswith("c="):
        return _case_detail(turn, data[2:])
    return _reprompt(turn, draft)


def _general_draft(turn):
    """ADR 0030: a problem or suggestion that starts at the division carousel."""
    turn.set_prompt()
    draft = _new_draft(turn)
    keep = {"anonymous": True} if draft.data.get("anonymous") else {}
    _touch(draft, turn.now, kind=K.PROBLEM, step="topic", data={"lines": [], "fields": {}, **keep})
    return _ask(turn, draft)


def _pick_division(turn, draft, code):
    """ADR 0030: the division tapped (or ปัญหาการใช้งานหรืออื่นๆ for the admins), then the
    details; a change from the summary goes back to the summary."""
    if code not in dict(problem_pick.choices()):
        return _ask(turn, draft)
    data = draft.data
    keep = {k: data[k] for k in ("anonymous", "location", "menu_label") if data.get(k)}
    fields = data["fields"] if data.get("topic") == Case.Topic.GENERAL else {}
    held = code == problem_pick.ADMIN
    draft.data = {
        **keep,
        "lines": [],
        "fields": fields,
        "topic": Case.Topic.GENERAL,
        "division": "" if held else code,
        **({"hold_for_admin": True} if held else {}),
    }
    return _set(turn, draft, _next_field(draft))


def _equipment_draft(turn, kind):
    draft = _new_draft(turn)
    _touch(draft, turn.now, kind=kind)
    return _set(
        turn,
        draft,
        "meter_group",
        equipment_menu="equipment",
        **({"topic": "other"} if kind == K.PROBLEM else {}),
    )


def _menu_entry(pk):
    if not str(pk).isdigit():
        return None
    return (
        LineMenuItem.objects.filter(pk=int(pk), is_active=True)
        .filter(Q(parent__isnull=True) | Q(parent__is_active=True, parent__action="topic"))
        .select_related("parent", "division")
        .first()
    )


def _menu_label(item):
    """The menu path, tile · choice; a choice whose label was shortened keeps its full name as
    its note."""
    return f"{item.parent.label} · {item.note or item.label}" if item.parent_id else item.label


def _held(turn):
    """The open draft the AI has been filling with the requester, when it holds something
    (ADR 0022 Q16): a menu tap asks before starting another."""
    from operations import agent_intake  # local: it reads AI versions

    draft = turn.conversation.active_draft
    if draft is None or draft.status != D.OPEN or not draft.data.get("ai_run_id"):
        return None
    data = draft.data
    if not any(data.get(k) for k in ("lines", "fields", "meters", "sap_number", "entry")):
        return None
    return draft if agent_intake.active() else None


def _menu_item(turn, pk, go=False):
    """A tap on the rich menu or on one of a topic's choices (15 ต.ค. 2569 Q17–Q20).
    `go`: the requester chose to leave the draft the AI was filling and follow this tap."""
    item = _menu_entry(pk)
    if item is None:
        return _menu(turn, "เมนูนี้ปิดแล้ว เลือกเมนูอื่นได้")
    if item.tab not in {LineMenuItem.Tab.REPORT, LineMenuItem.Tab.GUEST}:
        return _menu(turn, "เมนูนี้สำหรับเจ้าหน้าที่ที่เชื่อม LINE กับบัญชีแล้ว")
    action = item.action
    A = LineMenuItem.Action
    held = None if go else _held(turn)
    if held is not None and action not in {A.MINE, A.FIND, A.TRACK, A.REGISTER}:
        what = _kind_label(held.kind, held.data.get("topic")) if held.kind else ""
        turn.say(
            f"มีร่าง{what}ที่ยังไม่ส่งค้างอยู่ จะทำต่อไหม",
            [
                turn.button("ทำต่อ", "select_draft", {"id": str(held.pk)}),
                ("เริ่มตามเมนูที่กด", f"m=go&id={item.pk}"),
            ],
        )
        return
    if go and turn.conversation.active_draft_id:
        # The draft stays among the open drafts; this tap starts from the menu.
        turn.conversation.active_draft = None
        turn.conversation.save(update_fields=["active_draft", "updated_at"])
    if item.place == LineMenuItem.Place.TILE:
        from operations import (
            flow_runner,  # local: flow_runner builds on this module
            koj_chat,
        )

        published = flow_runner.published_for(item.label)
        koj = koj_chat.is_item(item)
        if koj and koj_chat.live(item) and not koj_chat.allowed(turn):
            return koj_chat.home(turn)  # says who may follow กอจ. work
        if koj and not koj_chat.live(item):
            published = None  # owner, 28 Oct 2569: the earlier card until a valid publish
        if published is not None:  # ADR 0023: the flow an admin published for this tile
            turn.set_prompt()
            return flow_runner.start(turn, published)
    choices = (
        list(item.choices.filter(is_active=True).order_by("position", "pk")[:12])
        if action == A.TOPIC
        else []
    )
    # 23 Oct 2569: a tile that asks for the equipment first also takes problem reports, so a
    # person who may only report problems opens it too (the withdrawal choices stay closed).
    equipment_first = any(c.action == A.EQUIPMENT for c in choices)
    if (
        turn.requester
        and (turn.requester.is_external or turn.requester.is_anonymous)
        and action in {A.TOPIC, A.RETURN}
        and not equipment_first
    ):
        return _menu(turn, "บัญชีนี้แจ้งได้เฉพาะปัญหา/ข้อเสนอแนะ พิมพ์ ลงทะเบียนพนักงาน เพื่อยืนยัน")
    if action == A.REGISTER:
        return _ask_kind(turn, "ลงทะเบียนหรือยืนยันตัวตน")
    if action == A.TOPIC:
        if not choices:
            return _menu(turn, "หัวข้อนี้ยังไม่มีเรื่องให้เลือก")
        turn.set_prompt()
        if equipment_first:
            equipment = [c for c in choices if c.action == A.EQUIPMENT]
            if len(equipment) == 1 and equipment[0].topic == "other":
                return _ask_equipment_name(turn, item, equipment[0], choices)
            return _card(
                turn,
                item.label,
                "เลือกอุปกรณ์",
                [(c.label, f"m=item&id={c.pk}") for c in choices],
            )
        turn.say(
            f"{item.label}\nเลือกเรื่องที่ต้องการ",
            [(c.label, f"m=item&id={c.pk}", c.note or c.label) for c in choices],
        )
        return
    if action == A.EQUIPMENT:
        from operations import koj_chat

        if koj_chat.is_item(item) and koj_chat.live(item):
            return koj_chat.services(turn, item.topic)
        if item.topic not in TYPED_GROUPS:
            return _menu(turn, "เมนูนี้ปิดแล้ว เลือกเมนูอื่นได้")
        if _problems_only(turn):
            turn.say("บัญชีนี้แจ้งได้เฉพาะปัญหา/ข้อเสนอแนะ")
            return _typed_draft(turn, item, K.PROBLEM)
        return _action_card(turn, item)
    if action == A.NEW:
        if item.kind == K.WITHDRAWAL:
            if (
                turn.internal
                or turn.requester.is_internal
                or turn.requester.is_external
                or turn.requester.is_anonymous
            ):
                return _menu(turn, "ต้องยืนยันตัวตนพนักงานก่อนเบิกของ")
            draft = _menu_draft(turn, item, K.WITHDRAWAL, "category")
            if item.topic in CATEGORY_LABELS:
                # All equipment services use the same four LINE groups.  The service
                # division remains on the draft and the selected item keeps its registry type.
                return _set(turn, draft, "meter_group", equipment_menu="equipment")
            return _ask(turn, draft)
        return _start_new(turn)
    if action == A.MINE:
        return _my_cases(turn)
    if action == A.FIND:
        return _track_prompt(turn)
    if action == A.TRACK:
        return _menu_track(turn, item)
    only_problems = turn.internal or (
        getattr(turn.requester, "is_internal", False)
        or getattr(turn.requester, "is_external", False)
        or getattr(turn.requester, "is_anonymous", False)
    )
    if action == A.RETURN:
        if only_problems:
            return _menu(turn, "บัญชีเจ้าหน้าที่แจ้งได้เฉพาะปัญหาและข้อเสนอแนะ")
        draft = _menu_draft(turn, item, K.RETURN, "category")
        if item.topic in CATEGORY_LABELS:
            return _set(turn, draft, "meter_group", equipment_menu="equipment")
        return _ask(turn, draft)
    if action == A.PROBLEM:
        topic = item.topic or Case.Topic.OTHER
        draft = _menu_draft(turn, item, K.PROBLEM, "topic", topic=topic)
        if topic == Case.Topic.USAGE:
            return _set(turn, draft, "meter", meters=[])  # 25 Oct 2569 Q11
        if topic in CATEGORY_LABELS:
            return _set(turn, draft, "meter_group", equipment_menu="equipment")
        return _set(turn, draft, _next_field(draft))
    # GENERAL: a problem or suggestion; the rules choose the division.
    return _ask(turn, _menu_draft(turn, item, K.PROBLEM, "topic", division=""))


def _problems_only(turn):
    """A staff-only LINE, an internal or outside reporter, someone waiting for approval, or an
    anonymous reporter: problems and suggestions only."""
    return bool(
        turn.internal
        or getattr(turn.requester, "is_internal", False)
        or getattr(turn.requester, "is_external", False)
        or getattr(turn.requester, "is_anonymous", False)
    )


def _card(turn, title, prompt, buttons, quick=()):
    """A choice of the menu as one message with its choices as quick replies (25 Oct 2569 Q1:
    the owner wants the "ปุ่ม bubble" instead of the Flex card of 23 Oct): the title and question
    as text, the choices first, then the step's navigation. LINE shows at most 13 quick replies
    with labels of up to 20 characters; the full name goes into the chat as displayText. Labels
    typed instead of tapped still work (_remember_choices reads every postback)."""
    turn.say(f"{title}\n{prompt}", [*buttons, *quick][:MAX_QUICK])


REPAIR = Case.Topic.REPAIR
ACTION_CARD = (
    ("ติดตามใบเบิก", K.WITHDRAWAL),
    ("ติดตามใบส่งคืน", K.RETURN),
    ("ติดตามสถานะการซ่อม", REPAIR),  # 24 Oct 2569: in place of แจ้งปัญหา
)
# Cards already in people's chats still carry แจ้งปัญหา, and problem-only accounts use it.
ACTION_LABELS = {**{kind: label for label, kind in ACTION_CARD}, K.PROBLEM: "แจ้งปัญหา"}


def _ask_equipment_name(turn, tile, choice, choices):
    """25 Oct 2569 Q6: a tile whose one equipment choice is named by the requester (กคอ.) asks
    for the name at once; its other choices (ติดตามเรื่อง) stay as quick replies."""
    turn.set_prompt("equipment_name", item=choice.pk)
    turn.say(
        f"{tile.label}\nพิมพ์ชื่ออุปกรณ์ เช่น {EXAMPLES['name']}",
        [(c.label, f"m=item&id={c.pk}") for c in choices if c.pk != choice.pk],
    )


def _equipment_named(turn, text):
    item = _menu_entry(turn.conversation.prompt_data.get("item"))
    if item is None or item.action != LineMenuItem.Action.EQUIPMENT:
        turn.set_prompt()
        return _menu(turn, "เมนูนี้ปิดแล้ว เลือกเมนูอื่นได้")
    name = " ".join(text.split())
    limit = typed_lines.LIMITS["name"]
    if len(name) > limit:
        turn.say(f"ชื่ออุปกรณ์พิมพ์ไม่เกิน {limit} ตัวอักษร กรุณาพิมพ์ใหม่")
        return
    if _problems_only(turn):
        turn.say("บัญชีนี้แจ้งได้เฉพาะปัญหา/ข้อเสนอแนะ")
        return _typed_draft(turn, item, K.PROBLEM, name=name)
    return _action_card(turn, item, name=name)


def _action_card(turn, item, name=""):
    """The second choice of an equipment-first tile: what to do with the chosen equipment (or,
    on the กคอ. tile, the equipment just named, kept with the prompt until a choice)."""
    if name:
        turn.set_prompt("equipment_name", item=item.pk, name=name)
    else:
        turn.set_prompt()
    return _card(
        turn,
        f"{item.parent.label} · {name}" if name and item.parent_id else _menu_label(item),
        "เลือกสิ่งที่ต้องการ",
        [(label, f"m=act&id={item.pk}&do={kind}") for label, kind in ACTION_CARD],
        [("ย้อนกลับ", f"m=item&id={item.parent_id}")],
    )


def _menu_act(turn, data):
    """ติดตามใบเบิก / ติดตามใบส่งคืน / ติดตามสถานะการซ่อม on the chosen equipment
    (m=act&id=…&do=…)."""
    from urllib.parse import parse_qs

    query = parse_qs(data[2:])
    item = _menu_entry((query.get("id") or [""])[0])
    kind = (query.get("do") or [""])[0]
    if item is None or item.action != LineMenuItem.Action.EQUIPMENT:
        return _menu(turn, "เมนูนี้ปิดแล้ว เลือกเมนูอื่นได้")
    if kind not in ACTION_LABELS:
        return _action_card(turn, item)
    from operations import koj_chat

    if koj_chat.is_item(item) and koj_chat.live(item):
        if item.topic not in {"transformer", "lbs", "rmu", "other"}:
            return koj_chat.home(turn)
        if kind not in {K.WITHDRAWAL, K.RETURN, REPAIR} or (
            kind == REPAIR and item.topic not in {"lbs", "rmu"}
        ):
            return koj_chat.services(turn, item.topic)
        return koj_chat.ask(turn, item.topic, kind)
    if kind in (K.WITHDRAWAL, K.RETURN) and _problems_only(turn):
        return _menu(turn, "ต้องยืนยันตัวตนพนักงานก่อนเบิกหรือส่งคืนอุปกรณ์")
    prompt = turn.conversation.prompt_data if turn.conversation.prompt == "equipment_name" else {}
    name = prompt.get("name", "") if prompt.get("item") == item.pk else ""
    return _typed_draft(turn, item, kind, name=name)


def _typed_draft(turn, item, kind, name=""):
    """A draft whose lines are typed (typed_lines): the SAP number first for a withdrawal or
    return when the division follows SAP slips, and always for ติดตามสถานะการซ่อม (a problem
    case with the repair topic), then the first row of the chosen group. A name typed on the
    กคอ. tile fills the first row's name (25 Oct 2569 Q6)."""
    group = item.topic
    action = ACTION_LABELS[kind]
    turn.set_prompt()
    extra = {}
    if kind == REPAIR:
        kind, extra = K.PROBLEM, {"topic": Case.Topic.REPAIR}
    elif kind == K.PROBLEM:
        shared = typed_lines.shared_item(group)
        extra = {"topic": shared.category if shared else Case.Topic.OTHER}
    # "เพิ่มรายการ" offers the groups of the same tile (กคว., กอจ. or กคอ.).
    extra["groups"] = _tile_groups(item)
    if name:
        extra["prefill"] = {"name": name}
    draft = _menu_draft(turn, item, kind, "sap", typed=group, **extra)
    label = f"{item.parent.label} · {name}" if name and item.parent_id else _menu_label(item)
    draft.data = dict(draft.data, menu_label=f"{label} · {action}")
    if extra.get("topic") == Case.Topic.REPAIR or typed_lines.needs_sap(kind, _owners(draft)):
        return _set(turn, draft, "sap")
    return _start_row(turn, draft, group)


def _tile_groups(item):
    """[[group, label]] of the equipment choices beside this one on its tile."""
    rows = LineMenuItem.objects.filter(
        parent_id=item.parent_id, action=LineMenuItem.Action.EQUIPMENT, is_active=True
    ).order_by("position", "pk")
    return [[row.topic, row.label] for row in rows if row.topic in TYPED_GROUPS] or [
        [item.topic, item.label]
    ]


def _draft_groups(draft):
    """The groups "เพิ่มรายการ" offers in a typed draft: its tile's, else every group."""
    return [tuple(g) for g in draft.data.get("groups") or []] or list(TYPED_GROUPS.items())


def _menu_draft(turn, item, kind, step, **data):
    draft = ConversationDraft.objects.create(
        identity=turn.identity,
        requester=turn.requester,
        kind=kind,
        step=step,
        data={
            "lines": [],
            "fields": {},
            "division": item.division.code if item.division_id else "",
            "menu_label": _menu_label(item),
            **({"anonymous": True} if turn.anonymous else {}),
            **data,
        },
        updated_at=turn.now,
    )
    _activate(turn, draft)
    return draft


def _menu_track(turn, item):
    return _case_list(turn, item=item)


def _menu_ask(turn, pk, lead=""):
    """A question to the division the menu named: an สอบถาม/ติดตาม case (Q18, Q35)."""
    item = _menu_entry(pk)
    if item is None:
        return _menu(turn, "เมนูนี้ปิดแล้ว เลือกเมนูอื่นได้")
    draft = _menu_draft(turn, item, K.PROBLEM, "topic", topic=Case.Topic.INQUIRY)
    if lead:
        turn.say(lead)
    return _set(turn, draft, _next_field(draft))


def _menu(turn, text):
    from operations import staff_line

    extra = [("งานของกอง", "งานของกอง")] if staff_line.staff_of(turn.identity) else []
    # 25 Oct 2569 Q2: the service quick replies are gone (the rich menu is the menu); what
    # stays is what belongs to this person now: a draft to finish, the staff menu.
    buttons = []
    if turn.requester and _open_drafts(turn).exists():
        buttons.append(("ร่างที่ยังไม่ส่ง", "m=new"))
    if turn.requester and turn.requester.is_external:
        text += "\nพิมพ์ ลงทะเบียนพนักงาน หากต้องการยืนยันตัวตน"
    turn.say(text, [*buttons, *[turn.button(label, "staff_menu") for label, _ in extra]])


def _reprompt(turn, draft):
    """Ask the current question again (free text is kept, never guessed into a field)."""
    if draft is not None:
        return _ask(turn, draft)
    drafts = list(_open_drafts(turn)[:10])
    if drafts:
        return _choose_draft(turn, drafts)
    _menu(turn, "เลือกบริการจากเมนูด้านล่าง หรือพิมพ์เรื่องที่ต้องการแจ้งได้เลย")


# --- Status replies ---------------------------------------------------------------------------


def _actor(turn):
    return Actor.for_requester(turn.requester)


def _when(moment):
    local = timezone.localtime(moment)
    return f"{local.day:02d}/{local.month:02d}/{local.year + 543} {local:%H:%M} น."


def _status_label(case, label=None):
    label = label or case.get_status_display()
    if case.kind == K.WITHDRAWAL:
        from operations import fulfilment

        if fulfilment.summary(case)["partially_issued"]:
            label += " (จ่ายบางส่วน)"
    elif case.kind == K.RETURN:
        from operations import returns

        if returns.summary(case)["partially_returned"]:
            label += " (รับคืนบางส่วน)"
    return label


def _case_list(turn, mode="open", page=1, item=None):
    from django.core.paginator import Paginator

    modes = {"open": "เรื่องที่ยังไม่จบ", "closed": "เรื่องที่จบแล้ว", "all": "ทุกเรื่อง"}
    if mode not in modes:
        mode = "open"
    cases = services.visible_cases(_actor(turn))
    if item is not None:
        if item.division_id:
            cases = cases.filter(owner_division_id=item.division_id)
        if item.kind:
            cases = cases.filter(kind=item.kind)
    if mode == "open":
        cases = cases.exclude(status__in=workflow.TERMINAL)
    elif mode == "closed":
        cases = cases.filter(status__in=workflow.TERMINAL)
    rows = Paginator(cases.order_by("-submitted_at", "-pk"), 5).get_page(page)
    scope = {"item": item.pk} if item else {}

    def link(label, selected=mode, number=1):
        return turn.button(label, "case_list", {**scope, "mode": selected, "page": number})

    title = _menu_label(item) if item else "เรื่องของฉัน"
    lines = [
        " · ".join(
            (c.number, _kind_label(c.kind, c.topic), _status_label(c, requester_steps.label(c)))
        )
        for c in rows
    ]
    text = f"{title} · {modes[mode]} {rows.paginator.count} เรื่อง\n"
    text += "\n".join(lines) if lines else "ยังไม่มีเรื่องในรายการนี้"
    text += f"\nหน้า {rows.number}/{rows.paginator.num_pages} · กดเลขเรื่องเพื่อดูรายละเอียด"
    quick = [(c.number, f"c={c.number}") for c in rows]
    if rows.has_previous():
        quick.append(link("ย้อนกลับ", number=rows.previous_page_number()))
    if rows.has_next():
        quick.append(link("ถัดไป", number=rows.next_page_number()))
    quick += [link(label, selected=key) for key, label in modes.items() if key != mode]
    if item is None and turn.requester and not turn.requester.is_anonymous:
        from operations import case_search  # local: case_search builds on this module

        # grill กคว. 27 Oct 2569 Q7: by slip number (its start is enough) or meter number.
        quick += [(label, f"m=find_{by}") for by, label in case_search.LABELS.items()]
    quick.append(turn.button("ค้นหาเลขเรื่อง", "case_search"))
    if item:
        quick.append(("ถามเรื่องอื่น", f"m=ask&id={item.pk}"))
    elif _open_drafts(turn).exists():
        quick.append(("ร่างที่ยังไม่ส่ง", "m=new"))
    turn.set_prompt()
    turn.say(text, quick)


def _my_cases(turn):
    return _case_list(turn)


def _track_prompt(turn):
    return _case_list(turn)


def _search_cases(turn):
    turn.set_prompt("track")
    turn.say("พิมพ์เลขเรื่องที่ต้องการติดตาม เช่น W69-000001 หรือเลขใบเบิก/ใบส่งคืน SAP")


def _sap_cases(turn, text):
    """ติดตามด้วยเลขใบเบิก/ใบส่งคืน SAP: shows the requester's own cases citing it; False when
    there are none (the caller says so)."""
    number, problem = typed_lines.clean_sap(text)
    if problem:
        return False
    found = list(
        services.visible_cases(_actor(turn))
        .filter(sap_number__iexact=number)
        .order_by("-submitted_at")[:5]
    )
    if len(found) == 1:
        _case_detail(turn, found[0].number)
        return True
    if found:
        turn.set_prompt()
        turn.say(
            f"เลข SAP {number} อยู่ใน {len(found)} เรื่อง กดเลขเรื่องเพื่อดูรายละเอียด",
            [(c.number, f"c={c.number}") for c in found],
        )
        return True
    return False


def _case_detail(turn, number):
    case = services.visible_cases(_actor(turn)).filter(number=number).first()
    turn.set_prompt()
    if case is None:
        # Same answer for other people's cases and numbers that do not exist.
        turn.say(f"ไม่พบเรื่อง {number} ในเรื่องของคุณ")
        return
    files = case.attachments.count()
    sap = (
        f"\n{typed_lines.sap_label(case.kind, case.topic)}: {case.sap_number}"
        if case.sap_number and typed_lines.sap_label(case.kind, case.topic)
        else ""
    )
    if case.meters:
        sap += f"\nเครื่องวัดฯ: {usage_meters.describe(case.meters)}"
    bar = requester_steps.bar(case.status)
    step = (
        f"{bar} {requester_steps.label(case)}\nสถานะในระบบ: {_status_label(case)}"
        if bar
        else f"สถานะ: {_status_label(case)}"
    )
    owner = problem_pick.ADMIN_TARGET if case.held_by_admin else case.owner_division.name
    text = (
        f"เรื่อง {case.number}\nประเภท: {_kind_label(case.kind, case.topic)}{sap}\n"
        f"{step}\nกองเจ้าภาพ: {owner}\n"
        f"อัปเดตล่าสุด: {_when(case.updated_at)}" + (f"\nไฟล์แนบ: {files} ไฟล์" if files else "")
    )
    buttons = []
    notices = requester_steps.notices(case)
    if notices and not (
        case.status == Case.Status.AWAITING_INFO and notices[0][0].action == "request_info"
    ):
        text += "\n" + _notice_text(*notices[0])
        buttons.append(turn.button("ตอบเจ้าหน้าที่", "case_reply", {"case": str(case.pk)}))
        if len(notices) > 1:
            buttons.append(turn.button("ดูข้อความก่อนหน้า", "case_notices", {"case": str(case.pk)}))
    from operations import case_chat, warehouse  # local: both build on this module

    stage_lines = warehouse.status_lines(case)
    if stage_lines:
        text += "\n" + "\n".join(stage_lines)
        keyed = warehouse.stages(case).get(warehouse.S.KEYED)
        if keyed and keyed.planned_on and not warehouse.stages(case).get(warehouse.S.ISSUED):
            # Q31: ฟข. may change how the goods come until they are issued.
            offer = warehouse.offer_buttons(case, turn.identity, keyed.planned_on)
            buttons += [tuple(b) for b in offer]
    if case_chat.M.objects.filter(case=case).exists():
        text += "\nมีการคุยกับเจ้าหน้าที่ในเรื่องนี้ พิมพ์ในแชตนี้หลังข้อความของเจ้าหน้าที่เพื่อตอบ"
    if case.kind == K.WITHDRAWAL:
        from operations import receipts  # local: receipts builds on this module (M07)

        detail = receipts.status_text(case)
        if detail:
            text += "\n" + detail
        buttons = [*receipts.pending_buttons(turn, case), *buttons]
    elif case.kind == K.RETURN:
        from operations import returns  # local: returns builds on services (M08)

        text += "\n" + returns.status_text(case)
    else:
        from operations import problem_chat  # local: problem_chat builds on services (M09)

        detail = problem_chat.status_text(case)
        if detail:
            text += "\n" + detail
        buttons = [*problem_chat.buttons(turn, case), *buttons]
    if case.status == Case.Status.AWAITING_INFO:
        text += "\nเจ้าหน้าที่ขอ: " + _info_asked(case)
        buttons += [
            turn.button(label, "info_target", {"case": str(case.pk)})
            for label in ["ตอบข้อมูลเพิ่ม", "แนบหลักฐาน"]
        ]
    turn.say(text, buttons[:MAX_QUICK])


def _notice_text(decision, text):
    """ข้อความแจ้งสถานะ as the requester reads it: who wrote it, when, and the words."""
    profile = getattr(decision.actor, "staff_profile", None) if decision.actor_id else None
    who = profile.display_name if profile else "เจ้าหน้าที่"
    return f"ข้อความจากเจ้าหน้าที่ ({_when(decision.created_at)} · {who})\n{text}"


def _case_reply(turn, case):
    """ตอบเจ้าหน้าที่ (ADR 0028 Q13): the notice was one way; this opens the case's chat."""
    from operations import case_chat  # local: case_chat builds on this module

    until = (turn.now + case_chat.OPEN_FOR).isoformat()
    turn.set_prompt("case_chat", case=str(case.pk), until=until)
    turn.say(
        f"พิมพ์ข้อความถึงเจ้าหน้าที่เรื่อง {case.number} ได้เลย ข้อความจะเข้าเรื่องนี้",
        [(case_chat.END_LABEL, case_chat.END_POSTBACK)],
    )


def _case_notices(turn, case):
    rows = requester_steps.notices(case)[:10]
    if not rows:
        return _case_detail(turn, case.number)
    turn.say(
        f"ข้อความจากเจ้าหน้าที่ เรื่อง {case.number}\n\n"
        + "\n\n".join(_notice_text(d, t) for d, t in rows),
        [(case.number, f"c={case.number}")],
    )


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
    only_problems = (
        anonymous
        or turn.internal
        or (
            getattr(turn.requester, "is_internal", False)
            or getattr(turn.requester, "is_external", False)
        )
    )
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
        buttons.append(turn.button("ยกเลิกร่างเดิม", "discard_and_new", {"id": str(drafts[0].pk)}))
    buttons.append(turn.button("เริ่มใหม่ เก็บเดิม", "new_draft"))
    turn.say(
        f"คุณมีร่างเรื่องที่ยังไม่ยื่น {len(drafts)} ร่าง\n"
        "กดร่างที่ต้องการทำต่อ ยกเลิกร่างเดิมแล้วเริ่มใหม่ หรือเริ่มใหม่โดยเก็บร่างเดิมไว้",
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
    if action.startswith("koj_"):
        from operations import koj_chat

        return koj_chat.on_token(turn, token)
    if action == "info_target":
        case = (
            services.visible_cases(_actor(turn))
            .filter(pk=token.value.get("case"), status=Case.Status.AWAITING_INFO)
            .first()
        )
        if case is None:
            return _menu(turn, "เรื่องนี้ไม่ได้รอข้อมูลจากคุณแล้ว")
        turn.set_prompt("info_target", case=str(case.pk))
        turn.say(
            f"ตอบข้อมูลเพิ่มสำหรับ {case.number}\n{_info_asked(case)}\n"
            "พิมพ์ข้อความหรือส่งรูป JPEG/PNG หรือไฟล์ PDF ในแชทนี้ได้"
        )
        return
    if action in ("case_reply", "case_notices"):
        case = services.visible_cases(_actor(turn)).filter(pk=token.value.get("case")).first()
        if case is None:
            return _menu(turn, "ไม่พบเรื่องนี้ในเรื่องของคุณ")
        return (_case_reply if action == "case_reply" else _case_notices)(turn, case)
    if action == "case_search":
        return _search_cases(turn)
    if action == "delivery":
        from operations import warehouse  # local: warehouse builds on this module

        return warehouse.on_choice(turn, token)
    if action == "flow_again":
        from operations import flow_runner  # local: flow_runner builds on this module

        return flow_runner.again(turn, token.value)  # ส่งใบถัดไป (grill กคว. รอบใหม่ Q7)
    if action == "case_list":
        item = _menu_entry(token.value["item"]) if token.value.get("item") else None
        if token.value.get("item") and item is None:
            return _menu(turn, "เมนูนี้ปิดแล้ว เลือกบริการอื่นได้")
        return _case_list(turn, token.value.get("mode", "open"), token.value.get("page", 1), item)
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
        turn.say(f"ยื่นเรื่องนี้แล้ว เลขเรื่อง {bound.case.number}")
        return
    if bound.status != D.OPEN or bound.revision != token.draft_revision:
        turn.say("ปุ่มนี้เป็นของขั้นตอนก่อนหน้า ใช้คำถามล่าสุดแทน")
        return _reprompt(turn, draft)
    if draft is None or draft.pk != bound.pk:
        _activate(turn, bound)
    if action == "flow":
        from operations import flow_runner  # local: flow_runner builds on this module

        return flow_runner.on_button(turn, bound, token.value)
    return _draft_button(turn, bound, action, token.value)


def _items(category, group=""):
    # A shared item of a typed group is never chosen from a list (typed_lines).
    items = EquipmentType.objects.filter(is_active=True, entry_group="").order_by("name")
    if group:
        matching = Q(line_group=group)
        if group == "meter":
            matching |= Q(line_group="", category="meter")
        elif group == "other":
            matching |= Q(line_group="", category__in=("transformer", "other"))
        return items.filter(matching)
    return items.filter(category=category)


def _ask(turn, draft):
    before = len(turn.replies)
    result = _ask_question(turn, draft)
    if len(turn.replies) > before:
        message = turn.replies[-1]
        items = message.setdefault("quickReply", {}).setdefault("items", [])
        labels = {item["action"]["label"] for item in items}
        navigation = []
        if draft.step.startswith("field:") and "ย้อนกลับ" not in labels:
            navigation.append(turn.button("ย้อนกลับ", "field_back", draft=draft))
        if "ยกเลิกร่าง" not in labels:
            navigation.append(turn.button("ยกเลิกร่าง", "discard", draft=draft))
        items.extend(_postback(*button) for button in navigation[: MAX_QUICK - len(items)])
    return result


def _ask_question(turn, draft):
    """Send the question for the draft's current step with fresh, revision-bound buttons."""
    step = draft.step
    data = draft.data
    b = turn.button
    if step == "flow":
        from operations import flow_runner  # local: flow_runner builds on this module

        return flow_runner.render(turn, draft)
    if step == "kind":
        turn.say(
            "ต้องการแจ้งเรื่องประเภทใด\n1. เบิก\n2. ส่งคืน\n"
            "3. แจ้งปัญหา/ข้อเสนอแนะ (ของชำรุด ใช้งานไม่ได้ ไฟแพง ลักใช้ไฟ หรือข้อเสนอแนะ)",
            [b(KIND_LABELS[k], "kind", {"kind": k}, draft) for k in KIND_ORDER]
            + [b("ยกเลิกร่าง", "discard", draft=draft)],
        )
    elif step == "topic":
        # ADR 0030: one bubble per division, then ปัญหาการใช้งานหรืออื่นๆ (no topic menu).
        turn.say(problem_pick.PROMPT)
        turn.replies.append(
            problem_pick.carousel(
                [
                    (label, b(label, "pick_division", {"division": code}, draft)[1])
                    for code, label in problem_pick.choices()
                ]
            )
        )
    elif step in {"meter_group", "category"}:
        buttons = [
            b(label, "equipment_group", {"group": group}, draft)
            for group, label in METER_GROUPS.items()
        ]
        if draft.kind == K.PROBLEM or data["lines"]:
            buttons.append(b("ไปต่อ" if data["lines"] else "ไม่ระบุอุปกรณ์", "lines_done", draft=draft))
        turn.say(
            "เลือกหมวดอุปกรณ์\n1. มิเตอร์\n2. CT\n3. VT\n4. อุปกรณ์อื่น",
            buttons,
        )
    elif step == "item":
        items = list(_items(data["line"]["category"], data["line"].get("group", "")))
        skip = _skip_model(turn, draft)
        if not items:
            turn.say(
                "ยังไม่มีรายการที่ใช้งานได้ในหมวดนี้ กรุณาติดต่อผู้ดูแลเพื่อจัดหมวดรายการอุปกรณ์ในระบบ",
                skip + [b("ย้อนกลับ", "back_category", draft=draft)],
            )
            return
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
            "ต้องการเพิ่มรายการอุปกรณ์หรือไม่",
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
    elif step == "sap":
        label = typed_lines.sap_label(draft.kind, draft.data.get("topic"))
        if draft.data.get("topic") != Case.Topic.REPAIR:
            turn.say(f"พิมพ์{label}ที่ทำไว้แล้ว")
        elif draft.data.get("sap_skipped"):
            # 25 Oct 2569 Q7: neither the SAP repair number nor a number on the piece yet.
            turn.say(f"ต้องมี{label}หรือหมายเลขอุปกรณ์อย่างน้อยหนึ่งอย่าง กรุณาพิมพ์{label}")
        else:
            turn.say(
                f"พิมพ์{label} ถ้าไม่มี กดข้ามแล้วพิมพ์หมายเลขอุปกรณ์ในขั้นถัดไป",
                [b("ข้าม", "sap_skip", draft=draft)],
            )
    elif step == "which_division":
        # 25 Oct 2569 Q10: a meter the AI could not place, a customer's (กตฟ.) or equipment of กคว.
        turn.say(
            "มิเตอร์ในข้อความนี้เป็นแบบไหน",
            [
                (*b("มิเตอร์บ้านผู้ใช้ไฟ", "which_division", {"to": "usage"}, draft), USAGE_CHOICE),
                (*b("มิเตอร์ของ กคว.", "which_division", {"to": "กคว."}, draft), KCW_CHOICE),
            ],
        )
    elif step == "usage_topic":
        turn.say(
            "เลือกหัวข้อของเรื่องการใช้ไฟฟ้า",
            [
                (*b(c.label, "usage_topic", {"id": c.pk}, draft), c.note or c.label)
                for c in _usage_topics()
            ],
        )
    elif step == "meter":
        head = draft.data.get("menu_label") or "เรื่องการใช้ไฟฟ้า"
        turn.say(f"{head}\nพิมพ์หมายเลขเครื่องวัดฯ")
    elif step == "district":
        turn.say(
            f"เครื่องวัดฯ {draft.data.get('meter', '')} ติดตั้งอยู่เขตไหน "
            f"พิมพ์ชื่อเขต เช่น {usage_meters.examples()}"
        )
    elif step == "meters_more":
        meters = draft.data.get("meters", [])
        buttons = [b("ไปต่อ", "meters_done", draft=draft)]
        if len(meters) < usage_meters.MAX_METERS:
            buttons.insert(0, b("เพิ่มเครื่องวัดฯ", "meter_add", draft=draft))
        turn.say(f"เครื่องวัดฯ ที่แจ้ง: {usage_meters.describe(meters)}", buttons)
    elif step == "entry":
        return _ask_entry(turn, draft)
    elif step == "typed_group":
        return _typed_group_card(turn, draft)
    elif step == "detail":
        return _detail_row(turn, draft)
    elif step == "summary" and data.get("topic") == Case.Topic.GENERAL:
        turn.say(  # ADR 0030 Q11, Q26: the division may still change before confirming
            _summary(draft),
            [
                b(CONFIRM_LABEL, "confirm", draft=draft, ttl=CONFIRM_TTL),
                b("เปลี่ยนกอง", "change_division", draft=draft),
                b("แก้ไข", "edit_details", draft=draft),
                b("ยกเลิกร่าง", "discard", draft=draft),
            ],
        )
    elif step == "summary":
        turn.say(
            _summary(draft),
            [
                b(CONFIRM_LABEL, "confirm", draft=draft, ttl=CONFIRM_TTL),
                b("กรอกใหม่", "restart", draft=draft),
                b("แนบหลักฐาน", "attach_help", draft=draft),
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
    lines = [f"{SUMMARY_HEAD} {_kind_label(draft.kind, data.get('topic'))}"]
    general = data.get("topic") == Case.Topic.GENERAL
    if data.get("menu_label") and not general:
        lines.append(f"หัวข้อ: {data['menu_label']}")
    if general:
        pick = problem_pick.ADMIN if data.get("hold_for_admin") else data.get("division", "")
        lines.append(f"ส่งถึง: {problem_pick.target(pick)}")
    elif data.get("division"):
        lines.append(f"ส่งถึง: {data['division']}")
    if data.get("sap_number"):
        label = typed_lines.sap_label(draft.kind, data.get("topic"))
        lines.append(f"{label}: {data['sap_number']}")
    if data.get("meters"):
        lines.append(f"เครื่องวัดฯ: {usage_meters.describe(data['meters'])}")
    items = {
        i.code: i
        for i in EquipmentType.objects.filter(code__in=[x["item_code"] for x in data["lines"]])
    }
    number = 0
    for line in data["lines"]:
        item = items.get(line["item_code"])
        if line.get("details") and typed_lines.is_shared(item):
            for row in line["details"]:
                number += 1
                typed = typed_lines.describe(row, item.unit)
                group = typed_lines.label_of(row["group"])
                lines.append(f"{number}. {group}" + (f" · {typed}" if typed else ""))
            continue
        number += 1
        text = f"{number}. {item.name if item else line['item_code']}"
        if line.get("quantity"):
            text += f" {line['quantity']} {item.unit if item else ''}".rstrip()
        if line.get("serials"):
            text += f" (หมายเลข {', '.join(line['serials'])})"
        for row in line.get("details", []):
            typed = typed_lines.describe(row)
            text += f" · {typed}" if typed else ""
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


def _topics():
    """The concise first menu for a general problem report."""
    return [*METER_GROUPS, Case.Topic.SUGGESTION]


def _topic_menu_label(topic):
    label = METER_GROUPS.get(topic)
    return label if label is not None else TOPIC_LABELS[topic]


def _returns_to_topic_menu(draft):
    """Whether Back returns to the general five-choice problem menu."""
    return draft.kind == K.PROBLEM and (
        draft.data.get("problem_topic_menu") or not draft.data.get("equipment_menu")
    )


def _kind_label(kind, topic=""):
    if topic == Case.Topic.GENERAL:
        return KIND_SHORT[K.PROBLEM]
    if topic in (Case.Topic.USAGE, Case.Topic.SUGGESTION):
        return TOPIC_LABELS[topic]
    if topic == Case.Topic.INQUIRY:
        return "สอบถาม/ติดตาม"
    if topic == Case.Topic.REPAIR:
        return "ติดตามสถานะการซ่อม"
    if topic == Case.Topic.SYSTEM:
        return "แจ้งปัญหาระบบงาน"
    return KIND_LABELS[kind]


def _fields(draft):
    topic = draft.data.get("topic")
    if topic == Case.Topic.GENERAL:
        return GENERAL_FIELDS
    if topic == Case.Topic.USAGE:
        return USAGE_FIELDS
    if topic == Case.Topic.SUGGESTION:
        return SUGGESTION_FIELDS
    if topic == Case.Topic.INQUIRY:
        return INQUIRY_FIELDS
    if topic == Case.Topic.REPAIR:
        return REPAIR_FIELDS
    if topic == Case.Topic.SYSTEM:
        return SYSTEM_FIELDS
    return FIELDS[draft.kind]


def _next_field(draft):
    filled = draft.data["fields"]
    if draft.data.get("sap_skipped") and _sap_missing(draft):
        return "sap"  # a repair skipped its SAP number but the rows name no piece (Q7)
    if draft.data.get("topic") == Case.Topic.USAGE and not draft.data.get("meters"):
        return "meter"  # 25 Oct 2569 Q11: every usage report names a meter and its district
    for name, _, _ in _fields(draft):
        if name not in filled:
            return f"field:{name}"
    if draft.data.get("anonymous") and not draft.data.get("location"):
        return "location"
    # 23 Oct 2569: a division that follows SAP slips needs the slip's number, and the same
    # typed fields as its own menu (ตัวเลือกระบบ sap_line_entry), before the summary.
    if _sap_missing(draft):
        return "sap"
    if _detail_missing(draft) is not None:
        return "detail"
    return "summary"


def _set(turn, draft, step, **data_changes):
    data = dict(draft.data)
    data.update(data_changes)
    _touch(draft, turn.now, step=step, data=data)
    return _ask(turn, draft)


def _draft_button(turn, draft, action, value):
    data = draft.data
    if action == "field_back" and draft.step.startswith("field:"):
        fields = [name for name, _, _ in _fields(draft)]
        current = draft.step.split(":", 1)[1]
        index = fields.index(current)
        if index == 0:
            return _set(
                turn,
                draft,
                "more"
                if data["lines"]
                else "topic"
                if _returns_to_topic_menu(draft)
                else "category",
            )
        previous = fields[index - 1]
        kept = {key: value for key, value in data["fields"].items() if key in fields[: index - 1]}
        return _set(turn, draft, f"field:{previous}", fields=kept)
    if action == "attach_help":
        turn.say(
            "ส่งรูป JPEG/PNG หรือไฟล์ PDF ในแชทนี้ได้ แล้วตรวจสรุปก่อนยืนยัน",
            [turn.button("กลับร่าง", "select_draft", {"id": str(draft.pk)})],
        )
        return
    if action == "discard":
        _touch(draft, turn.now, status=D.DISCARDED)
        turn.conversation.active_draft = None
        turn.conversation.save(update_fields=["active_draft", "updated_at"])
        return _menu(turn, "ยกเลิกร่างแล้ว")
    if action == "restart":
        if data.get("anonymous") or (
            getattr(draft.requester, "is_internal", False)
            or getattr(draft.requester, "is_external", False)
        ):
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
        if value["kind"] != K.PROBLEM:
            draft.data = dict(data, equipment_menu="equipment")
        _touch(draft, turn.now, kind=value["kind"], step=step)
        return _ask(turn, draft)
    if action == "pick_division" and draft.step == "topic":
        return _pick_division(turn, draft, value.get("division", ""))
    if action == "change_division" and draft.step == "summary":
        return _set(turn, draft, "topic")
    if action == "edit_details" and draft.step == "summary":
        fields = {k: v for k, v in data["fields"].items() if k != "description"}
        draft.data = dict(data, fields=fields)
        return _set(turn, draft, "field:description")
    if action == "topic":
        topic = value.get("topic")
        if topic not in _topics():
            return _ask(turn, draft)
        if topic in METER_GROUPS:
            category = "other" if topic == "other" else "meter"
            return _set(
                turn,
                draft,
                "item",
                equipment_menu="equipment",
                problem_topic_menu=True,
                topic=category,
                line={"category": category, "group": topic},
            )
        if topic in (Case.Topic.USAGE, Case.Topic.SUGGESTION):
            keep = {"anonymous": True} if data.get("anonymous") else {}
            draft.data = {"lines": [], "fields": {}, "topic": topic, **keep}
            return _set(turn, draft, _next_field(draft))
        draft.data = dict(data, topic=topic)
        return _set(turn, draft, "item", line={"category": topic})
    if action == "equipment_group" and draft.step in {"meter_group", "category"}:
        group = value.get("group")
        if group not in METER_GROUPS:
            return _ask(turn, draft)
        return _set(
            turn,
            draft,
            "item",
            line={"category": "other" if group == "other" else "meter", "group": group},
        )
    if action == "back_category":
        back = "topic" if not data["lines"] and _returns_to_topic_menu(draft) else "category"
        return _set(turn, draft, back, line={})
    if action == "item":
        return _choose_item(turn, draft, value["code"])
    if action == "skip_serials":
        return _finish_line(turn, draft, [])
    if action == "typed_group" and draft.step == "typed_group":
        if value.get("group") not in dict(_draft_groups(draft)):
            return _ask(turn, draft)
        return _start_row(turn, draft, value["group"])
    if action == "which_division" and draft.step == "which_division":
        if value.get("to") == "usage":
            draft.data = dict(data, topic=Case.Topic.USAGE, division="กตฟ.", lines=[], entry={})
            return _set(turn, draft, "usage_topic")
        draft.data = dict(data, division="กคว.", meters_pending=[])
        return _set(turn, draft, "entry" if data.get("entry") else _next_field(draft))
    if action == "usage_topic" and draft.step == "usage_topic":
        item = next((c for c in _usage_topics() if c.pk == value.get("id")), None)
        if item is None:
            return _ask(turn, draft)
        draft.data = dict(data, menu_label=_menu_label(item))
        return _next_meter(turn, draft)
    if action == "meter_add" and draft.step == "meters_more":
        return _set(turn, draft, "meter")
    if action == "meters_done" and draft.step == "meters_more":
        return _set(turn, draft, _next_field(draft))
    if action == "sap_skip" and draft.step == "sap" and data.get("topic") == Case.Topic.REPAIR:
        return _skip_sap(turn, draft)
    if action == "entry_skip" and draft.step == "entry":
        entry, fields = dict(data["entry"]), _row_fields(draft)
        if entry["at"] < len(fields) and not typed_lines.required(fields[entry["at"]], draft.kind):
            entry["at"] += 1
            if entry["at"] >= len(fields):
                return _finish_row(turn, draft, entry)
            return _set(turn, draft, "entry", entry=entry)
        return _ask(turn, draft)
    if action == "entry_back" and draft.step == "entry":
        entry, fields = dict(data["entry"]), _row_fields(draft)
        at = max(entry["at"] - 1, 0)
        row = dict(entry["row"])
        if at < len(fields):
            row.pop(fields[at].key, None)
            row.get("extra", {}).pop(fields[at].key, None)
        return _set(turn, draft, "entry", entry={**entry, "at": at, "row": row})
    if action == "add_line":
        return _set(turn, draft, "typed_group" if data.get("typed") else "category", line={})
    if action == "lines_done":
        if draft.kind != K.PROBLEM and not data["lines"]:
            return _set(turn, draft, "typed_group" if data.get("typed") else "category")
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


# --- ช่องที่ผู้ใช้พิมพ์ (typed_lines, 23 Oct 2569) --------------------------------------------
# A row is typed instead of chosen: one message split by "/" or field by field. The draft holds
# the row being typed in data["entry"]: {group, line, row, keys, at}. `line` is None for a new
# line of the group's shared item (the กคว. cards); an index when a line chosen from the list
# still needs its typed details (another path into a division that follows SAP slips).

EXAMPLES = {
    "numbers": "12345678 12345679",
    "size": {
        "meter": "5(100)A",
        "ct": "100/5A",
        "vt": "11000/110V",
        "other": "2 นิ้ว",
        "transformer": "160 kVA",
        "lbs": "24kV 630A",
        "rmu": "24kV 630A",
    },
    "quantity": "2",
    "name": "คีมตัดสาย",
    "equipment_number": "BB-011",
}


def _example(field, group):
    value = EXAMPLES.get(field.key, "…")
    return value.get(group, "…") if isinstance(value, dict) else value


def _row_fields(draft):
    entry = draft.data["entry"]
    by_key = {f.key: f for f in typed_lines.fields(entry["group"], draft.kind)}
    return [by_key[key] for key in entry["keys"] if key in by_key]


def _entry_unit(draft):
    entry = draft.data["entry"]
    if entry["line"] is not None:
        code = draft.data["lines"][entry["line"]]["item_code"]
        item = EquipmentType.objects.filter(code=code).first()
    else:
        item = typed_lines.shared_item(entry["group"])
    return item.unit if item else ""


def _group_of(item):
    """The LINE group of an item chosen from the list (as _items files it)."""
    if item.line_group:
        return item.line_group
    return "meter" if item.category == EquipmentType.Category.METER else "other"


def _start_row(turn, draft, group, line=None):
    """Begin one typed row of `group`: a new line of the shared item, or (line=index) the
    typed details a line chosen from the list still lacks."""
    fields = typed_lines.fields(group, draft.kind)
    if line is not None:
        target = draft.data["lines"][line]
        known = {"quantity", "name"} | ({"numbers"} if target.get("serials") else set())
        fields = [f for f in fields if f.key not in known]
    if not fields:
        if line is None:
            turn.say("หมวดนี้ยังไม่มีช่องให้พิมพ์ กรุณาติดต่อผู้ดูแล")
            return _set(turn, draft, "more" if draft.data["lines"] else "typed_group")
        lines = [dict(x) for x in draft.data["lines"]]
        lines[line]["details"] = [{"group": group}]
        draft.data = dict(draft.data, lines=lines)
        return _set(turn, draft, _next_field(draft))
    prefill = dict(draft.data.get("prefill") or {}) if line is None else {}
    fields = [f for f in fields if f.key not in prefill]
    entry = {"group": group, "line": line, "row": prefill, "keys": [f.key for f in fields], "at": 0}
    if prefill:
        entry["prefilled"] = True
        if not fields:
            draft.data = dict(draft.data, prefill={})
            return _finish_row(turn, draft, entry)
    changes = {"entry": entry, "prefill": {}, **({"typed": group} if line is None else {})}
    return _set(turn, draft, "entry", **changes)


def _fresh(entry):
    """Nothing typed into this row yet (a name typed before the draft does not count)."""
    return entry["at"] == 0 and (not entry["row"] or entry.get("prefilled"))


def _ask_entry(turn, draft):
    entry, kind = draft.data["entry"], draft.kind
    fields = _row_fields(draft)
    if entry["at"] >= len(fields):
        return _finish_row(turn, draft, dict(entry))
    field, group = fields[entry["at"]], entry["group"]
    buttons = []
    if not typed_lines.required(field, kind):
        buttons.append(turn.button("ข้าม", "entry_skip", draft=draft))
    if entry["at"] > 0:
        buttons.append(turn.button("ย้อนกลับ", "entry_back", draft=draft))
    if entry["line"] is not None:
        code = draft.data["lines"][entry["line"]]["item_code"]
        item = EquipmentType.objects.filter(code=code).first()
        head = item.name if item else code
    else:
        head = typed_lines.label_of(group)
        if entry["row"].get("name"):
            head = entry["row"]["name"]  # typed on the กคอ. tile (25 Oct 2569 Q6)
    if _fresh(entry) and len(fields) > 1:
        labels = " / ".join(f.label for f in fields)
        example = " / ".join(_example(f, group) for f in fields)
        text = f"{head}\nพิมพ์ {labels}\nเช่น {example}\nหรือพิมพ์ทีละช่อง เริ่มจาก{field.label}"
    else:
        text = f"{head}\nพิมพ์{field.label}"
    if field.key == "numbers":
        quantity = entry["row"].get("quantity") or (
            draft.data["lines"][entry["line"]].get("quantity") if entry["line"] is not None else ""
        )
        text += f" ทั้ง {quantity} หมายเลข" if quantity else ""
        text += " คั่นแต่ละหมายเลขด้วยเว้นวรรค"
    elif field.key == "quantity":
        text += f" เป็นตัวเลข หน่วย{_entry_unit(draft)}"
    if kind == K.PROBLEM and entry["at"] == 0:
        text += "\nช่องที่ไม่ทราบ กดข้ามได้"
    turn.say(text, buttons or None)


def _assign(fields, parts):
    """[(field, value)] of a row typed in one message. The size takes any extra "/" (a CT of
    100/5A); with fewer parts than fields the rest are asked one by one."""
    if len(parts) <= len(fields):
        return list(zip(fields, parts, strict=False))
    keys = [f.key for f in fields]
    if "size" not in keys:
        return None
    at = keys.index("size")
    after = len(fields) - at - 1
    values = [*parts[:at], "/".join(parts[at : len(parts) - after])]
    if after:
        values += parts[len(parts) - after :]
    return list(zip(fields, values, strict=True))


def _entry_text(turn, draft, text):
    entry, kind = dict(draft.data["entry"]), draft.kind
    fields, row, at = _row_fields(draft), dict(draft.data["entry"]["row"]), entry["at"]
    if at >= len(fields):
        return _finish_row(turn, draft, entry)
    unit = _entry_unit(draft)
    parts = typed_lines.split(text) if _fresh(entry) and len(fields) > 1 else None
    pairs = _assign(fields, parts) if parts else None
    if pairs:
        trial = {}
        if not any(typed_lines.take(trial, f, value, kind, unit) for f, value in pairs):
            row.update(trial)
            at = len(pairs)
        else:
            pairs = None  # not a whole row after all: the text answers this field alone
    if not pairs:
        problem = typed_lines.take(row, fields[at], text, kind, unit)
        if problem:
            turn.say(problem)
            return _ask(turn, draft)
        at += 1
    entry.update(row=row, at=at)
    if at < len(fields):
        return _set(turn, draft, "entry", entry=entry)
    return _finish_row(turn, draft, entry)


def _finish_row(turn, draft, entry):
    data, kind, group = draft.data, draft.kind, entry["group"]
    lines, row, target = data["lines"], dict(entry["row"]), entry["line"]
    others = [line for i, line in enumerate(lines) if i != target]
    checked = dict(row)
    if target is not None and lines[target].get("quantity") and "numbers" in row:
        checked["quantity"] = str(int(float(lines[target]["quantity"])))
    problem = typed_lines.check(checked, kind, group, others, draft.requester)
    if problem:
        turn.say(f"{problem} กรุณาพิมพ์รายการนี้ใหม่")
        return _set(turn, draft, "entry", entry={**entry, "row": {}, "at": 0})
    if target is None:
        lines = typed_lines.add(lines, group, row, kind)
        draft.data = dict(data, lines=lines, entry={})
        if draft.data.get("menu_label"):
            draft.data["menu_label"] = typed_topic(draft.data)
        return _set(turn, draft, "more" if len(lines) < MAX_LINES else _next_field(draft))
    lines = [dict(x) for x in lines]
    lines[target]["details"] = [{"group": group, **{k: v for k, v in row.items() if v}}]
    draft.data = dict(data, lines=lines, entry={})
    return _set(turn, draft, _next_field(draft))


def typed_topic(data):
    """A typed draft's topic ("Meter, CT, VT · มิเตอร์ · ติดตามใบเบิก") names its equipment only
    while every row is of it; with rows of another group it keeps the tile and the action."""
    label = data.get("menu_label", "")
    groups = {row.get("group") for line in data.get("lines", []) for row in line.get("details", [])}
    parts = label.split(" · ")
    if len(groups) > 1 and len(parts) == 3:
        return f"{parts[0]} · {parts[2]}"
    return label


USAGE_CHOICE = "เครื่องวัดที่บ้านผู้ใช้ไฟ (กตฟ.)"
KCW_CHOICE = "อุปกรณ์มิเตอร์ของ กคว."


def _usage_topics():
    """The กตฟ. tile's report topics, as the requester sees them on the menu."""
    return list(
        LineMenuItem.objects.filter(
            place=LineMenuItem.Place.CHOICE,
            topic=Case.Topic.USAGE,
            action=LineMenuItem.Action.PROBLEM,
            is_active=True,
            parent__is_active=True,
        )
        .select_related("parent")
        .order_by("position", "pk")
    )


def _next_meter(turn, draft):
    """After the topic or a district: a meter the AI read without its district is asked next,
    then any meter at all, then the fields (25 Oct 2569 Q11)."""
    pending = list(draft.data.get("meters_pending") or [])
    if pending:
        return _set(turn, draft, "district", meter=pending[0], meters_pending=pending[1:])
    if not draft.data.get("meters"):
        return _set(turn, draft, "meter")
    return _set(turn, draft, "meters_more")


def _meter_text(turn, draft, text):
    number = usage_meters.clean_number(text)
    if not number:
        turn.say("หมายเลขเครื่องวัดฯ ใช้ตัวอักษร ตัวเลข หรือขีด 4 ถึง 20 ตัว")
        return _ask(turn, draft)
    if number in {m["number"] for m in draft.data.get("meters", [])}:
        turn.say(f"เครื่องวัดฯ {number} อยู่ในเรื่องนี้แล้ว")
        return _set(turn, draft, "meters_more")
    return _set(turn, draft, "district", meter=number)


def _district_text(turn, draft, text):
    district = usage_meters.match_district(text)
    if district is None:
        turn.say(f'ไม่พบเขต "{" ".join(text.split())[:40]}" ใน กฟน.')
        return _ask(turn, draft)
    meters = [
        *draft.data.get("meters", []),
        {"number": draft.data["meter"], "district": district.code},
    ]
    draft.data = dict(draft.data, meters=meters, meter="")
    return _next_meter(turn, draft)


def _skip_sap(turn, draft):
    """ติดตามสถานะการซ่อม without its SAP repair number: the rows must then name the piece."""
    draft.data = dict(draft.data, sap_skipped=True)
    if draft.data.get("typed") and not draft.data["lines"]:
        return _start_row(turn, draft, draft.data["typed"])
    return _set(turn, draft, _next_field(draft))


def _sap_text(turn, draft, text):
    if (
        draft.data.get("topic") == Case.Topic.REPAIR
        and not draft.data.get("sap_skipped")
        and " ".join(text.split()) in {"ไม่มี", "ไม่ทราบ", "ข้าม"}
    ):
        return _skip_sap(turn, draft)
    number, problem = typed_lines.clean_sap(text)
    if not problem:
        problem = typed_lines.sap_in_use(number, draft.requester)
    if problem:
        turn.say(problem)
        return _ask(turn, draft)
    draft.data = dict(draft.data, sap_number=number)
    if draft.data.get("typed") and not draft.data["lines"]:
        return _start_row(turn, draft, draft.data["typed"])
    return _set(turn, draft, _next_field(draft))


def _owners(draft):
    if draft.data.get("division"):
        return [draft.data["division"]]
    return services.preview_owners(_payload(draft))


def _sap_missing(draft):
    if draft.data.get("sap_number") or not typed_lines.sap_label(
        draft.kind, draft.data.get("topic")
    ):
        return False
    if draft.data.get("topic") == Case.Topic.REPAIR:
        # 25 Oct 2569 Q7: the SAP repair number, or else a number on the piece in the rows.
        return not (draft.data.get("sap_skipped") and typed_lines.identified(draft.data["lines"]))
    return typed_lines.needs_sap(draft.kind, _owners(draft))


def _detail_missing(draft):
    """The first line chosen from the list that still lacks its typed details, when the draft
    goes to a division that follows SAP slips and ตัวเลือกระบบ asks for the same fields."""
    from operations import options

    if (
        draft.data.get("typed")
        or draft.kind not in typed_lines.SAP_LABELS
        or options.get(options.SAP_LINE_ENTRY) != "typed"
        or not typed_lines.needs_sap(draft.kind, _owners(draft))
    ):
        return None
    codes = [line["item_code"] for line in draft.data["lines"]]
    items = {i.code: i for i in EquipmentType.objects.filter(code__in=codes)}
    for index, line in enumerate(draft.data["lines"]):
        item = items.get(line["item_code"])
        if item is None or line.get("details"):
            continue
        known = {"quantity", "name"} | ({"numbers"} if line.get("serials") else set())
        if any(f.key not in known for f in typed_lines.fields(_group_of(item), draft.kind)):
            return index
    return None


def _detail_row(turn, draft):
    index = _detail_missing(draft)
    if index is None:
        _touch(draft, turn.now, step=_next_field(draft))
        return _ask(turn, draft)
    code = draft.data["lines"][index]["item_code"]
    item = EquipmentType.objects.get(code=code)
    return _start_row(turn, draft, _group_of(item), line=index)


def _typed_group_card(turn, draft):
    lines = draft.data["lines"]
    buttons = [
        turn.button(label, "typed_group", {"group": group}, draft)
        for group, label in _draft_groups(draft)
    ]
    quick = [turn.button("ไปต่อ", "lines_done", draft=draft)] if lines else []
    return _card(turn, "เพิ่มรายการ", "เลือกอุปกรณ์", buttons, quick)


def _choose_item(turn, draft, code):
    item = EquipmentType.objects.filter(code=code, is_active=True).first()
    if item is None:
        return _set(turn, draft, "category", line={})
    if any(line["item_code"] == code for line in draft.data["lines"]):
        turn.say("มีรายการนี้แล้ว ถ้าต้องการเปลี่ยนจำนวนให้กดกรอกใหม่ที่หน้าสรุป")
        return _set(turn, draft, "more", line={})
    line = {"category": item.category, "item_code": code}
    if draft.kind == K.PROBLEM:
        if draft.data.get("equipment_menu") == "equipment":
            draft.data = dict(draft.data, topic=item.category)
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
    if step == "sap":
        return _sap_text(turn, draft, text)
    if step == "meter":
        return _meter_text(turn, draft, text)
    if step == "district":
        return _district_text(turn, draft, text)
    if step == "entry":
        return _entry_text(turn, draft, text)
    if step == "typed_group":
        raw = text.translate(THAI_DIGITS)
        groups = _draft_groups(draft)
        if raw.isdigit() and 1 <= int(raw) <= len(groups):
            return _start_row(turn, draft, groups[int(raw) - 1][0])
        for group, label in groups:
            if raw.casefold() == label.casefold():
                return _start_row(turn, draft, group)
    if step == "item_search":
        query = text.strip()
        matches = list(
            _items(data["line"]["category"], data["line"].get("group", ""))
            .filter(Q(name__icontains=query) | Q(code__icontains=query))
            .order_by("name")[:11]
        )
        if not matches:
            turn.say("ไม่พบรายการที่ตรงกัน กรุณาระบุคำค้นใหม่")
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
    if step == "topic":
        raw = text.translate(THAI_DIGITS).strip()
        picks = problem_pick.choices()
        if raw.isdigit() and 1 <= int(raw) <= len(picks):
            return _pick_division(turn, draft, picks[int(raw) - 1][0])
        for code, label in picks:
            if raw.casefold() in (code.casefold(), label.casefold()):
                return _pick_division(turn, draft, code)
    if step in {"meter_group", "category"}:
        raw = text.translate(THAI_DIGITS)
        groups = list(METER_GROUPS)
        if raw in {"1", "2", "3", "4"}:
            return _draft_button(turn, draft, "equipment_group", {"group": groups[int(raw) - 1]})
        for group, label in METER_GROUPS.items():
            if raw.casefold() == label.casefold():
                return _draft_button(turn, draft, "equipment_group", {"group": group})
    # Free text where a button is expected: how to answer, then the question again.
    turn.say(CHOICE_HINTS.get(step, "เลือกจากปุ่มด้านล่าง"))
    return _ask(turn, draft)


CHOICE_HINTS = {
    "kind": "เลือกจากปุ่มด้านล่าง หรือพิมพ์เลขข้อ",
    "topic": "แตะชื่อกองด้านบน หรือพิมพ์ชื่อกอง",
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
    if data.get("division"):
        payload["division_code"] = data["division"]
    elif data.get("hold_for_admin"):
        payload["hold_for_admin"] = True  # ADR 0030: ปัญหาการใช้งานหรืออื่นๆ
    if data.get("menu_label"):
        payload["menu_label"] = data["menu_label"]
    if data.get("sap_number") and typed_lines.sap_label(draft.kind, data.get("topic")):
        payload["sap_number"] = data["sap_number"]
    if data.get("meters") and data.get("topic") == Case.Topic.USAGE:
        payload["meters"] = data["meters"]
    for name, value in data["fields"].items():
        if value:
            payload[name] = value
    if data.get("meters") and not payload.get("description"):
        # Q11: the details may be skipped; the case still says what was reported.
        topic = (data.get("menu_label") or "").split(" · ")[-1] or "เรื่องการใช้ไฟฟ้า"
        payload["description"] = f"{topic}: เครื่องวัดฯ {usage_meters.describe(data['meters'])}"
    return payload


def _confirm(turn, draft):
    # A draft that reached its summary another way (before 23 Oct 2569, or filled by the AI)
    # still needs what a division that follows SAP slips asks for.
    if _sap_missing(draft):
        return _set(turn, draft, "sap")
    if _detail_missing(draft) is not None:
        return _set(turn, draft, "detail")
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
    if case.topic == Case.Topic.GENERAL:
        text = problem_pick.done_text(case)  # ADR 0030 Q26
    elif len(numbers) > 1:
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
    following = _open_drafts(turn).order_by("updated_at", "pk").first() if remaining else None
    if following is not None:
        # ADR 0022 Q15: several requests in one message are sent one at a time; the next one
        # keeps what the message gave.
        items = turn.replies[-1].setdefault("quickReply", {}).setdefault("items", [])
        items.insert(
            0, _postback(*turn.button("ทำเรื่องถัดไป", "select_draft", {"id": str(following.pk)}))
        )
        del items[MAX_QUICK:]


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
            "ได้รับไฟล์แล้ว ต้องการเริ่มเรื่องใหม่เพื่อแนบไฟล์นี้หรือไม่",
            [turn.button("เริ่มเรื่องใหม่", "new_draft")],
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
        _remember_choices(inbox, messages)
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
