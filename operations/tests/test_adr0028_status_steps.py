"""ขั้นที่ผู้แจ้งเห็น and ข้อความแจ้งสถานะ (ADR 0028, grill สถานะแบบ ServiceNow 28 ต.ค. 2569):
staff change a status from the case page with a notice and an internal note (FormTests); the
requester reads four steps, the real status and the latest notice in LINE, once, and answers by
opening the case's chat (LineTests); which step a status shows is an admin option, and moving
into รอข้อมูลจากคุณ or เสร็จสิ้น needs a notice (RuleTests)."""

import uuid

from django.test import Client

from operations import options, requester_steps
from operations.models import Case, CaseMessage, Conversation
from operations.tests.test_m03_line import ALICE, texts
from operations.tests.test_m16_kcw_work import WorkTestCase

S = Case.Status


class StatusTestCase(WorkTestCase):
    def setUp(self):
        super().setUp()
        self.case = self.withdraw("4500070001")
        self.client = Client()
        self.client.force_login(self.kcw)

    def act(self, action, **data):
        case = Case.objects.get(pk=self.case.pk)
        form = {"revision": case.revision, "idempotency_key": uuid.uuid4().hex, **data}
        response = self.client.post(f"/cases/{case.number}/act/{action}/", form)
        self.case.refresh_from_db()
        return response


class FormTests(StatusTestCase):
    def test_a_status_form_carries_a_notice_and_an_internal_note(self):
        self.assertEqual(self.case.status, S.IN_PROGRESS)
        form = self.client.get(f"/cases/{self.case.number}/act/hold/")
        self.assertContains(form, "ข้อความแจ้งสถานะ")
        self.assertContains(form, "บันทึกภายใน (ผู้แจ้งไม่เห็น)")
        self.act(
            "hold", reason="รอของ", notice="รอของเข้าคลัง คาดว่าจ่ายได้ 5 พ.ย.", internal_note="ล็อตใหม่"
        )
        self.assertEqual(self.case.status, S.ON_HOLD)
        decision = self.case.decisions.order_by("-id").first()
        self.assertEqual(decision.data["notice"], "รอของเข้าคลัง คาดว่าจ่ายได้ 5 พ.ย.")
        self.assertEqual(decision.data["internal_note"], "ล็อตใหม่")
        page = self.client.get(f"/cases/{self.case.number}/")
        self.assertContains(page, "ข้อความแจ้งสถานะ: รอของเข้าคลัง")
        self.assertContains(page, "บันทึกภายใน: ล็อตใหม่")
        thread = self.client.get(f"/cases/{self.case.number}/chat/thread/")
        self.assertContains(thread, "chat__strip")
        self.assertContains(thread, "พักเรื่อง")
        self.assertNotContains(thread, "ล็อตใหม่")  # internal notes never reach the chat

    def test_a_reason_the_requester_reads_is_the_notice(self):
        form = self.client.get(f"/cases/{self.case.number}/act/request_info/")
        self.assertNotContains(form, 'name="notice"')
        self.act("request_info", reason="ขอรูปใบเบิกที่มีลายเซ็น")
        self.assertEqual(requester_steps.notices(self.case)[0][1], "ขอรูปใบเบิกที่มีลายเซ็น")

    def test_an_unchanged_status_keeps_no_notice(self):
        form = self.client.get(f"/cases/{self.case.number}/act/claim/")
        self.assertNotContains(form, 'name="notice"')
        self.act("claim", notice="ไม่ควรเก็บ")
        decision = self.case.decisions.order_by("-id").first()
        self.assertEqual(decision.action, "claim")
        self.assertNotIn("notice", decision.data)


class LineTests(StatusTestCase):
    def test_the_card_shows_the_step_the_status_and_the_notice_once(self):
        self.act("hold", reason="รอของ", notice="กำลังเตรียมของ")
        reply = self.type(ALICE, self.case.number)
        text = texts(reply)
        self.assertIn("●●○○ กำลังดำเนินการ", text)
        self.assertIn("สถานะในระบบ: พักเรื่อง", text)
        self.assertIn("ข้อความจากเจ้าหน้าที่", text)
        self.assertIn("กำลังเตรียมของ", text)
        self.assertNotIn("มีอัปเดตใหม่", text)  # Q10: the card already says it
        listing = texts(self.type(ALICE, "เรื่องของฉัน"))
        self.assertIn(f"{self.case.number} · ", listing)
        self.assertIn("กำลังดำเนินการ", listing)

    def test_a_case_waiting_for_its_division_reads_in_progress(self):
        # 30 ต.ค. 2569: the division's own status stays at the same step.
        text = texts(self.type(ALICE, self.case.number))
        self.assertIn("●●○○ กำลังดำเนินการ\n", text)  # ADR 0042 Q10: in progress at once
        self.assertNotIn("สถานะในระบบ", text)
        # Q3, Q5: the LINE user reads กำลังดำเนินการ; staff pages keep รอรับเรื่อง.
        Case.objects.filter(pk=self.case.pk).update(status=S.AWAITING_APPROVAL)
        text = texts(self.type(ALICE, self.case.number))
        self.assertIn("●●○○ กำลังดำเนินการ", text)
        self.assertNotIn("สถานะในระบบ", text)  # it would say the same again
        self.assertNotIn("รอรับเรื่อง", text)
        self.assertContains(self.client.get(f"/cases/{self.case.number}/"), "รอรับเรื่อง")
        options.change(None, options.LINE_AWAITING, "real", "ทดสอบ")
        options.change(None, options.STEP_PREFIX + "awaiting_approval", "received", "ทดสอบ")
        text = texts(self.type(ALICE, self.case.number))
        self.assertIn("●○○○ รับเรื่องแล้ว\nสถานะในระบบ: รอรับเรื่อง", text)

    def test_the_next_message_says_the_new_step_and_notice(self):
        self.type(ALICE, "เรื่องของฉัน")  # marks what the requester has seen
        self.act("hold", reason="รอของ", notice="ของเข้าคลังแล้ว")
        text = texts(self.type(ALICE, "สวัสดี"))
        self.assertIn(f"มีอัปเดตใหม่\n{self.case.number} · กำลังดำเนินการ", text)
        self.assertIn("ข้อความจากเจ้าหน้าที่: ของเข้าคลังแล้ว", text)

    def test_answering_staff_opens_the_case_chat(self):
        self.act("hold", reason="รอของ", notice="กำลังเตรียมของ")
        card = self.type(ALICE, self.case.number)
        sent = len(self.sent)
        reply = self.press(ALICE, "ตอบเจ้าหน้าที่", card)
        self.assertIn("พิมพ์ข้อความถึงเจ้าหน้าที่", texts(reply))
        self.assertEqual(Conversation.objects.get(identity__external_id=ALICE).prompt, "case_chat")
        self.type(ALICE, "ขอบคุณครับ")
        self.assertEqual(len(self.sent), sent + 1)  # only the prompt; the bot keeps quiet
        answer = CaseMessage.objects.get(direction="requester")
        self.assertEqual((answer.case_id, answer.text), (self.case.pk, "ขอบคุณครับ"))

    def test_earlier_notices_are_one_tap_away(self):
        self.act("hold", reason="รอของ", notice="ข้อความแรก")
        self.act("resume", notice="ข้อความที่สอง")
        card = self.type(ALICE, self.case.number)
        self.assertIn("ข้อความที่สอง", texts(card))
        self.assertNotIn("ข้อความแรก", texts(card))
        older = texts(self.press(ALICE, "ดูข้อความก่อนหน้า", card))
        self.assertLess(older.index("ข้อความที่สอง"), older.index("ข้อความแรก"))


class RuleTests(StatusTestCase):
    def test_an_admin_option_moves_a_status_to_another_step(self):
        self.assertEqual(requester_steps.key(S.ON_HOLD), "working")
        options.change(None, options.STEP_PREFIX + "on_hold", "need_you", "ทดสอบ")
        self.assertEqual(requester_steps.key(S.ON_HOLD), "need_you")
        self.assertEqual(requester_steps.bar(S.ON_HOLD), "●●●○")
        self.act("hold", reason="รอของ")  # 13 พ.ย. 2569: the notice is never forced
        self.assertEqual(self.case.status, S.ON_HOLD)

    def test_a_refused_case_has_no_step_and_a_closed_one_is_done(self):
        self.assertEqual(requester_steps.bar(S.REJECTED), "")
        self.assertEqual(requester_steps.LABEL[requester_steps.key(S.CLOSED)], "เสร็จสิ้น")
