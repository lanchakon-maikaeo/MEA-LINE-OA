"""ADR 0057 (12 พ.ย. 2569): a case followed by its number is answered with แจ้งผลให้ผู้แจ้ง, one
step in which staff type what was done; the requester gets it in LINE with ยืนยันผล and ปฏิเสธ.
The four warehouse stages stay as a choice, first only in a กอง that keeps กำหนดจ่ายตามขนาด
(กคว.); กคอ., whose truck day is its pay day, answers first like กอจ.

Acceptance → tests: in กอจ. แจ้งผลให้ผู้แจ้ง is the step to do next and the stage comes after
it; the typed text, a choice since 13 พ.ย. (a blank one sends the set words), moves the case
to รอยืนยันผล with no stage recorded and reaches the requester as a card with both buttons;
ยืนยันผล closes the case; ปฏิเสธ asks for a reason
and gives the case back, where staff answer again; staff close after the wait; the buttons of
a message that waited show under the answer that follows it; a งานคลัง page of returns reads
the return's stage names (KojTests). In
กคว. the stage stays first and the step is among the others, works from the LINE card, and the
last stage's own message carries ปฏิเสธ too; a กอง whose truck day is its pay day, as กคอ.,
answers first (TableDivisionTests). A withdrawal with item rows,
which answers per delivery, takes no refusal of this kind (ItemRowsTests).
"""

from datetime import timedelta

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from operations import case_menu, notifications, orchestration, problem_chat, warehouse
from operations.contracts import ErrorCode
from operations.models import (
    Case,
    CaseMilestone,
    ChannelIdentity,
    Decision,
    DivisionSchedule,
    OutboxEvent,
)
from operations.tests.test_adr0055_line_card import LineCardTestCase
from operations.tests.test_adr0056_slip_work_without_a_table import KojSlipTestCase
from operations.tests.test_m02_cases import CaseTestCase
from operations.tests.test_m03_line import ALICE, buttons, texts
from operations.tests.test_m28_accept_and_cards import HEAD, act

WAITING = Case.Status.AWAITING_CONFIRMATION
DONE = "รับคืนเข้าคลังแล้ว 12 พ.ย. เลขรับคืน 4917646"


def card(case):
    """The card pushed to the requester for the last แจ้งผลให้ผู้แจ้ง: (text, {label: postback})."""
    event = OutboxEvent.objects.filter(
        event_type=orchestration.EVENT_DECISION, payload__action=warehouse.REPORT
    ).latest("pk")
    planned = notifications.plan_for_event(event)
    footer = planned.message["contents"]["footer"]["contents"]
    return planned, {b["action"]["label"]: b["action"]["data"] for b in footer}


class KojTests(KojSlipTestCase):
    def test_a_page_of_returns_reads_the_returns_stages(self):
        self.file_return()
        page = self.web.get(reverse("warehouse", args=[self.koj.code])).content.decode()
        for words in ("ยังไม่รับใบส่งคืน", "รับใบส่งคืนแล้ว", "รับเข้าคลังแล้ว", ">เลขที่ใบ<"):
            self.assertIn(words, page)
        self.assertNotIn("รถรับของแล้ว", page)  # no stage of a return any more
        self.assertNotIn("ใบเบิกแล้ว", page)
        self.assertNotIn("ยังไม่รับใบเบิก", page)

    def test_the_buttons_of_a_waiting_message_show_under_a_menu_answer(self):
        # LINE shows the last message's quick replies only: a menu tap after the last stage
        # is answered with the list, which then carries ยืนยันผล and ปฏิเสธ.
        case = self.file_return()
        for stage in warehouse.stage_order(case.kind):
            warehouse.record(self.head, [case], stage, timezone.localdate())
        reply = self.say(self.postback(ALICE, "m=mine"))
        self.assertGreater(len(reply), 1)
        last = [i["action"]["label"] for i in reply[-1]["quickReply"]["items"]]
        self.assertEqual(last[:2], [warehouse.CONFIRM_LABEL, warehouse.REJECT_LABEL])
        self.assertNotIn("จบการคุย", last)
        self.press(ALICE, warehouse.REJECT_LABEL, reply[-1:])
        self.type(ALICE, "ยังไม่ได้รับของ")
        case.refresh_from_db()
        self.assertEqual(case.status, Case.Status.IN_PROGRESS)

    def test_the_step_comes_first_and_the_stage_after_it(self):
        case = self.file_return()
        menu = case_menu.build(self.head, case)
        self.assertEqual((menu.first.name, menu.first.label), ("report_result", "แจ้งผลให้ผู้แจ้ง"))
        self.assertIn(("stage", "received"), [(i.kind, i.name) for i in menu.more])

    def test_a_blank_answer_goes_out_with_the_set_words(self):
        # 13 พ.ย. 2569 (owner): the web forces no typing; the card then says the work is done.
        case = act(self.head, self.file_return(), "report_result")
        self.assertEqual(case.status, WAITING)
        identity = ChannelIdentity.objects.get(external_id=ALICE)
        text, _ = problem_chat.resolution_card(case, identity)
        self.assertIn(warehouse.DONE_TEXT, text)

    def test_the_typed_text_reaches_the_requester_who_confirms(self):
        case = self.file_return()
        case = act(self.head, case, "report_result", message=DONE)
        self.assertEqual(case.status, WAITING)
        self.assertFalse(CaseMilestone.objects.filter(case=case).exists())
        planned, taps = card(case)
        self.assertEqual((planned.kind, planned.level), ("resolution_request", "critical"))
        self.assertIn(DONE, planned.text)
        self.assertEqual(list(taps), ["ยืนยันผล", "ปฏิเสธ"])
        reply = self.say(self.postback(ALICE, taps["ยืนยันผล"]))
        self.assertIn("ปิดแล้ว", texts(reply))
        case.refresh_from_db()
        self.assertEqual(case.status, Case.Status.CLOSED)

    def test_a_refusal_gives_the_case_back_with_its_reason(self):
        case = act(self.head, self.file_return(), "report_result", message=DONE)
        _, taps = card(case)
        reply = self.say(self.postback(ALICE, taps["ปฏิเสธ"]))
        self.assertIn("พิมพ์", texts(reply))
        reply = self.type(ALICE, "ของยังอยู่ที่เขต รถยังไม่มารับ")
        self.assertIn("ดำเนินการต่อ", texts(reply))
        case.refresh_from_db()
        self.assertEqual(case.status, Case.Status.IN_PROGRESS)
        refusal = Decision.objects.get(case=case, action="dispute_resolution")
        self.assertEqual(refusal.reason, "ของยังอยู่ที่เขต รถยังไม่มารับ")
        self.assertEqual(case_menu.build(self.head, case).first.name, "report_result")
        case = act(self.head, case, "report_result", message="รถไปรับแล้ว 13 พ.ย.")
        self.assertIn("รถไปรับแล้ว 13 พ.ย.", card(case)[0].text)

    def test_the_requester_finds_both_buttons_on_the_case_again(self):
        case = act(self.head, self.file_return(), "report_result", message=DONE)
        detail = self.type(ALICE, case.number)
        self.assertIn("ยืนยันผล", buttons(detail))
        self.assertIn("ปฏิเสธ", buttons(detail))

    def test_no_stage_leads_while_the_requester_is_asked(self):
        case = self.file_return()
        self.assertEqual(warehouse.stage_label(case, "received"), "รับใบส่งคืนแล้ว")  # a return
        work = reverse("warehouse", args=[self.koj.code])
        self.assertContains(self.web.get(work), case.number)
        case = act(self.head, case, "report_result", message=DONE)
        menu = case_menu.build(self.head, case)
        self.assertNotIn("stage", [i.kind for i in menu.items])
        self.assertNotContains(self.web.get(work), case.number)  # no work left on งานคลัง

    def test_a_pushed_card_is_not_repeated_as_an_update(self):
        case = act(self.head, self.file_return(), "report_result", message=DONE)
        planned, taps = card(case)
        self.assertIn("มีอัปเดตใหม่", texts(self.type(ALICE, "สวัสดี")))  # not pushed: shown once
        case = act(self.head, case, "dispute_resolution", channel="phone", intake_note="โทรมา")
        case = act(self.head, case, "report_result", message="รถไปรับแล้ว")
        planned, taps = card(case)
        type(planned).objects.filter(pk=planned.pk).update(status="sent")
        self.assertNotIn("มีอัปเดตใหม่", texts(self.say(self.postback(ALICE, taps["ยืนยันผล"]))))

    def test_staff_close_only_after_the_wait(self):
        case = act(self.head, self.file_return(), "report_result", message=DONE)
        self.assertNotIn("close", [i.name for i in case_menu.build(self.head, case).items])
        days = settings.RECEIPT_NO_RESPONSE_DAYS + 1
        Decision.objects.filter(case=case, action=warehouse.REPORT).update(
            created_at=timezone.now() - timedelta(days=days)
        )
        self.assertIn("close", [i.name for i in case_menu.build(self.head, case).items])


class TableDivisionTests(LineCardTestCase):
    def test_a_กอง_paying_on_its_truck_day_answers_first_like_กอจ(self):
        DivisionSchedule.objects.filter(division=self.division).update(
            sla=[], delivery=DivisionSchedule.Delivery.TRUCK
        )
        case = self.withdraw("4500057004")
        menu = case_menu.build(self.head, case)
        self.assertEqual(menu.first.name, "report_result")
        self.assertIn("stage", [i.kind for i in menu.more])

    def test_the_stage_stays_first_and_the_step_is_among_the_others(self):
        case = self.withdraw("4500057001")
        menu = case_menu.build(self.head, case, line=True)
        self.assertEqual(menu.first.kind, "stage")
        item = menu.find("action", "report_result")
        self.assertIn(item, menu.more)
        self.assertTrue(item.line)  # asked in chat, no web form needed

    def test_the_step_is_done_from_the_line_card(self):
        case = self.withdraw("4500057002")
        reply = self.press(HEAD, "แจ้งผลให้ผู้แจ้ง", self.card(case))
        self.assertIn("ข้อความถึงผู้แจ้ง", texts(reply))
        reply = self.type(HEAD, "จ่ายของแล้ว รับได้ที่คลัง")
        while "บันทึก" not in buttons(reply):
            reply = self.press(HEAD, "ข้าม", reply)
        self.press(HEAD, "บันทึก", reply)
        case.refresh_from_db()
        self.assertEqual(case.status, WAITING)
        identity = ChannelIdentity.objects.get(external_id=ALICE)
        text, _ = problem_chat.resolution_card(case, identity)
        self.assertIn("จ่ายของแล้ว รับได้ที่คลัง", text)

    def test_the_last_stage_offers_the_refusal_too(self):
        case = self.withdraw("4500057003")
        for stage in CaseMilestone.ORDER:
            warehouse.record(self.head, [case], stage, timezone.localdate(), timezone.localdate())
        reply = self.type(ALICE, "ขอบคุณ")
        self.assertIn("ยืนยันผล", buttons(reply))
        self.press(ALICE, "ปฏิเสธ", reply)
        self.type(ALICE, "ยังไม่ได้รับของ")
        case.refresh_from_db()
        self.assertEqual(case.status, Case.Status.IN_PROGRESS)
        # every stage is recorded, so the answer is the step left
        self.assertEqual(case_menu.build(self.head, case).first.name, "report_result")


class ItemRowsTests(CaseTestCase):
    def test_a_case_that_is_not_followed_by_its_number_takes_no_refusal(self):
        case = self.make_case(Case.Kind.WITHDRAWAL, WAITING)
        self.assertFails(
            ErrorCode.INVALID_TRANSITION,
            self.act,
            self.as_requester(),
            case,
            "dispute_resolution",
            payload={"reason": "ยังไม่ได้รับ"},
        )
