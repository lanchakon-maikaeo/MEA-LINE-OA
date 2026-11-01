"""ไม่มีขั้นคัดกรอง (ADR 0025): a new case of any kind starts in its owner division at once
(ADR 0042 Q10); for cases filed earlier that still wait, whoever holds the division's
approval authority approves a case without item rows as a whole and sets the level in the same
form, or many at once from the queue; not approving needs a reason; cases already waiting for
screening move to approval; queues show the newest first with emergencies on top."""

from datetime import timedelta

from django.test import Client
from django.utils import timezone

from operations import approvals, options, policy, services
from operations.contracts import Actor, CommandFailed
from operations.models import AuthorityGrant, Case, Decision, Role, SystemOption
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import ALICE
from operations.tests.test_m16_flow_runner import FlowTestCase


class NoScreeningTestCase(FlowTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.approver = make_staff("kcw-approver", (Role.STAFF, "กคว."))
        AuthorityGrant.objects.create(
            user=cls.approver,
            activity=AuthorityGrant.Activity.WITHDRAWAL_APPROVAL,
            division=None,
            valid_from=timezone.now() - timedelta(minutes=1),
            reason="ทดสอบ",
            approval_reference="TEST-ADR-0025",
            granted_by=cls.flow_admin,
        )

    def withdraw(self, number):
        self.track("มิเตอร์", "ติดตามใบเบิก", number=number)
        self.press(ALICE, "เพิ่มรายละเอียด")  # 30 ต.ค. 2569: the summary comes right after the number
        self.type(ALICE, "มิเตอร์ 5(100)A 1 เครื่อง")
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        return Case.objects.get(sap_number=number)

    def problem(self, description):
        result = services.submit_case(
            Actor.for_requester(self.requester),
            idempotency_key=f"problem-{abs(hash(description))}",
            payload={"kind": "problem", "division_code": "กคว.", "description": description},
        )
        return Case.objects.get(pk=result.case_id)

    def waiting(self, case):
        """A case filed before ADR 0042 Q10 still waits for its division to accept it."""
        Case.objects.filter(pk=case.pk).update(status=Case.Status.AWAITING_APPROVAL)
        case.refresh_from_db()
        return case


class StatusTests(NoScreeningTestCase):
    def test_every_kind_starts_in_its_division_at_once(self):
        case = self.withdraw("4500100001")
        self.assertEqual(case.status, "in_progress")
        quiet = self.problem("ขอสอบถามการลงโปรแกรมอ่านมิเตอร์")
        self.assertEqual(quiet.status, "in_progress")
        hazard = self.problem("สายไฟขาดห้อย มีประกายไฟ")
        self.assertEqual(hazard.status, "in_progress")
        self.assertEqual(
            list(hazard.decisions.values_list("action", flat=True)), ["submit", "start_work"]
        )

    def test_cases_waiting_for_screening_move_to_approval(self):
        SystemOption.objects.filter(key=options.SCREENING).update(value="on")
        case = self.withdraw("4500100002")
        self.assertEqual(case.status, "submitted")
        SystemOption.objects.filter(key=options.SCREENING).update(value="off")
        self.assertEqual(approvals.move_waiting(), 1)
        case.refresh_from_db()
        self.assertEqual(case.status, "awaiting_approval")
        self.assertTrue(Decision.objects.filter(case=case, action="skip_screening").exists())


class ApprovalTests(NoScreeningTestCase):
    def test_a_whole_case_is_approved_with_its_level_in_one_form(self):
        case = self.waiting(self.problem("ขอสอบถามการลงโปรแกรมอ่านมิเตอร์"))
        client = Client()
        client.force_login(self.approver)
        page = client.get(f"/cases/{case.number}/act/approve/").content.decode()
        self.assertIn("ระดับความเร่งด่วน", page)
        self.assertNotIn("จำนวนที่อนุมัติ", page)  # no item rows to decide
        r = client.post(
            f"/cases/{case.number}/act/approve/",
            {
                "priority": "P2",
                "reason": "ตามที่กองตกลง",
                "revision": case.revision,
                "idempotency_key": "web-approve-1",
            },
        )
        self.assertIn(r.status_code, (200, 302))
        case.refresh_from_db()
        self.assertEqual(case.status, "in_progress")
        self.assertEqual(policy.state_for(case).confirmed_priority, "P2")

    def test_not_approving_needs_a_reason_and_staff_without_authority_cannot(self):
        case = self.waiting(self.withdraw("4500100003"))
        with self.assertRaises(CommandFailed):
            services.perform_action(
                Actor.staff(self.approver),
                case.pk,
                "deny_approval",
                expected_revision=case.revision,
                idempotency_key="deny-no-reason",
                payload={},
            )
        services.perform_action(
            Actor.staff(self.approver),
            case.pk,
            "deny_approval",
            expected_revision=case.revision,
            idempotency_key="deny-with-reason",
            payload={"reason": "ใบเบิกไม่มีลายเซ็น"},
        )
        case.refresh_from_db()
        self.assertEqual(case.status, "rejected")
        clerk = make_staff("kcw-no-authority", (Role.STAFF, "กคว."))
        other = self.waiting(self.withdraw("4500100004"))
        with self.assertRaises(CommandFailed):
            services.perform_action(
                Actor.staff(clerk),
                other.pk,
                "approve",
                expected_revision=other.revision,
                idempotency_key="approve-no-authority",
                payload={},
            )

    def test_many_are_approved_from_the_queue(self):
        first = self.waiting(self.withdraw("4500100005"))
        second = self.waiting(self.withdraw("4500100006"))
        client = Client()
        client.force_login(self.approver)
        page = client.get("/divisions/กคว./").content.decode()
        self.assertIn("รับเรื่องที่ติ๊ก", page)
        self.assertLess(page.index(second.number), page.index(first.number))  # newest first
        client.post("/divisions/กคว./approve/", {"case": [first.number, second.number]})
        for case in (first, second):
            case.refresh_from_db()
            self.assertEqual(case.status, "awaiting_execution")


class QueueOrderTests(NoScreeningTestCase):
    def test_an_emergency_is_on_top_and_the_old_order_is_one_tap_away(self):
        hazard = self.problem("สายไฟขาดห้อย มีประกายไฟ")
        newer = self.withdraw("4500100007")
        client = Client()
        client.force_login(self.approver)
        page = client.get("/divisions/กคว./").content.decode()
        self.assertLess(page.index(hazard.number), page.index(newer.number))
        self.assertIn("เรียงตามความเร่งด่วน", page)
        self.assertEqual(client.get("/divisions/กคว./?sort=urgent").status_code, 200)
