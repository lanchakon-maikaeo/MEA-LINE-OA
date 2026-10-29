"""หน้าเว็บที่ปรับตาม grill 28 ต.ค. 2569: the logo and no กอจ. tab in the header (Q1, Q2); the
warehouse page's stage tabs, next-step buttons and the tab of cases still awaiting approval,
which take no stage (Q3, Q4); the schedule's group boxes and worked example (Q5); one month for
every calendar of the division (Q6); "แสดงเพิ่มเติม" on the queue (Q8); approving many at once
(Q9); and item names in queue rows."""

from datetime import date, timedelta

from django.test import Client
from django.utils import timezone

from operations import queues, warehouse
from operations.models import (
    Calendar,
    CalendarSlot,
    CaseMilestone,
    District,
    DivisionSchedule,
    Role,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m16_kcw_work import WorkTestCase

S = CaseMilestone.Stage


class HeaderTests(WorkTestCase):
    def test_the_name_leads_and_the_koj_page_is_not_a_tab(self):
        admin = make_staff("header-admin", (Role.ADMIN, None))
        koj = make_staff("header-koj", (Role.STAFF, "กอจ."))
        for user in (admin, koj, self.kcw):
            client = Client()
            client.force_login(user)
            page = client.get("/console/").content.decode()
            # grill 29 ต.ค. 2569 Q1, Q18: DED Center in place of the logo, and in the tab.
            self.assertIn('<span class="brand__mark">DED</span>', page)
            self.assertNotIn("fajo-plus.webp", page)
            self.assertIn("· DED Center</title>", page)
            self.assertNotIn('href="/koj/"', page)
        client = Client()
        client.force_login(koj)
        self.assertEqual(client.get("/koj/").status_code, 403)
        client.force_login(admin)
        self.assertEqual(client.get("/koj/").status_code, 200)


class WarehousePageTests(WorkTestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.kcw)

    def test_a_case_awaiting_approval_waits_in_its_own_tab_and_takes_no_stage(self):
        waiting = self.withdraw("4500100001", approve=False)
        ready = self.withdraw("4500100002", meter="12345679")
        page = self.client.get("/divisions/กคว./warehouse/")
        self.assertContains(page, ready.number)
        self.assertNotContains(page, waiting.number)
        self.assertEqual(dict((k, n) for k, _, n in page.context["tabs"])["approval"], 1)
        tab = self.client.get("/divisions/กคว./warehouse/?tab=approval")
        self.assertContains(tab, waiting.number)
        self.assertContains(tab, "ไปอนุมัติที่หน้าคิว")
        done, skipped = warehouse.record(self.kcw, [waiting], S.RECEIVED, timezone.localdate())
        self.assertEqual(
            (done, [why for _, why in skipped]), ([], ["ยังรออนุมัติ บันทึกขั้นงานคลังได้หลังกองอนุมัติ"])
        )
        card = self.client.get(f"/cases/{waiting.number}/")
        self.assertContains(card, "บันทึกขั้นงานคลังได้หลังกองอนุมัติ")

    def test_the_row_button_records_the_next_stage_and_comes_back(self):
        case = self.withdraw("4500100003")
        page = self.client.get("/divisions/กคว./warehouse/?tab=none")
        self.assertContains(page, ">รับใบเบิก</button>")
        back = "/divisions/%E0%B8%81%E0%B8%84%E0%B8%A7./warehouse/?tab=none"
        response = self.client.post(
            f"/cases/{case.number}/stage/",
            {"stage": "received", "on": timezone.localdate().isoformat(), "back": back},
        )
        self.assertEqual(response.url, back)
        self.assertTrue(CaseMilestone.objects.filter(case=case, stage=S.RECEIVED).exists())
        page = self.client.get("/divisions/กคว./warehouse/?tab=received")
        self.assertContains(page, "ยืนยันวันจ่าย")  # คีย์จ่าย asks for the issue day
        # Somewhere else is never a place to go back to.
        response = self.client.post(
            f"/cases/{case.number}/stage/",
            {
                "stage": "keyed",
                "on": timezone.localdate().isoformat(),
                "planned_on": timezone.localdate().isoformat(),
                "back": "https://example.com/",
            },
        )
        self.assertTrue(response.url.startswith(f"/cases/{case.number}/"))

    def test_late_slips_are_marked(self):
        case = self.withdraw("4500100004")
        long_ago = timezone.localdate() - timedelta(days=10)
        warehouse.record(self.kcw, [case], S.RECEIVED, long_ago)
        warehouse.record(self.kcw, [case], S.KEYED, long_ago, planned_on=long_ago)
        page = self.client.get("/divisions/กคว./warehouse/?tab=keyed")
        self.assertContains(page, "เลยวันจ่าย")


class SchedulePageTests(WorkTestCase):
    def test_group_boxes_drop_rows_without_days_and_show_an_example(self):
        client = Client()
        client.force_login(self.kcw)
        district = District.objects.order_by("code").first()
        client.post(
            "/divisions/กคว./warehouse/schedule/",
            {
                "sla_group": ["meter", "meter", "ct"],
                "sla_size": ["", "5(100)A", ""],
                "sla_days": ["5", "", "2"],
                "issue": ["0", "1", "2", "3", "4"],
                f"truck_{district.code}": ["2"],
                "reason": "ทดสอบ",
            },
        )
        schedule = DivisionSchedule.objects.get(division=self.division)
        self.assertEqual(
            schedule.sla,
            [{"group": "meter", "size": "", "days": 5}, {"group": "ct", "size": "", "days": 2}],
        )
        monday = date(2026, 11, 2)
        self.assertEqual(
            warehouse.preview(schedule, "meter", district.code, monday),
            (date(2026, 11, 9), date(2026, 11, 11)),
        )
        page = client.get(f"/divisions/กคว./warehouse/schedule/?pg=meter&pd={district.code}")
        self.assertContains(page, "ถ้ารับใบเบิกวันนี้")
        self.assertContains(page, "+ เพิ่มขนาด")


class CombinedCalendarTests(WorkTestCase):
    def test_one_month_shows_every_calendar_and_adding_names_one(self):
        client = Client()
        client.force_login(self.kcw)
        start = timezone.now() + timedelta(days=2)
        for key in ("pickup", "notebook"):
            CalendarSlot.objects.create(
                calendar=Calendar.objects.get(key=key),
                starts_at=start,
                ends_at=start + timedelta(hours=1),
            )
        month = timezone.localtime(start).date().replace(day=1).isoformat()
        page = client.get(f"/divisions/กคว./calendar/?month={month}").content.decode()
        self.assertIn("mcal__chip--orange", page)
        self.assertIn("mcal__chip--blue", page)
        narrowed = client.get(f"/divisions/กคว./calendar/?cal=notebook&month={month}")
        # Narrowed to notebooks, orange is left only in the colour legend.
        self.assertEqual(narrowed.content.decode().count("mcal__chip--orange"), 1)
        self.assertGreater(page.count("mcal__chip--orange"), 1)
        day = (timezone.localdate() + timedelta(days=3)).isoformat()
        before = CalendarSlot.objects.count()
        client.post("/divisions/กคว./calendar/", {"action": "add", "day": day, "times": "09:00"})
        self.assertEqual(CalendarSlot.objects.count(), before)  # which calendar? asked first
        client.post(
            "/divisions/กคว./calendar/",
            {"action": "add", "day": day, "times": "09:00", "calendar": "pickup"},
        )
        self.assertEqual(CalendarSlot.objects.filter(calendar__key="pickup").count(), 2)


class QueueTests(WorkTestCase):
    def test_rows_name_the_item_and_the_approve_bar_offers_select_all(self):
        case = self.withdraw("4500100005", approve=False)
        self.assertNotIn("—", queues.summary_text(case))
        approver = Client()
        approver.force_login(self.approver)
        page = approver.get("/divisions/กคว./").content.decode()
        self.assertIn("data-bulk-all", page)
        self.assertIn('class="qrow__pick"', page)
