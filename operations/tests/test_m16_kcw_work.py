"""งานของ กคว. หลังยื่นเรื่อง (grill กคว. 27 ต.ค. 2569): search in เรื่องของฉัน by the start of a
slip number or a meter number, with the same district seeing status and the sender's employee
ID (SearchTests); warehouse stages recorded many at once, the issue day from the schedule, and
ฟข. choosing the truck day or collection (WarehouseTests); calendar requests staff decide on the
web (CalendarTests); and the case chat that never pushes (ChatTests)."""

from datetime import timedelta

from django.test import Client
from django.utils import timezone

from operations import calendars, case_chat, warehouse
from operations.models import (
    Appointment,
    AuthorityGrant,
    Calendar,
    CalendarSlot,
    Case,
    CaseMessage,
    CaseMilestone,
    Conversation,
    Division,
    DivisionSchedule,
    Employee,
    Role,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import ALICE, BOB, CAROL, buttons, texts
from operations.tests.test_m16_flow_runner import FlowTestCase

S = CaseMilestone.Stage


class WorkTestCase(FlowTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.kcw = make_staff("kcw-clerk", (Role.STAFF, "กคว."))
        cls.division = Division.objects.get(code="กคว.")
        cls.approver = make_staff("kcw-work-approver", (Role.STAFF, "กคว."))
        AuthorityGrant.objects.create(
            user=cls.approver,
            activity=AuthorityGrant.Activity.WITHDRAWAL_APPROVAL,
            division=None,
            valid_from=timezone.now() - timedelta(minutes=1),
            reason="ทดสอบ",
            approval_reference="TEST-ADR-0025",
            granted_by=cls.flow_admin,
        )

    def withdraw(self, slip, meter="12345678", size="5(100)A", user=ALICE, approve=True):
        self.track("มิเตอร์", "ติดตามใบเบิก", number=slip, user=user)
        self.press(user, "เพิ่มรายละเอียด")  # 30 ต.ค. 2569: the summary comes right after the number
        self.type(user, f"มิเตอร์ {meter} ขนาด {size} 1 เครื่อง ใช้ติดตั้ง")
        self.press(user, "ยืนยันส่งเรื่อง")
        case = Case.objects.order_by("-submitted_at").first()
        if approve:  # the warehouse records stages after the division approves (28 ต.ค. Q4)
            from operations import approvals

            done, skipped = approvals.bulk_approve(self.approver, [case])
            self.assertEqual((len(done), skipped), (1, []))
            case.refresh_from_db()
        return case


class SearchTests(WorkTestCase):
    def test_a_slip_start_finds_every_document_and_the_district_sees_the_employee_id(self):
        mine = self.withdraw("4500012345/10")
        employee = Employee.objects.create(employee_id="512345")
        self.requester.employee = employee
        self.requester.save(update_fields=["employee"])
        self.approve(BOB)  # the same district (D01)
        self.approve(CAROL, district="D02")
        self.type(BOB, "เรื่องของฉัน")
        reply = self.press(BOB, "ค้นด้วยเลขที่ใบเบิก")
        self.assertIn("พิมพ์แค่ต้นเลขก็ได้", texts(reply))
        reply = self.type(BOB, "4500012345")
        found = texts(reply)
        self.assertIn(mine.number, found)
        self.assertIn("เลข: 4500012345/10", found)
        self.assertIn("ผู้ส่ง: รหัสพนักงาน 512345", found)
        self.assertNotIn(self.requester.display_name, found)
        self.assertNotIn(mine.number, buttons(reply))  # someone else's case opens no detail
        self.type(CAROL, "เรื่องของฉัน")
        self.press(CAROL, "ค้นด้วยเลขที่ใบเบิก")
        self.assertIn("ไม่พบเรื่อง", texts(self.type(CAROL, "4500012345")))

    def test_a_meter_number_finds_its_case(self):
        case = self.withdraw("4500055555", meter="99887766")
        self.type(ALICE, "เรื่องของฉัน")
        self.press(ALICE, "ค้นเลขเครื่องวัดฯ")
        reply = self.type(ALICE, "11110000 , 99887766")
        self.assertIn(case.number, texts(reply))
        self.assertIn(case.number, buttons(reply))


class WarehouseTests(WorkTestCase):
    def setUp(self):
        super().setUp()
        DivisionSchedule.objects.update_or_create(
            division=self.division,
            defaults={
                "sla": [{"group": "meter", "size": "", "days": 3}],
                "issue_weekdays": [1, 3],  # Tuesday and Thursday
                "truck_days": {"D01": [4]},  # Friday
            },
        )

    def test_stages_in_bulk_the_issue_day_and_the_truck_choice(self):
        first = self.withdraw("4500070001")
        second = self.withdraw("4500070002", meter="12345679")
        monday = timezone.localdate() - timedelta(days=timezone.localdate().weekday() + 7)
        done, skipped = warehouse.record(self.kcw, [first, second], S.KEYED, monday)
        self.assertEqual((done, [why for _, why in skipped]), ([], ["ยังไม่ได้บันทึกรับใบเบิกแล้ว"] * 2))
        cases, missing = warehouse.find_cases(
            self.kcw, self.division, ["4500070001", second.number, "x"]
        )
        self.assertEqual((cases, missing), ([first, second], ["x"]))
        warehouse.record(self.kcw, cases, S.RECEIVED, monday)
        warehouse.record(self.kcw, cases, S.KEYED, monday)
        keyed = CaseMilestone.objects.get(case=first, stage=S.KEYED)
        # Monday + 3 working days = Thursday, an issue day.
        self.assertEqual(keyed.planned_on, monday + timedelta(days=3))
        held = CaseMessage.objects.filter(case=first, status="held")
        self.assertIn("กำหนดจ่าย", held.get().text)
        reply = self.type(ALICE, "สวัสดี")  # the next message carries the held ones
        self.assertIn("กำหนดจ่าย", texts(reply))
        friday = monday + timedelta(days=4)
        first_bubble = [m for m in reply if first.number in m.get("text", "")]
        truck = next(label for label in buttons(first_bubble) if label.startswith("รถส่ง"))
        self.assertIn(str(friday.day), truck)
        reply = self.press(ALICE, truck, first_bubble)
        self.assertIn("รับของทางรถ", texts(reply))
        chosen = Appointment.objects.get(case=first)
        self.assertEqual((chosen.method, chosen.status), ("truck", "confirmed"))
        warehouse.record(self.kcw, [first], S.ISSUED, monday + timedelta(days=3))
        warehouse.record(self.kcw, [second], S.ISSUED, monday + timedelta(days=3))
        warehouse.record(self.kcw, [second], S.DELIVERED, monday + timedelta(days=4))
        second.refresh_from_db()
        self.assertEqual(second.status, "closed")  # Q37: the district has it, the case ends
        reply = self.type(ALICE, "ขอบคุณ")
        self.assertIn("จ่ายแล้ว", texts(reply))
        self.assertIn("วิธีรับ: รถ กคว. ไปส่ง", texts(reply))
        reply = self.type(ALICE, first.number)
        self.assertIn("ขั้นงานคลัง: จ่ายแล้ว", texts(reply))

    def test_collection_lists_the_free_slots(self):
        case = self.withdraw("4500080001")
        today = timezone.localdate()
        warehouse.record(self.kcw, [case], S.RECEIVED, today)
        warehouse.record(self.kcw, [case], S.KEYED, today, planned_on=today)
        start = timezone.now() + timedelta(days=1)
        slot = CalendarSlot.objects.create(
            calendar=Calendar.objects.get(key="pickup"),
            starts_at=start,
            ends_at=start + timedelta(hours=1),
        )
        reply = self.type(ALICE, "สวัสดี")
        reply = self.press(ALICE, "มารับเอง", reply)
        self.assertIn("เลือกช่วงที่จะมารับ", texts(reply))
        from operations import calendars

        reply = self.press(ALICE, calendars.short(slot))
        self.assertIn("รอ กคว. ยืนยัน", texts(reply))
        self.assertEqual(Appointment.objects.get(case=case).slot, slot)

    def test_the_web_page_records_pasted_slips_and_the_schedule_needs_a_reason(self):
        case = self.withdraw("4500090001/10")
        client = Client()
        client.force_login(self.kcw)
        page = client.get("/divisions/กคว./warehouse/")
        self.assertContains(page, "4500090001/10")
        r = client.post(
            "/divisions/กคว./warehouse/",
            {
                "numbers": "4500090001/10\n4599999999",
                "stage": "received",
                "on": timezone.localdate().isoformat(),
            },
        )
        self.assertEqual(r.status_code, 302)
        self.assertTrue(CaseMilestone.objects.filter(case=case, stage=S.RECEIVED).exists())
        r = client.post(
            "/divisions/กคว./warehouse/schedule/",
            {"sla_group": ["ct"], "sla_size": [""], "sla_days": ["5"], "issue": ["0"]},
        )
        self.assertEqual(DivisionSchedule.objects.get(division=self.division).sla[0]["days"], 3)
        client.post(
            "/divisions/กคว./warehouse/schedule/",
            {
                "sla_group": ["ct"],
                "sla_size": [""],
                "sla_days": ["5"],
                "issue": ["0"],
                "truck_D01": ["2"],
                "reason": "ตามที่คลังตกลง",
            },
        )
        schedule = DivisionSchedule.objects.get(division=self.division)
        self.assertEqual(
            (schedule.sla, schedule.issue_weekdays, schedule.truck_days),
            ([{"group": "ct", "size": "", "days": 5}], [0], {"D01": [2]}),
        )
        outsider = Client()
        outsider.force_login(make_staff("other-clerk", (Role.STAFF, "กคอ.")))
        self.assertEqual(outsider.get("/divisions/กคว./warehouse/").status_code, 404)


class CalendarTests(WorkTestCase):
    def setUp(self):
        super().setUp()
        Calendar.objects.filter(key="notebook").update(default_times=[])  # staff open, as before

    def test_staff_open_slots_and_a_confirmation_reaches_the_requester(self):
        client = Client()
        client.force_login(self.kcw)
        day = timezone.localdate() + timedelta(days=3)
        r = client.post(
            "/divisions/กคว./calendar/?cal=notebook",
            {"action": "add", "day": day.isoformat(), "times": "09:00, 10:30", "minutes": "60"},
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(CalendarSlot.objects.filter(calendar__key="notebook").count(), 2)
        self.open_tile()
        self.press(ALICE, "โน้ตบุ๊กและหัวอ่าน")
        reply = self.type(ALICE, "ขอติดตั้งโปรแกรมอ่านมิเตอร์")
        first = CalendarSlot.objects.order_by("starts_at").first()
        self.press(ALICE, calendars.short(first), reply)  # ADR 0036: a time on its day's card
        self.press(ALICE, "ยืนยันส่งเรื่อง")  # รอบใหม่ Q8: the slot is confirmed at the summary
        appointment = Appointment.objects.get()
        page = client.get("/divisions/กคว./calendar/?cal=notebook")
        self.assertContains(page, "รอยืนยัน 1 คำขอ")
        client.post(
            "/divisions/กคว./calendar/?cal=notebook",
            {"action": "reject", "appointment": appointment.pk},
        )
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, "requested")  # rejecting needs a reason
        client.post(
            "/divisions/กคว./calendar/?cal=notebook",
            {"action": "confirm", "appointment": appointment.pk, "note": "นำโน้ตบุ๊กมาด้วย"},
        )
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, "confirmed")
        reply = self.type(ALICE, "สวัสดีครับ")
        self.assertIn("ยืนยันนัด", texts(reply))
        self.assertIn("นำโน้ตบุ๊กมาด้วย", texts(reply))


class ChatTests(WorkTestCase):
    def test_a_held_message_then_a_free_reply_while_the_requester_chats(self):
        case = self.withdraw("4500060001")
        client = Client()
        client.force_login(self.kcw)
        page = client.get(f"/cases/{case.number}/")
        self.assertContains(page, "คุยกับผู้แจ้ง")
        r = client.post(f"/cases/{case.number}/chat/", {"text": "ใบเบิกขาดลายเซ็น"})
        self.assertEqual(CaseMessage.objects.count(), 0)  # the first message needs a contact
        self.assertEqual(r.status_code, 302)
        sent = len(self.sent)
        client.post(
            f"/cases/{case.number}/chat/",
            {"text": "ใบเบิกขาดลายเซ็น", "contact": "สมหญิง กคว. โทร 1234"},
        )
        self.assertEqual(len(self.sent), sent)  # nothing pushed
        self.assertEqual(CaseMessage.objects.get().status, "held")
        reply = self.type(ALICE, "สอบถามใบเบิกครับ")
        self.assertIn("ใบเบิกขาดลายเซ็น\nติดต่อ: สมหญิง กคว. โทร 1234", texts(reply))
        self.assertEqual(Conversation.objects.get(identity__external_id=ALICE).prompt, "case_chat")
        sent = len(self.sent)
        self.type(ALICE, "จะส่งใบใหม่พรุ่งนี้")
        self.assertEqual(len(self.sent), sent)  # the bot keeps quiet so staff can answer free
        answer = CaseMessage.objects.get(direction="requester")
        self.assertEqual((answer.case, answer.text), (case, "จะส่งใบใหม่พรุ่งนี้"))
        with self.captureOnCommitCallbacks(execute=True):
            message = case_chat.send(self.kcw, case, "รับทราบ ขอบคุณครับ")
        self.assertEqual(message.status, "sent")
        self.assertIn("รับทราบ ขอบคุณครับ", texts(self.sent[-1]))
        self.assertNotIn("ติดต่อ:", texts(self.sent[-1]))  # only the first message signs
        reply = self.press(ALICE, "จบการคุย", self.sent[-1])
        self.assertIn("จบการคุยกับเจ้าหน้าที่แล้ว", texts(reply))
        self.assertEqual(Conversation.objects.get(identity__external_id=ALICE).prompt, "")

    def test_the_chat_ends_after_a_day_and_outsiders_cannot_write(self):
        case = self.withdraw("4500060002")
        case_chat.send(self.kcw, case, "ขอเลขใบเบิกตัวจริง", "กคว. 1234")
        self.type(ALICE, "สวัสดี")
        row = Conversation.objects.get(identity__external_id=ALICE)
        row.prompt_data["until"] = (timezone.now() - timedelta(minutes=1)).isoformat()
        row.save(update_fields=["prompt_data"])
        self.type(ALICE, "เรื่องของฉัน")
        self.assertFalse(CaseMessage.objects.filter(direction="requester").exists())
        from django.core.exceptions import PermissionDenied

        with self.assertRaises(PermissionDenied):
            case_chat.send(make_staff("chat-outsider", (Role.STAFF, "กคอ.")), case, "x", "y")
