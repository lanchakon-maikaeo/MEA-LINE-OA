"""Chat turns that used to go wrong (the user, 11 Oct 2569: "ปรับที่ยังปรับต่อได้").

Acceptance → tests: a greeting, a question, a request, one word or digits are not taken for a
registration name, and 'แก้ชื่อ' fixes a typo before the district (NameTests). While staff wait
for information, a greeting gets what they wait for, a question or a new request is offered
before it reaches staff, and 'ไม่ใช่ข้อมูลเพิ่ม' handles it like any other message (InfoTests,
InfoAITests). A question or a new request typed where the bot asked for a typed answer goes to
คลังคำตอบ and the AI first, and the step takes it when they find nothing (TypedStepTests,
TypedStepAITests). Free text at a choice says how to answer (ChoiceHintTests).
"""

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import Client
from django.utils import timezone

from operations import ai, ai_chat, answers, conversation, options, services
from operations.contracts import Actor
from operations.models import (
    AIRun,
    Case,
    ConversationDraft,
    ConversationMessage,
    RegistrationRequest,
    Role,
)
from operations.tests.test_m02_cases import key, make_staff
from operations.tests.test_m03_line import ALICE, InfoCase, LineTestCase, buttons, texts
from operations.tests.test_m13_chat_answers import AnswerTestCase, FillCase, answer_json

NOTHING = ai.Extraction(requests=[], raw_codes=[])


def load_bank(cls):
    answers.load(json.loads(answers.DEFAULT.read_text(encoding="utf-8")), approve_as=cls.admin)


def choose(key, value):
    options.change(None, key, value, "ทดสอบ")


class NameTests(LineTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        load_bank(cls)

    def start(self):
        self.say(self.event(ALICE, "follow"))
        self.press(ALICE, "พนักงาน กฟน.")
        self.press(ALICE, "ใช้วิธีอื่น")

    def test_greetings_requests_one_word_and_digits_are_not_names(self):
        self.start()
        for typed, expected in (
            ("สวัสดีครับ", "ยังไม่ได้ลงทะเบียนเป็นผู้แจ้ง"),
            ("สวัสดีครับ ผมชื่อสมชาย ใจดี", "พิมพ์เฉพาะชื่อและนามสกุล"),
            ("ขอเบิกมิเตอร์ 2 ตัว", "ต้องลงทะเบียนก่อน"),
            ("สมชาย", "พิมพ์ทั้งชื่อและนามสกุล"),
            ("สมชาย 1234", "ไม่มีตัวเลข"),
        ):
            with self.subTest(typed=typed):
                reply = self.type(ALICE, typed)
                self.assertIn(expected, texts(reply))
                self.assertNotIn("การไฟฟ้านครหลวงเขตวัดเลียบ", buttons(reply))
        self.assertIn("ชื่อ: สมชาย ใจดี", texts(self.type(ALICE, "ผมชื่อสมชาย ใจดี")))
        self.press(ALICE, "การไฟฟ้านครหลวงเขตวัดเลียบ")
        self.assertEqual(RegistrationRequest.objects.get().submitted_name, "สมชาย ใจดี")

    def test_a_question_gets_its_answer_then_the_name_again(self):
        choose(options.CHAT_ANSWERS, "faq")
        self.start()
        reply = texts(self.type(ALICE, "ลงทะเบียนยังไงครับ"))
        self.assertIn("ลงทะเบียนครั้งแรก", reply)
        self.assertIn("พิมพ์เฉพาะชื่อและนามสกุล", reply)

    def test_rename_before_choosing_the_district(self):
        self.start()
        self.assertIn("แก้ชื่อ", buttons(self.type(ALICE, "สมชาย ใจดีดี")))
        self.assertIn("พิมพ์ชื่อและนามสกุลใหม่", texts(self.press(ALICE, "แก้ชื่อ")))
        self.type(ALICE, "สมชาย ใจดี")
        reply = self.type(ALICE, "วัดเลียบ")  # typed instead of chosen: the list again
        self.assertIn("ชื่อ: สมชาย ใจดี", texts(reply))
        self.press(ALICE, "การไฟฟ้านครหลวงเขตวัดเลียบ")
        self.assertEqual(RegistrationRequest.objects.get().submitted_name, "สมชาย ใจดี")

    def test_any_takes_what_was_typed(self):
        choose(options.REGISTRATION_NAME, "any")
        self.start()
        self.assertIn("ชื่อ: สวัสดีครับ", texts(self.type(ALICE, "สวัสดีครับ")))


class InfoTests(InfoCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        load_bank(cls)

    def waiting(self):
        self.approve(ALICE)
        return self.ask(self.filed(ALICE))

    def attached(self, case):
        return ConversationMessage.objects.filter(case=case).exists()

    def test_a_greeting_gets_what_staff_wait_for(self):
        case = self.waiting()
        reply = texts(self.type(ALICE, "สวัสดีครับ"))
        # "มีอัปเดตใหม่" already says it once; the reminder does not repeat it.
        self.assertEqual(reply.count("ขอทราบตำแหน่งตู้"), 1)
        self.assertIn("กรุณาแจ้งข้อมูลในแชทนี้", reply)
        reply = texts(self.type(ALICE, "ขอบคุณครับ"))
        self.assertIn(f"เจ้าหน้าที่รอข้อมูลเพิ่ม\n{case.number}: ขอทราบตำแหน่งตู้", reply)
        self.assertFalse(self.attached(case))

    def test_a_question_is_offered_before_it_reaches_staff(self):
        case = self.waiting()
        reply = self.type(ALICE, "ต้องส่งรูปตู้ด้วยไหมครับ")
        self.assertIn(f"ข้อมูลที่เจ้าหน้าที่ขอสำหรับเรื่อง {case.number} หรือไม่", texts(reply))
        self.assertEqual(texts(reply).count("ขอทราบตำแหน่งตู้"), 1)
        self.assertFalse(self.attached(case))
        reply = self.press(ALICE, "ส่งถึงเจ้าหน้าที่")
        self.assertIn(f"ส่งข้อมูลถึงเจ้าหน้าที่แล้ว (เรื่อง {case.number})", texts(reply))
        self.assertEqual(ConversationMessage.objects.get(case=case).text, "ต้องส่งรูปตู้ด้วยไหมครับ")

    def test_not_information_is_handled_like_any_other_message(self):
        choose(options.CHAT_ANSWERS, "faq")
        case = self.waiting()
        self.type(ALICE, "เวลาทำการกี่โมง")
        reply = texts(self.press(ALICE, "ไม่ใช่ข้อมูลเพิ่ม"))
        self.assertIn("ข้อความนี้ไม่ได้ส่งถึงเจ้าหน้าที่", reply)
        self.assertIn("เวลาทำการของ ฝอจ.", reply)
        reply = self.type(ALICE, "ขอเบิกฟิวส์ 3 ชิ้น")  # a new request is offered too
        self.assertIn("ไม่ใช่ข้อมูลเพิ่ม", buttons(reply))
        self.assertFalse(self.attached(case))

    def test_confirm_asks_every_time(self):
        choose(options.INFO_REPLY, "confirm")
        case = self.waiting()
        self.assertIn("ส่งถึงเจ้าหน้าที่", buttons(self.type(ALICE, "ตู้หมายเลข 12 ปากซอย 3")))
        self.assertFalse(self.attached(case))


class TypedStepTests(LineTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        load_bank(cls)

    def to_purpose(self):
        self.approve(ALICE)
        self.say(self.postback(ALICE, "m=new"))
        self.press(ALICE, "เบิก")
        self.press(ALICE, "มิเตอร์")
        self.press(ALICE, "MTR_SYN_A")
        self.type(ALICE, "3 เครื่อง")
        return self.press(ALICE, "ไปต่อ")

    def fields(self):
        return ConversationDraft.objects.get(status="open").data["fields"]

    def test_a_question_at_a_typed_step_is_answered_then_asked_again(self):
        choose(options.CHAT_ANSWERS, "faq")
        choose(options.CHAT_TYPED, "check")
        self.assertIn("ขอเบิกเพื่อใช้ทำอะไร", texts(self.to_purpose()))
        reply = texts(self.type(ALICE, "เวลาทำการกี่โมงครับ"))
        self.assertIn("เวลาทำการของ ฝอจ.", reply)
        self.assertIn("ขอเบิกเพื่อใช้ทำอะไร", reply)
        self.assertNotIn("purpose", self.fields())
        self.type(ALICE, "เปลี่ยนมิเตอร์ชำรุด ไม่รู้เป็นอะไร")  # a story, not a question
        self.assertEqual(self.fields()["purpose"], "เปลี่ยนมิเตอร์ชำรุด ไม่รู้เป็นอะไร")

    def test_by_default_the_step_takes_it(self):
        choose(options.CHAT_ANSWERS, "faq")
        self.to_purpose()
        self.type(ALICE, "เวลาทำการกี่โมงครับ")
        self.assertEqual(self.fields()["purpose"], "เวลาทำการกี่โมงครับ")

    def test_what_reads_as_a_question_or_a_request(self):
        for text, expected in (
            ("เปิดกี่โมงครับ", "question"),
            ("เรื่องของผมถึงไหนแล้ว", "question"),
            ("ต้องรออีกนานแค่ไหน", "question"),
            ("ต้องรอนานแค่ไหนกว่าจะได้ของครับ", "question"),  # found with the live model
            ("ต้องส่งรูปด้วยไหม?", "question"),
            ("ขอเบอร์ติดต่อหน่อย", "question"),
            ("ขอเบิกมิเตอร์เพิ่ม 2 เครื่อง", "request"),
            ("อยากแจ้งไฟดับ", "request"),
            ("ไฟดับเป็นช่วงๆ ไม่แน่ใจว่าเกิดจากอะไร", ""),
            ("สายไฟไหม้", ""),
            ("คืนนี้หม้อแปลงดังมาก", ""),
            ("อยากให้มีแบบฟอร์มออนไลน์", ""),
        ):
            with self.subTest(text=text):
                self.assertEqual(conversation.reads_as(text), expected)

    def test_a_count_is_never_a_new_request(self):
        choose(options.CHAT_TYPED, "check")
        quantity = SimpleNamespace(step="quantity")
        self.assertEqual(conversation.off_step(quantity, "ขอเบิก 5 ตัว"), "")
        self.assertEqual(conversation.off_step(quantity, "เบิกได้สูงสุดกี่ตัว"), "question")
        date = SimpleNamespace(step="field:needed_by")
        self.assertEqual(conversation.off_step(date, "ขอเบิกเพิ่มอีกรายการ"), "request")
        self.assertEqual(conversation.off_step(SimpleNamespace(step="kind"), "ขอเบิก"), "")


class TypedCase(FillCase):
    """The AI answers chat, typed steps are checked, and a fuse withdrawal waits for its purpose."""

    def setUp(self):
        super().setUp()
        self.mode("write")
        choose(options.CHAT_TYPED, "check")
        self.approve(ALICE)
        self.fill("ขอเบิกฟิวส์ 5 ชิ้น", answer_json("none"))  # the AI fills it: purpose next

    def draft(self):
        return ConversationDraft.objects.get(status="open", identity__external_id=ALICE)


class TypedStepAITests(TypedCase):
    def test_a_new_request_at_a_typed_step_goes_to_the_ai(self):
        self.assertEqual(self.draft().step, "field:purpose")
        run, _ = self.fill(
            "ขอเบิกมิเตอร์เพิ่ม 2 เครื่อง", answer_json("none"), self.withdrawal("MTR_SYN_A", "2")
        )
        self.assertEqual((run.status, run.result["typed"]), (AIRun.Status.APPLIED, "field:purpose"))
        lines = {line["item_code"]: line["quantity"] for line in self.draft().data["lines"]}
        self.assertEqual(lines, {"FUSE_SYN_A": "5", "MTR_SYN_A": "2"})
        self.assertNotIn("purpose", self.draft().data["fields"])

    def test_a_new_request_does_not_answer_the_step_it_was_typed_at(self):
        # The live model took "เพิ่มอีก" as the purpose of "ขอเบิกมิเตอร์เพิ่มอีก 2 เครื่อง".
        extraction = self.withdrawal("MTR_SYN_A", "2")
        extraction.requests[0].fields = {"purpose": "เพิ่มอีก"}
        self.fill("ขอเบิกมิเตอร์เพิ่มอีก 2 เครื่อง", answer_json("none"), extraction)
        self.assertNotIn("purpose", self.draft().data["fields"])
        self.assertEqual(self.draft().step, "field:purpose")

    def test_a_question_at_a_typed_step_gets_an_approved_answer(self):
        # Only the answer task reads it: the live extraction once made a question a new problem.
        problem = ai.Extraction(requests=[ai.Proposal(intent="problem")], raw_codes=[])
        run, reply = self.fill("ต้องรออีกนานแค่ไหนครับ", answer_json("why_slow"), problem)
        self.assertEqual(run.status, AIRun.Status.ANSWERED)
        self.assertEqual(run.result["typed_as"], "question")
        self.assertEqual(ConversationDraft.objects.filter(status="open").count(), 1)
        self.assertIn("แต่ละเรื่องมีกรอบเวลา", texts(reply))
        self.assertIn("ขอเบิกเพื่อใช้ทำอะไร", texts(reply))
        self.assertNotIn("purpose", self.draft().data["fields"])

    def test_when_the_ai_finds_nothing_the_step_takes_the_text(self):
        run, reply = self.fill("ขอเบิกไปใช้ซ่อมตู้หน้าวัด", answer_json("none"), NOTHING)
        self.assertEqual(run.status, AIRun.Status.TAKEN)
        self.assertEqual(self.draft().data["fields"]["purpose"], "ขอเบิกไปใช้ซ่อมตู้หน้าวัด")
        self.assertIn("ต้องการใช้ภายในวันที่", texts(reply))
        self.assertNotIn("ตอบในแชตนี้ไม่ได้", texts(reply))

    def test_a_late_ai_leaves_the_text_to_the_step(self):
        self.type(ALICE, "ขอเบิกไปใช้ซ่อมตู้หน้าวัด")
        run = AIRun.objects.order_by("-pk").first()
        type(run.inbox).objects.filter(pk=run.inbox_id).update(
            received_at=timezone.now() - timedelta(seconds=60)
        )
        ai_chat.expire_waiting()
        self.assertEqual(self.draft().data["fields"]["purpose"], "ขอเบิกไปใช้ซ่อมตู้หน้าวัด")
        self.assertIn("ต้องการใช้ภายในวันที่", texts(self.sent[-1]))


class InfoAITests(FillCase):
    """'ไม่ใช่ข้อมูลเพิ่ม' on a new request: the AI reads the message typed before the button."""

    def test_the_ai_reads_the_message_offered_first(self):
        self.mode("write")
        alice = self.approve(ALICE)
        submitted = services.submit_case(
            Actor.for_requester(alice),
            idempotency_key=key(),
            payload={"kind": "problem", "description": "มิเตอร์หน้าจอดับ (ทดสอบ)"},
        )
        case = Case.objects.get(pk=submitted.case_id)
        owner = make_staff("owner", (Role.STAFF, case.owner_division.code))
        for action, payload in (("start_triage", None), ("request_info", {"reason": "ขอรูป"})):
            services.perform_action(
                Actor.staff(owner),
                case.pk,
                action,
                expected_revision=Case.objects.get(pk=case.pk).revision,
                idempotency_key=key(),
                payload=payload,
            )
        self.type(ALICE, "ขอเบิกฟิวส์ 5 ชิ้น")
        self.next = answer_json("none")
        with patch.object(ai, "extract", return_value=(self.withdrawal(), [{"outcome": "ok"}])):
            self.press(ALICE, "ไม่ใช่ข้อมูลเพิ่ม")
            run = AIRun.objects.order_by("-pk").first()
            self.assertEqual(ai_chat.process(run.pk), AIRun.Status.APPLIED)
        self.assertIn("ข้อความนี้ไม่ได้ส่งถึงเจ้าหน้าที่", texts(self.sent[-1]))
        draft = ConversationDraft.objects.get(status="open", identity__external_id=ALICE)
        self.assertEqual(draft.data["lines"][0]["item_code"], "FUSE_SYN_A")
        self.assertFalse(ConversationMessage.objects.filter(case=case).exists())


class ReviewTests(TypedCase):
    """ADR 0012: ฝอจ. read the AI's chat answers back on the AI page."""

    def test_the_ai_page_shows_what_the_ai_did_and_what_it_refused(self):
        self.fill("ต้องรออีกนานแค่ไหนครับ", answer_json("why_slow", "อีก 3 วันได้ของแน่นอน"), NOTHING)
        dev = make_staff("dev", (Role.ADMIN, None), (Role.DEVELOPER_ADMIN, None))
        client = Client()
        client.force_login(dev)
        page = client.get("/console/admin/ai/")
        self.assertContains(page, "พิมพ์ในขั้น field:purpose (คำถาม)")
        self.assertContains(page, "คำตอบ: ")
        self.assertContains(page, "ไม่ส่งถ้อยคำของ AI เพราะ")
        self.assertContains(page, 'data-refused="yes"')
        self.assertContains(page, "เติมร่าง 1 เรื่อง")  # the first message, "ขอเบิกฟิวส์ 5 ชิ้น"
        self.assertNotContains(page, "ต้องรออีกนานแค่ไหน")  # the requester's own words stay out


class ChoiceHintTests(AnswerTestCase):
    def test_free_text_at_a_choice_says_how_to_answer(self):
        self.approve(ALICE)
        self.say(self.postback(ALICE, "m=new"))
        reply = texts(self.type(ALICE, "อะไรนะ"))
        self.assertIn("เลือกจากปุ่มด้านล่าง หรือพิมพ์เลขข้อ", reply)
        self.assertIn("ต้องการแจ้งเรื่องประเภทใด", reply)
        self.press(ALICE, "แจ้งปัญหา/ข้อเสนอแนะ")
        reply = texts(self.type(ALICE, "ไม่รู้เหมือนกัน"))
        self.assertIn("แตะชื่อกองด้านบน หรือพิมพ์ชื่อกอง", reply)
        self.assertIn("เลือกกองที่ต้องการแจ้ง", reply)  # ADR 0030: the division carousel
