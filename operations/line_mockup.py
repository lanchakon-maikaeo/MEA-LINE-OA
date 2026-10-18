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

from operations import ai, ai_chat, conversation, line, line_api, orchestration, services
from operations import identity as identity_services
from operations.contracts import Actor, CommandFailed
from operations.management.commands.demo_withdrawal import _grant, _user
from operations.management.commands.run_worker import handle_event
from operations.management.commands.setup_line_rich_menu import AREAS, IMAGE
from operations.models import (
    AIRun,
    AuthorityGrant,
    Case,
    CaseTriage,
    ChannelIdentity,
    EquipmentType,
    FakeSerial,
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
MENU = {label: data for data, label in AREAS}
LINE = ChannelIdentity.Channel.LINE
STAFF = {
    "sim-intake": [(Role.STAFF, "SIM-TRIAGE")],
    "sim-mtr": [(Role.STAFF, "SIM-MTR")],
    "sim-mtr-issuer": [(Role.ISSUER, "SIM-MTR")],
    "sim-mtr-receiver": [(Role.RECEIVER, "SIM-MTR")],
    "sim-mtr-inspector": [(Role.INSPECTOR, "SIM-MTR")],
    "sim-eqp": [(Role.STAFF, "SIM-EQP")],
    "sim-trf": [(Role.STAFF, "SIM-TRF")],
    "sim-trf-resolver": [(Role.RESOLVER, "SIM-TRF")],
    "sim-approver": [(Role.APPROVER, "SIM-MTR"), (Role.APPROVER, "SIM-EQP")],
    "sim-allocator": [(Role.SUPERVISOR, "SIM-REG")],
}


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
                if label in (action["label"], action.get("displayText")):
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

    def menu(self, label, say, note=None):
        event = self._event("postback", postback={"data": MENU[label]})
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
    if isinstance(node, dict):
        if node.get("type") == "button":
            yield node["action"]
        for value in node.values():
            yield from _actions(value)
    elif isinstance(node, list):
        for value in node:
            yield from _actions(value)


# --- The world each scenario starts from (rolled back afterwards) ---------------------------


@dataclass
class World:
    users: dict
    admin: object
    requester: object = None

    def act(self, who, case, action, payload=None):
        actor = Actor.staff(self.users[who]) if isinstance(who, str) else Actor.staff(who)
        return services.perform_action(
            actor,
            case.pk,
            action,
            expected_revision=Case.objects.get(pk=case.pk).revision,
            idempotency_key=f"mockup-{uuid.uuid4().hex}",
            payload=payload,
        )

    def latest_case(self):
        return Case.objects.filter(requester=self.requester).order_by("-pk").first()

    def triage(self, case, division, staff, expected=None):
        """Intake hands the case to the division, which confirms the suggested level."""
        self.act("sim-intake", case, "start_triage")
        self.act("sim-intake", case, "request_handoff", {"to_division": division})
        self.act(staff, case, "accept_handoff")
        suggested = CaseTriage.objects.get(case=case).suggested_priority
        if expected and suggested != expected:
            raise MockupError(f"{case.number}: the rules now suggest {suggested}, not {expected}.")
        self.act(staff, case, "set_priority", {"priority": suggested})
        self.act(staff, case, "complete_triage")


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
    users = {name: _user(name, roles) for name, roles in STAFF.items()}
    _grant(users["sim-approver"], AuthorityGrant.Activity.WITHDRAWAL_APPROVAL)
    _grant(users["sim-allocator"], AuthorityGrant.Activity.ALLOCATION)
    admin, created = get_user_model().objects.get_or_create(username="sim-mockup-admin")
    if created:
        admin.set_unusable_password()
        admin.save(update_fields=["password"])
        StaffProfile.objects.create(user=admin, display_name="แอดมินจำลอง")
    RoleGrant.objects.get_or_create(user=admin, role=Role.ADMIN, division=None)
    return World(users, admin)


def _registered(world, user_id):
    registration = identity_services.submit_registration(LINE, user_id, REQUESTER, DISTRICT)
    world.requester = identity_services.approve_registration(world.admin, registration)


# --- Scenarios ---------------------------------------------------------------------------------


def register(r, w):
    r.follow("ผู้แจ้งเพิ่มบัญชี LINE ของ ฝอจ. เป็นเพื่อนครั้งแรก ระบบทักทายและขอชื่อเพื่อลงทะเบียน")
    r.type(REQUESTER, "พิมพ์ชื่อ-นามสกุล แล้วเลื่อนการ์ดเพื่อเลือกเขตที่สังกัด (18 เขต)")
    r.tap(
        "การไฟฟ้านครหลวงเขตคลองเตย",
        "เลือกเขตแล้ว คำขอลงทะเบียนไปรอเจ้าหน้าที่ ฝอจ. รับรอง",
    )
    r.menu(
        "ส่งเรื่องใหม่",
        "ระหว่างรอรับรอง กดเมนูไหนก็ได้คำตอบเดิม และยกเลิกคำขอได้",
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
    r.menu("ส่งเรื่องใหม่", "พร้อมแจ้งเรื่องแล้ว ระบบถามว่าจะแจ้งเรื่องประเภทใด")


def withdrawal(r, w):
    meter = EquipmentType.objects.get(code="MTR_SYN_A")
    r.menu("ส่งเรื่องใหม่", "กดเมนู 'ส่งเรื่องใหม่' ระบบถามประเภทเรื่อง ตอบด้วยปุ่มได้ทุกข้อ")
    r.tap("เบิก", "เลือก 'เบิก' แล้วเลือกหมวดของ")
    r.tap("มิเตอร์", "ระบบแสดงรายการของในหมวดมิเตอร์")
    r.tap(meter.name, "เลือกรุ่นแล้ว ระบบถามจำนวน")
    r.type("3", "พิมพ์จำนวนเป็นตัวเลข จะมีหน่วยต่อท้ายหรือใช้เลขไทยก็ได้")
    r.tap("ไปต่อ", "ไม่เพิ่มรายการแล้ว ระบบถามวัตถุประสงค์")
    r.type(
        "เปลี่ยนมิเตอร์ชำรุดของผู้ใช้ไฟ 3 ราย ในซอยตัวอย่าง 50",
        "พิมพ์วัตถุประสงค์ ระบบถามวันที่ต้องการใช้ (ข้ามได้)",
    )
    r.type(
        thai_date(timezone.localdate() + timedelta(days=7)),
        "พิมพ์วันที่แบบ พ.ศ. ระบบสรุปให้ตรวจก่อนยืนยัน",
        note="ข้อสังเกต: หน้าสรุปแสดงวันที่ที่พิมพ์เป็นแบบ ค.ศ. (ปี-เดือน-วัน) "
        "ควรแสดงกลับเป็น วัน/เดือน/ปี พ.ศ. ตามที่ผู้แจ้งพิมพ์",
    )
    r.tap(
        "ยืนยันส่งเรื่อง",
        "กดยืนยันแล้วได้เลขเรื่องทันที เรื่องเข้าคิวของกองรับเรื่องกลาง",
    )
    case = w.latest_case()
    r.staff(
        "กองรับเรื่องกลาง → กองมิเตอร์",
        "คัดกรองแล้วส่งต่อให้กองมิเตอร์ กองมิเตอร์รับงานและยืนยันระดับความเร่งด่วนที่ระบบเสนอ",
        lambda: w.triage(case, "SIM-MTR", "sim-mtr"),
        "การคัดกรองและส่งต่อเป็นงานภายใน ผู้แจ้งจึงไม่ได้รับข้อความ",
        later=timedelta(minutes=25),
    )
    r.staff(
        "ผู้อนุมัติ กองมิเตอร์",
        "อนุมัติ 2 จาก 3 เครื่อง (อีก 1 เครื่องไม่อนุมัติ พร้อมเหตุผล)",
        lambda: w.act(
            "sim-approver",
            case,
            "approve",
            {"lines": [{"line_no": 1, "quantity": "2", "reason": "เครื่องที่ 3 ใช้ซ่อมแทนได้"}]},
        ),
        "อนุมัติบางส่วนไม่ส่งข้อความทันที ผู้แจ้งจะเห็นเป็น 'มีอัปเดตใหม่' ตอนทักแชตครั้งถัดไป",
        later=timedelta(hours=2),
    )
    serials = list(
        FakeSerial.objects.filter(item=meter, status=FakeSerial.Status.AVAILABLE)
        .order_by("serial")
        .values_list("serial", flat=True)[:2]
    )

    def issue():
        payload = {
            "recipient_name": "ผู้รับของจำลอง เขตคลองเตย",
            "lines": [{"line_no": 1, "quantity": "2", "serials": serials}],
        }
        try:
            w.act("sim-mtr-issuer", case, "issue", payload)
        except CommandFailed:
            # Other open cases want the same stock (Q8): the allocator decides first.
            w.act(
                "sim-allocator",
                case,
                "allocate",
                {"lines": [{"line_no": 1, "quantity": "2"}], "reason": "จัดสรรตามลำดับคิว"},
            )
            w.act("sim-mtr-issuer", case, "issue", payload)

    r.staff(
        "ผู้จ่าย กองมิเตอร์",
        f"จ่ายมิเตอร์ 2 เครื่อง เลขเครื่อง {', '.join(serials)}",
        issue,
        "จ่ายของแล้ว ระบบส่งการ์ด 'ของพร้อมรับ' ให้ผู้แจ้งยืนยันทันที",
        later=r.wait_until(1, 10, 5),
    )
    r.tap(
        "ได้รับครบ",
        "ผู้แจ้งได้ของครบแล้วกด 'ได้รับครบ' ถ้าจำนวนหรือเลขเครื่องไม่ตรงให้กด 'ไม่ตรง'",
        note="ข้อสังเกต: 'มีอัปเดตใหม่' ซ้ำกับการ์ดที่เพิ่งได้รับ เพราะข้อความที่ส่งไปแล้ว"
        "ยังไม่นับว่าผู้แจ้งเห็น (เกิดเหมือนกันหลังขอข้อมูลเพิ่มและหลังการ์ดผลการแก้ปัญหา)",
    )
    r.menu(
        "ติดตามเรื่อง",
        "เรื่องปิดแล้วจึงไม่อยู่ใน 'เรื่องของฉัน' แต่ยังติดตามได้ ระบบเสนอเลขเรื่องล่าสุดให้กด",
    )
    r.tap(case.number, "รายละเอียดแสดงจำนวนขอ อนุมัติ จ่าย รับ แยกกัน พร้อมเหตุผลที่ไม่อนุมัติ")


# A meter out at the district that no other case holds (the demo database keeps other returns).
RETURNED = "MTR_SYN_021"
TYPED = "ขอเบิกฟิวส์ 20 ชิ้น กับสายไฟ 50 เมตร ใช้ซ่อมตู้จ่ายไฟชุมชนริมคลองที่ชำรุด"
REPLY = "ตู้หมายเลข 12 ปากซอยริมคลอง 3 ครับ"
ANSWERS = {
    TYPED: {
        "requests": [
            {
                "intent": "withdrawal",
                "items": [
                    {"item_code": "FUSE_SYN_A", "item_text": "ฟิวส์", "quantity": "20"},
                    {"item_code": "CBL_SYN_A", "item_text": "สายไฟ", "quantity": "50"},
                ],
                "purpose": "ซ่อมตู้จ่ายไฟชุมชนริมคลองที่ชำรุด",
            }
        ]
    },
}


def typed(r, w):
    r.type(
        TYPED,
        "ไม่ต้องกดเมนูก็ได้ พิมพ์แจ้งเหมือนคุยกับเจ้าหน้าที่ ระบบอ่านข้อความแล้วเติมร่างให้",
        note="ขั้นนี้ใช้คำตอบ AI ที่กำหนดไว้ล่วงหน้า (ไม่ได้เรียก AI จริง) แต่ผ่านการตรวจ"
        "กับทะเบียนและข้อความต้นฉบับด้วยโค้ดจริง",
    )
    r.tap("ข้าม", "ข้อความไม่ได้บอกวันที่ต้องการใช้ ระบบจึงถามเฉพาะข้อที่ขาด")
    r.tap("ยืนยันส่งเรื่อง", "ตรวจสรุปแล้วกดยืนยัน ผู้แจ้งเป็นคนยืนยันทุกครั้ง AI ไม่ยื่นเรื่องเอง")
    case = w.latest_case()
    r.staff(
        "กองรับเรื่องกลาง",
        "ขอข้อมูลเพิ่ม: ตำแหน่งตู้ที่จะซ่อม",
        lambda: (
            w.act("sim-intake", case, "start_triage"),
            w.act(
                "sim-intake",
                case,
                "request_info",
                {"reason": "ขอทราบตำแหน่งตู้ (ซอยและหมายเลขตู้) เพื่อเตรียมของให้ถูกจุด"},
            ),
        ),
        "เจ้าหน้าที่ขอข้อมูลเพิ่มจากหน้าเว็บ ผู้แจ้งได้รับข้อความทันที",
        later=timedelta(minutes=50),
    )
    r.type(
        REPLY,
        "ผู้แจ้งตอบกลับในแชตตามที่ข้อความบอก ระบบผูกคำตอบกับเรื่องที่รอข้อมูลให้เอง (ไม่ส่งให้ AI และไม่เปิดร่างใหม่)",
    )
    r.staff(
        "กองรับเรื่องกลาง",
        "เห็นคำตอบในหน้าเรื่อง (ข้อมูลเพิ่มจากผู้แจ้ง) แล้วกด 'ได้ข้อมูลครบแล้ว'",
        lambda: w.act("sim-intake", case, "info_received", {"reason": "ได้ตำแหน่งตู้แล้ว"}),
        "คิวขึ้นป้าย 'ผู้แจ้งตอบข้อมูลแล้ว' เจ้าหน้าที่ยืนยันแล้วเรื่องเดินต่อ",
        later=timedelta(minutes=15),
    )


def return_case(r, w):
    meter = EquipmentType.objects.get(code="MTR_SYN_A")
    r.menu("ส่งเรื่องใหม่", "เริ่มเรื่องใหม่จากเมนู")
    r.tap("ส่งคืน", "เลือก 'ส่งคืน'")
    r.tap("มิเตอร์", "เลือกหมวดของที่จะคืน")
    r.tap(meter.name, "เลือกรุ่น")
    r.type("1", "พิมพ์จำนวน ของที่มีเลขเครื่องต้องบอกเลขทุกเครื่อง")
    r.type(RETURNED, "พิมพ์เลขเครื่อง (หลายเครื่องคั่นด้วยเว้นวรรค)")
    r.tap("ไปต่อ", "ไม่มีรายการอื่น ระบบถามสภาพของ")
    r.type("ชำรุด หน้าจอไม่แสดงผล", "บอกสภาพของ")
    r.type("ถอดเปลี่ยนเครื่องใหม่ให้ผู้ใช้ไฟแล้ว", "บอกเหตุผลที่ส่งคืน")
    r.tap("ข้าม", "ไม่มีเลขเอกสารเบิกเดิม กดข้ามได้")
    r.tap("ยืนยันส่งเรื่อง", "ยืนยันแล้วได้เลขเรื่องคืน (ขึ้นต้นด้วย R)")
    case = w.latest_case()
    start = timezone.localdate() + timedelta(days=1)

    def instructions():
        w.triage(case, "SIM-MTR", "sim-mtr")
        w.act(
            "sim-mtr",
            case,
            "return_instructions",
            {
                "place": "คลังกองมิเตอร์ อาคาร 2 ชั้น 1 (จำลอง)",
                "from_date": start.isoformat(),
                "to_date": (start + timedelta(days=6)).isoformat(),
                "note": "นำของมาพร้อมใบนำส่งของเขต",
            },
        )

    r.staff(
        "กองมิเตอร์",
        "รับเรื่องคืนแล้วแจ้งสถานที่และช่วงวันที่ส่งคืน",
        instructions,
        "ผู้แจ้งได้รับข้อความบอกว่าจะส่งของคืนที่ไหน เมื่อไร",
        later=timedelta(minutes=40),
        note="ข้อสังเกต: วันที่ในข้อความยังเป็นรูปแบบ ค.ศ. (ปี-เดือน-วัน) ควรแสดงแบบ วัน/เดือน/ปี พ.ศ. เหมือนส่วนอื่น",
    )
    r.staff(
        "ผู้รับคืน กองมิเตอร์",
        f"รับมิเตอร์ {RETURNED} ที่เขตนำมาส่ง",
        lambda: w.act(
            "sim-mtr-receiver",
            case,
            "receive",
            {
                "carrier_name": "ผู้นำส่งจำลอง เขตคลองเตย",
                "items": [{"item_code": meter.code, "serials": [RETURNED]}],
            },
        ),
        "ของมาถึงแล้ว ยังไม่ส่งข้อความทันที (รอผลตรวจสภาพ)",
        later=r.wait_until(1, 9, 30),
    )
    r.staff(
        "ผู้ตรวจสภาพ กองมิเตอร์",
        "ตรวจสภาพแล้ว ส่งซ่อม 1 เครื่อง",
        lambda: w.act("sim-mtr-inspector", case, "inspect", {"unit": 1, "split": {"repair": "1"}}),
        "ตรวจครบทุกหน่วยแล้ว เรื่องปิดเอง และระบบแจ้งผลการรับคืน",
        later=timedelta(hours=3),
        note="ข้อสังเกต: บรรทัดผลแสดงทุกตัวเลขแม้เป็น 0 (พร้อมใช้ ซ่อม อื่น รอตรวจ ยุติ ค้างส่ง) "
        "ผู้แจ้งอ่านยาก ควรแสดงเฉพาะตัวเลขที่มีค่า",
    )
    r.menu("ติดตามเรื่อง", "ติดตามเรื่องจากเมนู ระบบเสนอเลขเรื่องล่าสุดให้กด หรือพิมพ์เลขเรื่องเอง")
    r.type(case.number, "พิมพ์เลขเรื่องเพื่อดูสถานะ")


def problem(r, w):
    transformer = EquipmentType.objects.get(code="TRF_SYN_A")
    r.menu("ส่งเรื่องใหม่", "เริ่มเรื่องใหม่จากเมนู")
    r.tap("แจ้งปัญหา/ข้อเสนอแนะ", "เลือก 'แจ้งปัญหา/ข้อเสนอแนะ'")
    r.tap("หม้อแปลง", "เลือกว่าเรื่องเกี่ยวกับอะไร (กด ไม่แน่ใจ ได้)")
    r.tap(transformer.name, "เลือกรุ่น")
    r.type("TRF_SYN_004", "บอกเลขเครื่องที่มีปัญหา")
    r.tap("ไปต่อ", "ระบบถามอาการ")
    r.type(
        "หม้อแปลงหน้าซอยมีควันออกและมีกลิ่นไหม้หลังฝนตก",
        "แจ้งอาการด้วยคำของตัวเอง",
    )
    r.photo("ส่งรูปประกอบได้ทุกเมื่อ ระบบตรวจไวรัสก่อนให้เจ้าหน้าที่เปิด แล้วถามข้อเดิมต่อ")
    r.type("ไฟดับประมาณ 30 ครัวเรือน", "บอกผลกระทบ")
    r.tap("ข้าม", "ไม่เกี่ยวกับเรื่องเดิม กดข้าม")
    r.tap(
        "ยืนยันส่งเรื่อง",
        "ข้อความมีคำว่า 'ควัน' และ 'ไหม้' ระบบให้คำแนะนำเร่งด่วนทันทีพร้อมเลขเรื่อง",
    )
    case = w.latest_case()
    r.staff(
        "กองรับเรื่องกลาง → กองหม้อแปลง",
        "เห็นเรื่องเป็นระดับ 'ฉุกเฉิน' ส่งต่อกองหม้อแปลง กองหม้อแปลงรับงานทันที",
        lambda: w.triage(case, "SIM-TRF", "sim-trf", "P0"),
        "การส่งต่อเป็นงานภายใน ผู้แจ้งดูความคืบหน้าได้จากเมนู",
        later=timedelta(minutes=8),
    )
    r.menu("ติดตามเรื่อง", "ผู้แจ้งเปิดดูความคืบหน้า")
    r.tap(case.number, "สถานะบอกกองที่รับผิดชอบและกำหนดอัปเดตถัดไป")
    r.staff(
        "ผู้แก้ปัญหา กองหม้อแปลง",
        "บันทึกผลการแก้ปัญหา: แก้แล้ว",
        lambda: w.act(
            "sim-trf-resolver",
            case,
            "resolve",
            {
                "category": "fixed",
                "message": "ตัดไฟ เปลี่ยนขั้วต่อที่ไหม้ และจ่ายไฟคืนแล้ว (ข้อมูลจำลอง)",
            },
        ),
        "ผู้แจ้งได้รับการ์ดผลการแก้ปัญหา ให้ยอมรับผลหรือแจ้งว่ายังไม่เรียบร้อย",
        later=timedelta(hours=2, minutes=10),
    )
    r.tap("ยังไม่เรียบร้อย", "ยังมีอาการอยู่ กด 'ยังไม่เรียบร้อย' ระบบให้พิมพ์เหตุผล")
    r.type("ไฟยังกะพริบเป็นช่วง ๆ ตอนหัวค่ำ", "เรื่องกลับไปให้กองหม้อแปลงทำต่อ")
    r.staff(
        "ผู้แก้ปัญหา กองหม้อแปลง",
        "แก้เพิ่มแล้วบันทึกผลใหม่",
        lambda: w.act(
            "sim-trf-resolver",
            case,
            "resolve",
            {
                "category": "fixed",
                "message": "เปลี่ยนฟิวส์แรงต่ำและขันขั้วต่อใหม่ ตรวจวัดแรงดันปกติ (ข้อมูลจำลอง)",
            },
        ),
        "ผู้แจ้งได้รับการ์ดผลการแก้ปัญหาอีกครั้ง",
        later=r.wait_until(1, 11, 20),
    )
    r.tap("ยอมรับผล", "ยอมรับผลแล้ว เรื่องปิด (เปิดใหม่ได้ภายใน 30 วัน)")


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    summary: str
    play: object
    registered: bool = True
    needs_ai: bool = False


SCENARIOS = [
    Scenario(
        "register",
        "ลงทะเบียนครั้งแรก",
        "เพิ่มเพื่อน พิมพ์ชื่อ เลือกเขต แล้วรอเจ้าหน้าที่รับรอง",
        register,
        registered=False,
    ),
    Scenario(
        "withdrawal",
        "เบิกของด้วยปุ่ม",
        "กดตอบทีละข้อ อนุมัติบางส่วน จ่ายของ แล้วยืนยันรับในแชต",
        withdrawal,
    ),
    Scenario(
        "typed",
        "พิมพ์แจ้งเรื่องเอง",
        "พิมพ์ประโยคเดียว ระบบเติมร่างให้ เจ้าหน้าที่ขอข้อมูลเพิ่ม",
        typed,
        needs_ai=True,
    ),
    Scenario(
        "return",
        "ส่งคืนของ",
        "แจ้งคืนพร้อมเลขเครื่อง รับคำแนะนำการส่งคืน แล้วรับผลการตรวจ",
        return_case,
    ),
    Scenario(
        "problem",
        "แจ้งปัญหาเร่งด่วน",
        "มีควันและกลิ่นไหม้ ได้คำแนะนำทันที โต้แย้งผลครั้งแรก แล้วยอมรับผล",
        problem,
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
        .replace("__RICH_MENU__", f"data:image/png;base64,{menu}")
        .replace("__MOCKUP_DATA__", payload)
    )
