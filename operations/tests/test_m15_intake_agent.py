"""The LLM intake agent (owner, 25 Oct 2569 evening, ADR 0022): the AI reads every typed message
against the open draft and words the replies; the program checks values, shows the summary and
files only after a confirmation. The first four tests are the cases found on the Mac with v16:
"ยังไม่มีหมายเลข" looped, "ต้องใส่เลขอะไร" was taken as the answer, a purpose typed at the "more"
step got an off-topic answer, and the first reply did not say what was read."""

import json
from unittest.mock import patch

from django.test import override_settings

from operations import agent_chat, agent_intake, ai, ai_dialogue, options
from operations.management.commands.run_worker import run_once
from operations.models import AIRun, Case, ConversationDraft, LineMenuItem
from operations.tests.test_m03_line import ALICE, buttons, texts
from operations.tests.test_m05_ai import answer
from operations.tests.test_m14_typed_ai import typed_request
from operations.tests.test_m15_menu_ai import MenuTestCase, menu_request

READY = agent_intake.ready  # before the tests patch it
ORDER = "เบิกมิเตอร์ 15(45)A 1 เครื่อง"
METER = ("TYPED_METER", 1, "มิเตอร์", [], "15(45)A")


def decided(act, value=None, reply=None):
    return {"act": act, "value": value, "reply": reply}


class Script:
    """The provider for the agent's calls (by system prompt) and the extraction."""

    def __init__(self, extraction):
        self.decide, self.word, self.extraction = [], [], extraction
        self.seen = []

    def __call__(self, body):
        system = body["messages"][0]["content"]
        self.seen.append(system[:20])
        if system.startswith(agent_intake.DECIDE_SYSTEM[:60]):
            content = self.decide.pop(0) if self.decide else decided("fill")
        elif system.startswith(agent_intake.WORD_SYSTEM[:60]):
            content = self.word.pop(0) if self.word else {"reply": None}
        else:
            return answer(*self.extraction)
        if isinstance(content, Exception):
            raise content
        return answer(content=json.dumps(content, ensure_ascii=False))


class IntakeTestCase(MenuTestCase):
    def setUp(self):
        super().setUp()
        self.version = self.menu_version()
        ready = patch.object(agent_intake, "ready", return_value=True)
        ready.start()
        self.addCleanup(ready.stop)
        self.script = Script([typed_request("withdrawal", METER)])
        self.llm.default = self.script

    def message(self, text, outcome=None):
        before = len(self.sent)
        self.post(self.text(ALICE, text))
        run_once()
        outcomes = self.claim_and_run_ai()
        if outcome is not None:
            self.assertEqual(outcomes, [outcome])
        return [m for reply in self.sent[before:] for m in reply]

    def draft(self):
        return ConversationDraft.objects.get(status="open")


@override_settings(LINE_TYPED_SLIPS=True)  # ADR 0032: the earlier typed intake
class FoundCasesTests(IntakeTestCase):
    def test_no_number_yet_is_explained_not_asked_again(self):
        self.script.word = [{"reply": "ได้ข้อมูลเบิกมิเตอร์ 15(45)A 1 เครื่อง ต่อไปพิมพ์หมายเลขเครื่องวัด"}]
        self.message(ORDER, "applied")
        self.assertEqual(self.draft().step, "entry")
        why = "เรื่องเบิกมิเตอร์ต้องมีหมายเลขเครื่องวัดก่อนจึงจะส่งได้ ร่างนี้เก็บไว้ 7 วัน พิมพ์ต่อได้"
        self.script.decide = [decided("reply", reply=why)]
        sent = self.message("ยังไม่มีหมายเลข", "answered")
        self.assertEqual(texts(sent), why)
        self.assertNotIn("ตัวอักษร ตัวเลข หรือขีด", texts(sent))  # not the step's error
        self.assertIn("ยกเลิกร่าง", buttons(sent))  # the step's buttons stay
        self.assertEqual(self.draft().step, "entry")

    def test_a_question_at_a_typed_step_is_not_its_answer(self):
        self.message(ORDER, "applied")
        self.script.decide = [
            decided("reply", reply="พิมพ์หมายเลขเครื่องวัดของมิเตอร์แต่ละเครื่อง ตัวอักษร ตัวเลข หรือขีด")
        ]
        self.message("ต้องใส่เลขอะไร", "answered")
        draft = self.draft()
        self.assertEqual((draft.step, draft.data["entry"]["row"].get("numbers")), ("entry", None))

    def test_a_purpose_typed_at_the_more_step_fills_the_purpose(self):
        self.message(ORDER, "applied")
        self.script.decide = [decided("answer", value="MT-55667788")]
        self.message("ได้แล้ว หมายเลข MT-55667788", "taken")
        self.assertEqual(self.draft().step, "more")
        self.script.extraction = [typed_request("withdrawal", METER, purpose="ติดตั้งบ้านลูกค้าใหม่")]
        self.script.decide = [decided("fill")]
        sent = self.message("เอาไปติดตั้งบ้านลูกค้าใหม่", "applied")
        draft = self.draft()
        self.assertEqual(draft.data["fields"]["purpose"], "ติดตั้งบ้านลูกค้าใหม่")
        # Read again with the draft as context, the meter row is not added a second time.
        self.assertEqual(len(draft.data["lines"][0]["details"]), 1)
        self.assertNotIn("บริการ", texts(sent))

    def test_the_first_reply_says_what_was_read(self):
        lead = "ได้ข้อมูลเบิกมิเตอร์ 15(45)A 1 เครื่อง แล้ว ต่อไปพิมพ์หมายเลขเครื่องวัดของมิเตอร์เครื่องนี้"
        self.script.word = [{"reply": lead}]
        sent = self.message(ORDER, "applied")
        self.assertEqual(texts(sent), lead)
        self.assertIn("ยกเลิกร่าง", buttons(sent))
        run = AIRun.objects.get()
        self.assertEqual(run.result["intake"]["said"], lead)


@override_settings(LINE_TYPED_SLIPS=True)  # ADR 0032: the earlier typed intake
class ValueTests(IntakeTestCase):
    def test_an_answer_goes_through_the_steps_checks(self):
        self.message(ORDER, "applied")
        self.script.decide = [decided("answer", value="ab")]
        self.script.word = [{"reply": "หมายเลขต้องยาว 4 ถึง 20 ตัว กรุณาพิมพ์หมายเลขเครื่องวัดใหม่"}]
        sent = self.message("เลข ab", "taken")
        self.assertIn("หมายเลขต้องยาว", texts(sent))
        self.assertEqual(self.draft().data["entry"]["at"], 0)

    def test_an_answer_not_in_the_message_is_not_taken(self):
        self.message(ORDER, "applied")
        self.script.decide = [decided("answer", value="99887766")]
        self.script.extraction = []
        self.message("ยังหาเลขอยู่", "taken")  # read as the step's text instead
        self.assertNotIn("99887766", json.dumps(self.draft().data))

    def test_a_count_change_corrects_the_row(self):
        self.message(ORDER, "applied")
        self.script.decide = [decided("answer", value="MT-55667788")]
        self.message("MT-55667788", "taken")
        self.script.extraction = [
            typed_request("withdrawal", ("TYPED_METER", 2, "มิเตอร์", [], None))
        ]
        self.script.decide = [decided("fill")]
        self.message("ขอเปลี่ยนเป็น 2 เครื่อง", "applied")
        draft = self.draft()
        self.assertEqual(draft.data["lines"], [])  # the old row of 1 is replaced
        self.assertEqual(draft.data["entry"]["row"], {"size": "15(45)A", "quantity": "2"})


@override_settings(LINE_TYPED_SLIPS=True)  # ADR 0032: the earlier typed intake
class ConfirmTests(IntakeTestCase):
    def reach_summary(self):
        self.script.extraction = [
            typed_request(
                "withdrawal",
                ("TYPED_METER", 1, "มิเตอร์", ["MT-55667788"], "15(45)A"),
                sap_number="4500100001",
                purpose="ติดตั้งใหม่",
            )
        ]
        self.message(f"{ORDER} หมายเลข MT-55667788 ใบเบิก 4500100001 ติดตั้งใหม่", "applied")
        self.script.decide = [decided("confirm")]
        return self.message("ส่งเลย", "answered")

    def test_send_now_shows_the_summary_and_files_nothing(self):
        sent = self.reach_summary()
        self.assertEqual(self.draft().step, "summary")
        self.assertIn("ยืนยันส่งเรื่อง", buttons(sent))
        self.assertFalse(Case.objects.exists())

    def test_a_confirming_word_right_after_the_summary_files(self):
        self.reach_summary()
        calls = len(self.llm.calls)
        self.say(self.text(ALICE, "ยืนยันครับ"))
        self.assertEqual(Case.objects.count(), 1)
        self.assertEqual(len(self.llm.calls), calls)  # no AI decided it

    def test_a_confirming_word_after_a_change_does_not_file(self):
        self.reach_summary()
        self.script.decide = [decided("reply", reply="เลขใบเบิก SAP ทำได้ในระบบ SAP")]
        self.message("เลข SAP ทำที่ไหน", "answered")  # the summary card is shown again
        self.script.extraction = [
            typed_request(
                "withdrawal",
                ("TYPED_METER", 1, "มิเตอร์", ["MT-55667788"], "15(45)A"),
                sap_number="4500100001",
                purpose="ติดตั้งบ้านใหม่",
            )
        ]
        self.script.decide = [decided("fill")]
        self.message("วัตถุประสงค์ ติดตั้งบ้านใหม่", "applied")  # a correction: a new summary
        draft = self.draft()
        self.assertEqual(draft.step, "summary")
        draft.revision += 1  # a change after this summary
        draft.save()
        self.script.decide = [decided("confirm")]
        self.say(self.text(ALICE, "ยืนยัน"))
        self.assertFalse(Case.objects.exists())

    def test_the_agent_never_drops_a_draft(self):
        self.message(ORDER, "applied")
        self.script.decide = [decided("cancel")]
        sent = self.message("ไม่เอาแล้ว", "answered")
        self.assertEqual(set(buttons(sent)), {"ยกเลิกร่าง", "ทำต่อ"})
        self.assertEqual(self.draft().status, "open")


@override_settings(LINE_TYPED_SLIPS=True)  # ADR 0032: the earlier typed intake
class GuardTests(IntakeTestCase):
    def test_confirmation_deadline_is_not_a_required_wait(self):
        for text in ("รออีก 30 นาทีเพื่อยืนยันส่งเรื่อง", "รอให้ครบ ๓๐ นาทีแล้วค่อยยืนยัน"):
            with self.subTest(text=text):
                self.assertTrue(agent_intake.problem(text, "30 ๓๐", []))
        for text in ("ยืนยันภายใน 30 นาที", "ไม่ต้องรออีก 30 นาที ยืนยันได้เลย"):
            with self.subTest(text=text):
                self.assertEqual(agent_intake.problem(text, "30", []), "")

    def test_dialogue_accepts_typed_confirmations_but_rejects_unknown_buttons(self):
        for text in ('พิมพ์คำว่า "ยืนยัน" เพื่อยืนยันส่งเรื่อง', "พิมพ์ 'ส่งเลย' ได้"):
            with self.subTest(text=text):
                self.assertEqual(ai_dialogue.off_menu(text, []), "")
        for text in ('กด "ยืนยัน" ได้', 'พิมพ์คำว่า "ส่งเรื่องใหม่"', "กด 'บริการ'"):
            with self.subTest(text=text):
                self.assertTrue(ai_dialogue.off_menu(text, []))

    def test_a_reply_that_fails_the_checks_is_retried_once_then_standard(self):
        self.message(ORDER, "applied")
        bad = decided("reply", reply="ส่งเรื่องแล้ว กด 'บริการ' เพื่อดูต่อ")
        self.script.decide = [bad, bad]
        calls = len(self.llm.calls)
        sent = self.message("ทำไมต้องใส่", "answered")
        self.assertEqual(len(self.llm.calls) - calls, 2)
        self.assertNotIn("ส่งเรื่องแล้ว", texts(sent))
        self.assertIn("หมายเลขเครื่องวัด", texts(sent))
        run = AIRun.objects.order_by("-pk").first()
        self.assertEqual(run.result["intake"]["by"], "template")

    def test_old_menus_claims_and_unknown_names_are_refused(self):
        for text in (
            "กด 'บริการ' แล้วเลือกเบิก",
            "เลือกส่งเรื่องใหม่ด้านล่าง",
            "ยื่นเรื่องแล้ว รอผลได้เลย",
            "กด 'ขอใบเสนอราคา' ได้",
        ):
            with self.subTest(text=text):
                self.assertTrue(agent_intake.problem(text, "", []))
        self.assertEqual(agent_intake.problem("กด 'ยกเลิกร่าง' ได้", "", [], ["ยกเลิกร่าง"]), "")

    def test_the_agents_knowledge_names_the_current_menu(self):
        old = "เบิกของ: กด 'ส่งเรื่องใหม่' เลือก 'เบิก' เลือกหมวดและรายการ"
        self.assertEqual(
            agent_chat.OLD_MENU.sub(agent_chat.CURRENT_MENU, old),
            f"เบิกของ: {agent_chat.CURRENT_MENU} เลือกหมวดและรายการ",
        )
        self.assertNotIn("บริการ", agent_chat.SYSTEM_PROMPT)

    def test_a_worded_question_must_ask_what_the_step_asks(self):
        self.assertTrue(agent_intake.off_question("ได้ข้อมูลแล้ว", "more", ("เพิ่ม",)))
        self.assertTrue(agent_intake.off_question("ต่อไปพิมพ์หมายเลขเครื่องวัด", "more", ("เพิ่ม",)))
        self.assertEqual(agent_intake.off_question("ต้องการเพิ่มรายการหรือไม่", "more", ("เพิ่ม",)), "")


@override_settings(LINE_TYPED_SLIPS=True)  # ADR 0032: the earlier typed intake
class SwitchTests(IntakeTestCase):
    def test_the_mode_counts_only_with_a_version_that_passed_dialogue_v1(self):
        with patch.object(agent_intake, "ready", return_value=False):
            self.assertFalse(agent_intake.active())
        self.assertTrue(agent_intake.active())
        options.change(self.dev, options.CHAT_MODE, "guided", "กลับไปถามทีละขั้น")
        self.assertFalse(agent_intake.active())

    def test_ready_needs_the_menu_gate_with_dialogue_v1(self):
        version = self.version
        self.assertFalse(READY(version))  # not evaluated
        version.eval_passed, version.eval_result = True, {"gate": ai.TYPED_EVAL_GATE}
        self.assertFalse(READY(version))
        version.eval_result = {"gate": ai.MENU_EVAL_GATE}
        self.assertTrue(READY(version))
        self.assertIn("dialogue-v1", ai.MENU_EVAL_GATE)

    def test_a_failed_decision_leaves_the_guided_step(self):
        self.message(ORDER, "applied")
        self.script.decide = [ai.AIError("HTTP 503")] * 6
        self.llm.by_model = {}
        self.message("MT-55667788")
        self.assertEqual(self.draft().step, "more")  # the step took the text as before

    def test_at_most_three_model_calls_go_to_one_message(self):
        self.message(ORDER, "applied")
        self.script.decide = [decided("answer", value="MT-55667788")]
        self.message("MT-55667788", "taken")
        calls = len(self.llm.calls)
        self.script.decide = [decided("fill")]
        self.message("เอาไปติดตั้งบ้านลูกค้าใหม่", "applied")
        self.assertLessEqual(len(self.llm.calls) - calls, agent_intake.MAX_CALLS)


@override_settings(LINE_TYPED_SLIPS=True)  # ADR 0032: the earlier typed intake
class MenuTapTests(IntakeTestCase):
    def test_a_menu_tap_with_an_ai_draft_open_asks_first(self):
        self.message(ORDER, "applied")
        tile = LineMenuItem.objects.filter(is_active=True, action="topic").first()
        sent = self.say(self.postback(ALICE, f"m=item&id={tile.pk}"))
        self.assertIn("จะทำต่อไหม", texts(sent))
        self.assertEqual(set(buttons(sent)), {"ทำต่อ", "เริ่มตามเมนูที่กด"})
        sent = self.press(ALICE, "เริ่มตามเมนูที่กด", sent)
        self.assertNotIn("จะทำต่อไหม", texts(sent))
        self.assertEqual(ConversationDraft.objects.filter(status="open").count(), 1)  # kept


@override_settings(LINE_TYPED_SLIPS=True)  # ADR 0032: the earlier typed intake
class FoundByDialoguesTests(IntakeTestCase):
    """Program faults dialogue-v1 found on the real API (25 Oct 2569)."""

    def test_a_withdrawal_never_becomes_a_usage_report(self):
        self.message(ORDER, "applied")  # the meter row is still being typed
        self.script.extraction = [
            menu_request("problem", topic="เครื่องวัดฯ ผิดปกติ", description="ติดตั้งบ้านลูกค้า")
        ]
        self.script.decide = [decided("fill")]
        self.message("ใช้ติดตั้งบ้านลูกค้า")
        draft = self.draft()
        self.assertEqual(draft.kind, "withdrawal")
        self.assertNotEqual(draft.data.get("topic"), "usage")

    def test_a_count_change_keeps_the_row_being_typed(self):
        self.message(ORDER, "applied")
        self.script.extraction = [
            typed_request("withdrawal", ("TYPED_METER", 2, "มิเตอร์", [], None))
        ]
        self.script.decide = [decided("fill")]
        self.message("ขอเปลี่ยนเป็น 2 เครื่อง", "applied")
        draft = self.draft()
        self.assertEqual(draft.step, "entry")
        self.assertEqual(draft.data["entry"]["row"], {"size": "15(45)A", "quantity": "2"})

    def test_the_row_being_typed_stays_when_read_again(self):
        self.message(ORDER, "applied")
        self.script.extraction = [typed_request("withdrawal", METER, purpose="ติดตั้งใหม่")]
        self.script.decide = [decided("fill")]
        self.message("เอาไปติดตั้งใหม่นะ", "applied")
        draft = self.draft()
        self.assertEqual((draft.step, draft.data["entry"]["group"]), ("entry", "meter"))
        self.assertIn("ติดตั้งใหม่", draft.data["fields"]["purpose"])

    def test_send_now_skips_optional_row_fields(self):
        self.script.extraction = [
            typed_request(
                "problem", ("TYPED_OTHER", None, "สว่านไฟฟ้า", [], None), description="เปิดไม่ติด"
            )
        ]
        self.message("สว่านไฟฟ้าเปิดไม่ติด", "applied")
        self.assertEqual(self.draft().step, "entry")
        self.script.decide = [decided("confirm")]
        self.message("ส่งเลย", "answered")
        self.assertEqual(self.draft().step, "summary")

    def test_a_number_is_not_taken_as_a_purpose(self):
        self.script.extraction = [
            typed_request("withdrawal", ("TYPED_METER", 1, "มิเตอร์", ["MT-55667788"], "15(45)A"))
        ]
        self.message(f"{ORDER} หมายเลข MT-55667788", "applied")
        self.assertEqual(self.draft().step, "field:purpose")
        self.script.extraction = [
            typed_request(
                "withdrawal",
                ("TYPED_METER", 1, "มิเตอร์", ["MT-55667788"], "15(45)A"),
                sap_number="4500100013",
            )
        ]
        self.script.decide = [decided("answer", value="4500100013")]
        self.message("4500100013")
        draft = self.draft()
        self.assertNotIn("purpose", draft.data["fields"])
        self.assertEqual(draft.data.get("sap_number"), "4500100013")

    def test_a_purpose_read_as_another_request_stays_in_the_draft(self):
        self.message(ORDER, "applied")
        self.script.decide = [decided("answer", value="MT-55667788")]
        self.message("MT-55667788", "taken")
        self.assertEqual(self.draft().step, "more")
        self.script.extraction = [
            menu_request("problem", topic="เครื่องวัดฯ ผิดปกติ", description="ใช้ติดตั้งบ้านลูกค้า")
        ]
        self.script.decide = [decided("fill")]
        self.message("ใช้ติดตั้งบ้านลูกค้า", "taken")
        draft = self.draft()  # still the one draft
        self.assertEqual(draft.kind, "withdrawal")
        self.assertEqual(draft.data["fields"]["purpose"], "ใช้ติดตั้งบ้านลูกค้า")

    def test_other_equipment_keeps_the_division_the_message_named(self):
        item = ("TYPED_OTHER", None, "สว่านไฟฟ้า", ["DR-2001"], None)
        self.script.extraction = [menu_request("repair", item, division="กคอ.")]
        self.message("ติดตามงานซ่อมสว่านไฟฟ้า หมายเลข DR-2001", "applied")
        self.assertEqual(self.draft().data.get("division"), "กคอ.")
