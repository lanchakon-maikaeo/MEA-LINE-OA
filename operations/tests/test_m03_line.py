import base64
import hashlib
import hmac
import json
import secrets
import shutil
import socket
import socketserver
import struct
import tempfile
import threading
import uuid
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import OperationalError
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from operations import conversation, identity, line_api
from operations import options as system_options
from operations.management.commands.run_worker import run_once
from operations.models import (
    Attachment,
    Case,
    ChannelIdentity,
    ConversationDraft,
    ConversationMessage,
    District,
    EquipmentType,
    OutboxEvent,
    PostbackToken,
    RegistrationRequest,
    Requester,
    Role,
    SystemOption,
    WebhookInbox,
)
from operations.tests.test_m02_cases import ITEMS, make_staff

ALICE = "U" + "a" * 32
BOB = "U" + "b" * 32
CAROL = "U" + "c" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
D = ConversationDraft.Status


class _ClamdHandler(socketserver.BaseRequestHandler):
    def handle(self):
        stream = self.request.makefile("rb")
        if stream.read(10) != b"zINSTREAM\0":
            return
        data = b""
        while True:
            (size,) = struct.unpack(">I", stream.read(4))
            if not size:
                break
            data += stream.read(size)
        self.server.received.append(data)
        self.request.sendall(self.server.verdict + b"\0")


class FakeClamd(socketserver.ThreadingTCPServer):
    """Speaks clamd's INSTREAM protocol so the real client code is exercised."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, verdict=b"stream: OK"):
        self.verdict = verdict
        self.received = []
        super().__init__(("127.0.0.1", 0), _ClamdHandler)


def buttons(messages):
    found = {}
    for message in messages:
        for item in message.get("quickReply", {}).get("items", []):
            action = item["action"]
            # Location and link buttons carry no postback data (ADR 0010).
            found[action["label"]] = action.get("data") or action.get("uri") or action["type"]
        if message["type"] == "flex":
            for bubble in message["contents"]["contents"]:
                for button in bubble["body"]["contents"]:
                    found[button["action"]["label"]] = button["action"]["data"]
    return found


def texts(messages):
    return "\n".join(m["text"] for m in messages if m["type"] == "text")


class LineTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        # New cases are screened, as before ADR 0025 (tests of the new way switch it off).
        SystemOption.objects.update_or_create(
            key=system_options.SCREENING, defaults={"value": "on"}
        )
        call_command("load_districts", stdout=StringIO())
        call_command("seed_demo_units", stdout=StringIO())
        for code, category, unit, serialized, divisible in ITEMS:
            EquipmentType.objects.create(
                code=code,
                name=code,
                category=category,
                unit=unit,
                is_serialized=serialized,
                is_divisible=divisible,
            )
        cls.admin = make_staff("admin", (Role.ADMIN, None))
        cls.intake = make_staff("intake", (Role.STAFF, "SIM-TRIAGE"))
        cls.meter = make_staff("meter", (Role.STAFF, "SIM-MTR"))

    def setUp(self):
        self.secret = secrets.token_hex(16)
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        settings_patch = override_settings(
            LINE_CHANNEL_SECRET=self.secret,
            LINE_CHANNEL_ACCESS_TOKEN="test-access",
            OUTBOUND_ENABLED=True,
            LINE_ALLOWED_RECIPIENTS=frozenset({ALICE, BOB, CAROL}),
            MEDIA_ROOT=self.media,
        )
        settings_patch.enable()
        self.addCleanup(settings_patch.disable)
        self.sent = []
        reply = patch.object(
            line_api, "reply", side_effect=lambda token, messages: self.sent.append(messages)
        )
        reply.start()
        self.addCleanup(reply.stop)
        self.ts = int(timezone.now().timestamp() * 1000)
        self.message_ids = iter(range(10**12, 10**13))

    # --- fixtures --------------------------------------------------------------------------

    def event(self, user, event_type, source_type="user", **extra):
        self.ts += 1000
        event = {
            "type": event_type,
            "mode": "active",
            "timestamp": self.ts,
            "webhookEventId": uuid.uuid4().hex.upper()[:26],
            "deliveryContext": {"isRedelivery": False},
            "source": {"type": source_type, "userId": user},
            "replyToken": uuid.uuid4().hex,
        }
        event.update(extra)
        return event

    def text(self, user, value):
        return self.event(
            user,
            "message",
            message={"id": str(next(self.message_ids)), "type": "text", "text": value},
        )

    def postback(self, user, data):
        return self.event(user, "postback", postback={"data": data})

    def image(self, user):
        return self.event(
            user,
            "message",
            message={
                "id": str(next(self.message_ids)),
                "type": "image",
                "contentProvider": {"type": "line"},
            },
        )

    def file(self, user, name="report.pdf", size=1000):
        return self.event(
            user,
            "message",
            message={
                "id": str(next(self.message_ids)),
                "type": "file",
                "fileName": name,
                "fileSize": size,
            },
        )

    def sign(self, body):
        return base64.b64encode(
            hmac.new(self.secret.encode(), body, hashlib.sha256).digest()
        ).decode()

    def post(self, *events, body=None, signature=None):
        body = (
            body
            if body is not None
            else json.dumps({"destination": "Ux", "events": list(events)}).encode()
        )
        return Client().post(
            "/webhooks/line",
            data=body,
            content_type="application/json",
            HTTP_X_LINE_SIGNATURE=self.sign(body) if signature is None else signature,
        )

    def say(self, *events):
        """Deliver events through the signed webhook and the runner; returns the last reply."""
        before = len(self.sent)
        self.assertEqual(self.post(*events).status_code, 200)
        run_once()
        replies = self.sent[before:]
        for message in replies:
            # Static labels must fit LINE limits without the ellipsis cut.
            self.assertFalse([lbl for lbl in buttons(message) if lbl.endswith("…")], message)
        return replies[-1] if replies else []

    def type(self, user, value):
        return self.say(self.text(user, value))

    def press(self, user, label, messages=None):
        options = buttons(messages if messages is not None else self.sent[-1])
        self.assertIn(label, options, f"{label!r} not in {list(options)}")
        return self.say(self.postback(user, options[label]))

    def approve(self, user, district="D01"):
        requester = Requester.objects.create(
            display_name=f"ผู้แจ้ง {user[:4]}",
            district=District.objects.get(code=district),
            approved_by=self.admin,
        )
        ChannelIdentity.objects.create(
            channel="line", external_id=user, requester=requester, status="active"
        )
        return requester

    def withdraw(self, user, quantity="3 เครื่อง"):
        self.say(self.postback(user, "m=new"))
        self.press(user, "เบิก")
        self.press(user, "มิเตอร์")
        self.press(user, "MTR_SYN_A")
        self.type(user, quantity)
        self.press(user, "ไปต่อ")
        self.type(user, "เปลี่ยนมิเตอร์ชำรุด")
        return self.press(user, "ข้าม")


# --- Signed webhook ------------------------------------------------------------------------------


class WebhookTests(LineTestCase):
    def test_unsigned_tampered_or_unconfigured_requests_are_refused(self):
        body = json.dumps({"events": [self.text(ALICE, "สวัสดี")]}).encode()
        self.assertEqual(self.post(body=body, signature="").status_code, 401)
        self.assertEqual(self.post(body=body, signature=self.sign(b"other")).status_code, 401)
        self.assertEqual(self.post(body=body + b" ", signature=self.sign(body)).status_code, 401)
        with override_settings(LINE_CHANNEL_SECRET=""):
            self.assertEqual(self.post(body=body).status_code, 503)
        self.assertEqual(Client().get("/webhooks/line").status_code, 405)
        self.assertFalse(WebhookInbox.objects.exists())

    def test_batch_is_stored_with_dispatch_intent_and_deduplicated(self):
        events = [self.text(ALICE, "1"), self.follow_event(BOB), self.event(ALICE, "read")]
        self.assertEqual(self.post(*events).status_code, 200)
        self.assertEqual(WebhookInbox.objects.count(), 3)
        self.assertEqual(WebhookInbox.objects.get(event_type="read").status, "ignored")
        self.assertEqual(OutboxEvent.objects.filter(subject_type="line_inbox").count(), 2)
        inbox = WebhookInbox.objects.get(event_type="message")
        self.assertEqual((inbox.reply_deadline - inbox.received_at).total_seconds(), 50)
        # LINE redelivers the same events: nothing new is stored or dispatched.
        redelivered = [dict(e, deliveryContext={"isRedelivery": True}) for e in events]
        self.assertEqual(self.post(*redelivered).status_code, 200)
        self.assertEqual(WebhookInbox.objects.count(), 3)
        self.assertEqual(OutboxEvent.objects.count(), 2)

    def follow_event(self, user):
        return self.event(user, "follow")

    def test_malformed_and_oversized_payloads(self):
        self.assertEqual(self.post(body=b"{not json").status_code, 400)
        self.assertEqual(self.post(body=b'{"events": {}}').status_code, 400)
        broken = self.text(ALICE, "x")
        del broken["webhookEventId"]
        self.assertEqual(self.post(self.text(ALICE, "ok"), broken).status_code, 400)
        self.assertFalse(WebhookInbox.objects.exists())
        with override_settings(LINE_WEBHOOK_MAX_BYTES=10):
            self.assertEqual(self.post(self.text(ALICE, "ยาว")).status_code, 413)
        self.assertEqual(self.post().status_code, 200)  # LINE's verify call has no events

    def test_database_failure_is_not_acknowledged(self):
        with patch.object(WebhookInbox.objects, "create", side_effect=OperationalError("down")):
            self.assertEqual(self.post(self.text(ALICE, "สวัสดี")).status_code, 503)
        self.assertFalse(WebhookInbox.objects.exists())
        self.assertFalse(OutboxEvent.objects.exists())

    def test_group_and_room_content_is_never_processed(self):
        group_text = self.text(ALICE, "ขอเบิก")
        group_text["source"] = {"type": "group", "groupId": "C1", "userId": ALICE}
        join = self.event("", "join", source_type="room")
        with override_settings(APP_ENV="production"):  # the allowlist covers user IDs only
            reply = self.say(group_text, join)
        self.assertIn("แชตส่วนตัว", texts(reply))
        self.assertFalse(ChannelIdentity.objects.exists())
        self.assertEqual(set(WebhookInbox.objects.values_list("status", flat=True)), {"ignored"})


# --- Registration ------------------------------------------------------------------------------


class RegistrationTests(LineTestCase):
    def register(self, user, name="สมชาย ใจดี", district="การไฟฟ้านครหลวงเขตวัดเลียบ"):
        reply = self.say(self.event(user, "follow"))
        self.press(user, "พนักงาน กฟน.")
        reply = self.press(user, "ใช้วิธีอื่น")
        self.assertIn("พิมพ์ชื่อ-นามสกุล", texts(reply))
        reply = self.type(user, name)
        self.assertIn(district, buttons(reply))
        return self.press(user, district)

    def test_following_grants_nothing_and_registration_waits_for_admin(self):
        reply = self.register(ALICE)
        self.assertIn("ระหว่างรอแจ้งปัญหา", texts(reply))
        registration = RegistrationRequest.objects.get()
        self.assertEqual(
            (
                registration.submitted_name,
                registration.requested_district.code,
                registration.status,
            ),
            ("สมชาย ใจดี", "D01", "pending"),
        )
        identity_row = ChannelIdentity.objects.get(external_id=ALICE)
        self.assertEqual(identity_row.status, "active")
        self.assertTrue(identity_row.requester.is_external)
        reply = self.say(self.postback(ALICE, "m=new"))
        self.assertNotIn("เบิก", buttons(reply))
        identity.approve_registration(self.admin, registration)
        identity_row.refresh_from_db()
        self.assertTrue(identity_row.requester.is_external)

    def test_pending_request_can_be_cancelled_and_restarted(self):
        self.register(ALICE)
        reply = self.say(self.text(ALICE, "สถานะลงทะเบียน"))
        reply = self.press(ALICE, "ยกเลิกคำขอ", reply)
        self.assertIn("ยกเลิกคำขอแล้ว", texts(reply))
        self.assertEqual(RegistrationRequest.objects.get().status, "cancelled")
        reply = self.type(ALICE, "สมชาย ใหม่")
        self.press(ALICE, "การไฟฟ้านครหลวงเขตคลองเตย", reply)
        self.assertEqual(
            RegistrationRequest.objects.get(status="pending").requested_district.name,
            "การไฟฟ้านครหลวงเขตคลองเตย",
        )

    def test_unfollow_is_recorded_without_changing_access(self):
        self.approve(ALICE)
        self.say(self.event(ALICE, "unfollow"))
        row = ChannelIdentity.objects.get(external_id=ALICE)
        self.assertEqual((row.is_following, row.status), (False, "active"))


# --- Guided drafts -----------------------------------------------------------------------------


class DraftConversationTests(LineTestCase):
    def setUp(self):
        super().setUp()
        self.alice = self.approve(ALICE)

    def test_guided_withdrawal_becomes_a_case_owned_by_the_requester(self):
        summary = self.withdraw(ALICE)
        self.assertIn("MTR_SYN_A 3 เครื่อง", texts(summary))
        self.assertIn("วัตถุประสงค์: เปลี่ยนมิเตอร์ชำรุด", texts(summary))
        reply = self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertIn(f"เลขเรื่อง: {case.number}", texts(reply))
        self.assertEqual(
            (case.requester, case.channel, case.status), (self.alice, "line", "submitted")
        )
        self.assertEqual(case.owner_division.code, "SIM-TRIAGE")
        self.assertEqual(case.lines.get().requested_quantity, Decimal("3"))
        self.assertEqual(case.purpose, "เปลี่ยนมิเตอร์ชำรุด")
        draft = ConversationDraft.objects.get()
        self.assertEqual((draft.status, draft.case), (D.SUBMITTED, case))
        self.assertTrue(
            ConversationMessage.objects.filter(draft=draft, text="เปลี่ยนมิเตอร์ชำรุด").exists()
        )

    def test_confirming_twice_creates_one_case(self):
        summary = self.withdraw(ALICE)
        confirm = buttons(summary)["ยืนยันส่งเรื่อง"]
        self.say(self.postback(ALICE, confirm))
        reply = self.say(self.postback(ALICE, confirm))
        self.assertIn("ยื่นเรื่องนี้แล้ว", texts(reply))
        self.assertEqual(Case.objects.count(), 1)

    def test_buttons_from_an_earlier_step_are_refused(self):
        self.say(self.postback(ALICE, "m=new"))
        kind_question = self.sent[-1]
        self.press(ALICE, "เบิก", kind_question)
        reply = self.press(ALICE, "ส่งคืน", kind_question)
        self.assertIn("ขั้นตอนก่อนหน้า", texts(reply))
        self.assertEqual(ConversationDraft.objects.get().kind, "withdrawal")

    def test_another_users_button_is_refused(self):
        self.approve(BOB)
        summary = self.withdraw(ALICE)
        reply = self.say(self.postback(BOB, buttons(summary)["ยืนยันส่งเรื่อง"]))
        self.assertIn("ใช้ไม่ได้", texts(reply))
        self.assertFalse(Case.objects.exists())

    def test_buttons_of_another_user_do_nothing_even_without_a_draft(self):
        self.approve(BOB)
        self.say(self.postback(ALICE, "m=new"))
        reply = self.say(self.postback(ALICE, "m=new"))
        new_draft = buttons(reply)["เริ่มใหม่ เก็บเดิม"]
        reply = self.say(self.postback(BOB, new_draft))
        self.assertIn("ใช้ไม่ได้", texts(reply))
        self.assertFalse(ConversationDraft.objects.filter(identity__external_id=BOB).exists())

    def test_confirm_button_expires_after_thirty_minutes(self):
        self.withdraw(ALICE)
        PostbackToken.objects.filter(action="confirm").update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        reply = self.press(ALICE, "ยืนยันส่งเรื่อง")
        self.assertIn("หมดเวลา", texts(reply))
        self.assertFalse(Case.objects.exists())
        self.assertIn("ยืนยันส่งเรื่อง", buttons(reply))  # a fresh confirmation is offered

    def test_free_text_is_kept_and_the_question_is_asked_again(self):
        self.say(self.postback(ALICE, "m=new"))
        reply = self.type(ALICE, "ขอเบิกมิเตอร์ 3 เครื่อง")
        self.assertIn("ประเภทใด", texts(reply))
        draft = ConversationDraft.objects.get()
        self.assertEqual(draft.kind, "")
        self.assertTrue(draft.messages.filter(text="ขอเบิกมิเตอร์ 3 เครื่อง").exists())
        self.type(ALICE, "1")  # numbered menu choice, not text guessing
        draft.refresh_from_db()
        self.assertEqual(draft.kind, "withdrawal")

    def test_quantity_follows_the_unit_rules(self):
        self.say(self.postback(ALICE, "m=new"))
        self.press(ALICE, "เบิก")
        self.press(ALICE, "อุปกรณ์อื่น")
        self.press(ALICE, "FUSE_SYN_A")
        for bad in ("1.5", "ศูนย์", "0", "2 เครื่อง", "2 หรือ 3", "2 ชิ้น อนุมัติเลย", "2,3", "2e3"):
            with self.subTest(bad):
                self.assertIn("จำนวนไม่ถูกต้อง", texts(self.type(ALICE, bad)))
        self.type(ALICE, "๕๐ ชิ้น")  # Thai digits
        self.press(ALICE, "เพิ่มรายการ")
        self.press(ALICE, "อุปกรณ์อื่น")
        self.press(ALICE, "CBL_SYN_A")
        self.assertIn("จำนวนไม่ถูกต้อง", texts(self.type(ALICE, "12.345")))
        self.type(ALICE, "12.35")
        draft = ConversationDraft.objects.get()
        self.assertEqual(
            draft.data["lines"],
            [
                {"item_code": "FUSE_SYN_A", "quantity": "50"},
                {"item_code": "CBL_SYN_A", "quantity": "12.35"},
            ],
        )

    def test_return_of_serialised_items_needs_one_serial_each(self):
        self.say(self.postback(ALICE, "m=new"))
        self.press(ALICE, "ส่งคืน")
        self.press(ALICE, "มิเตอร์")
        self.press(ALICE, "MTR_SYN_A")
        self.type(ALICE, "2")
        self.assertIn("ครบ 2", texts(self.type(ALICE, "S1")))
        self.assertIn("ครบ 2", texts(self.type(ALICE, "S1 S1")))
        self.type(ALICE, "S1, S2")
        self.press(ALICE, "ไปต่อ")
        self.type(ALICE, "ชำรุด")
        self.type(ALICE, "ถอดเปลี่ยน")
        self.press(ALICE, "ข้าม")
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual((case.kind, case.lines.get().serials), ("return", ["S1", "S2"]))

    def test_problem_may_skip_items_and_link_only_own_cases(self):
        self.withdraw(ALICE)
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        own = Case.objects.get()
        self.approve(BOB)
        self.withdraw(BOB)
        self.press(BOB, "ยืนยันส่งเรื่อง")
        other = Case.objects.exclude(pk=own.pk).get()

        self.say(self.postback(ALICE, "m=new"))
        self.press(ALICE, "แจ้งปัญหา/ข้อเสนอแนะ")
        self.press(ALICE, "อุปกรณ์อื่น")
        self.press(ALICE, "ไม่ระบุรุ่น")
        self.type(ALICE, "หม้อแปลงมีเสียงดัง")
        reply = self.press(ALICE, "ข้าม")
        options = buttons(reply)
        self.assertIn(own.number, options)
        self.assertNotIn(other.number, options)
        self.press(ALICE, own.number, reply)
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        problem = Case.objects.get(kind="problem")
        self.assertEqual((problem.related_case, problem.lines.count()), (own, 0))

    def test_several_open_drafts_must_be_chosen_not_guessed(self):
        self.say(self.postback(ALICE, "m=new"))
        self.press(ALICE, "เบิก")
        reply = self.say(self.postback(ALICE, "m=new"))
        self.assertIn("ร่างเรื่องที่ยังไม่ยื่น 1 ร่าง", texts(reply))
        self.press(ALICE, "เริ่มใหม่ เก็บเดิม", reply)
        self.press(ALICE, "ส่งคืน")
        self.assertEqual(ConversationDraft.objects.filter(status=D.OPEN).count(), 2)
        # Without an active draft (e.g. after a submission), the user must pick one.
        active = ConversationDraft.objects.get(kind="return")
        conversation_row = active.identity.conversation
        conversation_row.active_draft = None
        conversation_row.save()
        reply = self.type(ALICE, "มิเตอร์")
        self.assertIn("เลือกร่างเรื่องที่จะทำต่อก่อน", texts(reply))
        self.assertEqual(
            set(ConversationDraft.objects.values_list("step", flat=True)), {"category"}
        )

    def test_discard_old_draft_and_start_new(self):
        self.say(self.postback(ALICE, "m=new"))
        self.press(ALICE, "เบิก")
        reply = self.say(self.postback(ALICE, "m=new"))
        self.press(ALICE, "ยกเลิกร่างเดิม", reply)
        statuses = sorted(ConversationDraft.objects.values_list("status", flat=True))
        self.assertEqual(statuses, ["discarded", "open"])

    def test_idle_drafts_expire_after_seven_days(self):
        self.say(self.postback(ALICE, "m=new"))
        ConversationDraft.objects.update(updated_at=timezone.now() - timedelta(days=8))
        reply = self.type(ALICE, "ต่อ")
        self.assertIn("หมดอายุ", texts(reply))
        self.assertEqual(ConversationDraft.objects.get().status, D.EXPIRED)

    def test_status_replies_show_only_own_cases(self):
        self.withdraw(ALICE)
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        mine = Case.objects.get()
        self.approve(BOB)  # same district, different person
        reply = self.say(self.postback(ALICE, "m=mine"))
        self.assertIn(f"{mine.number} · เบิก · รับเรื่องแล้ว", texts(reply))  # ADR 0028
        reply = self.press(ALICE, mine.number, reply)
        self.assertIn("กองเจ้าภาพ: ", texts(reply))
        for attempt in (self.text(BOB, mine.number), self.postback(BOB, f"c={mine.number}")):
            reply = self.say(attempt)
            self.assertIn(f"ไม่พบเรื่อง {mine.number}", texts(reply))
            self.assertNotIn("รอคัดกรอง", texts(reply))

    def test_suspended_requester_cannot_continue(self):
        self.alice.status = Requester.Status.SUSPENDED
        self.alice.save()
        reply = self.say(self.postback(ALICE, "m=new"))
        self.assertIn("ถูกระงับ", texts(reply))
        self.assertFalse(ConversationDraft.objects.exists())


# --- Replies ----------------------------------------------------------------------------------


class ReplyTests(LineTestCase):
    def stored(self, user=ALICE, seconds_left=50):
        self.post(self.text(user, "สวัสดี"))
        inbox = WebhookInbox.objects.latest("id")
        WebhookInbox.objects.filter(pk=inbox.pk).update(
            reply_deadline=timezone.now() + timedelta(seconds=seconds_left)
        )
        return inbox.pk

    def test_reply_token_is_used_once(self):
        inbox_id = self.stored()
        conversation.send_reply(inbox_id, [conversation.text_message("a")])
        conversation.send_reply(inbox_id, [conversation.text_message("b")])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(WebhookInbox.objects.get(pk=inbox_id).reply_status, "sent")

    def test_late_disallowed_or_disabled_replies_are_not_sent(self):
        cases = [
            (self.stored(seconds_left=-1), {}, "expired"),
            (self.stored(user="U" + "d" * 32), {}, "skipped"),
            (self.stored(), {"OUTBOUND_ENABLED": False}, "skipped"),
        ]
        for inbox_id, overrides, status in cases:
            with self.subTest(status=status), override_settings(**overrides):
                conversation.send_reply(inbox_id, [conversation.text_message("x")])
                self.assertEqual(WebhookInbox.objects.get(pk=inbox_id).reply_status, status)
        self.assertEqual(self.sent, [])
        with override_settings(APP_ENV="production"):
            inbox_id = self.stored(user="U" + "d" * 32)
            conversation.send_reply(inbox_id, [conversation.text_message("x")])
        self.assertEqual(len(self.sent), 1)

    def test_failed_and_unknown_outcomes_are_recorded(self):
        for error, status in (
            (line_api.LineApiError(400, "bad"), "failed"),
            (TimeoutError(), "unknown"),
        ):
            inbox_id = self.stored()
            with self.subTest(status=status), patch.object(line_api, "reply", side_effect=error):
                conversation.send_reply(inbox_id, [conversation.text_message("x")])
                self.assertEqual(WebhookInbox.objects.get(pk=inbox_id).reply_status, status)


class CatchUpTests(LineTestCase):
    """ADR 0015 Q12: an answer that missed its reply token goes out once by push."""

    def setUp(self):
        super().setUp()
        from operations.models import ChannelIdentity

        for user in (ALICE, BOB):  # people who talked to the bot before the outage
            ChannelIdentity.objects.get_or_create(
                channel=ChannelIdentity.Channel.LINE, external_id=user
            )

    def expired(self, minutes_ago=0, user=ALICE):
        self.post(self.text(user, "สวัสดี"))
        inbox = WebhookInbox.objects.latest("id")
        WebhookInbox.objects.filter(pk=inbox.pk).update(
            reply_deadline=timezone.now() - timedelta(seconds=1),
            occurred_at=timezone.now() - timedelta(minutes=minutes_ago),
        )
        return inbox.pk

    def planned(self):
        from operations.models import NotificationDelivery as N

        return list(N.objects.filter(kind="catch_up", status="planned").order_by("id"))

    def test_late_answer_is_planned_as_one_push_with_an_apology_after_ten_minutes(self):
        conversation.send_reply(self.expired(minutes_ago=12), [conversation.text_message("a")])
        texts_ = [d.text for d in self.planned()]
        self.assertEqual(len(texts_), 2)
        self.assertIn("ขอโทษที่ตอบช้า", texts_[0])
        self.assertEqual(texts_[1], "a")
        self.assertEqual(self.sent, [])  # pushed later by n8n, inside the push budget

    def test_newer_answer_replaces_the_waiting_one_and_a_sent_one_is_not_repeated(self):
        from operations.models import NotificationDelivery as N

        conversation.send_reply(self.expired(), [conversation.text_message("old")])
        conversation.send_reply(self.expired(), [conversation.text_message("new")])
        self.assertEqual([d.text for d in self.planned()], ["new"])
        self.assertEqual(N.objects.filter(kind="catch_up", status="skipped").count(), 1)
        N.objects.filter(kind="catch_up", status="planned").update(
            status="sent", sent_at=timezone.now()
        )
        conversation.send_reply(self.expired(), [conversation.text_message("again")])
        self.assertEqual(self.planned(), [])

    def test_refused_reply_token_is_caught_up_but_other_errors_are_not(self):
        bad = line_api.LineApiError(400, '{"message":"Invalid reply token"}')
        with patch.object(line_api, "reply", side_effect=bad):
            conversation.send_reply(self.stored_live(), [conversation.text_message("x")])
        self.assertEqual(len(self.planned()), 1)
        with patch.object(line_api, "reply", side_effect=line_api.LineApiError(400, "bad")):
            conversation.send_reply(self.stored_live(user=BOB), [conversation.text_message("y")])
        self.assertEqual(len(self.planned()), 1)

    def test_admin_can_turn_it_off(self):
        from operations import options

        options.change(None, options.CATCH_UP, "off", "ทดสอบปิดคำตอบที่ค้าง")
        conversation.send_reply(self.expired(minutes_ago=30), [conversation.text_message("a")])
        self.assertEqual(self.planned(), [])

    def stored_live(self, user=ALICE):
        self.post(self.text(user, "สวัสดี"))
        inbox = WebhookInbox.objects.latest("id")
        WebhookInbox.objects.filter(pk=inbox.pk).update(
            reply_deadline=timezone.now() + timedelta(seconds=50)
        )
        return inbox.pk


# --- Attachments ------------------------------------------------------------------------------


class AttachmentTests(LineTestCase):
    def setUp(self):
        super().setUp()
        self.approve(ALICE)
        self.clamd = FakeClamd()
        threading.Thread(target=self.clamd.serve_forever, daemon=True).start()
        self.addCleanup(self.clamd.server_close)
        self.addCleanup(self.clamd.shutdown)
        scanner = override_settings(
            CLAMAV_HOST="127.0.0.1", CLAMAV_PORT=self.clamd.server_address[1]
        )
        scanner.enable()
        self.addCleanup(scanner.disable)
        self.content = PNG
        fetch = patch.object(line_api, "fetch_content", side_effect=lambda mid, limit: self.content)
        fetch.start()
        self.addCleanup(fetch.stop)

    def test_image_is_quarantined_scanned_and_served_only_to_owner_division(self):
        self.say(self.postback(ALICE, "m=new"))
        reply = self.say(self.image(ALICE))
        self.assertIn("ได้รับไฟล์แล้ว", texts(reply))
        attachment = Attachment.objects.get()
        self.assertEqual(attachment.scan_status, "clean")  # the runner fetched and scanned it
        self.assertEqual(self.clamd.received, [PNG])
        self.assertEqual((attachment.content_type, attachment.size), ("image/png", len(PNG)))

        url = f"/attachments/{attachment.pk}/"
        intake = Client()
        intake.force_login(self.intake)
        self.assertEqual(intake.get(url).status_code, 404)  # not part of a case yet
        self.press(ALICE, "เบิก")
        self.press(ALICE, "มิเตอร์")
        self.press(ALICE, "MTR_SYN_A")
        self.type(ALICE, "1")
        self.press(ALICE, "ไปต่อ")
        self.type(ALICE, "ทดแทน")
        summary = self.press(ALICE, "ข้าม")
        self.assertIn("ไฟล์แนบ 1 ไฟล์", texts(summary))
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        attachment.refresh_from_db()
        self.assertEqual(attachment.case, case)

        response = intake.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), PNG)
        self.assertIn(f"{case.number}-1.png", response["Content-Disposition"])
        other = Client()
        other.force_login(self.meter)
        self.assertEqual(other.get(url).status_code, 404)
        for status in ("pending_scan", "infected", "rejected"):
            Attachment.objects.filter(pk=attachment.pk).update(scan_status=status)
            self.assertEqual(intake.get(url).status_code, 404, status)
        self.assertEqual(Client().get(url).status_code, 302)

    def test_infected_disguised_too_large_or_expired_files_never_pass(self):
        self.say(self.postback(ALICE, "m=new"))
        scenarios = [
            ({"verdict": b"stream: Eicar-Signature FOUND"}, PNG, "infected"),
            ({}, b"MZ\x90\x00 not an image", "rejected"),
            ({"error": line_api.ContentTooLarge()}, None, "rejected"),
            ({"error": line_api.LineApiError(410, "gone")}, None, "rejected"),
        ]
        for setup, content, expected in scenarios:
            with self.subTest(expected=expected, setup=list(setup)):
                self.clamd.verdict = setup.get("verdict", b"stream: OK")
                self.content = content
                error = setup.get("error")
                fetch = (
                    patch.object(line_api, "fetch_content", side_effect=error) if error else None
                )
                if fetch:
                    fetch.start()
                self.say(self.image(ALICE))
                if fetch:
                    fetch.stop()
                self.assertEqual(Attachment.objects.latest("created_at").scan_status, expected)

    def test_scanner_outage_keeps_files_pending(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed_port = probe.getsockname()[1]
        self.say(self.postback(ALICE, "m=new"))
        with override_settings(CLAMAV_PORT=closed_port):
            self.say(self.image(ALICE))
        attachment = Attachment.objects.get()
        self.assertEqual(attachment.scan_status, "pending_scan")
        run_once()  # scanner is back
        attachment.refresh_from_db()
        self.assertEqual(attachment.scan_status, "clean")

    def test_file_rules_and_per_draft_limit(self):
        self.say(self.postback(ALICE, "m=new"))
        self.assertIn("PDF", texts(self.say(self.file(ALICE, "tool.exe"))))
        self.assertIn("10 MB", texts(self.say(self.file(ALICE, "big.pdf", 11 * 1024 * 1024))))
        self.assertFalse(Attachment.objects.exists())
        with override_settings(ATTACHMENTS_PER_DRAFT=1):
            self.say(self.file(ALICE, "ok.pdf"))
            self.assertIn("ไม่เกิน 1 ไฟล์", texts(self.say(self.image(ALICE))))
        self.assertEqual(Attachment.objects.count(), 1)

    def test_file_without_a_draft_joins_the_draft_the_user_starts(self):
        reply = self.say(self.image(ALICE))
        self.assertIn("เริ่มเรื่องใหม่", buttons(reply))
        self.press(ALICE, "เริ่มเรื่องใหม่", reply)
        self.assertEqual(Attachment.objects.get().draft, ConversationDraft.objects.get())


# --- Runner --------------------------------------------------------------------------------------


class RunnerTests(LineTestCase):
    def test_events_run_in_event_time_order_and_failures_retry_without_blocking(self):
        self.approve(ALICE)
        late = self.text(ALICE, "ข้อความหลัง")
        early = self.text(ALICE, "ข้อความแรก")
        early["timestamp"] = late["timestamp"] - 5000
        self.post(late, early)
        seen = []
        original = conversation.handle_inbox

        def spy(inbox_id):
            seen.append(WebhookInbox.objects.get(pk=inbox_id).payload["message"]["text"])
            if len(seen) == 1:
                raise RuntimeError("boom")
            return original(inbox_id)

        with patch.object(conversation, "handle_inbox", side_effect=spy):
            run_once()
            self.assertEqual(seen, ["ข้อความแรก", "ข้อความหลัง"])
            failed = OutboxEvent.objects.get(
                payload__inbox_id=WebhookInbox.objects.get(payload__message__text="ข้อความแรก").pk
            )
            self.assertEqual(
                (failed.status, failed.attempts, failed.last_error), ("pending", 1, "RuntimeError")
            )
            self.assertGreater(failed.next_attempt_at, timezone.now())
            run_once()  # not due yet: nothing runs twice
            self.assertEqual(len(seen), 2)
            for _ in range(4):  # retried after backoff until the attempts run out
                OutboxEvent.objects.filter(pk=failed.pk).update(next_attempt_at=timezone.now())
                with patch.object(conversation, "handle_inbox", side_effect=RuntimeError("boom")):
                    run_once()
        failed.refresh_from_db()
        self.assertEqual((failed.status, failed.attempts), ("failed", 5))
        self.assertEqual(
            sorted(WebhookInbox.objects.values_list("status", flat=True)), ["done", "failed"]
        )


class RichMenuTests(LineTestCase):
    def test_legacy_command_delegates_to_resumable_role_menus(self):
        with patch("operations.line_menu_release.sync") as sync:
            call_command("setup_line_rich_menu", stdout=StringIO())
        sync.assert_called_once_with()


# --- Answers to "เจ้าหน้าที่ขอข้อมูลเพิ่ม" (found with the LINE mockup, 8 Oct 2569) -----------


class InfoCase(LineTestCase):
    def filed(self, user):
        self.withdraw(user)
        self.press(user, "ยืนยันส่งเรื่อง")
        return Case.objects.filter(requester__channelidentity__external_id=user).latest(
            "submitted_at"
        )

    def ask(self, case, reason="ขอทราบตำแหน่งตู้"):
        from operations import services
        from operations.contracts import Actor
        from operations.tests.test_m02_cases import key

        for action, payload in (("start_triage", None), ("request_info", {"reason": reason})):
            services.perform_action(
                Actor.staff(self.intake),
                case.pk,
                action,
                expected_revision=Case.objects.get(pk=case.pk).revision,
                idempotency_key=key(),
                payload=payload,
            )
        return Case.objects.get(pk=case.pk)


class InfoReplyTests(InfoCase):
    """The push says "ตอบกลับในแชตนี้ได้เลย": the answer must reach the case and its staff,
    not start a new draft or go to AI as a new request."""

    def test_a_typed_answer_goes_to_the_waiting_case_and_staff_see_it(self):
        self.approve(ALICE)
        case = self.ask(self.filed(ALICE))
        reply = self.type(ALICE, "ตู้หมายเลข 12 ปากซอย 3")
        self.assertIn(f"ส่งข้อมูลถึงเจ้าหน้าที่แล้ว (เรื่อง {case.number})", texts([reply[-1]]))
        self.assertNotIn("ยังไม่เข้าใจ", texts(reply))
        message = ConversationMessage.objects.get(text="ตู้หมายเลข 12 ปากซอย 3")
        self.assertEqual(message.case, case)
        self.assertFalse(ConversationDraft.objects.filter(status=D.OPEN).exists())
        staff = Client()
        staff.force_login(self.intake)
        page = staff.get(f"/cases/{case.number}/")
        self.assertContains(page, "ข้อมูลเพิ่มจากผู้แจ้ง (LINE)")
        self.assertContains(page, "ตู้หมายเลข 12 ปากซอย 3")
        whats = [e["what"] for e in page.context["page"]["timeline"]]
        self.assertIn("ผู้แจ้งตอบข้อมูลเพิ่มทาง LINE", whats)
        queue = staff.get("/divisions/SIM-TRIAGE/", {"tab": "district"})
        self.assertContains(queue, "ผู้แจ้งตอบข้อมูลแล้ว")

    def test_with_two_waiting_cases_the_requester_chooses(self):
        self.approve(ALICE)
        first = self.ask(self.filed(ALICE))
        second = self.ask(self.filed(ALICE), "ขอรูปมิเตอร์")
        reply = self.type(ALICE, "ส่งรูปให้ทางอีเมลแล้ว")
        self.assertIn("ข้อความนี้เป็นข้อมูลของเรื่องไหน", texts(reply))
        self.press(ALICE, second.number)
        self.assertEqual(ConversationMessage.objects.get(text="ส่งรูปให้ทางอีเมลแล้ว").case, second)
        self.assertFalse(ConversationMessage.objects.filter(case=first).exists())

    def test_a_file_sent_after_the_request_is_attached_to_the_case(self):
        self.approve(ALICE)
        case = self.ask(self.filed(ALICE))
        reply = self.say(self.image(ALICE))
        self.assertIn(f"แนบกับเรื่อง {case.number}", texts(reply))
        self.assertEqual(Attachment.objects.get(identity__external_id=ALICE).case, case)

    def test_a_draft_started_after_the_request_keeps_its_typing(self):
        self.approve(ALICE)
        case = self.ask(self.filed(ALICE))
        self.say(self.postback(ALICE, "m=new"))
        self.type(ALICE, "1")  # the kind question of the new draft
        draft = ConversationDraft.objects.get(status=D.OPEN)
        self.assertEqual(draft.kind, Case.Kind.WITHDRAWAL)
        self.assertFalse(ConversationMessage.objects.filter(case=case).exists())

    def test_once_staff_have_the_information_typing_is_a_new_conversation(self):
        from operations import services
        from operations.contracts import Actor
        from operations.tests.test_m02_cases import key

        self.approve(ALICE)
        case = self.ask(self.filed(ALICE))
        services.perform_action(
            Actor.staff(self.intake),
            case.pk,
            "info_received",
            expected_revision=Case.objects.get(pk=case.pk).revision,
            idempotency_key=key(),
            payload={"reason": "เขตโทรแจ้งแล้ว"},
        )
        reply = self.type(ALICE, "สวัสดีครับ")
        self.assertNotIn("ส่งข้อมูลถึงเจ้าหน้าที่แล้ว", texts(reply))
        self.assertFalse(ConversationMessage.objects.filter(case=case).exists())


class MissingFileTests(LineTestCase):
    def test_a_file_that_was_never_stored_is_refused_without_stopping_the_worker(self):
        from operations import attachments, services
        from operations.contracts import Actor
        from operations.tests.test_m02_cases import key, withdrawal

        submitted = services.submit_case(
            Actor.for_requester(self.approve(ALICE)), idempotency_key=key(), payload=withdrawal()
        )
        lost = Attachment.objects.create(
            uploaded_by=self.intake,
            case_id=submitted.case_id,
            declared_name="lost.pdf",
            content_type="application/pdf",
            size=1,
            sha256="0" * 64,
            storage_name="",
            scan_status="pending_scan",
        )
        attachments.process_pending()
        lost.refresh_from_db()
        self.assertEqual((lost.scan_status, lost.scan_detail), ("rejected", "missing"))
