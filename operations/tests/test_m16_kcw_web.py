"""หน้าเว็บของ กคว. รอบใหม่ (27 ต.ค. 2569): the next warehouse stage and the appointment from
the case card (Q38, Q9); the floating chat window and its thread, with the answers typed while
filing (Q15, Q16); the month calendar (Q10); วันหยุด picked by admins, skipped as working days
and asked about before slots open on them (Q11); and the people an admin chose to hear of an
emergency (Q33)."""

from datetime import date, timedelta

from django.test import Client
from django.utils import timezone

from operations import calendars, staff_email, warehouse
from operations.models import (
    Appointment,
    Calendar,
    CalendarSlot,
    CaseMilestone,
    Division,
    DivisionSchedule,
    EmergencyContact,
    Holiday,
    Role,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import ALICE
from operations.tests.test_m16_kcw_work import WorkTestCase

S = CaseMilestone.Stage


class CaseCardTests(WorkTestCase):
    def test_the_next_stage_is_one_button_on_the_card(self):
        case = self.withdraw("4500200001")
        DivisionSchedule.objects.update_or_create(
            division=self.division,
            defaults={"sla": [{"group": "meter", "size": "", "days": 2}], "issue_weekdays": []},
        )
        client = Client()
        client.force_login(self.kcw)
        page = client.get(f"/cases/{case.number}/").content.decode()
        self.assertIn("บันทึกรับใบเบิกแล้ว", page)
        today = timezone.localdate().isoformat()
        client.post(f"/cases/{case.number}/stage/", {"stage": "received", "on": today})
        page = client.get(f"/cases/{case.number}/").content.decode()
        self.assertIn("บันทึกคีย์จ่ายแล้ว", page)
        self.assertIn("ระบบเสนอตามตาราง", page)
        client.post(
            f"/cases/{case.number}/stage/",
            {"stage": "keyed", "on": today, "planned_on": today},
        )
        self.assertEqual(
            CaseMilestone.objects.get(case=case, stage=S.KEYED).planned_on.isoformat(), today
        )

    def test_an_appointment_is_confirmed_from_the_card(self):
        start = timezone.now() + timedelta(days=2)
        CalendarSlot.objects.create(
            calendar=Calendar.objects.get(key="notebook"),
            starts_at=start,
            ends_at=start + timedelta(hours=1),
        )
        self.open_tile()
        self.press(ALICE, "โน้ตบุ๊กและหัวอ่าน")
        self.type(ALICE, "ลงโปรแกรมอ่านมิเตอร์")
        self.press(ALICE, calendars.short(CalendarSlot.objects.get()))
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        appointment = Appointment.objects.get(status="requested")
        client = Client()
        client.force_login(self.kcw)
        page = client.get(f"/cases/{appointment.case.number}/").content.decode()
        self.assertIn("ยืนยันนัด", page)
        client.post(
            f"/cases/{appointment.case.number}/appointment/",
            {"appointment": appointment.pk, "action": "confirm"},
        )
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, "confirmed")


class ChatWindowTests(WorkTestCase):
    def test_the_window_shows_the_chat_and_what_was_typed_while_filing(self):
        case = self.withdraw("4500200002")
        client = Client()
        client.force_login(self.kcw)
        page = client.get(f"/cases/{case.number}/").content.decode()
        self.assertIn('class="chatwin"', page)
        thread = client.get(f"/cases/{case.number}/chat/thread/").content.decode()
        self.assertIn("ตอนสร้างเรื่อง", thread)  # the answers typed in the flow, faint
        self.assertIn("ใช้ติดตั้ง", thread)
        outsider = Client()
        outsider.force_login(make_staff("chat-window-outsider", (Role.STAFF, "กคอ.")))
        self.assertEqual(outsider.get(f"/cases/{case.number}/chat/thread/").status_code, 404)


class CalendarTests(WorkTestCase):
    def test_the_month_grid_and_holidays(self):
        admin = make_staff("holiday-admin", (Role.ADMIN, None))
        client = Client()
        client.force_login(admin)
        day = timezone.localdate() + timedelta(days=10)
        page = client.get(f"/console/admin/holidays/?month={day.isoformat()}").content.decode()
        self.assertIn(calendars.day_label(day), page)  # e.g. อังคาร, 20 ต.ค. 69
        client.post("/console/admin/holidays/", {"day": day.isoformat(), "name": "วันหยุดพิเศษ"})
        self.assertEqual(Holiday.objects.get().name, "วันหยุดพิเศษ")
        clerk = Client()
        clerk.force_login(self.kcw)
        url = f"/divisions/กคว./calendar/?cal=notebook&month={day.isoformat()}"
        page = clerk.get(url).content.decode()
        self.assertIn("วันหยุดพิเศษ", page)
        self.assertIn("mcal__day", page)
        clerk.post(url, {"action": "add", "day": day.isoformat(), "times": "09:00"})
        self.assertFalse(CalendarSlot.objects.exists())  # asked first on a holiday
        clerk.post(
            url, {"action": "add", "day": day.isoformat(), "times": "09:00", "on_holiday": "1"}
        )
        self.assertEqual(CalendarSlot.objects.count(), 1)

    def test_working_days_skip_holidays(self):
        monday = date(2026, 11, 2)
        Holiday.objects.create(day=monday + timedelta(days=1), name="วันหยุด")
        self.assertEqual(warehouse.working_days_after(monday, 2), monday + timedelta(days=3))
        self.assertEqual(calendars.day_label(monday + timedelta(days=1)), "อังคาร, 3 พ.ย. 69")


class EmergencyTests(WorkTestCase):
    def test_an_admin_chooses_who_hears_of_an_emergency(self):
        admin = make_staff("emergency-admin", (Role.ADMIN, None))
        on_call = make_staff("kcw-on-call", (Role.STAFF, "กคว."))
        client = Client()
        client.force_login(admin)
        self.assertEqual(client.get("/console/admin/emergency/").status_code, 200)
        division = Division.objects.get(code="กคว.")
        client.post("/console/admin/emergency/", {"division": division.pk, "user": [on_call.pk]})
        self.assertEqual(
            list(EmergencyContact.objects.values_list("user_id", flat=True)), [on_call.pk]
        )
        self.assertEqual(
            [u.pk for u in staff_email._with_email(c.user for c in EmergencyContact.objects.all())],
            [on_call.pk],
        )
