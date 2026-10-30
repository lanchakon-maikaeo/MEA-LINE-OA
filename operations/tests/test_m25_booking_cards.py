"""ADR 0036 (grill กคว. นัดโน้ตบุ๊ก 30 ต.ค. 2569): โน้ตบุ๊กและหัวอ่าน books on a card per day. Its
default times are free on the ten working days from the next one until booked or closed; a full
time shows เต็ม; ขอให้ติดต่อกลับ asks only the day that suits; staff close a day or a time."""

from django.test import Client
from django.utils import timezone

from operations import calendars
from operations.models import Appointment, Calendar, CalendarSlot, Case, Holiday, Role
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import ALICE, BOB, buttons, texts
from operations.tests.test_m16_flow_runner import FlowTestCase

TIMES = ["09:00", "10:00", "11:00", "13:00", "14:00", "15:00"]


def cards(reply):
    """[(day on the card, [(time, button data or None when full)])] of the reply's carousel."""
    flex = next(m for m in reply if m["type"] == "flex")
    found = []
    for bubble in flex["contents"]["contents"]:
        times = []
        for row in bubble["body"]["contents"]:
            for cell in row["contents"]:
                if cell["type"] == "button":
                    times.append((cell["action"]["label"], cell["action"]["data"]))
                elif cell["type"] == "box":
                    times.append((cell["contents"][0]["text"], None))
        found.append((bubble["header"]["contents"][0]["text"], times))
    return found


class BookingCardTestCase(FlowTestCase):
    def setUp(self):
        super().setUp()
        self.calendar = Calendar.objects.get(key="notebook")

    def ask_slot(self, user=ALICE):
        self.open_tile(user)
        self.press(user, "โน้ตบุ๊กและหัวอ่าน")
        return self.type(user, "ขอนัดลงโปรแกรมอ่านมิเตอร์")


class CardTests(BookingCardTestCase):
    def test_the_seeded_calendar_has_the_agreed_times(self):
        self.assertEqual(self.calendar.default_times, TIMES)

    def test_ten_working_days_from_the_next_one_with_six_times_each(self):
        days = calendars.bookable_days()
        Holiday.objects.create(day=days[2], name="วันหยุดทดสอบ")
        days = calendars.bookable_days()
        reply = self.ask_slot()
        self.assertIn("นัดวันเวลาเข้ารับบริการ\nกดเวลาในการ์ด ปัดซ้ายดูวันอื่น", texts(reply))
        shown = cards(reply)
        self.assertEqual([day for day, _ in shown], [calendars.day_full(d) for d in days])
        self.assertTrue(all(d.weekday() < 5 for d in days))
        self.assertNotIn(calendars.day_full(Holiday.objects.get().day), [d for d, _ in shown])
        self.assertEqual([t for t, _ in shown[0][1]], TIMES)
        self.assertGreater(days[0], timezone.localdate())  # Q24: never today
        self.assertEqual(list(buttons(reply))[:1], ["ขอให้ติดต่อกลับ"])

    def test_a_full_time_says_so_and_a_closed_one_is_gone(self):
        day = calendars.bookable_days()[0]
        calendars.fill_defaults(self.calendar, [day])
        slots = list(CalendarSlot.objects.filter(calendar=self.calendar).order_by("starts_at"))
        Appointment.objects.create(calendar=self.calendar, slot=slots[0], status="confirmed")
        CalendarSlot.objects.filter(pk=slots[1].pk).update(is_open=False)
        first = cards(self.ask_slot())[0][1]
        self.assertEqual(first[0], ("09:00 เต็ม", None))
        self.assertNotIn("10:00", [t for t, _ in first])
        self.assertEqual(first[1][0], "11:00")

    def test_a_tapped_time_is_held_and_asked_for_at_the_summary(self):
        reply = self.ask_slot()
        slot = CalendarSlot.objects.filter(calendar=self.calendar).order_by("starts_at").first()
        reply = self.press(ALICE, calendars.short(slot))
        self.assertIn(f"ขอนัด: {calendars.label(slot)} (รอเจ้าหน้าที่ยืนยัน)", texts(reply))
        reply = self.press(ALICE, "ยืนยันส่งเรื่อง")
        self.assertIn("ทำการนัดหมายแล้ว กรุณารอเจ้าหน้าที่ยืนยัน", texts(reply))
        self.assertEqual(Appointment.objects.get(status="requested").slot, slot)

    def test_a_time_taken_meanwhile_sends_new_cards(self):
        mine = self.ask_slot()
        slot = CalendarSlot.objects.filter(calendar=self.calendar).order_by("starts_at").first()
        self.approve(BOB)
        self.ask_slot(BOB)
        self.press(BOB, calendars.short(slot))
        reply = self.press(ALICE, calendars.short(slot), mine)
        self.assertIn("ช่วงเวลาอยู่ระหว่างดำเนินการ กรุณาเลือกช่วงใหม่", texts(reply))
        self.assertEqual(cards(reply)[0][1][0], ("09:00 เต็ม", None))  # Q20: fresh cards

    def test_filling_twice_makes_no_second_slot(self):
        days = calendars.bookable_days()
        calendars.fill_defaults(self.calendar, days)
        calendars.fill_defaults(self.calendar, days)
        self.assertEqual(CalendarSlot.objects.filter(calendar=self.calendar).count(), 60)


class CallBackTests(BookingCardTestCase):
    def test_a_call_back_takes_the_day_that_suits(self):
        self.ask_slot()
        reply = self.press(ALICE, "ขอให้ติดต่อกลับ")
        self.assertIn("สะดวกวันไหน", texts(reply))
        days = calendars.bookable_days()
        self.assertEqual(
            list(buttons(reply))[:11], [calendars.day_short(d) for d in days] + ["วันไหนก็ได้"]
        )
        reply = self.press(ALICE, calendars.day_short(days[1]))
        line = f"วันที่สะดวก: {calendars.day_full(days[1])} (เจ้าหน้าที่จะติดต่อกลับเพื่อนัดเวลา)"
        self.assertIn(line, texts(reply))
        self.assertNotIn("ขอนัด:", texts(reply))
        reply = self.press(ALICE, "ยืนยันส่งเรื่อง")
        self.assertIn("เจ้าหน้าที่จะติดต่อกลับเพื่อทำการนัดหมายวันเวลา", texts(reply))
        self.assertIn(f"วันที่สะดวก: {calendars.day_full(days[1])}", Case.objects.get().description)
        self.assertFalse(Appointment.objects.exclude(status="cancelled").exists())

    def test_any_day_will_do(self):
        self.ask_slot()
        self.press(ALICE, "ขอให้ติดต่อกลับ")
        reply = self.press(ALICE, "วันไหนก็ได้")
        self.assertIn("วันที่สะดวก: วันไหนก็ได้", texts(reply))
        self.assertFalse(Appointment.objects.filter(status="requested").exists())


class StaffTests(BookingCardTestCase):
    def setUp(self):
        super().setUp()
        self.clerk = Client()
        self.clerk.force_login(make_staff("kcw-cards", (Role.STAFF, "กคว.")))

    def test_the_month_shows_the_default_times_and_a_day_closes(self):
        day = calendars.bookable_days()[0]
        url = (
            f"/divisions/กคว./calendar/?cal=notebook&month={day.isoformat()}&day={day.isoformat()}"
        )
        page = self.clerk.get(url).content.decode()
        self.assertIn("ปิดทั้งวัน", page)
        self.assertEqual(CalendarSlot.objects.filter(starts_at__date=day).count(), 6)
        self.clerk.post(
            url, {"action": "close_day", "calendar": "notebook", "day": day.isoformat()}
        )
        self.assertFalse(CalendarSlot.objects.filter(starts_at__date=day, is_open=True).exists())
        shown = [d for d, _ in cards(self.ask_slot())]
        self.assertNotIn(calendars.day_full(day), shown)
        slot = CalendarSlot.objects.filter(starts_at__date=day).order_by("starts_at").first()
        self.clerk.post(url, {"action": "reopen", "slot": slot.pk, "day": day.isoformat()})
        self.assertEqual(CalendarSlot.objects.filter(starts_at__date=day, is_open=True).get(), slot)
