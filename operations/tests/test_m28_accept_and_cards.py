"""การ์ดเรื่องของเจ้าหน้าที่ รับงาน แจ้งเตือนมอบหมาย อีเมลรายประเภท และการแก้บัญชี (ADR 0049)."""

from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse

from operations import (
    case_numbers,
    options,
    orchestration,
    services,
    staff_email,
    staff_inbox,
    staff_line,
    web_actions,
)
from operations.contracts import Actor, CommandFailed
from operations.models import (
    AuditEvent,
    Case,
    NotificationDelivery,
    OutboxEvent,
    Role,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import ALICE, BOB, CAROL, buttons, texts
from operations.tests.test_m16_kcw_work import WorkTestCase

WORKER = "U" + "1" * 32
HEAD = "U" + "2" * 32
DUTY = "U" + "3" * 32
TYPED = "Ded-Center-2569!"  # pragma: allowlist secret (a test-only password)
N = NotificationDelivery


class AcceptTestCase(WorkTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.head = make_staff("kcw-head-28", (Role.SUPERVISOR, "กคว."))
        cls.other = make_staff("kcw-other-28", (Role.STAFF, "กคว."))
        cls.duty = make_staff("dept-28", (Role.DEPARTMENT_SUPERVISOR, None))

    def setUp(self):
        super().setUp()
        allowed = frozenset({ALICE, BOB, CAROL, WORKER, HEAD, DUTY})
        self.enterContext(override_settings(LINE_ALLOWED_RECIPIENTS=allowed))
        for user, line_user in ((self.kcw, WORKER), (self.head, HEAD), (self.duty, DUTY)):
            self.type(line_user, f"เชื่อม {staff_line.issue_code(user)}")

    def act(self, user, case, action, **payload):
        case.refresh_from_db()
        services.perform_action(
            Actor.staff(user),
            case.pk,
            action,
            expected_revision=case.revision,
            idempotency_key=f"{action}-{case.pk}-{case.revision}",
            payload=payload,
        )
        case.refresh_from_db()
        return case

    def assign(self, case, person, by=None):
        return self.act(by or self.head, case, "assign", assignee_id=person.pk)

    def alerts(self, case, kind):
        """Plan the alerts of the case's latest decision, as the worker does."""
        event = OutboxEvent.objects.filter(
            event_type=orchestration.EVENT_DECISION, subject_id=str(case.pk)
        ).latest("pk")
        staff_email.plan_for_event(event)
        return N.objects.filter(case=case, kind=kind).order_by("pk")

    def names(self, user, case):
        return [o.name for o in web_actions.available(user, case)]


class CardTests(AcceptTestCase):
    def card(self, case):
        reply = self.say(self.postback(HEAD, "m=staff_inbox"))
        return self.press(HEAD, case.number, reply)

    def test_the_card_names_each_number_as_picked_and_labels_the_details(self):
        case = self.withdraw("142019186")
        case_numbers.add(self.kcw, case, "slip", ["142019188"])
        case_numbers.add(self.kcw, case, "meter", ["99887766"])
        shown = texts(self.card(case))
        self.assertIn("เลขที่ใบเบิก: 142019186, 142019188", shown)
        self.assertIn("หมายเลขเครื่องวัดฯ: 99887766", shown)
        self.assertNotIn("เลขใบเบิก SAP", shown)
        self.assertIn("รายละเอียด: มิเตอร์ 12345678", shown)
        self.assertEqual(staff_inbox.title(case), "เลขที่ใบเบิก 142019186 +2")
        Case.objects.filter(pk=case.pk).update(purpose="-")
        case.refresh_from_db()
        self.assertIn("รายละเอียด: ไม่ระบุ", texts(self.card(case)))
        Case.objects.filter(pk=case.pk).update(sap_number="")
        case.extra_numbers.all().delete()
        case.refresh_from_db()
        self.assertEqual(staff_inbox.title(case), "ติดตามใบเบิก")  # no number, no details


class AcceptTests(AcceptTestCase):
    def test_the_assignee_takes_the_case_once_and_the_timeline_keeps_who(self):
        case = self.assign(self.withdraw("4500070101"), self.kcw)
        self.assertTrue(case.awaits_acceptance)
        self.assertEqual(self.names(self.kcw, case), ["accept_assignment", "return_assignment"])
        self.assertNotIn("accept_assignment", self.names(self.head, case))
        self.assertIn(case, staff_inbox.sources(self.head, "unassigned")[0])
        with self.assertRaises(CommandFailed):
            self.act(self.head, case, "accept_assignment")  # nobody presses it for them
        case = self.act(self.kcw, case, "accept_assignment")
        self.assertEqual((case.accepted_by, case.awaits_acceptance), (self.kcw, False))
        self.assertNotIn(case, staff_inbox.sources(self.head, "unassigned")[0])
        self.assertIn("request_info", self.names(self.kcw, case))
        page = Client()
        page.force_login(self.head)
        detail = page.get(reverse("case-detail", args=[case.number]))
        self.assertContains(detail, "รับงาน")
        self.assertContains(detail, "รับงานแล้ว")
        # Each new assignment asks again, even of someone who took it before.
        case = self.assign(case, self.other)
        case = self.assign(case, self.kcw)
        self.assertTrue(case.awaits_acceptance)

    def test_taking_it_yourself_is_accepting(self):
        case = self.act(self.other, self.withdraw("4500070102"), "claim")
        self.assertEqual(case.accepted_by, self.other)
        case = self.assign(case, self.head, by=self.head)
        self.assertEqual(case.accepted_by, self.head)

    def test_giving_it_back_needs_a_reason_and_tells_the_division_admin(self):
        case = self.assign(self.withdraw("4500070103"), self.kcw)
        with self.assertRaises(CommandFailed):
            self.act(self.kcw, case, "return_assignment")
        case = self.act(self.kcw, case, "return_assignment", reason="ลาพักร้อน")
        self.assertIsNone(case.assignee)
        told = self.alerts(case, "staff_assignment_returned")
        self.assertEqual([d.recipient_user for d in told], [self.head])


class AssignAlertTests(AcceptTestCase):
    def test_the_assignee_hears_with_an_accept_button_and_the_duty_admin_a_copy(self):
        case = self.assign(self.withdraw("4500070104"), self.kcw)
        told = {d.recipient_user: d for d in self.alerts(case, "staff_assigned")}
        self.assertEqual(set(told), {self.kcw, self.duty})
        self.assertIn("มีเรื่องมอบหมายให้คุณ", told[self.kcw].text)
        self.assertIn("มอบหมายเรื่องให้", told[self.duty].text)
        self.assertNotIn("รับงาน", buttons([told[self.duty].message]))
        take = buttons([told[self.kcw].message])["รับงาน"]
        reply = self.say(self.postback(WORKER, take))
        self.assertIn("บันทึกเรียบร้อยแล้ว: รับงาน", texts(reply))
        case.refresh_from_db()
        self.assertEqual(case.accepted_by, self.kcw)

    def test_nobody_is_told_of_a_case_they_took_and_the_switches_hold(self):
        case = self.assign(self.withdraw("4500070105"), self.head, by=self.head)
        self.assertFalse(self.alerts(case, "staff_assigned").exists())
        options.change(None, "push:staff_assigned:duty", "off", "")
        case = self.assign(case, self.kcw)
        self.assertEqual(
            [d.recipient_user for d in self.alerts(case, "staff_assigned")], [self.kcw]
        )

    @override_settings(STAFF_EMAIL_ENABLED=True)
    def test_email_goes_per_kind_as_push_message_sets_it(self):
        get_user_model().objects.filter(pk=self.kcw.pk).update(email="kcw@example.test")
        case = self.assign(self.withdraw("4500070106"), self.kcw)
        mails = self.alerts(case, "staff_assigned").filter(channel=N.Channel.EMAIL)
        self.assertEqual([m.email for m in mails], ["kcw@example.test"])
        options.change(None, "push:staff_assigned:email", "off", "")
        case = self.assign(case, self.other)
        case = self.assign(case, self.kcw)
        self.assertEqual(self.alerts(case, "staff_assigned").filter(channel="email").count(), 1)
        self.assertFalse(options.push_on("staff_new_case", "email"))  # LINE only before

    def test_the_push_page_has_an_email_switch_and_says_when_email_is_off(self):
        admin = make_staff("push-admin-28", (Role.ADMIN, None))
        page = Client()
        page.force_login(admin)
        shown = page.get(reverse("admin-push"))
        self.assertContains(shown, "มอบหมายผู้รับผิดชอบ")
        self.assertContains(shown, 'value="push:staff_assigned:email"')
        self.assertContains(shown, "STAFF_EMAIL_ENABLED")


class AccountTests(WorkTestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_staff("acct-admin-28", (Role.ADMIN, None))
        self.target = make_staff("line-employee-2171408", (Role.STAFF, "กคว."))
        self.web = Client()
        self.web.force_login(self.admin)

    def post(self, action, **data):
        url = reverse("admin-user-action", args=[self.target.pk, action])
        return self.web.post(url, data, follow=True)

    def test_an_admin_renames_an_account_and_sets_its_email(self):
        page = self.post(
            "account", username="somchai.k", display_name="  สมชาย  ใจดี ", email="s@example.test"
        )
        self.assertContains(page, "บันทึกข้อมูลบัญชีแล้ว")
        self.target.refresh_from_db()
        self.assertEqual(
            (self.target.username, self.target.staff_profile.display_name, self.target.email),
            ("somchai.k", "สมชาย ใจดี", "s@example.test"),
        )
        event = AuditEvent.objects.get(action="staff.account_changed")
        self.assertEqual(event.data["previous_username"], "line-employee-2171408")
        self.assertContains(
            self.post("account", username="ACCT-ADMIN-28", display_name="x"), "ใช้แล้ว"
        )
        self.assertContains(
            self.post("account", username="ok", display_name="x", email="bad"), "อีเมล"
        )

    def test_an_admin_types_a_password_the_person_must_change(self):
        self.assertContains(self.post("set-password", password="a", password2="b"), "ไม่ตรงกัน")
        self.post("set-password", password="1234", password2="1234")
        self.target.refresh_from_db()
        self.assertFalse(self.target.check_password("1234"))  # too weak for the validators
        self.post("set-password", password=TYPED, password2=TYPED)
        self.target.refresh_from_db()
        self.assertTrue(self.target.check_password(TYPED))
        self.assertTrue(self.target.staff_profile.must_change_password)
        self.assertTrue(AuditEvent.objects.filter(action="staff.password_set").exists())
        staff = Client()
        staff.force_login(self.target)
        url = reverse("admin-user-action", args=[self.admin.pk, "account"])
        staff.post(url, {"username": "taken-over", "display_name": "x"})
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.username, "acct-admin-28")
