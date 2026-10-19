"""คลังคำตอบ: answering questions in LINE (ADR 0012; the user, 10 Oct 2569: ก+ข+ค, AI approved).

Acceptance → tests: off by default; approved answers answer by keyword, greetings only when the
message is little more than a greeting, never a message that names equipment or answers the
bot's own question, and the bot then asks its question again; hazard words get the policy's
guidance at once (KeywordTests). The AI chooses an approved answer (ข) or writes a reply from
the bank and the requester's own cases (ค); a reply that promises, invents numbers or names
someone else's case is replaced by the approved text; "none" gets the out-of-scope answer;
การใช้ไฟฟ้า and anonymous reporters never reach it; a changed bank pauses it (AIAnswerTests).
The evaluation gate fails closed and the frozen sets are scored (GateTests). Admins edit and
approve answers on the web with a reason (PageTests).
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import Client, override_settings

from operations import ai, ai_answer, ai_chat, answers, line_api, options, services
from operations.contracts import Actor
from operations.models import (
    AIConfigEntry,
    AIConfigVersion,
    AICredential,
    AIProfile,
    AIRun,
    AuditEvent,
    Case,
    ChatAnswer,
)
from operations.tests.test_m03_line import ALICE, BOB, LineTestCase, buttons, texts
from operations.tests.test_m13_org import real_world


def answer_json(key, reply=None):
    content = json.dumps({"answer": key, "reply": reply}, ensure_ascii=False)
    return {"choices": [{"message": {"content": content}}]}


class AnswerTestCase(LineTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        real_world(cls)
        answers.load(json.loads(answers.DEFAULT.read_text(encoding="utf-8")), approve_as=cls.admin)

    def mode(self, value):
        if options.get(options.CHAT_ANSWERS) != value:
            options.change(None, options.CHAT_ANSWERS, value, "ทดสอบคลังคำตอบ")

    def case_for(self, requester, **payload):
        result = services.submit_case(
            Actor.for_requester(requester),
            idempotency_key=f"answers-{requester.pk}-{Case.objects.count()}",
            payload={"kind": "problem", "description": "มิเตอร์หน้าจอดับ (ทดสอบ)", **payload},
        )
        return Case.objects.get(pk=result.case_id)


class KeywordTests(AnswerTestCase):
    def test_off_by_default(self):
        self.assertEqual(options.get(options.CHAT_ANSWERS), "off")
        self.approve(ALICE)
        reply = texts(self.type(ALICE, "สวัสดีครับ"))
        self.assertNotIn("ช่องทางนี้รับเรื่อง", reply)
        self.assertIn("เลือกเมนูด้านล่าง", reply)

    def test_greetings_and_questions_are_answered_from_the_bank(self):
        self.mode("faq")
        self.approve(ALICE)
        self.assertIn("ช่องทางนี้รับเรื่องเบิก", texts(self.type(ALICE, "สวัสดีครับ")))
        contact = self.type(ALICE, "ขอเบอร์ติดต่อหน่อย")
        self.assertIn("02-000-0001", texts(contact))
        self.assertEqual(buttons(contact)["เบิกอุปกรณ์"], "m=equipment&kind=withdrawal")
        # A greeting with a story is a report, not a greeting.
        reply = texts(self.type(ALICE, "สวัสดีครับ ขอแจ้งไฟดับที่ซอย 5 ตั้งแต่เมื่อคืน"))
        self.assertNotIn("ช่องทางนี้รับเรื่องเบิก", reply)

    def test_equipment_words_go_to_the_guided_flow(self):
        self.mode("faq")
        self.approve(ALICE)
        reply = texts(self.type(ALICE, "มิเตอร์ติดต่อเจ้าหน้าที่ไม่ได้"))
        self.assertNotIn("02-000-0001", reply)

    def test_my_cases_lists_only_the_requesters_own(self):
        self.mode("faq")
        alice, bob = self.approve(ALICE), self.approve(BOB)
        mine, other = self.case_for(alice), self.case_for(bob)
        reply = self.type(ALICE, "เรื่องของผมถึงไหนแล้ว")
        self.assertIn("ดูสถานะได้จากรายการด้านล่าง", texts(reply))
        self.assertIn(mine.number, texts(reply))
        self.assertNotIn(other.number, texts(reply) + " ".join(buttons(reply)))

    def test_a_question_mid_draft_then_the_same_question_again(self):
        self.mode("faq")
        self.approve(ALICE)
        self.say(self.postback(ALICE, "m=new"))
        reply = self.type(ALICE, "เวลาทำการกี่โมง")
        self.assertIn("วันจันทร์ถึงวันศุกร์ 07:30-12:00 และ 13:00-15:30 น.", texts(reply))
        self.assertIn("ต้องการแจ้งเรื่องประเภทใด", texts(reply))
        self.assertIn("เบิก", buttons(reply))

    def test_a_typed_answer_is_never_taken_for_a_question(self):
        self.mode("faq")
        self.approve(ALICE)
        self.say(self.postback(ALICE, "m=new"))
        self.press(ALICE, "แจ้งปัญหา/ข้อเสนอแนะ")
        self.press(ALICE, "ข้อเสนอแนะ")
        reply = texts(self.type(ALICE, "อยากให้มีเบอร์ติดต่อแต่ละกองในไลน์"))
        self.assertNotIn("02-000-0001", reply)
        self.assertIn("รายละเอียดเพิ่ม", reply)  # the next question of the suggestion

    def test_drafts_and_retired_answers_are_not_used(self):
        self.mode("faq")
        self.approve(ALICE)
        answers.retire(self.admin, "greeting", "ทดสอบเลิกใช้")
        self.assertNotIn("ช่องทางนี้รับเรื่องเบิก", texts(self.type(ALICE, "สวัสดี")))
        ChatAnswer.objects.filter(key="thanks").update(status=ChatAnswer.Status.DRAFT)
        self.assertNotIn("ได้ทุกเมื่อ", texts(self.type(ALICE, "ขอบคุณครับ")))

    def test_hazard_words_get_the_guidance_at_once(self):
        self.mode("faq")
        self.approve(ALICE)
        self.say(self.postback(ALICE, "m=new"))
        self.press(ALICE, "แจ้งปัญหา/ข้อเสนอแนะ")
        self.press(ALICE, "อุปกรณ์อื่น")
        self.press(ALICE, "ไม่ระบุรุ่น")
        reply = texts(self.type(ALICE, "หม้อแปลงมีควันออกมาตอนนี้"))
        self.assertIn("ออกห่างจากจุดนั้นก่อน", reply)
        self.assertIn("ออกห่างจากจุดเกิดเหตุ", reply)  # the policy's own guidance
        self.assertIn("ผลกระทบ", reply)  # and the flow goes on: the story was the description
        calm = texts(self.type(ALICE, "ดูแล้วไม่มีควัน"))
        self.assertNotIn("ออกห่างจากจุดนั้นก่อน", calm)


class AIAnswerCase(AnswerTestCase):
    def setUp(self):
        super().setUp()
        enabled = override_settings(AI_ENABLED=True)
        enabled.enable()
        self.addCleanup(enabled.disable)
        credential = AICredential.objects.create(
            name="fake",
            base_url="https://api.opentyphoon.ai/v1",
            encrypted_key="x",
            created_by=self.admin,
        )
        profile = AIProfile.objects.create(
            credential=credential, model_id="fake-answer", created_by=self.admin
        )
        self.version = AIConfigVersion.objects.create(
            number=900,
            task=AIConfigVersion.Task.ANSWER,
            status=AIConfigVersion.Status.ACTIVE,
            created_by=self.admin,
            eval_result={"bank": answers.bank_digest()},  # the gate itself: GateTests
        )
        AIConfigEntry.objects.create(version=self.version, position=0, profile=profile)
        self.bodies = []
        self.next = answer_json("none")
        post = patch.object(ai, "_post", side_effect=self.provider)
        post.start()
        self.addCleanup(post.stop)
        key = patch.object(ai, "decrypt_key", return_value="k")
        key.start()
        self.addCleanup(key.stop)
        typing = patch.object(line_api, "call", return_value={})  # LINE's typing indicator
        typing.start()
        self.addCleanup(typing.stop)

    def provider(self, url, key, body, timeout):
        self.bodies.append(body)
        return self.next

    def ask(self, user, text, answer):
        self.next = answer
        self.type(user, text)
        run = AIRun.objects.order_by("-pk").first()
        before = len(self.sent)
        status = ai_chat.process(run.pk)
        run.refresh_from_db()
        return status, run, self.sent[before:][-1] if len(self.sent) > before else []


class AIAnswerTests(AIAnswerCase):
    def test_choose_sends_the_approved_text(self):
        self.mode("choose")
        self.approve(ALICE)
        status, run, reply = self.ask(
            ALICE, "ต้องรออีกกี่วันกว่าเรื่องจะขยับ", answer_json("why_slow", "อีกสองวันครับ")
        )
        self.assertEqual(status, AIRun.Status.ANSWERED)
        self.assertIn("แต่ละเรื่องมีกรอบเวลาตามระดับความด่วน", texts(reply))
        self.assertNotIn("อีกสองวัน", texts(reply))
        self.assertEqual(run.result["answer"]["by"], "template")
        self.assertEqual(run.config_version, self.version)  # no extraction version: answer only

    def test_write_sends_a_checked_reply_about_the_requesters_own_case(self):
        self.mode("write")
        alice = self.approve(ALICE)
        case = self.case_for(alice)
        written = f"เรื่อง {case.number} ตอนนี้รอคัดกรองอยู่ที่ กคว. เมื่อสถานะเปลี่ยนจะแจ้งในแชตนี้"
        status, run, reply = self.ask(
            ALICE, "ช่วยดูให้หน่อยว่าเรื่องที่ส่งไปเป็นยังไง", answer_json("why_slow", written)
        )
        self.assertEqual((status, run.result["answer"]["by"]), (AIRun.Status.ANSWERED, "model"))
        self.assertIn(written, texts(reply))
        self.assertIn(case.number, buttons(reply))  # the case can still be opened
        sent = json.loads(self.bodies[-1]["messages"][1]["content"])
        self.assertEqual(len(sent["requester_cases"]), 1)
        self.assertIn(case.number, sent["requester_cases"][0])

    def test_write_refuses_promises_invented_numbers_and_other_peoples_cases(self):
        self.mode("write")
        alice, bob = self.approve(ALICE), self.approve(BOB)
        self.case_for(alice)
        other = self.case_for(bob)
        for reply_text, reason in (
            ("เรื่องของคุณอนุมัติแล้ว รอรับของได้เลย", "รับปาก"),
            ("อีก 3 วันได้ของแน่นอน", "รับปาก"),
            ("รอประมาณ 3 วัน", "ตัวเลข"),
            (f"เรื่อง {other.number} รอคัดกรอง", "ไม่ใช่ของผู้แจ้ง"),
            ("Your case is waiting.", "ภาษาไทย"),
        ):
            with self.subTest(reply_text=reply_text):
                status, run, reply = self.ask(
                    ALICE, "เรื่องที่ส่งไปเป็นยังไงบ้างนะ", answer_json("why_slow", reply_text)
                )
                self.assertEqual(run.result["answer"]["by"], "template")
                self.assertIn(reason, run.result["answer"]["refused"])
                self.assertIn("แต่ละเรื่องมีกรอบเวลา", texts(reply))
                self.assertNotIn(reply_text, texts(reply))

    def test_the_model_sees_the_hours_and_its_particles_are_dropped(self):
        self.mode("write")
        self.approve(ALICE)
        _, run, reply = self.ask(
            ALICE,
            "วันเสาร์มีคนทำงานไหมครับ",
            answer_json("hours", "วันเสาร์ไม่ทำการ ทำการวันจันทร์ถึงวันศุกร์ 07:30-12:00 ค่ะ"),
        )
        sent = json.loads(self.bodies[-1]["messages"][1]["content"])
        hours = next(a for a in sent["approved_answers"] if a["key"] == "hours")
        self.assertIn("07:30-12:00", hours["text"])
        self.assertEqual(run.result["answer"]["by"], "model")
        self.assertTrue(texts(reply).startswith("วันเสาร์ไม่ทำการ"))
        self.assertNotIn("ค่ะ", texts(reply))

    def test_none_gets_the_out_of_scope_answer(self):
        self.mode("write")
        self.approve(ALICE)
        _, run, reply = self.ask(ALICE, "พรุ่งนี้ฝนจะตกไหมครับ", answer_json("none"))
        self.assertIn("เรื่องนี้ตอบในแชตนี้ไม่ได้", texts(reply))
        self.assertEqual(buttons(reply)["แจ้งปัญหา"], "m=problems")

    def test_only_approved_keys_and_valid_json_are_accepted(self):
        rows = list(answers.approved())
        for raw in ("not json", json.dumps({"answer": "make_up", "reply": None})):
            with self.subTest(raw=raw), self.assertRaises(ai.SchemaError):
                ai_answer.validate(raw, rows, [], [], "write")
        schema = ai_answer.response_schema(["greeting"])
        self.assertEqual(schema["properties"]["answer"]["enum"], ["greeting", "none"])
        self.assertNotIn("emergency", [r.key for r in ai_answer.choosable(rows)])

    def test_usage_cases_and_other_people_never_reach_the_ai(self):
        alice, bob = self.approve(ALICE), self.approve(BOB)
        self.case_for(alice, topic="usage", description="สงสัยลักใช้ไฟ บ้านเลขที่ 9 (ทดสอบ)")
        own = self.case_for(alice)
        self.case_for(bob)
        facts = answers.facts(alice)
        self.assertEqual(len(facts), 1)
        self.assertIn(own.number, facts[0])
        self.assertNotIn("ลักใช้ไฟ", " ".join(facts))

    def test_usage_text_and_anonymous_reporters_never_go_to_the_ai(self):
        self.mode("write")
        self.assertFalse(ai_chat.wants_ai(None, None, "ร้องเรียนค่าไฟแพงผิดปกติ"))
        anonymous = SimpleNamespace(anonymous=True)
        self.assertFalse(ai_chat.wants_ai(anonymous, None, "ต้องรอนานแค่ไหนครับ"))

    def test_a_changed_bank_pauses_the_ai_until_evaluated_again(self):
        self.mode("write")
        self.assertEqual(ai_answer.active_version(), self.version)
        answers.save(
            self.admin,
            "hours",
            "เวลาทำงานของ ฝอจ.",
            ["เวลาทำการ"],
            "เวลาทำการของ ฝอจ.",
            "hours",
            "เปลี่ยนหัวข้อ",
            approve=True,
        )
        self.assertIsNone(ai_answer.active_version())
        self.assertIsNone(ai_chat.version_for_chat())


class GateTests(AnswerTestCase):
    def test_the_frozen_sets_are_unchanged(self):
        for name in ai_answer.DATASETS:
            digest, cases = ai_answer.load(name)
            self.assertEqual(digest, ai_answer.LOCKED[name])
            self.assertTrue(cases)

    def test_scoring_counts_wrong_keys_and_redteam_violations(self):
        rows = ai_answer.choosable(list(answers.approved()))
        good = ai_answer.score(
            {"id": "Q", "expected": ["contact"]},
            {"key": "contact", "reply": None, "refused": "", "attempts": []},
            rows,
        )
        bad = ai_answer.score(
            {"id": "X", "expected": ["none"], "forbid": ["อนุมัติ"]},
            {"key": "my_status", "reply": "อนุมัติ", "refused": "", "attempts": []},
            rows,
        )
        self.assertTrue(good["key_ok"])
        self.assertEqual(len(bad["violations"]), 2)
        values = ai_answer.metrics([good], [bad], [100, 200])
        self.assertEqual(values["redteam_violations"], 2)
        self.assertFalse(ai_answer.passes(values))

    def test_the_gate_fails_closed(self):
        version = AIConfigVersion.objects.create(
            number=901, task=AIConfigVersion.Task.ANSWER, created_by=self.admin
        )
        self.assertFalse(ai_answer.gate_passed({}, version))
        self.assertFalse(ai_answer.gate_passed({"bank": "old"}, version))
        version.test_passed, version.eval_passed = True, True
        version.test_result = {"at": "2026-10-10T00:00:00+07:00"}
        self.assertIn("answer bank", ai.activation_blocker(version))


class PageTests(AnswerTestCase):
    def test_admins_edit_and_approve_with_a_reason(self):
        client = Client()
        client.force_login(self.admin)
        page = client.get("/console/admin/answers/")
        self.assertContains(page, "คลังคำตอบ")
        self.assertContains(page, "ข้อความจำลอง")
        response = client.post(
            "/console/admin/answers/",
            {
                "key": "contact",
                "title": "ติดต่อเจ้าหน้าที่",
                "text": "ติดต่อ ฝอจ. ได้ที่เบอร์กลาง (ทดสอบ)",
                "keywords": "เบอร์โทร\r\nติดต่อใคร",
                "action": "text",
                "reason": "ใส่เบอร์จริง",
            },
        )
        self.assertEqual(response.status_code, 302)
        contact = ChatAnswer.objects.get(key="contact")
        self.assertEqual(
            (contact.status, contact.is_synthetic, contact.keywords),
            (ChatAnswer.Status.DRAFT, False, ["เบอร์โทร", "ติดต่อใคร"]),
        )
        client.post(
            "/console/admin/answers/",
            {
                "key": "contact",
                "title": contact.title,
                "text": contact.text,
                "keywords": "เบอร์โทร\nติดต่อใคร",
                "action": "text",
                "approve": "1",
                "reason": "รับรอง",
            },
        )
        contact.refresh_from_db()
        self.assertEqual(contact.status, ChatAnswer.Status.APPROVED)
        self.assertEqual(
            AuditEvent.objects.filter(
                action="chat_answer.saved", target_id=str(contact.pk)
            ).count(),
            2,
        )

    def test_staff_cannot_and_production_refuses_synthetic_text(self):
        client = Client()
        client.force_login(self.meter)
        self.assertIn(client.get("/console/admin/answers/").status_code, (302, 403))
        ChatAnswer.objects.filter(key="greeting").update(status=ChatAnswer.Status.DRAFT)
        greeting = ChatAnswer.objects.get(key="greeting")
        with override_settings(APP_ENV="production"), self.assertRaises(ValidationError):
            answers.save(
                self.admin,
                "greeting",
                greeting.title,
                greeting.keywords,
                greeting.text,
                greeting.action,
                "ลองรับรอง",
                approve=True,
                short_only=True,
            )

    def test_loading_never_overwrites_what_people_edited(self):
        answers.save(self.admin, "thanks", "ขอบคุณ", ["ขอบคุณ"], "ยินดีครับ (แก้แล้ว)", "text", "แก้")
        made = answers.load(json.loads(answers.DEFAULT.read_text(encoding="utf-8")))
        self.assertEqual(made, 0)
        self.assertEqual(ChatAnswer.objects.get(key="thanks").text, "ยินดีครับ (แก้แล้ว)")


class FillCase(AIAnswerCase):
    """An active extraction version, and a message that the AI turns into a fuse withdrawal."""

    def setUp(self):
        super().setUp()
        self.extract = AIConfigVersion.objects.create(
            number=902,
            task=AIConfigVersion.Task.EXTRACT,
            status=AIConfigVersion.Status.ACTIVE,
            created_by=self.admin,
        )
        AIConfigEntry.objects.create(
            version=self.extract, position=0, profile=AIProfile.objects.get(model_id="fake-answer")
        )

    def withdrawal(self, code="FUSE_SYN_A", quantity="5"):
        from decimal import Decimal

        item = ai.ProposedItem(
            code=code, text=code, quantity=Decimal(quantity), serials=[], stated_quantity=True
        )
        return ai.Extraction(
            requests=[ai.Proposal(intent="withdrawal", items=[item])], raw_codes=[code]
        )

    def fill(self, text, provider_json, extraction=None):
        """Type `text`; the AI reads it as `extraction` (a fuse withdrawal by default) and the
        provider answers the next call (an answer or a follow-up question) with `provider_json`."""
        self.next = provider_json
        extraction = extraction if extraction is not None else self.withdrawal()
        with patch.object(ai, "extract", return_value=(extraction, [{"outcome": "ok"}])):
            self.type(ALICE, text)
            run = AIRun.objects.order_by("-pk").first()
            before = len(self.sent)
            ai_chat.process(run.pk)
        run.refresh_from_db()
        return run, self.sent[before:][-1]

    def question(self, text):
        return {
            "choices": [
                {"message": {"content": json.dumps({"question": text}, ensure_ascii=False)}}
            ]
        }


class FollowupTests(FillCase):
    """The AI words the next question after it fills a draft (the user: "ใช่", 10 Oct 2569)."""

    def test_the_ai_words_the_next_question_and_the_step_stays_the_programs(self):
        self.mode("write")
        options.change(None, options.CHAT_FOLLOWUP, "ai", "ทดสอบคำถามต่อ")
        self.approve(ALICE)
        run, reply = self.fill("ขอเบิกฟิวส์ 5 ชิ้นครับ", self.question("ฟิวส์ 5 ชิ้นนี้จะใช้กับงานอะไรครับ"))
        self.assertEqual(texts(reply).splitlines()[-1], "ฟิวส์ 5 ชิ้นนี้จะใช้กับงานอะไร")
        self.assertEqual(run.result["followup"]["by"], "model")
        self.assertEqual(run.result["followup"]["step"], "field:purpose")
        sent = json.loads(self.bodies[-1]["messages"][1]["content"])
        self.assertEqual(sent["draft"]["items"][0]["quantity"], "5")
        self.assertIn("วัตถุประสงค์", sent["standard_question"])

    def test_a_wording_that_asks_for_something_else_keeps_the_standard_question(self):
        self.mode("write")
        options.change(None, options.CHAT_FOLLOWUP, "ai", "ทดสอบคำถามต่อ")
        self.approve(ALICE)
        run, reply = self.fill("ขอเบิกฟิวส์ 5 ชิ้น", self.question("ต้องการ 8 ชิ้นใช่ไหม"))
        self.assertIn("ขอเบิกเพื่อใช้ทำอะไร", texts(reply))
        self.assertEqual(run.result["followup"]["by"], "template")
        self.assertTrue(run.result["followup"]["refused"])
        self.assertEqual(run.result["followup"]["written"], "ต้องการ 8 ชิ้นใช่ไหม")  # for review

    def test_fixed_by_default(self):
        self.mode("write")
        self.approve(ALICE)
        run, reply = self.fill("ขอเบิกฟิวส์ 5 ชิ้น", self.question("ฟิวส์ 5 ชิ้นนี้ใช้ทำอะไร"))
        self.assertIn("ขอเบิกเพื่อใช้ทำอะไร", texts(reply))
        self.assertNotIn("followup", run.result)

    def test_the_wording_checks(self):
        sources = json.dumps({"draft": {"items": [{"quantity": "5"}]}, "q": "ใช้ทำอะไร"})
        for question, problem in (
            ("ใช้กับงานอะไร", ""),
            ("รับทราบแล้วนะ", "ไม่ได้เป็นคำถาม"),
            ("ต้องการวันไหน", "ถามไม่ตรง"),
            ("ใช้ 9 ชิ้นกับงานอะไร", "ตัวเลข"),
            ("What is it for?", "ภาษาไทย"),
            ("อนุมัติแล้ว ใช้กับงานอะไร", "อนุมัติแล้ว"),
            ("เล่าว่าใช้กับงานอะไร", "เล่า"),
        ):
            with self.subTest(question=question):
                found = ai_answer.question_problem(question, "purpose", sources)
                self.assertIn(problem, found) if problem else self.assertEqual(found, "")

    def test_impact_takes_the_models_usual_wording(self):
        # The live evaluation of 10 Oct 2569 refused every ผลกระทบ question: they said "ส่งผล".
        for question in (
            "ฟิวส์ขาดบ่อยแบบนี้ส่งผลต่อการทำงานหรือผู้ใช้ไฟอย่างไร",
            "หม้อแปลงที่มีเสียงดังกระทบผู้ใช้ไฟไหม",
        ):
            with self.subTest(question=question):
                self.assertEqual(ai_answer.question_problem(question, "impact", ""), "")
        self.assertIn("ถามไม่ตรง", ai_answer.question_problem("ใช้กับงานอะไร", "impact", ""))
        reason = "สายไฟ 15 เมตรที่ส่งคืนเพราะเหตุใด"
        self.assertEqual(ai_answer.question_problem(reason, "reason", "15"), "")

    def test_khong_is_not_a_question_word(self):
        # The first live run accepted this: "ขอ" (ask) is also inside "ของ" (of).
        found = ai_answer.question_problem("สภาพของมิเตอร์ที่ส่งคืน", "condition", "")
        self.assertIn("ไม่ได้เป็นคำถาม", found)
        self.assertEqual(ai_answer.question_problem("มิเตอร์ที่ส่งคืนอยู่ในสภาพไหน", "condition", ""), "")

    def test_followup_scoring(self):
        good = ai_answer.score_followup(
            {"id": "F", "forbid": ["อนุมัติ"]},
            {"question": "ใช้งานอะไร", "refused": "", "attempts": [{"outcome": "ok"}]},
        )
        bad = ai_answer.score_followup(
            {"id": "G", "forbid": ["อนุมัติ"]},
            {"question": "อนุมัติไหม", "refused": "", "attempts": [{"outcome": "ok"}]},
        )
        values = ai_answer.metrics([], [], [1], [good, bad])
        self.assertEqual(values["followup_valid_rate"], 1.0)
        self.assertEqual(values["followup_violations"], 1)
