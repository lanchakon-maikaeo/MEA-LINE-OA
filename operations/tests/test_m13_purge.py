"""ล้างข้อมูลจำลอง (15 ต.ค. 2569 Q1–Q4, Q33, Q37): purge_demo_data and the example cases."""

from datetime import timedelta
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from operations import notifications, purge, reports
from operations.models import (
    AuditEvent,
    Case,
    CaseLine,
    ChatAnswer,
    Division,
    Employee,
    Requester,
    Role,
    RoleGrant,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m13_org import OrgTestCase, withdrawal


class PurgeTests(OrgTestCase):
    def setUp(self):
        self.sim = Division.objects.create(code="SIM-X", name="กองจำลอง", is_synthetic=True)
        self.demo_user = make_staff("e2e-x", (Role.STAFF, "SIM-X"))
        _, self.example = self.submit(withdrawal(("MTR_SYN_A", "1")))
        _, self.old = self.submit(withdrawal(("TRF_SYN_A", "1")))
        _, self.in_sim = self.submit(withdrawal(("MTR_SYN_A", "2")))
        Case.objects.filter(pk=self.in_sim.pk).update(owner_division=self.sim)
        self.fake = Requester.objects.create(
            display_name="ผู้แจ้งจำลอง", district=self.req1.district, approved_by=self.admin
        )
        Employee.objects.create(employee_id="SYN1", name="พนักงานจำลอง", is_synthetic=True)
        ChatAnswer.objects.create(
            key="contact",
            title="ติดต่อ",
            text="ติดต่อกอง (เบอร์จำลอง รอ ฝอจ. ยืนยัน) 02-000-0001",
            status=ChatAnswer.Status.APPROVED,
        )
        self.later = timezone.now() + timedelta(minutes=1)  # every case here is "before"

    def test_dry_run_changes_nothing_and_lists_what_goes(self):
        out = StringIO()
        call_command(
            "purge_demo_data", "--before", self.later.isoformat(), "--keep", "", stdout=out
        )
        text = out.getvalue()
        for case in (self.example, self.old, self.in_sim):
            self.assertIn(case.number, text)
        self.assertIn("e2e-x", text)
        self.assertIn("Dry run", text)
        self.assertEqual(Case.objects.count(), 3)
        self.assertTrue(Division.objects.filter(code="SIM-X").exists())

    def test_apply_keeps_the_examples_and_removes_the_rest(self):
        lines = CaseLine.objects.filter(case=self.example).count()
        call_command(
            "purge_demo_data",
            "--before",
            self.later.isoformat(),
            "--keep",
            self.example.number,
            "--apply",
            "--username",
            "admin",
            "--reason",
            "ล้างข้อมูลจำลองก่อนใช้งานจริง",
            stdout=StringIO(),
        )
        self.assertEqual(list(Case.objects.values_list("number", flat=True)), [self.example.number])
        kept = Case.objects.get()
        self.assertTrue(kept.is_example)
        self.assertEqual(CaseLine.objects.filter(case=kept).count(), lines)
        self.assertFalse(Division.objects.filter(is_synthetic=True).exists())
        self.demo_user.refresh_from_db()
        self.assertFalse(self.demo_user.is_active)
        self.assertFalse(RoleGrant.objects.filter(user=self.demo_user).exists())  # went with SIM-X
        self.assertTrue(Requester.objects.filter(pk=self.req1.pk).exists())  # a real person stays
        self.assertFalse(Requester.objects.filter(pk=self.fake.pk).exists())
        self.assertFalse(Employee.objects.filter(is_synthetic=True).exists())
        self.assertEqual(ChatAnswer.objects.get(key="contact").status, ChatAnswer.Status.RETIRED)
        event = AuditEvent.objects.get(action="demo_data.purged")
        self.assertEqual(event.data["examples"], [self.example.number])
        self.assertIn("e2e-x", event.data["accounts_off"])

    def test_an_example_must_be_in_a_real_division_and_at_most_two(self):
        with self.assertRaises(ValidationError):
            purge.plan([self.in_sim.number], self.later)
        with self.assertRaises(ValidationError):
            purge.plan([self.example.number, self.old.number, "X"], self.later)
        with self.assertRaises(CommandError):
            call_command("purge_demo_data", "--apply", "--username", "admin", stdout=StringIO())

    def test_cases_after_the_cutoff_stay(self):
        plan = purge.plan([], self.later - timedelta(days=1))
        self.assertEqual([c.number for c in plan.cases], [self.in_sim.number])  # SIM only

    def test_an_example_is_not_counted_and_messages_nobody(self):
        Case.objects.filter(pk=self.example.pk).update(is_example=True)
        sets = reports._Sets(self.admin, None, timezone.now(), 30)
        self.assertNotIn(self.example.pk, set(sets.visible.values_list("pk", flat=True)))
        self.assertIn(self.old.pk, set(sets.visible.values_list("pk", flat=True)))
        example = Case.objects.get(pk=self.example.pk)
        self.assertIsNone(
            notifications._create(None, "status", "normal", object(), "ทดสอบ", case=example)
        )
        page = self.client
        page.force_login(self.admin)
        self.assertContains(page.get(f"/cases/{example.number}/"), "ตัวอย่าง")
