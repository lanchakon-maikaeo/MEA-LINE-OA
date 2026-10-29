"""ย้าย LINE OA (28 Oct 2569): the bot moves from @894attlg to @705ttrga, which sits under another
LINE provider, so every person comes back with a new LINE user ID.

The user chose ข: "ให้แอดมินผูก LINE ใหม่กับผู้แจ้งเดิมในหน้ารับรอง". Acceptance → tests: an
admin approves the new LINE as the requester it was before, so old cases and their notices
follow it and the old LINE no longer receives them (LinkTests); a registration matched to another
employee is refused (LinkTests); the approval page suggests the requester with the same name and
the approve button links it (PageTests).
"""

from django.core.exceptions import ValidationError

from operations import identity
from operations.management.commands.run_worker import run_once
from operations.models import ChannelIdentity, Employee, NotificationDelivery, RegistrationRequest
from operations.tests.test_m03_line import ALICE
from operations.tests.test_m04_orchestration import OrchestrationTestCase

NEW = "U" + "n" * 32  # the same person, as the new OA's provider sees them


class LinkTests(OrchestrationTestCase):
    def register_again(self, name="ผู้แจ้ง Uaaa", employee=None):
        return identity.submit_registration("line", NEW, name, "D01", employee=employee)

    def test_the_new_line_takes_over_the_old_requester_and_its_cases(self):
        case = self.case()
        registration = self.register_again()

        requester = identity.approve_registration(
            self.admin, registration, "ย้าย LINE OA", requester=self.alice
        )

        self.assertEqual(requester, self.alice)
        line = ChannelIdentity.objects.get(external_id=NEW)
        self.assertEqual((line.requester, line.status), (self.alice, "active"))
        self.assertNotEqual(ChannelIdentity.objects.get(external_id=ALICE).status, "active")
        self.act(case, "start_triage")
        self.act(case, "request_info", reason="ขอเลขมิเตอร์")
        run_once()
        notice = NotificationDelivery.objects.get(kind="request_info")
        self.assertEqual(notice.identity.external_id, NEW)

    def test_a_registration_matched_to_another_employee_is_refused(self):
        mine = Employee.objects.create(employee_id="SYN90001")
        other = Employee.objects.create(employee_id="SYN90002")
        self.alice.employee = mine
        self.alice.save(update_fields=["employee"])
        registration = self.register_again(employee=other)

        with self.assertRaises(ValidationError):
            identity.approve_registration(self.admin, registration, "", requester=self.alice)
        self.assertEqual(ChannelIdentity.objects.get(external_id=ALICE).status, "active")


class PageTests(OrchestrationTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def test_the_page_suggests_the_same_name_and_approving_links_it(self):
        registration = identity.submit_registration("line", NEW, self.alice.display_name, "D01")

        page = self.client.get("/console/admin/registrations/")
        self.assertContains(page, "ผูกกับผู้แจ้งเดิม")
        self.assertContains(page, f'<option value="{self.alice.pk}" selected>')

        self.client.post(
            f"/console/admin/registrations/{registration.pk}/approve/",
            {"requester": self.alice.pk, "reason": "ย้าย LINE OA"},
        )
        registration.refresh_from_db()
        self.assertEqual(registration.status, RegistrationRequest.Status.APPROVED)
        self.assertEqual(ChannelIdentity.objects.get(external_id=NEW).requester, self.alice)
