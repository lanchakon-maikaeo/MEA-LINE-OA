"""ADR 0050 (grill 7 พ.ย. 2569): the roster fills a web account's สังกัด, staff alerts go to the
แอดมินฝ่าย on duty, and that admin acts from LINE.

Acceptance → tests: a web account takes its สังกัด from the roster by email, never by name, and
an ID held by a LINE account waits for รวมบัญชี (MatchTests); staff alerts go to the แอดมินฝ่าย
on duty, a kind's own people only while their switch is on, never to whoever did the step
unless that switch is on, and email has its own switch (RoutingTests); the on-duty admin's
alert carries the steps they may take, the assign list holds the owner's แอดมินกอง with their
open cases and pages with ถัดไป ›, a แอดมินฝ่าย may take the case, and a long form answers
without a link (LineStepTests); the one on duty sees until when (DutyBadgeTests); a Push
Message switch saves without a reload (PushPageTests); the release turns email off where LINE
was off (MigrationTests).
"""

from datetime import timedelta
from importlib import import_module
from unittest.mock import patch

from django.apps import apps
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from operations import (
    identity,
    options,
    orchestration,
    roster_match,
    staff_email,
    staff_line,
    staff_work,
)
from operations.models import (
    AuditEvent,
    District,
    Division,
    DutyShift,
    Employee,
    NotificationDelivery,
    OutboxEvent,
    Requester,
    Role,
    StaffProfile,
    SystemOption,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import BOB, CAROL, buttons, texts
from operations.tests.test_m13_roster import RosterTestCase
from operations.tests.test_m19_problem_tile import PickTestCase
from operations.tests.test_m26_push_and_duty import on_line

N = NotificationDelivery
ANN_LINE = BOB  # the แอดมินฝ่าย's own LINE; ALICE files the cases
HEAD_LINE = CAROL


class MatchTests(RosterTestCase):
    def setUp(self):
        super().setUp()
        self.web = make_staff("web-one", (Role.STAFF, "กคว."))
        self.web.email = "one@example.test"
        self.web.save(update_fields=["email"])

    def test_the_roster_import_ties_an_account_by_its_email(self):
        self.load()  # 510001 one@example.test
        profile = StaffProfile.objects.get(user=self.web)
        self.assertEqual(profile.employee_id, "510001")
        event = AuditEvent.objects.get(action="staff.employee_id_matched")
        self.assertEqual((event.actor, event.reason), (self.admin, "จับคู่จากอีเมลที่ตรงกับทะเบียน"))

    def test_no_match_keeps_na_and_says_why(self):
        self.load()
        other = make_staff("nobody", (Role.STAFF, "กคว."))
        StaffProfile.objects.filter(user=other).update(display_name="ทดสอบ")  # a roster name
        match = roster_match.link(other)
        self.assertIsNone(match.employee)
        self.assertIn("ไม่มีอีเมล", match.reason)
        other.email = "else@example.test"
        other.save(update_fields=["email"])
        self.assertIn("ไม่ตรงกับใคร", roster_match.link(other).reason)
        self.assertEqual(StaffProfile.objects.get(user=other).employee_id, "")

    def test_an_id_kept_by_a_line_account_waits_for_the_admin_to_merge(self):
        self.web.email = ""
        self.web.save(update_fields=["email"])
        self.load()
        line_account = make_staff(f"{identity.LINE_ACCOUNT}510001")
        StaffProfile.objects.filter(user=line_account).update(employee_id="510001")
        self.web.email = "one@example.test"
        self.web.save(update_fields=["email"])
        match = roster_match.link(self.web, self.admin)
        self.assertEqual(match.merge_from, line_account)
        self.assertEqual(StaffProfile.objects.get(user=self.web).employee_id, "")  # not merged
        client = Client()
        client.force_login(self.admin)
        page = client.get(reverse("admin-user-detail", args=[self.web.pk]))
        self.assertContains(page, "รวมบัญชี")


class ChartMatchTests(RosterTestCase):
    """Owner, 8 พ.ย. 2569: a web account an admin made takes its สังกัด from ผังหน่วยงาน, the
    people who passed the employee check in LINE, by ชื่อ-นามสกุล; or from a username that is a
    รหัสพนักงาน."""

    def setUp(self):
        super().setUp()
        self.kcw = Division.objects.get(code="กคว.")

    def employee(self, employee_id, first_name):
        return Employee.objects.create(employee_id=employee_id, name=first_name, division=self.kcw)

    def in_line(self, employee, name):
        return Requester.objects.create(
            display_name=name,
            district=District.objects.order_by("code").first(),
            approved_by=self.admin,
            employee=employee,
            employee_link=Requester.EmployeeLink.VERIFIED,
        )

    def web(self, username, name):
        user = make_staff(username)
        StaffProfile.objects.filter(user=user).update(display_name=name)
        return user

    def test_the_full_name_of_a_person_in_line_gives_the_unit(self):
        self.in_line(self.employee("520001", "สมหญิง"), "สมหญิง ใจดี")
        user = self.web("A001", "สมหญิง  ใจดี")
        self.assertEqual(roster_match.link(user).how, "name")
        profile = StaffProfile.objects.get(user=user)
        self.assertEqual((profile.employee_id, profile.division), ("520001", self.kcw))

    def test_a_first_name_alone_or_a_shared_name_matches_nobody(self):
        self.in_line(self.employee("520001", "สมหญิง"), "สมหญิง")
        self.assertIsNone(roster_match.link(self.web("A002", "สมหญิง")).employee)
        self.in_line(self.employee("520002", "สมชาย"), "สมชาย ใจดี")
        self.in_line(self.employee("520003", "สมชาย"), "สมชาย ใจดี")
        match = roster_match.link(self.web("A003", "สมชาย ใจดี"))
        self.assertIsNone(match.employee)
        self.assertIn("มากกว่าหนึ่งคน", match.reason)

    def test_a_username_that_is_an_employee_id(self):
        self.employee("2635435", "ทดสอบ")
        user = self.web("2635435", "Test")
        self.assertEqual(roster_match.link(user).how, "username")
        self.assertEqual(StaffProfile.objects.get(user=user).division, self.kcw)

    def test_a_person_checked_in_line_later_fills_the_web_account(self):
        user = self.web("A004", "ชนันพร ผ่องศรี")
        self.assertIsNone(roster_match.link(user).employee)
        requester = self.in_line(self.employee("520004", "ชนันพร"), "ชนันพร ผ่องศรี")
        roster_match.link_requester(requester, self.admin)
        self.assertEqual(StaffProfile.objects.get(user=user).division, self.kcw)


class AlertTestCase(PickTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ann = make_staff("ann-dept", (Role.DEPARTMENT_SUPERVISOR, None))
        cls.cat = make_staff("cat-dept", (Role.DEPARTMENT_SUPERVISOR, None))  # no LINE
        cls.head = make_staff("kcw-head", (Role.SUPERVISOR, "กคว."))
        for user in (cls.ann, cls.cat, cls.head):
            user.email = f"{user.username}@example.test"
            user.save(update_fields=["email"])

    def link(self, user, line_user):
        self.type(line_user, f"เชื่อม {staff_line.issue_code(user)}")

    def new_case(self):
        case, _ = self.file("กองเครื่องวัดหน่วยไฟฟ้า")
        event = OutboxEvent.objects.get(
            event_type=orchestration.EVENT_CASE_SUBMITTED, subject_id=str(case.pk)
        )
        staff_email.plan_for_event(event)
        return case

    def labels(self, user, kind="staff_new_case"):
        delivery = N.objects.get(kind=kind, recipient_user=user, channel=N.Channel.LINE_PUSH)
        return list(buttons([delivery.message]))


class RoutingTests(AlertTestCase):
    def test_a_new_case_goes_to_the_department_admin_on_duty(self):
        self.link(self.ann, ANN_LINE)
        self.link(self.head, HEAD_LINE)
        self.new_case()
        people = set(
            N.objects.filter(kind="staff_new_case").values_list("recipient_user", flat=True)
        )
        self.assertEqual(people, {self.ann.pk})  # Q6: the แอดมินกอง's switch starts off

    @override_settings(STAFF_EMAIL_ENABLED=True)
    def test_email_has_its_own_switch_and_reaches_admins_without_line(self):
        self.link(self.ann, ANN_LINE)
        case = self.new_case()
        options.change(None, "push:staff_reopen", "off", "")  # LINE off, email still on (Q7)
        staff_email.plan("staff_reopen", "reopen-1", case, [])
        mails = N.objects.filter(kind="staff_reopen", channel=N.Channel.EMAIL)
        self.assertEqual({d.recipient_user for d in mails}, {self.ann, self.cat})  # Q8
        options.change(None, "push:staff_reopen:email", "off", "")
        staff_email.plan("staff_reopen", "reopen-2", case, [])
        self.assertEqual(N.objects.filter(kind="staff_reopen", channel=N.Channel.EMAIL).count(), 2)

    def test_the_one_who_did_the_step_is_left_out_unless_switched_on(self):
        self.link(self.ann, ANN_LINE)
        case = self.new_case()
        staff_email.plan("staff_reopen", "reopen-1", case, [], actor_id=self.ann.pk)
        self.assertFalse(N.objects.filter(kind="staff_reopen").exists())  # Q13
        options.change(None, options.PUSH_SELF, "on", "")
        staff_email.plan("staff_reopen", "reopen-2", case, [], actor_id=self.ann.pk)
        self.assertTrue(N.objects.filter(kind="staff_reopen", recipient_user=self.ann).exists())

    def test_on_duty_means_only_those_on_duty(self):
        bob = on_line(make_staff("bob-dept", (Role.DEPARTMENT_SUPERVISOR, None)))
        self.link(self.ann, ANN_LINE)
        with patch("operations.duty.on_duty", return_value=[bob]):
            self.new_case()
        people = set(
            N.objects.filter(kind="staff_new_case").values_list("recipient_user", flat=True)
        )
        self.assertEqual(people, {bob.pk})


class LineStepTests(AlertTestCase):
    def setUp(self):
        super().setUp()
        self.link(self.ann, ANN_LINE)

    def step(self, case, label):
        delivery = N.objects.get(kind="staff_new_case", recipient_user=self.ann)
        return self.say(self.postback(ANN_LINE, buttons([delivery.message])[label]))

    def test_the_duty_alert_carries_the_steps_open_to_the_admin(self):
        self.new_case()
        labels = self.labels(self.ann)
        self.assertTrue(labels[0].startswith("ดูเรื่อง"))
        self.assertEqual(labels[1:4], ["ดำเนินการ", "มอบหมายให้แอดมินกอง", "รับงานเอง"])
        self.assertNotIn("เปิดในเว็บ", labels)

    def test_view_only_keeps_just_the_view_button(self):
        options.change(None, options.STAFF_LINE_ACTIONS, "view", "")
        self.new_case()
        labels = self.labels(self.ann)
        self.assertEqual(len(labels), 1)
        self.assertTrue(labels[0].startswith("ดูเรื่อง"))

    def test_the_admin_assigns_one_of_the_owner_heads_and_confirms(self):
        away = make_staff("kcw-away", (Role.SUPERVISOR, "กคว."))
        StaffProfile.objects.filter(user=away).update(away_until=timezone.localdate())
        make_staff("kcw-staff", (Role.STAFF, "กคว."))
        case = self.new_case()
        reply = self.step(case, "มอบหมายให้แอดมินกอง")
        names = [label for label in buttons(reply) if label != "ยกเลิก"]
        self.assertEqual(names, ["Kcw-Head (0)"])  # Q18: แอดมินกอง only, nobody away
        reply = self.press(ANN_LINE, "Kcw-Head (0)")
        self.assertIn(f"มอบหมายเรื่อง {case.number} ให้ Kcw-Head ใช่ไหม", texts(reply))
        self.press(ANN_LINE, "ยืนยัน")
        case.refresh_from_db()
        self.assertEqual(case.assignee, self.head)

    def test_a_long_list_pages_with_next(self):
        for n in range(12):
            make_staff(f"kcw-head-{n:02d}", (Role.SUPERVISOR, "กคว."))
        case = self.new_case()
        reply = self.step(case, "มอบหมายให้แอดมินกอง")
        first = list(buttons(reply))
        self.assertEqual(len(first), 13)
        self.assertEqual(first[-2:], ["ถัดไป ›", "ยกเลิก"])
        reply = self.press(ANN_LINE, "ถัดไป ›")
        second = list(buttons(reply))
        self.assertEqual(second[-2:], ["‹ หน้าแรก", "ยกเลิก"])
        self.assertEqual(len(first) - 2 + len(second) - 2, 13)  # every แอดมินกอง once
        self.assertFalse(set(first[:-2]) & set(second[:-2]))

    def test_the_department_admin_takes_the_case_themselves(self):
        case = self.new_case()
        self.step(case, "รับงานเอง")
        case.refresh_from_db()
        self.assertEqual((case.assignee, case.accepted_by), (self.ann, self.ann))  # Q20

    def test_a_long_form_step_answers_without_a_link(self):
        self.link(self.head, HEAD_LINE)
        case = self.new_case()
        line = self.head_line()
        step = {"case": str(case.pk), "action": "cancel"}  # cannot be undone: the web
        data = staff_line._token(line, "staff_step", step, timezone.now())
        reply = self.say(self.postback(HEAD_LINE, data))
        self.assertIn("ขั้นนี้ทำในเว็บ", texts(reply))
        self.assertFalse([v for v in buttons(reply).values() if str(v).startswith("http")])

    def head_line(self):
        from operations.models import ChannelIdentity

        return ChannelIdentity.objects.get(external_id=HEAD_LINE)

    def test_a_division_admin_picks_from_everyone_in_the_division(self):
        case = self.new_case()
        member = make_staff("kcw-member", (Role.STAFF, "กคว."))
        choices = dict(staff_work.assignees(case))
        self.assertIn(str(member.pk), choices)
        self.assertNotIn(str(member.pk), dict(staff_work.assignees(case, heads=True)))


class DutyBadgeTests(AlertTestCase):
    def test_the_one_on_duty_sees_until_when(self):
        self.link(self.ann, ANN_LINE)
        start = timezone.localtime() - timedelta(hours=1)
        end = start + timedelta(hours=3)
        DutyShift.objects.create(on=start.date(), start=start.time(), end=end.time(), user=self.ann)
        client = Client()
        client.force_login(self.ann)
        page = client.get(reverse("console"))
        self.assertContains(page, "อยู่เวรถึง")
        self.assertContains(page, f"{end:%H:%M} น.")
        self.assertNotContains(page, reverse("admin-duty"))  # Q8: the page is the admins'
        client.force_login(self.cat)
        self.assertNotContains(client.get(reverse("console")), "อยู่เวรถึง")


class PushPageTests(AlertTestCase):
    def test_a_switch_saves_without_a_reload(self):
        client = Client()
        client.force_login(self.boss)
        page = client.get(reverse("admin-push"))
        self.assertContains(page, "แจ้งเรื่องที่ตัวเองทำ")
        self.assertContains(page, "ขยายทั้งหมด")
        fetch = {"HTTP_X_REQUESTED_WITH": "fetch"}
        response = client.post(
            reverse("admin-push"), {"key": "push:staff_handoff:email", "value": "off"}, **fetch
        )
        self.assertEqual(response.json(), {"key": "push:staff_handoff:email", "value": "off"})
        self.assertFalse(options.push_on("staff_handoff", "email"))
        response = client.post(reverse("admin-push"), {"key": "push:nope", "value": "x"}, **fetch)
        self.assertEqual(response.status_code, 400)


class MigrationTests(TestCase):
    def test_email_is_turned_off_where_line_was_off(self):
        SystemOption.objects.create(key="push:staff_handoff", value="off")
        migration = import_module("operations.migrations.0102_email_switch_follows_line")
        migration.forward(apps, None)
        self.assertEqual(SystemOption.objects.get(key="push:staff_handoff:email").value, "off")
        self.assertFalse(SystemOption.objects.filter(key="push:staff_reopen:email").exists())
