"""Push Message switches and ตารางเวร (ADR 0048, grill 4 พ.ย. 2569)."""

from datetime import date, datetime, time
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from operations import duty, notifications, options
from operations.models import (
    DutyShift,
    Holiday,
    NotificationDelivery,
    OutboxEvent,
    Role,
    SystemOption,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m19_problem_tile import PickTestCase

BKK = ZoneInfo("Asia/Bangkok")
PUSH_ON = {"OUTBOUND_ENABLED": True, "LINE_CHANNEL_ACCESS_TOKEN": "x", "LINE_PUSH_ENABLED": True}


def at(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), BKK)


class PushSwitchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = make_staff("push-admin", (Role.ADMIN, None))

    def test_every_kind_is_sent_except_a_cancelled_case(self):
        self.assertFalse(options.push_on("case_cancelled"))
        self.assertTrue(options.push_on("case_closed"))
        self.assertTrue(options.push_on("staff_new_case", "duty"))
        self.assertFalse(options.push_on("staff_new_case", "division_admin"))
        self.assertTrue(options.push_on("catch_up"))  # not listed: its own ตัวเลือกระบบ decides

    @override_settings(**PUSH_ON)
    def test_a_kind_switched_off_is_skipped_when_it_would_be_sent(self):
        N = NotificationDelivery
        cancelled = N(channel=N.Channel.LINE_PUSH, kind="case_cancelled")
        closed = N(channel=N.Channel.LINE_PUSH, kind="case_closed")
        self.assertEqual(notifications._recipient_problem(cancelled), "ปิดใน Push Message")
        self.assertEqual(notifications._recipient_problem(closed), "no LINE identity")
        options.change(self.admin, "push:case_closed", "off", "")
        self.assertEqual(notifications._recipient_problem(closed), "ปิดใน Push Message")

    def test_the_push_page_switches_one_kind_and_keeps_to_its_own_options(self):
        client = Client()
        client.force_login(self.admin)
        page = client.get(reverse("admin-push"))
        self.assertContains(page, "เจ้าหน้าที่ยกเลิกเรื่อง")
        self.assertContains(page, "แอดมินฝ่ายที่อยู่เวร")
        self.assertContains(page, "งบข้อความ LINE ที่เด้งถึงเจ้าหน้าที่ต่อเดือน")
        client.post(reverse("admin-push"), {"key": "push:case_cancelled", "value": "on"})
        self.assertTrue(options.push_on("case_cancelled"))
        client.post(reverse("admin-push"), {"key": options.SCREENING, "value": "on"})
        self.assertFalse(SystemOption.objects.filter(key=options.SCREENING).exists())
        others = client.get(reverse("admin-options"))
        self.assertNotContains(others, "Push Message:")
        self.assertNotContains(others, "งบข้อความ LINE ที่เด้งถึงเจ้าหน้าที่ต่อเดือน")


class DutyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ann = make_staff("ann", (Role.DEPARTMENT_SUPERVISOR, None))
        cls.bob = make_staff("bob", (Role.DEPARTMENT_SUPERVISOR, None))
        cls.member = make_staff("member")

    monday = date(2026, 11, 9)

    def test_nobody_on_duty_tells_every_department_admin(self):
        self.assertEqual(duty.on_duty(at(self.monday, 9)), [])
        self.assertEqual(set(duty.receivers(at(self.monday, 9))), {self.ann, self.bob})

    def test_weekday_rows_and_an_overnight_shift(self):
        duty.add(self.ann, "07:30", "15:30", [self.ann.pk], weekday=0)
        duty.add(self.ann, "15:30", "07:30", [self.bob.pk], weekday=0)
        self.assertEqual(duty.on_duty(at(self.monday, 9)), [self.ann])
        self.assertEqual(duty.on_duty(at(self.monday, 20)), [self.bob])
        tuesday = date(2026, 11, 10)
        self.assertEqual(duty.on_duty(at(tuesday, 6)), [self.bob])  # Monday's night shift
        self.assertEqual(duty.on_duty(at(tuesday, 9)), [])

    def test_a_holiday_uses_its_own_row(self):
        duty.add(self.ann, "07:30", "15:30", [self.ann.pk], weekday=0)
        duty.add(self.ann, "08:00", "12:00", [self.bob.pk], weekday=DutyShift.HOLIDAY)
        Holiday.objects.create(day=self.monday, name="วันหยุดชดเชย")
        self.assertEqual(duty.on_duty(at(self.monday, 9)), [self.bob])

    def test_an_exception_replaces_the_day_and_nobody_means_everyone(self):
        duty.add(self.ann, "07:30", "15:30", [self.ann.pk], weekday=0)
        duty.add(self.bob, "07:30", "15:30", [self.bob.pk], on=self.monday)
        self.assertEqual(set(duty.on_duty(at(self.monday, 9))), {self.ann, self.bob})
        ann = DutyShift.objects.get(on=self.monday, user=self.ann)
        duty.remove(self.bob, ann.pk)  # Ann on leave
        self.assertEqual(duty.on_duty(at(self.monday, 9)), [self.bob])
        duty.nobody(self.ann, self.monday)
        self.assertEqual(duty.on_duty(at(self.monday, 9)), [])
        duty.reset(self.ann, self.monday)
        self.assertEqual(duty.on_duty(at(self.monday, 9)), [self.ann])

    def test_only_admins_and_department_admins_edit(self):
        with self.assertRaises(ValidationError):
            duty.add(self.ann, "07:30", "07:30", [self.ann.pk], weekday=0)
        with self.assertRaises(ValidationError):
            duty.add(self.ann, "07:30", "15:30", [self.member.pk], weekday=0)
        client = Client()
        client.force_login(self.member)
        self.assertEqual(client.get(reverse("admin-duty")).status_code, 403)
        client.force_login(self.ann)
        page = client.get(reverse("admin-duty"), {"day": "2026-11-09"})
        self.assertContains(page, "เวรประจำสัปดาห์")
        self.assertContains(page, 'value="07:30"')
        client.post(
            reverse("admin-duty"),
            {
                "action": "add",
                "weekday": "0",
                "start": "07:30",
                "end": "15:30",
                "user": self.bob.pk,
            },
        )
        self.assertTrue(DutyShift.objects.filter(weekday=0, user=self.bob).exists())


class NewCaseAlertTests(PickTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ann = make_staff("ann-dept", (Role.DEPARTMENT_SUPERVISOR, None))
        cls.head = make_staff("kcw-head", (Role.SUPERVISOR, "กคว."))

    def planned(self, case):
        from operations import orchestration, staff_email

        event = OutboxEvent.objects.get(
            event_type=orchestration.EVENT_CASE_SUBMITTED, subject_id=str(case.pk)
        )
        with patch("operations.staff_line.plan") as plan:
            staff_email.plan_for_event(event)
        return [c.args for c in plan.call_args_list if c.args[0] == "staff_new_case"]

    def test_a_case_waiting_at_receive_tells_the_department_admin_on_duty(self):
        case, _ = self.file("กองเครื่องวัดหน่วยไฟฟ้า")
        self.assertEqual(case.status, case.Status.AWAITING_APPROVAL)
        calls = self.planned(case)
        self.assertEqual(len(calls), 1)
        self.assertIn(self.ann, calls[0][3])
        self.assertNotIn(self.head, calls[0][3])
        options.change(None, "push:staff_new_case:division_admin", "on", "")
        options.change(None, "push:staff_new_case:duty", "off", "")
        calls = self.planned(case)
        self.assertEqual([c[3] for c in calls], [[self.head]])
