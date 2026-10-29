"""ล้างผู้แจ้งและเรื่องทั้งหมด (29 Oct 2569): with the move to LINE OA @705ttrga the user chose
"ล้างผู้แจ้งและเรื่องทั้งหมด" at the switch, so everyone registers again from nothing.

Acceptance → tests: a dry run lists what goes and changes nothing; --apply removes every case,
requester, LINE account and registration, keeps staff accounts, employees and divisions, and is
audited; case numbers go on counting unless --reset-numbers is given (RestartTests).
"""

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command

from operations import identity
from operations.models import (
    AuditEvent,
    Case,
    ChannelIdentity,
    Division,
    Employee,
    RegistrationRequest,
    Requester,
)
from operations.tests.test_m13_org import OrgTestCase, withdrawal

REASON = "ย้ายไป LINE OA ใหม่ เริ่มลงทะเบียนใหม่ทั้งหมด"


class RestartTests(OrgTestCase):
    def setUp(self):
        ChannelIdentity.objects.create(
            channel="line", external_id="U" + "a" * 32, requester=self.req1, status="active"
        )
        identity.submit_registration("line", "U" + "b" * 32, "รอรับรอง", "D01")
        _, self.case = self.submit(withdrawal(("MTR_SYN_A", "1")))
        Employee.objects.create(employee_id="SYN1")

    def restart(self, *extra):
        out = StringIO()
        call_command("restart_line_data", *extra, stdout=out)
        return out.getvalue()

    def apply(self, *extra):
        return self.restart("--apply", "--username", "admin", "--reason", REASON, *extra)

    def test_a_dry_run_lists_what_goes_and_changes_nothing(self):
        text = self.restart()
        self.assertIn("Dry run", text)
        self.assertIn("Case", text)
        self.assertIn("ChannelIdentity", text)
        self.assertEqual(Case.objects.count(), 1)
        self.assertEqual(ChannelIdentity.objects.count(), 2)

    def test_apply_removes_people_and_cases_and_keeps_staff_and_the_roster(self):
        staff, divisions = get_user_model().objects.count(), Division.objects.count()
        self.apply()
        for model in (Case, Requester, ChannelIdentity, RegistrationRequest):
            self.assertFalse(model.objects.exists(), model.__name__)
        self.assertEqual(get_user_model().objects.count(), staff)
        self.assertEqual(Division.objects.count(), divisions)
        self.assertTrue(Employee.objects.filter(employee_id="SYN1").exists())
        self.assertTrue(AuditEvent.objects.filter(action="line_data.restarted").exists())

    def submit_as_newcomer(self):
        self.req1 = Requester.objects.create(
            display_name="คนใหม่", district=self.req1.district, approved_by=self.admin
        )
        return self.submit(withdrawal(("MTR_SYN_A", "1")))[1]

    def test_numbers_go_on_unless_reset(self):
        self.apply()
        self.assertGreater(self.submit_as_newcomer().number, self.case.number)

    def test_reset_numbers_starts_from_one(self):
        self.apply("--reset-numbers")
        self.assertTrue(self.submit_as_newcomer().number.endswith("-000001"))
