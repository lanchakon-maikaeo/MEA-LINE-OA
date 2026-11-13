"""LINE mockup (before M11): the requester's journeys in LINE, recorded from the real chat code.

Each scenario runs in one transaction that is rolled back, so the demo database keeps nothing.
Synthetic webhook events go through `line.store_events` and the worker's own `handle_event`; staff
steps go through `services.perform_action`, as the staff web does; replies and planned pushes are
captured instead of sent. Nothing reaches LINE or an AI provider: the free-text scenario answers
with a fixed model output that still has to pass `ai.validate`. Every tap must find its button in
the messages before it, so a changed chat fails the build instead of showing an outdated mockup.
"""

import base64
import json
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import timedelta
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import transaction
from django.db.models import Max
from django.test.utils import override_settings
from django.utils import timezone

from operations import (
    ai,
    ai_chat,
    conversation,
    line,
    line_api,
    line_menu_catalog,
    orchestration,
    services,
    warehouse,
)
from operations import identity as identity_services
from operations.contracts import Actor
from operations.management.commands.demo_withdrawal import _user
from operations.management.commands.run_worker import handle_event
from operations.models import (
    AIRun,
    Case,
    CaseMilestone,
    ChannelIdentity,
    Division,
    LineMenuItem,
    NotificationDelivery,
    OutboxEvent,
    Role,
    RoleGrant,
    StaffProfile,
    WebhookInbox,
)

OA_NAME = "ฝอจ. รับเรื่อง"
REQUESTER = "สมใจ ทดสอบ"
DISTRICT = "D02"
TEMPLATE = Path(__file__).resolve().parent / "line_assets" / "mockup.html"
# The role menus replaced the single rich menu (f0cdae1): its words still open the same chat
# steps, and the mockup shows the user menu's picture.
MENU = dict(conversation.MENU)
IMAGE = Path(__file__).resolve().parent / "line_assets" / "user.jpg"
LINE = ChannelIdentity.Channel.LINE
S = CaseMilestone.Stage


class _Rollback(Exception):
    pass


class MockupError(Exception):
    """The chat no longer matches a scenario (a button or an answer is missing)."""


def _no_network(*args, **kwargs):
    raise MockupError("The mockup never calls LINE.")


@contextmanager
def _swap(owner, name, value):
    original = getattr(owner, name)
    setattr(owner, name, value)
    try:
        yield
    finally:
        setattr(owner, name, original)


def thai_date(day):
    return f"{day.day:02d}/{day.month:02d}/{day.year + 543}"


# --- Recording ----------------------------------------------------------------------------------


class Recorder:
    """One requester's LINE chat: synthetic events in, captured replies and pushes out."""

    def __init__(self, user_id, answers=None):
        self.user_id = user_id
        self.answers = answers or {}
        self.clock = timezone.localtime().replace(second=0, microsecond=0)
        self.timestamp = int(timezone.now().timestamp() * 1000)
        self.message_id = 10**15
        self.steps = []
        self.last = []  # the latest bot messages: quick replies live only on the last one
        self.cards = []  # flex messages stay tappable in the history
        self.sent = {}
        self.mark()

    def mark(self):
        """Ignore everything before now (setup, other activity in the demo database)."""
        self.outbox_mark = OutboxEvent.objects.aggregate(top=Max("pk"))["top"] or 0
        self.push_mark = NotificationDelivery.objects.aggregate(top=Max("pk"))["top"] or 0

    # Patched in during recording.
    def capture(self, inbox_id, messages):
        self.sent.setdefault(inbox_id, []).extend(messages)
        # As the real reply does: the token is spent, so a later staff message waits in the
        # chat for the requester's next message instead of answering this one.
        WebhookInbox.objects.filter(pk=inbox_id).update(reply_used_at=timezone.now())

    def model_answer(self, text, draft_context, version, budget_seconds=None, catalog=None):
        if text not in self.answers:
            raise MockupError(f"No fixed model answer for: {text}")
        catalog = ai._catalog() if catalog is None else catalog
        raw = json.dumps(self.answers[text], ensure_ascii=False)
        extraction = ai.validate(raw, text, catalog, draft_context)
        return extraction, [{"model": "mockup (fixed answer)", "outcome": "ok"}]

    # --- plumbing ---------------------------------------------------------------------------

    def _event(self, event_type, **extra):
        self.timestamp += 1000
        event = {
            "type": event_type,
            "mode": "active",
            "timestamp": self.timestamp,
            "webhookEventId": uuid.uuid4().hex.upper()[:26],
            "deliveryContext": {"isRedelivery": False},
            "source": {"type": "user", "userId": self.user_id},
            "replyToken": uuid.uuid4().hex,
        }
        event.update(extra)
        return event

    def _next_message_id(self):
        self.message_id += 1
        return str(self.message_id)

    def _pump(self):
        """What the worker does for Django-owned outbox events created by this scenario."""
        while True:
            pending = list(
                OutboxEvent.objects.filter(
                    pk__gt=self.outbox_mark,
                    status=OutboxEvent.Status.PENDING,
                    event_type__in=orchestration.DJANGO_TYPES,
                ).order_by("pk")
            )
            if not pending:
                return
            for event in pending:
                handle_event(event)
                OutboxEvent.objects.filter(pk=event.pk).update(status=OutboxEvent.Status.DONE)

    def _send(self, event):
        line.store_events([event])
        inbox = WebhookInbox.objects.get(webhook_event_id=event["webhookEventId"])
        self._pump()
        runs = list(AIRun.objects.filter(inbox=inbox, status=AIRun.Status.PENDING))
        for run in runs:  # n8n would call this through the internal API
            ai_chat.process(run.pk)
        self._pump()
        return self.sent.pop(inbox.pk, []), bool(runs)

    def _pushes(self):
        rows = list(NotificationDelivery.objects.filter(pk__gt=self.push_mark).order_by("pk"))
        if rows:
            self.push_mark = rows[-1].pk
        # The mockup shows each push as delivered, so the chat treats it as read too.
        NotificationDelivery.objects.filter(pk__in=[r.pk for r in rows]).update(
            status=NotificationDelivery.Status.SENT
        )
        return [
            row.message or conversation.text_message(row.text)
            for row in rows
            if row.channel == NotificationDelivery.Channel.LINE_PUSH
            and row.identity is not None
            and row.identity.external_id == self.user_id
        ]

    def _step(self, say, user=None, bot=(), *, loading=False, staff=None, note=None, later=None):
        self.clock += later if later is not None else timedelta(seconds=40)
        bot = list(bot)
        self.steps.append(
            {
                "say": say,
                "at": self.clock.isoformat(),
                "user": user,
                "bot": bot,
                "loading": loading,
                "staff": staff,
                "note": note,
            }
        )
        if bot:
            self.last = bot
            self.cards += [m for m in bot if m["type"] == "flex"]

    def _find(self, label):
        """The postback behind a visible button: the last message's quick reply or a card."""
        last = self.last[-1] if self.last else {}
        for item in (last.get("quickReply") or {}).get("items", []):
            action = item["action"]
            if label in (action["label"], action.get("displayText")):
                return action["data"], action.get("displayText"), "quick"
        for message in reversed(self.cards):
            contents = message["contents"]
            where = "carousel" if contents["type"] == "carousel" else "flex"
            for action in _actions(contents):
                if label in (action.get("label"), action.get("displayText")):
                    return action["data"], action.get("displayText"), where
        raise MockupError(f"No button '{label}' in the latest messages.")

    # --- what the requester does ------------------------------------------------------------

    def follow(self, say, note=None):
        replies, _ = self._send(self._event("follow"))
        self._step(say, {"type": "follow"}, replies, note=note)

    def type(self, text, say, note=None):
        message = {"id": self._next_message_id(), "type": "text", "text": text}
        replies, loading = self._send(self._event("message", message=message))
        self._step(say, {"type": "text", "text": text}, replies, loading=loading, note=note)

    def tap(self, label, say, note=None):
        data, display, where = self._find(label)
        replies, loading = self._send(self._event("postback", postback={"data": data}))
        user = {"type": "tap", "label": label, "where": where, "display": display}
        self._step(say, user, replies, loading=loading, note=note)

    def menu(self, label, say, note=None, data=None):
        """A rich menu tap: the fixed areas by label, or `data` for a tile of the menu catalog."""
        event = self._event("postback", postback={"data": data or MENU[label]})
        replies, loading = self._send(event)
        user = {"type": "tap", "label": label, "where": "menu", "display": label}
        self._step(say, user, replies, loading=loading, note=note)

    def photo(self, say, note=None):
        message = {
            "id": self._next_message_id(),
            "type": "image",
            "contentProvider": {"type": "line"},
        }
        replies, _ = self._send(self._event("message", message=message))
        self._step(say, {"type": "image"}, replies, note=note)

    # --- what staff do in the web -----------------------------------------------------------

    def staff(self, who, what, work, say, *, later, note=None):
        work()
        self._pump()
        self._step(
            say, None, self._pushes(), staff={"who": who, "what": what}, note=note, later=later
        )

    def wait_until(self, days, hour, minute=0):
        """A gap to a later time of day (for the next staff step)."""
        target = (self.clock + timedelta(days=days)).replace(hour=hour, minute=minute)
        return max(target - self.clock, timedelta(minutes=1))


def _actions(node):
    """Every postback in a card: its buttons, and the boxes that are tapped as a whole."""
    if isinstance(node, dict):
        action = node.get("action")
        if isinstance(action, dict) and action.get("type") == "postback":
            yield action
        for value in node.values():
            yield from _actions(value)
    elif isinstance(node, list):
        for value in node:
            yield from _actions(value)


# --- The world each scenario starts from (rolled back afterwards) ---------------------------


@dataclass
class World:
    admin: object
    requester: object = None

    def act(self, who, case, action, payload=None):
        return services.perform_action(
            Actor.staff(who),
            case.pk,
            action,
            expected_revision=Case.objects.get(pk=case.pk).revision,
            idempotency_key=f"mockup-{uuid.uuid4().hex}",
            payload=payload,
        )

    def latest_case(self):
        return Case.objects.filter(requester=self.requester).order_by("-pk").first()


def _world():
    quiet = StringIO()
    for command in (
        "load_districts",
        "seed_demo_units",
        "seed_demo_cases",
        "seed_demo_policy",
        "seed_fake_inventory",
    ):
        call_command(command, stdout=quiet)
    admin, created = get_user_model().objects.get_or_create(username="sim-mockup-admin")
    if created:
        admin.set_unusable_password()
        admin.save(update_fields=["password"])
        StaffProfile.objects.create(user=admin, display_name="แอดมินจำลอง")
    RoleGrant.objects.get_or_create(user=admin, role=Role.ADMIN, division=None)
    return World(admin)


def _registered(world, user_id):
    registration = identity_services.submit_registration(LINE, user_id, REQUESTER, DISTRICT)
    world.requester = identity_services.approve_registration(world.admin, registration)


# --- Scenarios ---------------------------------------------------------------------------------


def register(r, w):
    r.follow("ผู้แจ้งเพิ่มบัญชี LINE ของ ฝอจ. เป็นเพื่อนครั้งแรก ระบบถามว่าเป็นพนักงาน กฟน. ไหม")
    r.tap("พนักงาน กฟน.", "กด พนักงาน กฟน. ระบบขอรหัสพนักงาน")
    r.tap("ใช้วิธีอื่น", "ยืนยันด้วยรหัสพนักงานไม่ได้ กด ใช้วิธีอื่น ให้เจ้าหน้าที่ตรวจแทน")
    r.type(REQUESTER, "พิมพ์ชื่อ-นามสกุล แล้วเลือกหน่วยงานที่สังกัด")
    r.tap(
        "การไฟฟ้านครหลวงเขตคลองเตย",
        "เลือกหน่วยงานแล้ว คำขอไปรอแอดมินตรวจ ระหว่างรอแจ้งปัญหาหรือข้อเสนอแนะได้",
    )
    registration = ChannelIdentity.objects.get(channel=LINE, external_id=r.user_id)
    pending = registration.registrations.get()
    r.staff(
        "แอดมิน ฝอจ.",
        "รับรองคำขอลงทะเบียน (หน้าแอดมิน › คำขอลงทะเบียน)",
        lambda: identity_services.approve_registration(w.admin, pending),
        "แอดมินรับรองแล้ว ระบบส่งข้อความแจ้งผู้แจ้งทันที",
        later=timedelta(minutes=35),
    )
    r.menu(
        "ส่งเรื่องใหม่",
        "รับรองแล้วแจ้งปัญหาหรือข้อเสนอแนะได้ ส่วนการติดตามใบเบิกและใบส่งคืนต้องยืนยันรหัสพนักงานก่อน",
    )


def _desk(w, code, username):
    """A real กอง of ฝอจ. and its แอดมินกอง. The real structure has no กองรับเรื่องกลาง (ADR
    0008): the tile's กอง takes the case and numbers it, so the demo world's intake desk steps
    aside. Following a slip needs a verified employee; the mockup registers without the roster.
    Everything here is rolled back with the scenario."""
    if not Division.objects.filter(code=code).exists():
        raise MockupError(f"ยังไม่มีหน่วยงาน {code} โหลดโครงสร้างองค์กรก่อน")
    line_menu_catalog.sync()  # the tiles as the release sets them up
    w.requester.is_external = False
    w.requester.save(update_fields=["is_external"])
    Division.objects.filter(is_intake=True).update(is_intake=False)
    head = _user(username, [(Role.SUPERVISOR, code)])
    StaffProfile.objects.filter(user=head).update(display_name=f"เจ้าหน้าที่ {code} (จำลอง)")
    return head


def _start(head, case, priority="P3"):
    """ดำเนินการ, with the level (ADR 0025 Q25)."""
    services.perform_form_action(
        Actor.staff(head),
        case.pk,
        "approve",
        expected_revision=case.revision,
        idempotency_key=f"mockup-{uuid.uuid4().hex}",
        payload={"priority": priority},
    )


def kcw(r, w):
    """ติดตามใบเบิก at กคว.: the slip number in chat, the four warehouse stages, ยืนยันผล."""
    head = _desk(w, "กคว.", "sim-kcw")
    tile = LineMenuItem.objects.get(place="tile", label="Meter, CT, VT")
    r.menu(
        "Meter, CT, VT",
        "กดปุ่ม Meter, CT, VT บน Rich Menu ระบบให้เลือกอุปกรณ์ก่อน",
        data=f"m=item&id={tile.pk}",
    )
    r.tap("มิเตอร์", "เลือกมิเตอร์ แล้วเลือกสิ่งที่ต้องการ")
    r.tap("ติดตามใบเบิก", "เลือกติดตามใบเบิก ระบบถามว่าจะติดตามด้วยเลขอะไร")
    r.tap("เลขที่ใบเบิก", "เลือกเลขที่ใบเบิก")
    r.type("4500012345/10", "พิมพ์เลขที่ใบเบิกที่ทำไว้ใน SAP ระบบไม่รับเลขที่อยู่ในเรื่องที่ยังไม่จบ")
    r.type(
        "มิเตอร์ 5(100)A 2 เครื่อง ใช้ติดตั้งผู้ใช้ไฟรายใหม่",
        "พิมพ์ของที่เบิกในข้อความเดียว ระบบสรุปให้ตรวจก่อนยืนยัน",
    )
    r.tap("ยืนยันส่งเรื่อง", "ยืนยันแล้วได้เลขเรื่อง เรื่องส่งถึง กคว. พร้อมเลขที่ใบเบิก")
    case = w.latest_case()
    today = timezone.localdate()

    def stage(name):
        def work():
            now = Case.objects.get(pk=case.pk)
            plan = None
            if name == S.KEYED and warehouse.suggested_issue(now, today, today) is None:
                plan = today + timedelta(days=5)  # no table yet: the clerk types the pay day
            done, skipped = warehouse.record(head, [now], name, today, plan)
            if not done:
                raise MockupError(f"{case.number}: {skipped[0][1]}")

        return work

    r.staff(
        "กคว.",
        "กดดำเนินการ แล้วบันทึกรับใบเบิกในหน้างานคลัง",
        lambda: (_start(head, case), stage(S.RECEIVED)()),
        "การรับเรื่องและการรับใบเบิกเป็นงานภายใน ผู้แจ้งดูได้จากเรื่องของฉัน",
        later=timedelta(minutes=30),
    )
    r.staff(
        "กคว.",
        "บันทึกคีย์จ่าย ระบบคำนวณวันจ่ายจากตารางกำหนดจ่ายของกอง",
        stage(S.KEYED),
        "ข้อความวันจ่ายรอผู้แจ้งอยู่ในแชต ไปถึงตอนผู้แจ้งเปิดแชตครั้งถัดไป",
        later=r.wait_until(1, 9, 30),
    )
    r.menu("เรื่องของฉัน", "ผู้แจ้งเปิดแชต ได้กำหนดจ่ายพร้อมปุ่มเลือกวิธีรับของ")
    ways = [item["action"]["label"] for item in r.last[-1]["quickReply"]["items"]]
    # The truck day is the กอง's own table; a กอง without one offers มารับเอง only.
    r.tap(
        next((label for label in ways if label.startswith("รถส่ง")), "มารับเอง"),
        "เลือกวิธีรับของจากปุ่ม เจ้าหน้าที่เห็นคำตอบในเรื่อง",
    )
    r.staff(
        "กคว.",
        "บันทึกจ่ายแล้ว",
        stage(S.ISSUED),
        "ข้อความว่าจ่ายแล้วรอผู้แจ้งอยู่ในแชตเช่นกัน",
        later=r.wait_until(2, 10, 0),
    )
    r.staff(
        "กคว.",
        "บันทึกเขตได้รับแล้ว ซึ่งเป็นขั้นสุดท้าย",
        stage(S.DELIVERED),
        "เรื่องรอผู้แจ้งยืนยันผล ข้อความขั้นนี้ไปพร้อมคำตอบของข้อความถัดไปที่ผู้แจ้งพิมพ์",
        later=timedelta(hours=3),
    )
    r.menu("เรื่องของฉัน", "ผู้แจ้งเปิดแชต ได้ข้อความที่รออยู่ พร้อมปุ่มยืนยันผลและปุ่มปฏิเสธ")
    r.tap("ยืนยันผล", "ได้รับของครบแล้ว กดยืนยันผล เรื่องปิด")


TYPED = "ขอเบิกมิเตอร์ 20 เครื่อง ใช้ติดตั้งผู้ใช้ไฟรายใหม่"
ANSWERS = {
    TYPED: {
        "requests": [
            {
                "intent": "withdrawal",
                "items": [{"item_code": "MTR_SYN_A", "item_text": "มิเตอร์", "quantity": "20"}],
                "purpose": "ติดตั้งผู้ใช้ไฟรายใหม่",
            }
        ]
    },
}


def typed(r, w):
    line_menu_catalog.sync()
    w.requester.is_external = False
    w.requester.save(update_fields=["is_external"])
    r.type(
        TYPED,
        "พิมพ์ขอเบิกของในแชต ระบบบอกว่าการเบิกทำใน SAP แล้วเสนอปุ่มติดตามของอุปกรณ์ที่พิมพ์ถึง",
        note="ขั้นนี้ใช้คำตอบ AI ที่กำหนดไว้ล่วงหน้า (ไม่ได้เรียก AI จริง) แต่ผ่านการตรวจ"
        "กับทะเบียนและข้อความต้นฉบับด้วยโค้ดจริง",
    )
    r.tap("มิเตอร์", "กดปุ่มอุปกรณ์ ระบบให้เลือกติดตามใบเบิกหรือใบส่งคืน")
    r.tap("ติดตามใบเบิก", "มีเลขใบแล้วจึงติดตามต่อได้จากตรงนี้")


def problem(r, w):
    head = _desk(w, "กอจ.", "sim-koj")
    r.menu("ส่งเรื่องใหม่", "กดส่งเรื่องใหม่ ระบบถามว่าจะทำเรื่องใด")
    r.tap("แจ้งปัญหา/ข้อเสนอแนะ", "เลือกแจ้งปัญหา/ข้อเสนอแนะ แล้วเลื่อนการ์ดเลือกกอง")
    r.tap("กองอุปกรณ์การจำหน่าย", "เลือกกองที่ต้องการแจ้ง")
    r.type(
        "หม้อแปลงหน้าซอยมีควันออกและมีกลิ่นไหม้หลังฝนตก",
        "แจ้งอาการด้วยคำของตัวเอง ข้อความมีคำว่าควันและไหม้ ระบบให้คำแนะนำเร่งด่วนก่อนสรุป",
    )
    r.photo("ส่งรูปประกอบได้ ระบบตรวจไวรัสก่อนให้เจ้าหน้าที่เปิด")
    r.tap("ยืนยันส่งเรื่อง", "กดยืนยันแล้วได้เลขเรื่อง เรื่องส่งถึง กอจ.")
    case = w.latest_case()

    def resolve(text):
        return lambda: w.act(
            head, case, "resolve", {"category": "fixed", "as": "problem", "message": text}
        )

    r.menu("เรื่องของฉัน", "ผู้แจ้งเปิดดูความคืบหน้า")
    r.tap(case.number, "เรื่องถึง กอจ. แล้วโดยไม่ผ่านกองกลาง รายละเอียดบอกสถานะและกองที่รับผิดชอบ")
    r.staff(
        "เจ้าหน้าที่ กอจ.",
        "บันทึกผลการแก้ปัญหา: แก้แล้ว",
        resolve("ตัดไฟ เปลี่ยนขั้วต่อที่ไหม้ และจ่ายไฟคืนแล้ว (ข้อมูลจำลอง)"),
        "ผู้แจ้งได้การ์ดผลการแก้ปัญหา ให้กดยืนยันหรือแจ้งว่ายังไม่เรียบร้อย",
        later=timedelta(hours=2, minutes=10),
    )
    r.tap("ยังไม่เรียบร้อย", "ยังมีอาการอยู่ กด ยังไม่เรียบร้อย ระบบให้พิมพ์เหตุผล")
    r.type("ไฟยังกะพริบเป็นช่วง ๆ ตอนหัวค่ำ", "เรื่องกลับไปให้ กอจ. ทำต่อ")
    r.staff(
        "เจ้าหน้าที่ กอจ.",
        "แก้เพิ่มแล้วบันทึกผลใหม่",
        resolve("เปลี่ยนฟิวส์แรงต่ำและขันขั้วต่อใหม่ ตรวจวัดแรงดันปกติ (ข้อมูลจำลอง)"),
        "ผู้แจ้งได้การ์ดผลการแก้ปัญหาอีกครั้ง",
        later=r.wait_until(1, 11, 20),
    )
    r.tap("ยืนยัน", "ยืนยันผลแล้ว เรื่องปิด")


def slip_result(r, w):
    """ADR 0057: กอจ. answers a ติดตามใบส่งคืน with แจ้งผลให้ผู้แจ้ง; ยืนยันผล or ปฏิเสธ."""
    koj = _desk(w, "กอจ.", "sim-koj")
    found = {}

    def start():
        # Filed as the กอจ. tile files it; the tile's own chat depends on กอจ.'s lookup sheet.
        filed = services.submit_case(
            Actor.for_requester(w.requester),
            idempotency_key=f"mockup-{uuid.uuid4().hex}",
            payload={
                "kind": "return",
                "lines": [],
                "menu_label": "หม้อแปลง LBS RMU · LBS · ติดตามใบส่งคืน",
                "division_code": "กอจ.",
                "sap_number": "4917646",
                "sap_kind": "return_transfer",
                "condition": "LBS 1 ชุด ส่งใบโอนขอคืนแล้ว",
                "reason": "LBS 1 ชุด ส่งใบโอนขอคืนแล้ว",
            },
        )
        found["case"] = Case.objects.get(pk=filed.case_id)
        _start(koj, found["case"])

    def tell(text):
        return lambda: w.act(koj, found["case"], "report_result", {"message": text})

    r.staff(
        "กอจ.",
        "ผู้แจ้งติดตามใบโอนขอคืน 4917646 ทางปุ่ม หม้อแปลง LBS RMU แล้ว กอจ. กดดำเนินการ",
        start,
        "เรื่องอยู่ที่ กอจ. สถานะกำลังดำเนินการ",
        later=timedelta(minutes=20),
    )
    r.staff(
        "เจ้าหน้าที่ กอจ.",
        "กด แจ้งผลให้ผู้แจ้ง (บนเว็บหรือในการ์ด LINE ของเจ้าหน้าที่) แล้วพิมพ์รายละเอียด",
        tell("รับใบโอนขอคืนแล้ว รถของ กอจ. จะเข้าไปรับ LBS ที่เขตวันพฤหัสนี้"),
        "ผู้แจ้งได้การ์ดทันที มีข้อความที่เจ้าหน้าที่พิมพ์ ปุ่มยืนยันผล และปุ่มปฏิเสธ",
        later=timedelta(hours=1),
    )
    r.tap("ปฏิเสธ", "ยังไม่เรียบร้อย กดปฏิเสธ ระบบให้พิมพ์เหตุผล")
    r.type(
        "รถยังไม่มารับของ รบกวนแจ้งวันใหม่",
        "เรื่องกลับไปกำลังดำเนินการที่ กอจ. ผู้รับผิดชอบได้แจ้งเตือน",
    )
    r.staff(
        "เจ้าหน้าที่ กอจ.",
        "กด แจ้งผลให้ผู้แจ้ง อีกครั้ง",
        tell("รถไปรับ LBS แล้ว 13 พ.ย. รับคืนเข้าคลังเรียบร้อย"),
        "ผู้แจ้งได้การ์ดใบใหม่",
        later=r.wait_until(1, 14, 0),
    )
    r.tap("ยืนยันผล", "เรียบร้อยแล้ว กดยืนยันผล เรื่องปิด")


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    summary: str
    play: object
    registered: bool = True
    needs_ai: bool = False


def order(r, w):
    """ADR 0041: a แอดมินฝ่าย orders work in LINE on cards, picking the กอง then the person."""
    from django.utils import timezone as tz

    from operations import staff_orders_chat

    director = _user("sim-director", [])
    RoleGrant.objects.get_or_create(user=director, role=Role.DEPARTMENT_SUPERVISOR, division=None)
    StaffProfile.objects.filter(user=director).update(display_name="ผอ.จำลอง")
    eqp = Division.objects.filter(code="กอจ.").first()
    if eqp is None:
        raise MockupError("ยังไม่มีหน่วยงาน กอจ. โหลดโครงสร้างองค์กรก่อน")
    head = _user("sim-koj-head", [(Role.SUPERVISOR, eqp.code)])
    StaffProfile.objects.filter(user=head).update(display_name="สมศักดิ์", division=eqp)
    ChannelIdentity.objects.filter(channel=LINE, external_id=r.user_id).update(
        staff_user=director, staff_linked_at=tz.now()
    )
    r.type("สั่งงาน", "แอดมินฝ่ายพิมพ์ สั่งงาน ระบบเปิดการ์ดให้เลือกกอง (อื่นๆ คือแอดมินฝ่ายคนอื่น)")
    r.tap(eqp.code, "เลือกกอง ระบบแสดงแอดมินกองของกองนั้น และปุ่มสั่งทั้งกอง")
    head.refresh_from_db()
    r.tap(staff_orders_chat.who(head, eqp), "เลือกผู้รับ ระบบขอให้พิมพ์รายละเอียดงาน")
    r.type(
        "ตรวจนับอุปกรณ์คงคลังและรายงานผล",
        "พิมพ์รายละเอียด ระบบสรุปร่างเป็นการ์ด เลือกวันกำหนดได้จากปฏิทินของ LINE ในการ์ด",
    )
    r.tap("เปลี่ยนเป็นด่วน", "กดเปลี่ยนความเร่งด่วนในการ์ด การ์ดสรุปใหม่แสดงทันที")
    r.tap("ยืนยัน", "สั่งงานแล้ว ผู้รับได้ LINE พร้อมปุ่มดูงาน ไม่มีลิงก์เปิดเว็บ")


SCENARIOS = [
    Scenario(
        "register",
        "ลงทะเบียนครั้งแรก",
        "เพิ่มเพื่อน พิมพ์ชื่อ เลือกเขต แล้วรอเจ้าหน้าที่รับรอง",
        register,
        registered=False,
    ),
    Scenario(
        "kcw",
        "กคว. ติดตามใบเบิก",
        "พิมพ์เลขที่ใบเบิก กคว. บันทึกขั้นงานคลังสี่ขั้น ผู้แจ้งได้วันจ่ายแล้วกดยืนยันผล",
        kcw,
    ),
    Scenario(
        "typed",
        "พิมพ์ขอเบิกของในแชต",
        "ระบบบอกว่าการเบิกทำใน SAP แล้วพาไปติดตามใบเบิกของอุปกรณ์นั้น",
        typed,
        needs_ai=True,
    ),
    Scenario(
        "problem",
        "แจ้งปัญหาเร่งด่วน",
        "เลือกกอง พิมพ์อาการ ได้คำแนะนำทันที แจ้งว่ายังไม่เรียบร้อยครั้งแรก แล้วกดยืนยัน",
        problem,
    ),
    Scenario(
        "slip_result",
        "กอจ. แจ้งผลเรื่องติดตามใบส่งคืน",
        "เจ้าหน้าที่พิมพ์ผล ผู้แจ้งกดปฏิเสธครั้งแรก เจ้าหน้าที่แจ้งผลใหม่ แล้วผู้แจ้งกดยืนยันผล",
        slip_result,
    ),
    Scenario(
        "order",
        "แอดมินฝ่ายสั่งงาน",
        "เลือกกอง เลือกแอดมินกอง พิมพ์รายละเอียด ตั้งความเร่งด่วนในการ์ด แล้วยืนยัน",
        order,
    ),
]


def _run(index, scenario):
    user_id = f"U{index + 1:x}{'0' * 27}m0ck"  # a synthetic LINE user ID per scenario
    recorder = Recorder(user_id, ANSWERS)
    try:
        with ExitStack() as stack:
            stack.enter_context(
                override_settings(
                    OUTBOUND_ENABLED=False,
                    LINE_PUSH_ENABLED=False,
                    STAFF_EMAIL_ENABLED=False,
                    AI_ENABLED=True,
                )
            )
            for name in ("reply", "push", "call", "fetch_content"):
                stack.enter_context(_swap(line_api, name, _no_network))
            stack.enter_context(_swap(conversation, "send_reply", recorder.capture))
            stack.enter_context(_swap(ai, "extract", recorder.model_answer))
            with transaction.atomic():
                world = _world()
                if scenario.registered:
                    _registered(world, user_id)
                recorder.mark()
                scenario.play(recorder, world)
                raise _Rollback
    except _Rollback:
        pass
    except MockupError as exc:
        exc.steps = recorder.steps  # what the chat said up to the mismatch
        raise
    return {
        "key": scenario.key,
        "title": scenario.title,
        "summary": scenario.summary,
        "steps": recorder.steps,
    }


def record(keys=None):
    """Every scenario's steps (each rolled back); AI scenarios need an active AI version."""
    scenarios, skipped = [], []
    for index, scenario in enumerate(SCENARIOS):
        if keys and scenario.key not in keys:
            continue
        if scenario.needs_ai and ai.active_version() is None:
            skipped.append(scenario.key)
            continue
        scenarios.append(_run(index, scenario))
    return {
        "oa": OA_NAME,
        "requester": REQUESTER,
        "generated": timezone.localtime().isoformat(),
        "scenarios": scenarios,
        "skipped": skipped,
    }


def render(data):
    """The self-contained HTML page (data and rich menu image inline)."""
    menu = base64.b64encode(IMAGE.read_bytes()).decode()
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return (
        TEMPLATE.read_text()
        .replace("__RICH_MENU__", f"data:image/jpeg;base64,{menu}")
        .replace("__MOCKUP_DATA__", payload)
    )
