"""ADR 0053 (grill 10 พ.ย. 2569): a LINE that passed the employee check joins the staff account
holding the same รหัสพนักงาน, and that person hears of the cases waiting for their รับงาน; every
list shows the case's own status, with รอรับงาน beside the ผู้รับผิดชอบ.

Acceptance → tests: the LINE joins the account whichever comes first, the registration or the
account's รหัส, even when the roster row names no กอง, and an admin typing the รหัส ties it too
(TieTests); the deploy sweep ties what waits and leaves an account that already has a LINE
(TieTests); the tied LINE hears once of each case waiting for its รับงาน and takes it from the
push (CatchUpTests); the staff email carries no mock-data note (ListTests). ADR 0058 replaced how
lists show รอรับงาน (test_adr0058_untaken); the handoff tab name stays in WebTests there.
"""

from django.conf import settings
from django.test import override_settings

from operations import identity, roster_match, staff_email, staff_line
from operations.models import (
    AuditEvent,
    ChannelIdentity,
    District,
    Employee,
    NotificationDelivery,
    Requester,
    Role,
    StaffProfile,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import buttons, texts
from operations.tests.test_m13_roster import RosterTestCase
from operations.tests.test_m28_accept_and_cards import AcceptTestCase

N = NotificationDelivery
CHANANPORN = "U" + "4" * 32


class TieTests(RosterTestCase):
    def register(self, employee_id):
        """The person passes the employee check in LINE; their roster row names no กอง."""
        employee = Employee.objects.create(employee_id=employee_id, name="ชนันพร")
        identity.submit_registration(
            "line",
            f"tie-{employee_id}",
            f"พนักงาน {employee_id}",
            "D01",
            employee=employee,
            verified_by="otp",
        )
        self.assertEqual(identity.approve_verified_registrations(), 1)
        return ChannelIdentity.objects.get(external_id=f"tie-{employee_id}")

    def holder(self, username, employee_id, role=(Role.SUPERVISOR, "กคว.")):
        """An account that already holds the รหัส, set the way production was left."""
        user = make_staff(username, role)
        StaffProfile.objects.filter(user=user).update(employee_id=employee_id)
        return user

    def test_a_line_checked_before_the_account_had_its_id_joins_it_later(self):
        line = self.register("2630617")
        self.assertIsNone(line.staff_user)  # nobody held the รหัส yet
        user = make_staff("2630617", (Role.SUPERVISOR, "กคว."))
        self.assertEqual(roster_match.link(user).how, "username")
        line.refresh_from_db()
        self.assertEqual(line.staff_user, user)
        event = AuditEvent.objects.get(action="staff.line_linked")
        self.assertEqual(event.data["method"], "employee_record")

    def test_a_registration_joins_the_account_holding_its_id_without_a_kong(self):
        user = self.holder("A002", "2240035", (Role.DEPARTMENT_SUPERVISOR, None))
        self.assertEqual(self.register("2240035").staff_user, user)

    def test_an_admin_typing_the_id_ties_the_waiting_line(self):
        line = self.register("520030")
        user = make_staff("A011", (Role.STAFF, "กคว."))
        identity.set_employee_id(self.admin, user, "520030", "ใส่รหัสพนักงาน")
        line.refresh_from_db()
        self.assertEqual(line.staff_user, user)

    def test_the_deploy_sweep_ties_what_waits_and_leaves_an_account_with_a_line(self):
        waiting = self.register("2244925")
        other = self.register("520020")
        user = self.holder("line-employee-1300", "2244925")
        busy = self.holder("A010", "520020")
        ChannelIdentity.objects.create(
            channel=ChannelIdentity.Channel.LINE, external_id="busy-own-line", staff_user=busy
        )
        self.assertEqual(staff_line.tie_all(), 1)
        waiting.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual((waiting.staff_user, other.staff_user), (user, None))
        self.assertEqual(staff_line.tie_all(), 0)


class CatchUpTests(AcceptTestCase):
    def setUp(self):
        super().setUp()
        allowed = settings.LINE_ALLOWED_RECIPIENTS | {CHANANPORN}
        self.enterContext(override_settings(LINE_ALLOWED_RECIPIENTS=allowed))

    def test_a_line_tied_later_hears_once_of_each_case_waiting_for_its_accept(self):
        person = make_staff("2630617", (Role.SUPERVISOR, "กคว."))
        StaffProfile.objects.filter(user=person).update(employee_id="2630617")
        case = self.assign(self.withdraw("4500070301"), person)
        pushes = N.objects.filter(
            kind="staff_assigned", recipient_user=person, channel=N.Channel.LINE_PUSH
        )
        self.alerts(case, "staff_assigned")
        self.assertFalse(pushes.exists())  # no LINE yet: only the email could go
        requester = Requester.objects.create(
            display_name="พนักงาน 2630617",
            district=District.objects.order_by("code").first(),
            approved_by=self.head,
            employee=Employee.objects.create(employee_id="2630617", name="ชนันพร"),
            employee_link=Requester.EmployeeLink.VERIFIED,
        )
        line = ChannelIdentity.objects.create(
            channel=ChannelIdentity.Channel.LINE,
            external_id=CHANANPORN,
            requester=requester,
            status=ChannelIdentity.Status.ACTIVE,
        )
        self.assertEqual(staff_line.tie_verified(line), person)
        told = pushes.get()
        self.assertIn("มีเรื่องมอบหมายให้คุณ", told.text)
        self.assertEqual(staff_line.catch_up_assigned(person), [])  # once per case
        reply = self.say(self.postback(CHANANPORN, buttons([told.message])["รับงาน"]))
        self.assertIn("บันทึกเรียบร้อยแล้ว: รับงาน", texts(reply))
        case.refresh_from_db()
        self.assertEqual(case.accepted_by, person)


class ListTests(AcceptTestCase):
    def test_the_staff_email_has_no_mock_data_note(self):
        case = self.assign(self.withdraw("4500070304"), self.kcw)
        text = staff_email._text("staff_assigned", case)
        self.assertTrue(text.endswith("อีเมลนี้ส่งอัตโนมัติจาก DED Center"))
        self.assertNotIn("ข้อมูลจำลอง", text)
