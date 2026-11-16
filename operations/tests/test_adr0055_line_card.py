"""ADR 0055 (grill 10 พ.ย. 2569): the LINE case card and the web's สิ่งที่ทำได้ are one list;
warehouse stages, steps that cannot be undone and appointments work in LINE; a time picked
freely belongs to its appointment; ปิดเรื่อง waits for the requester's ยืนยันผล.

Acceptance → tests: the card shows the web's list in the web's order and groups, a web-only
step grey, one card with nothing to swipe to (MenuTests). A stage is recorded in LINE with
วันนี้ first, back-dating but not a future day, the pay day suggested, the consequence said,
and marked ทาง LINE on the web; an assignee who has not pressed รับงาน cannot; an older card's
button gives the fresh card (StageTests). An appointment is confirmed, moved to a tapped time or
a freely picked one, or refused with a reason; the free time clashes only with a full overlapping
appointment, warns on a weekend, stays off the requester's cards and is never shown as free
(AppointmentTests). The last stage of a tracking case waits for the requester's ยืนยันผล, which
closes it; staff record the answer for them or close after the wait; a refusal without a reason
has no empty เหตุผล line (CloseTests).
"""

from datetime import datetime, time, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from operations import calendars, case_menu, notifications, orchestration, staff_line, warehouse
from operations.models import (
    Appointment,
    Calendar,
    CalendarSlot,
    Case,
    CaseMilestone,
    Decision,
    DivisionSchedule,
    OutboxEvent,
)
from operations.tests.test_m03_line import ALICE, buttons, texts
from operations.tests.test_m28_accept_and_cards import HEAD, AcceptTestCase, act

S = CaseMilestone.Stage


class LineCardTestCase(AcceptTestCase):
    def setUp(self):
        super().setUp()
        DivisionSchedule.objects.update_or_create(
            division=self.division,
            defaults={
                "sla": [{"group": "meter", "size": "", "days": 3}],
                "issue_weekdays": [1, 3],
                "truck_days": {"D01": [4]},
            },
        )

    def card(self, case, user=HEAD):
        token = staff_line._token(
            self.line_of(user), "staff_case", {"case": str(case.pk)}, timezone.now()
        )
        return self.say(self.postback(user, token))

    def line_of(self, external_id):
        from operations.models import ChannelIdentity

        return ChannelIdentity.objects.get(external_id=external_id)

    def web(self, user, case):
        client = Client()
        client.force_login(user)
        return client.get(reverse("case-detail", args=[case.number]))


class MenuTests(LineCardTestCase):
    def test_the_card_shows_the_web_list_in_the_web_order(self):
        case = self.withdraw("4500055001")
        menu = case_menu.build(self.head, case, line=True)
        self.assertEqual(menu.first.label, "บันทึกรับใบเบิกแล้ว")  # ADR 0051 Q8: the stage first
        reply = self.card(case)
        shown = [label for label in buttons(reply) if label != "‹ กลับไปที่รายการ"]
        rest = [i for i in menu.items if i is not menu.first]
        grouped = [
            menu.first,
            *(i for i in rest if i.line and not i.irreversible),
            *(i for i in rest if not i.line and not i.irreversible),
            *(i for i in rest if i.irreversible),
        ]
        self.assertEqual(shown, [i.label for i in grouped])
        self.assertIn("‹ กลับไปที่รายการ", buttons(reply))
        page = self.web(self.head, case).content.decode()
        places = [page.index(f">{i.label}<") for i in menu.items]
        self.assertEqual(places, sorted(places))  # the web in the same order
        self.assertIn("ทำต่อ", texts(reply))
        self.assertEqual(len(reply[-1]["contents"]["contents"]), 1)  # one card, nothing to swipe

    def test_every_step_is_on_the_card_with_none_cut(self):
        case = self.withdraw("4500055002")
        menu = case_menu.build(self.head, case, line=True)
        self.assertGreater(len(menu.items), 0)
        shown = buttons(self.card(case))
        for item in menu.items:
            self.assertIn(item.label, shown)


class StageTests(LineCardTestCase):
    def test_a_stage_is_recorded_in_line_and_marked_on_the_web(self):
        case = self.withdraw("4500055011")
        reply = self.press(HEAD, "บันทึกรับใบเบิกแล้ว", self.card(case))
        self.assertIn("ย้อนหลังได้ แต่ไม่เกินวันนี้", texts(reply))
        self.assertEqual(list(buttons(reply))[0], "วันนี้")
        reply = self.press(HEAD, "วันนี้", reply)
        self.assertIn("โปรดตรวจสอบก่อนบันทึก", texts(reply))
        reply = self.press(HEAD, "บันทึก", reply)
        self.assertIn("บันทึกเรียบร้อยแล้ว: บันทึกรับใบเบิกแล้ว", texts(reply))
        milestone = CaseMilestone.objects.get(case=case, stage=S.RECEIVED)
        self.assertEqual((milestone.via, milestone.recorded_by), ("line", self.head))
        self.assertContains(self.web(self.head, case), "ทาง LINE")
        self.assertIn("บันทึกคีย์จ่ายแล้ว", buttons(reply))  # the fresh card, next stage

    def test_keyed_asks_the_pay_day_and_says_what_the_requester_sees(self):
        case = self.withdraw("4500055012")
        warehouse.record(self.head, [case], S.RECEIVED, timezone.localdate())
        reply = self.press(HEAD, "บันทึกคีย์จ่ายแล้ว", self.card(case))
        reply = self.press(HEAD, "วันนี้", reply)
        self.assertIn("วันจ่าย", texts(reply))
        suggested = next(label for label in buttons(reply) if label.endswith("ตามตาราง"))
        reply = self.press(HEAD, suggested, reply)
        self.assertIn("ผู้แจ้งจะเห็นวันจ่าย", texts(reply))
        self.press(HEAD, "บันทึก", reply)
        keyed = CaseMilestone.objects.get(case=case, stage=S.KEYED)
        self.assertIsNotNone(keyed.planned_on)

    def test_a_past_day_is_kept_and_a_future_one_refused(self):
        case = self.withdraw("4500055013")
        reply = self.press(HEAD, "บันทึกรับใบเบิกแล้ว", self.card(case))
        tomorrow = timezone.localdate() + timedelta(days=1)
        reply = self.type(HEAD, f"{tomorrow.day}/{tomorrow.month}/{tomorrow.year + 543}")
        self.assertIn("ต้องไม่เกินวันนี้", texts(reply))
        before = timezone.localdate() - timedelta(days=2)
        reply = self.type(HEAD, f"{before.day}/{before.month}/{before.year + 543}")
        self.press(HEAD, "บันทึก", reply)
        self.assertEqual(CaseMilestone.objects.get(case=case, stage=S.RECEIVED).on, before)

    def test_an_assignee_who_has_not_taken_it_records_no_stage(self):
        case = self.assign(self.withdraw("4500055014"), self.kcw)
        self.assertFalse([i for i in case_menu.build(self.kcw, case).items if i.kind == "stage"])
        done, skipped = warehouse.record(self.kcw, [case], S.RECEIVED, timezone.localdate())
        self.assertEqual((done, skipped[0][1][:9]), ([], "กดรับงานเ"))

    def test_an_older_card_gives_the_fresh_one(self):
        case = self.withdraw("4500055015")
        old = self.card(case)
        warehouse.record(self.head, [case], S.RECEIVED, timezone.localdate())
        reply = self.press(HEAD, "บันทึกรับใบเบิกแล้ว", old)
        self.assertIn("ขั้นนี้ทำไม่ได้แล้ว", texts(reply))
        self.assertIn("บันทึกคีย์จ่ายแล้ว", buttons(reply))


class AppointmentTests(LineCardTestCase):
    """ADR 0055's appointment steps are นัดรับของ's; โน้ตบุ๊กและหัวอ่าน keeps its flow."""

    def setUp(self):
        super().setUp()
        Calendar.objects.filter(key__in=["pickup", "notebook"]).update(default_times=[])
        self.calendar = Calendar.objects.get(key="pickup")
        self.notebook = Calendar.objects.get(key="notebook")
        self.day = calendars.bookable_days()[1]

    def slot(self, hour, capacity=1, calendar=None):
        start = timezone.make_aware(datetime.combine(self.day, time(hour)))
        return CalendarSlot.objects.create(
            calendar=calendar or self.calendar,
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            capacity=capacity,
        )

    def ask(self, case, slot):
        return Appointment.objects.create(
            calendar=slot.calendar, case=case, slot=slot, status=Appointment.Status.REQUESTED
        )

    def test_the_requested_time_is_confirmed_from_the_card(self):
        case = self.withdraw("4500055021")
        asked = self.ask(case, self.slot(9))
        label = case_menu.build(self.head, case).first.label
        reply = self.press(HEAD, label, self.card(case))
        self.assertIn("ช่วงที่ขอ", texts(reply))
        self.assertIn("ไม่รับนัด", buttons(reply))
        reply = self.press(HEAD, "ยืนยันนัด", reply)
        self.assertIn("รับของทางไหน", texts(reply))  # a withdrawal's นัดรับของ asks how
        reply = self.press(HEAD, "ฟข. มารับเอง", reply)
        self.assertIn("โปรดตรวจสอบก่อนบันทึก: ยืนยันนัด", texts(reply))
        self.press(HEAD, "บันทึก", reply)
        asked.refresh_from_db()
        self.assertEqual(
            (asked.status, asked.method),
            (Appointment.Status.CONFIRMED, Appointment.Method.PICKUP),
        )

    def test_a_tapped_time_moves_it_and_a_refusal_needs_a_reason(self):
        case = self.withdraw("4500055022")
        asked = self.ask(case, self.slot(9))
        other = self.slot(11)
        label = case_menu.build(self.head, case).first.label
        reply = self.press(HEAD, label, self.card(case))
        reply = self.press(HEAD, f"ย้ายนัดไป {calendars.short(other)}", reply)
        reply = self.press(HEAD, "ฟข. มารับเอง", reply)
        self.press(HEAD, "บันทึก", reply)
        asked.refresh_from_db()
        self.assertEqual((asked.slot, asked.status), (other, Appointment.Status.CONFIRMED))
        second = self.ask(case, self.slot(13))
        label = case_menu.build(self.head, case).first.label
        reply = self.press(HEAD, "ไม่รับนัด", self.press(HEAD, label, self.card(case)))
        self.assertIn("พิมพ์เหตุผล", texts(reply))
        reply = self.type(HEAD, "ช่วงนั้นรถไม่ว่าง")
        self.press(HEAD, "บันทึก", reply)
        second.refresh_from_db()
        self.assertEqual(second.status, Appointment.Status.REJECTED)

    def test_a_free_time_is_private_and_clashes_only_with_a_full_one(self):
        case = self.withdraw("4500055023")
        asked = self.ask(case, self.slot(9))
        full = self.slot(14)
        self.ask(self.withdraw("4500055024"), full)
        clash = timezone.make_aware(datetime.combine(self.day, time(14, 30)))
        with self.assertRaises(ValidationError):
            calendars.free_time(self.calendar, clash, asked)
        at = timezone.make_aware(datetime.combine(self.day, time(16, 15)))
        calendars.decide(self.head, asked, "move", at=at)
        asked.refresh_from_db()
        self.assertTrue(asked.slot.private)
        self.assertEqual(asked.slot.ends_at - at, timedelta(minutes=self.calendar.default_minutes))
        self.assertNotIn(asked.slot, calendars.free_slots(self.calendar.key))
        cards = calendars.day_cards(self.calendar.key)
        self.assertNotIn(asked.slot, [s for _, times in cards for s, _ in times])
        saturday = self.day + timedelta(days=(5 - self.day.weekday()) % 7)
        weekend = timezone.make_aware(datetime.combine(saturday, time(10)))
        self.assertEqual(calendars.free_time(self.calendar, weekend)[2], ["วันเสาร์"])
        past = timezone.now() - timedelta(hours=1)
        with self.assertRaises(ValidationError):
            calendars.free_time(self.calendar, past)

    def test_an_unused_private_time_is_not_on_the_month(self):
        CalendarSlot.objects.create(
            calendar=self.calendar,
            starts_at=timezone.make_aware(datetime.combine(self.day, time(8))),
            ends_at=timezone.make_aware(datetime.combine(self.day, time(9))),
            private=True,
        )
        weeks = calendars.month(self.head, self.calendar, self.day)
        shown = [s for week in weeks for d in week for s in d["slots"]]
        self.assertFalse([s for s in shown if s.private])

    def test_the_web_form_takes_a_free_time(self):
        case = self.withdraw("4500055025")
        asked = self.ask(case, self.slot(9))
        client = Client()
        client.force_login(self.head)
        at = datetime.combine(self.day, time(15, 40))
        client.post(
            reverse("case-appointment", args=[case.number]),
            {"appointment": asked.pk, "action": "move", "at": at.strftime("%Y-%m-%dT%H:%M")},
        )
        asked.refresh_from_db()
        self.assertEqual(asked.status, Appointment.Status.CONFIRMED)
        self.assertTrue(asked.slot.private)

    def test_the_notebook_and_reader_flow_is_as_it_was(self):
        """No pickup method, no free time, no LINE appointment step and no รับงาน first."""
        case = self.withdraw("4500055026")
        booked = self.ask(case, self.slot(9, calendar=self.notebook))
        Appointment.objects.filter(pk=booked.pk).update(method=Appointment.Method.PICKUP)
        booked.refresh_from_db()
        self.assertFalse(booked.takes_method)
        item = case_menu.build(self.head, case, line=True).first
        self.assertEqual((item.name, item.line), (str(booked.pk), False))
        card = self.card(case)
        self.assertNotIn("ฟข. มารับเอง", texts(card))
        self.assertIn("ขั้นนี้ทำในเว็บ", texts(self.press(HEAD, item.label, card)))
        form = Client()
        form.force_login(self.head)
        url = f"{reverse('case-appointment', args=[case.number])}?appointment={booked.pk}"
        page = form.get(url, HTTP_HX_REQUEST="true")
        self.assertNotContains(page, "วิธีรับของ")
        self.assertNotContains(page, "หรือเลือกวันเวลาเอง")
        self.assertNotContains(self.web(self.head, case), "ฟข. มารับเอง")
        at = timezone.make_aware(datetime.combine(self.day, time(15)))
        with self.assertRaises(ValidationError):
            calendars.decide(self.head, booked, "move", at=at)
        with self.assertRaises(ValidationError):
            calendars.decide(self.head, booked, "confirm", method=Appointment.Method.PICKUP)
        # An assignee answers a notebook booking before รับงาน, as before ADR 0055.
        self.assign(case, self.kcw)
        steps = case_menu.build(self.kcw, case).items
        self.assertIn(str(booked.pk), [i.name for i in steps if i.kind == "appointment"])
        calendars.decide(self.kcw, booked, "confirm")
        booked.refresh_from_db()
        self.assertEqual(booked.status, Appointment.Status.CONFIRMED)

    def test_an_assignee_answers_a_pickup_only_after_taking_the_case(self):
        case = self.withdraw("4500055027")
        asked = self.ask(case, self.slot(9))
        self.assign(case, self.kcw)
        self.assertFalse(
            [i for i in case_menu.build(self.kcw, case).items if i.kind == "appointment"]
        )
        with self.assertRaises(ValidationError):
            calendars.decide(self.kcw, asked, "confirm")
        pickup = Appointment(calendar=self.calendar, case=case)
        self.assertTrue(pickup.takes_method)
        case.kind = Case.Kind.RETURN
        self.assertFalse(pickup.takes_method)


class CloseTests(LineCardTestCase):
    def finish(self, slip):
        case = self.withdraw(slip)
        for stage in CaseMilestone.ORDER:
            warehouse.record(self.head, [case], stage, timezone.localdate(), timezone.localdate())
        case.refresh_from_db()
        return case

    def test_the_last_stage_waits_for_the_requester(self):
        case = self.finish("4500055031")
        self.assertEqual(case.status, Case.Status.AWAITING_CONFIRMATION)
        reply = self.type(ALICE, "ขอบคุณ")
        self.assertIn("ยืนยันผล", buttons(reply))
        self.press(ALICE, "ยืนยันผล", reply)
        case.refresh_from_db()
        self.assertEqual(case.status, Case.Status.CLOSED)

    def test_staff_close_only_after_the_wait(self):
        case = self.finish("4500055032")
        menu = [i.name for i in case_menu.build(self.head, case).items]
        self.assertNotIn("close", menu)
        days = settings.RECEIPT_NO_RESPONSE_DAYS + 1
        Decision.objects.filter(case=case, action=warehouse.FINISH).update(
            created_at=timezone.now() - timedelta(days=days)
        )
        self.assertIn("close", [i.name for i in case_menu.build(self.head, case).items])
        case = act(self.head, case, "close", notice="ปิดเรื่องแล้ว")
        self.assertEqual(case.status, Case.Status.CLOSED)

    def test_a_cancel_without_a_reason_has_no_empty_line(self):
        case = act(self.head, self.withdraw("4500055033"), "cancel", notice="ยกเลิกแล้ว")
        event = OutboxEvent.objects.filter(
            event_type=orchestration.EVENT_DECISION, subject_id=str(case.pk)
        ).latest("pk")
        delivery = notifications.plan_for_event(event)
        self.assertEqual(delivery.text, f"เรื่อง {case.number} ถูกยกเลิกโดยเจ้าหน้าที่")


class LevelTests(LineCardTestCase):
    """The owner (11 Nov 2569): only ฉุกเฉิน and เร่งด่วน show a level; ข้อมูลยังไม่ครบ is gone."""

    def test_only_an_urgent_case_shows_its_level(self):
        from operations import staff_work
        from operations.models import CaseTriage

        case = self.withdraw("4500055031")
        page = self.web(self.head, case)
        self.assertNotContains(page, 'class="prio prio--')
        self.assertNotContains(page, "รอคัดกรอง")
        self.assertFalse([r for r in staff_work.case_rows(case) if "ระดับ" in r])
        queue = Client()
        queue.force_login(self.head)
        self.assertNotContains(
            queue.get(reverse("division-detail", args=[self.division.code])), "ข้อมูลยังไม่ครบ"
        )
        CaseTriage.objects.filter(case=case).update(effective_priority="P1")
        self.assertContains(self.web(self.head, case), 'class="prio prio--P1"')
        case = Case.objects.get(pk=case.pk)
        # ADR 0058: nobody has taken it, so the status reads รอรับงาน
        self.assertIn("สถานะ: รอรับงาน · ระดับเร่งด่วน", staff_work.case_rows(case))
