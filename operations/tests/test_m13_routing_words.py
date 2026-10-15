"""คำที่ระบบจำ and คำถามที่บอทตอบไม่ได้ (ADR 0013; routing grill 14 Oct 2569).

Acceptance → tests:
- an unclear case goes to ส่วนกลาง, which hands it on with words from its text; when the
  division accepts, the next unclear case of that kind with a word goes straight there
  (TeachTests: Q1-Q6);
- the words are checked, and only ส่วนกลาง teaches, only on an unclear case (TeachTests: Q2,
  Q8, Q31);
- the rules always win, the kind must match, words pointing two ways leave the case with
  ส่วนกลาง, and the option sends, proposes for one press, or switches off (UseTests: Q7, Q10,
  Q12);
- a case the words sent that is handed on as "ส่งผิดกอง" moves the words, two moves show
  "ย้ายบ่อย", other reasons move nothing, and a coordinator's routing decision teaches at once
  (MoveTests: Q3, Q9, Q13);
- admins and ส่วนกลาง manage the words with a reason; other divisions see their own (PageTests:
  Q36);
- a question the bot could not answer is kept masked and counted, a greeting is not, and an admin
  adds it as an answer or sets it aside (QuestionTests, AIQuestionTests: Q1, Q11).
"""

from django.test import Client

from operations import identity, options, routing_words, web_actions
from operations.contracts import CommandFailed
from operations.management.commands.run_worker import run_once
from operations.models import (
    AuditEvent,
    Case,
    CaseTriage,
    Division,
    RoutingWord,
    UnansweredQuestion,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import ALICE, LineTestCase
from operations.tests.test_m05_ai import AITestCase, answer
from operations.tests.test_m13_org import OrgTestCase

LIGHT = "ไฟถนนหน้าซอยดับมาสามวัน"
WORDS = "/routing/words/"
QUESTIONS = "/console/admin/questions/"


class WordTestCase(OrgTestCase):
    def unclear(self, text=LIGHT):
        _, case = self.submit({"kind": "problem", "description": text})
        return case

    def hand(self, case, to, by=None, **extra):
        by = by or self.exec
        if Case.objects.get(pk=case.pk).status == Case.Status.SUBMITTED:
            self.act(by, case, "start_triage")
        payload = {"to_division": to, "reason_category": "wrong_division", "reason": "งานของกองนี้"}
        return self.act(by, case, "request_handoff", {**payload, **extra})

    def teach(self, text=LIGHT, word="ไฟถนน", to="กองอุปกรณ์ฯ", receiver=None):
        case = self.unclear(text)
        self.hand(case, to, keywords=word)
        self.act(receiver or self.kae, case, "accept_handoff")
        return case

    def proposal(self, case):
        return CaseTriage.objects.get(case=case).current_recommendation

    def fails(self, *args, **kwargs):
        with self.assertRaises(CommandFailed) as caught:
            self.hand(*args, **kwargs)
        return caught.exception.message


class TeachTests(WordTestCase):
    def test_the_coordinator_teaches_a_word_and_the_next_case_goes_straight_there(self):
        first = self.unclear()
        self.assertEqual(first.owner_division.code, "ฝอจ.")
        self.hand(first, "กองอุปกรณ์ฯ", keywords="ไฟถนน")
        self.assertFalse(RoutingWord.objects.exists())  # not before the division accepts
        self.act(self.kae, first, "accept_handoff")
        word = RoutingWord.objects.get()
        self.assertEqual(
            (word.word, word.kind, word.division.code, word.source, word.learned_from),
            ("ไฟถนน", "problem", "กองอุปกรณ์ฯ", "handoff", first),
        )
        self.assertEqual((word.taught_by, word.accepted_by), (self.exec, self.kae))
        second = self.unclear("ไฟถนนหน้าบ้านดับอีกแล้ว")
        self.assertEqual(second.owner_division.code, "กองอุปกรณ์ฯ")
        rec = self.proposal(second)
        self.assertEqual((rec.rules, rec.proposed_owner.code), (["R10"], "กองอุปกรณ์ฯ"))
        self.assertIn(f"ส่งตามคำที่ระบบจำ 'ไฟถนน' (สอนจากเรื่อง {first.number})", rec.notes)
        word.refresh_from_db()
        self.assertEqual(word.uses, 1)

    def test_words_are_checked_and_only_the_coordinator_teaches_an_unclear_case(self):
        case = self.unclear("ไม่ใช่มิเตอร์ ไฟถนนดับ โทร 0812345678")
        self.assertEqual(case.owner_division.code, "ฝอจ.")  # a negated word the rules skip
        self.assertIn("ไม่อยู่ในข้อความ", self.fails(case, "บท.", keywords="ป้ายรถเมล์"))
        self.assertIn("ต้องยาว", self.fails(case, "บท.", keywords="ไฟ"))
        self.assertIn("ไม่เกิน 3", self.fails(case, "บท.", keywords="ไฟถนน,ไม่ใช่,ถนนดับ,โทร 08"))
        self.assertIn("คำในกติกา", self.fails(case, "บท.", keywords="มิเตอร์"))
        self.assertIn("ข้อมูลส่วนบุคคล", self.fails(case, "บท.", keywords="0812345678"))
        _, meter = self.submit({"kind": "problem", "topic": "meter", "description": LIGHT})
        self.assertEqual(meter.owner_division.code, "กคว.")
        message = self.fails(meter, "บท.", by=self.kcw, keywords="ไฟถนน")
        self.assertIn("เฉพาะเรื่องที่ส่วนกลางส่งต่อ", message)
        self.assertFalse(RoutingWord.objects.exists())


class UseTests(WordTestCase):
    def test_rules_win_the_kind_must_match_and_two_ways_stay_with_the_coordinator(self):
        self.teach()
        _, meter = self.submit({"kind": "problem", "topic": "meter", "description": LIGHT})
        self.assertEqual(meter.owner_division.code, "กคว.")
        withdrawal_word = RoutingWord.objects.get()
        withdrawal_word.kind = "withdrawal"
        withdrawal_word.save()
        self.assertEqual(self.unclear().owner_division.code, "ฝอจ.")
        withdrawal_word.kind = "problem"
        withdrawal_word.save()
        self.teach("เสาไฟเอียงหน้าบ้าน", "เสาไฟ", "บท.", self.bt)
        both = self.unclear("ไฟถนนดับและเสาไฟเอียง")
        self.assertEqual(both.owner_division.code, "ฝอจ.")
        rec = self.proposal(both)
        self.assertEqual(rec.proposed_owner.code, "ฝอจ.")
        self.assertTrue(any("ชี้ไปหลายกอง" in note for note in rec.notes))

    def test_the_option_proposes_for_one_press_or_switches_off(self):
        self.teach()
        options.change(None, options.LEARNED_ROUTES, "propose", "ทดสอบ")
        proposed = self.unclear("ไฟถนนซอยสองดับ")
        self.assertEqual(proposed.owner_division.code, "ฝอจ.")
        self.assertEqual(self.proposal(proposed).proposed_owner.code, "กองอุปกรณ์ฯ")
        self.act(self.exec, proposed, "start_triage")
        self.act(self.exec, proposed, "request_handoff", {"to_division": "กองอุปกรณ์ฯ"})
        self.act(self.kae, proposed, "accept_handoff")  # followed the proposal: no reason asked

        options.change(None, options.LEARNED_ROUTES, "off", "ทดสอบ")
        off = self.unclear("ไฟถนนซอยสามดับ")
        self.assertEqual(self.proposal(off).proposed_owner.code, "ฝอจ.")
        self.act(self.exec, off, "start_triage")
        state = web_actions.situation(self.exec, Case.objects.get(pk=off.pk))
        form = web_actions.build_form("request_handoff", state, {})
        self.assertNotIn("keywords", [f.name for f in form.fields])
        self.assertIn("เฉพาะเรื่องที่ส่วนกลางส่งต่อ", self.fails(off, "บท.", keywords="ซอยสาม"))


class MoveTests(WordTestCase):
    def test_wrong_division_moves_the_words_and_other_reasons_do_not(self):
        self.teach()
        sent = self.unclear("ไฟถนนหน้าโรงเรียนดับ")
        self.assertEqual(sent.owner_division.code, "กองอุปกรณ์ฯ")
        self.hand(sent, "บท.", by=self.kae)
        self.act(self.bt, sent, "accept_handoff")
        word = RoutingWord.objects.get()
        self.assertEqual((word.division.code, word.moves), ("บท.", 1))
        back = self.unclear("ไฟถนนหน้าวัดดับ")
        self.assertEqual(back.owner_division.code, "บท.")
        self.hand(back, "กองอุปกรณ์ฯ", by=self.bt)
        self.act(self.kae, back, "accept_handoff")
        word.refresh_from_db()
        self.assertEqual((word.division.code, word.moves), ("กองอุปกรณ์ฯ", 2))
        self.client.force_login(self.exec)
        self.assertContains(self.client.get(WORDS), "ย้ายบ่อย")

        busy = self.unclear("ไฟถนนหน้าตลาดดับ")
        self.hand(busy, "กคว.", by=self.kae, reason_category="resource_limit")
        self.act(self.kcw, busy, "accept_handoff")
        word.refresh_from_db()
        self.assertEqual((word.division.code, word.moves), ("กองอุปกรณ์ฯ", 2))
        self.assertEqual(AuditEvent.objects.filter(action="routing_word.moved").count(), 2)

    def test_a_routing_decision_teaches_at_once(self):
        boss = make_staff("boss")
        identity.grant_full_division(
            self.admin, boss, Division.objects.get(code="ฝอจ."), "ผู้บริหาร", "REF-2"
        )
        case = self.unclear("ป้ายประกาศหน้าสำนักงานหลุด")
        self.act(self.exec, case, "start_triage")
        self.act(self.exec, case, "request_routing_decision", {"reason": "ไม่แน่ใจว่ากองไหน"})
        self.act(
            boss,
            case,
            "decide_routing",
            {"division": "บท.", "keywords": "ป้ายประกาศ", "reason": "งานอาคาร"},
        )
        word = RoutingWord.objects.get()
        self.assertEqual(
            (word.word, word.division.code, word.taught_by), ("ป้ายประกาศ", "บท.", boss)
        )
        self.assertEqual(self.unclear("ป้ายประกาศชำรุด").owner_division.code, "บท.")


class PageTests(WordTestCase):
    def test_admins_and_the_coordinator_manage_words_and_others_see_their_own(self):
        self.teach()
        word = RoutingWord.objects.get()
        coordinator = Client()
        coordinator.force_login(self.exec)
        page = coordinator.get(WORDS)
        self.assertContains(page, "ไฟถนน")
        self.assertContains(page, "ยกเลิกคำ")
        moved = {"word_id": word.pk, "action": "move", "division": "บท.", "reason": "งานของบท."}
        coordinator.post(WORDS, moved)
        word.refresh_from_db()
        self.assertEqual(word.division.code, "บท.")
        self.assertTrue(AuditEvent.objects.filter(action="routing_word.changed").exists())

        meter = Client()
        meter.force_login(self.kcw)
        page = meter.get(WORDS)
        self.assertNotContains(page, "ไฟถนน")  # points to บท., not กคว.
        self.assertNotContains(page, "ยกเลิกคำ")
        self.assertEqual(meter.post(WORDS, {**moved, "action": "cancel"}).status_code, 403)
        own = Client()
        own.force_login(self.bt)
        self.assertContains(own.get(WORDS), "ไฟถนน")

        coordinator.post(WORDS, {"word_id": word.pk, "action": "cancel", "reason": "ผิด"})
        word.refresh_from_db()
        self.assertIsNotNone(word.cancelled_at)
        self.assertEqual(self.unclear().owner_division.code, "ฝอจ.")
        self.assertEqual(routing_words.matches(self.unclear()), [])


class QuestionTests(LineTestCase):
    def setUp(self):
        super().setUp()
        self.approve(ALICE)

    def test_a_question_the_bot_cannot_answer_is_kept_masked_and_counted(self):
        self.type(ALICE, "สำนักงานเปิดวันเสาร์ไหม ติดต่อ 081-234-5678")
        self.type(ALICE, "สำนักงานเปิดวันเสาร์ไหม  ติดต่อ 081-234-5678 ครับ")
        self.type(ALICE, "สวัสดีครับ")
        self.type(ALICE, "ok")
        question = UnansweredQuestion.objects.get()
        self.assertEqual(
            (question.text, question.count, question.status),
            ("สำนักงานเปิดวันเสาร์ไหม ติดต่อ [เบอร์โทร-1]", 2, "open"),
        )

    def test_an_admin_adds_it_as_an_answer_or_sets_it_aside(self):
        self.type(ALICE, "สำนักงานเปิดวันเสาร์ไหมครับ")
        self.type(ALICE, "มีที่จอดรถให้ผู้มาติดต่อไหม")
        saturday, parking = UnansweredQuestion.objects.order_by("first_at", "pk")
        self.client.force_login(self.intake)
        self.assertEqual(self.client.get(QUESTIONS).status_code, 403)
        self.client.force_login(self.admin)
        page = self.client.get(QUESTIONS)
        self.assertContains(page, "สำนักงานเปิดวันเสาร์ไหมครับ")
        self.assertContains(
            self.client.get(f"/console/admin/answers/?question={saturday.pk}"),
            "จากคำถามที่บอทตอบไม่ได้",
        )
        self.client.post(
            "/console/admin/answers/",
            {
                "key": "saturday",
                "title": "เปิดวันเสาร์",
                "text": "สำนักงานปิดวันเสาร์และวันหยุด",
                "keywords": "วันเสาร์",
                "action": "text",
                "reason": "ตอบคำถามที่ถามบ่อย",
                "question": saturday.pk,
            },
        )
        saturday.refresh_from_db()
        self.assertEqual(
            (saturday.status, saturday.answer_key, saturday.handled_by),
            ("answered", "saturday", self.admin),
        )
        self.client.post(QUESTIONS, {"question": parking.pk, "status": "ignored"})
        parking.refresh_from_db()
        self.assertEqual(parking.status, "ignored")
        self.client.post(QUESTIONS, {"question": parking.pk, "status": "open"})
        parking.refresh_from_db()
        self.assertEqual(parking.status, "open")
        self.assertEqual(AuditEvent.objects.filter(action="question.handled").count(), 3)


class AIQuestionTests(AITestCase):
    def test_a_message_the_ai_could_not_place_is_kept(self):
        self.llm.default = answer()  # no request in the message
        self.post(self.text(ALICE, "ขอทราบเวลาทำการของฝ่ายครับ"))
        run_once()
        self.claim_and_run_ai()
        self.assertEqual(UnansweredQuestion.objects.get().text, "ขอทราบเวลาทำการของฝ่ายครับ")
