"""ผู้ช่วยเหลือระบบ (ADR 0007, 9 ตุลาคม 2569): one person acts for every division.

Acceptance → tests: granted on the host only, one holder at a time, with real authority grants
that end with the role (GrantTests); alone it carries a withdrawal from intake to receipt across
divisions and duties, every step with a reason and labelled as การทำแทนกอง (ActingTests); the
case page, report and supervisors' e-mail show it (VisibilityTests).
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import CommandError, call_command
from django.test import override_settings

from operations import forms, identity, orchestration, services, staff_email, support
from operations.contracts import Actor, CommandFailed, ErrorCode
from operations.models import (
    AuditEvent,
    AuthorityGrant,
    Case,
    Decision,
    NotificationDelivery,
    OutboxEvent,
    Role,
    RoleGrant,
)
from operations.tests.test_m02_cases import key, make_staff, withdrawal
from operations.tests.test_m07_fulfilment import fuse, lines
from operations.tests.test_m10_web import WebTestCase

S = Case.Status


class SupportTestCase(WebTestCase):
    def setUp(self):
        super().setUp()
        self.designer = make_staff("designer")
        support.grant(self.designer, "ผู้ออกแบบระบบ (ทดสอบ)")

    def by_designer(self, case, action, payload=None, reason="ช่วยกองที่ติดงาน"):
        payload = dict(payload or {})
        if reason:
            payload.setdefault("reason", reason)
        return services.perform_action(
            Actor.staff(self.designer),
            case.pk,
            action,
            expected_revision=Case.objects.get(pk=case.pk).revision,
            idempotency_key=key(),
            payload=payload,
        )


class GrantTests(SupportTestCase):
    def test_role_comes_with_every_authority_and_ends_with_it(self):
        scope = identity.staff_scope(self.designer)
        self.assertTrue(scope.is_support and scope.has_any_access)
        activities = set(
            AuthorityGrant.objects.filter(
                user=self.designer, revoked_at__isnull=True, division__isnull=True
            ).values_list("activity", flat=True)
        )
        self.assertEqual(activities, set(AuthorityGrant.Activity.values))
        # An admin can take it away on the web at once (RB06); the authority goes with it.
        role = RoleGrant.objects.get(user=self.designer, role=Role.SYSTEM_SUPPORT)
        identity.revoke_role(self.admin, role, "บัญชีรั่ว")
        self.assertFalse(identity.staff_scope(self.designer).is_support)
        self.assertFalse(
            AuthorityGrant.objects.filter(user=self.designer, revoked_at__isnull=True).exists()
        )

    def test_only_on_the_host_and_one_holder(self):
        other = make_staff("other")
        with self.assertRaises(PermissionDenied):
            identity.grant_role(self.admin, other, Role.SYSTEM_SUPPORT, reason="ขอด้วย")
        self.assertNotIn(Role.SYSTEM_SUPPORT, dict(forms.GrantRoleForm().fields["role"].choices))
        with self.assertRaises(ValidationError):
            support.grant(other, "คนที่สอง")
        with self.assertRaises(CommandError):
            call_command("system_support", grant="other", reason="คนที่สอง")
        call_command("system_support", revoke="designer", reason="เปลี่ยนผู้ออกแบบ")
        call_command("system_support", grant="other", reason="ผู้ออกแบบคนใหม่")
        self.assertEqual(support.holder(), other)
        self.assertTrue(
            AuditEvent.objects.filter(action="role.system_support_granted", reason="ผู้ออกแบบคนใหม่")
        )


class ActingTests(SupportTestCase):
    def test_alone_from_intake_to_receipt_across_divisions_and_duties(self):
        case = self.submit(fuse(3))
        self.by_designer(case, "start_triage")
        self.by_designer(case, "request_handoff", {"to_division": "SIM-EQP"})
        self.by_designer(case, "accept_handoff")  # as SIM-EQP, though a member of none
        self.by_designer(case, "set_priority", {"priority": "P3"})
        self.by_designer(case, "complete_triage")
        self.by_designer(case, "approve", lines((1, "3")))  # the authority it holds
        # Approver issues the same line and confirms its own delivery: no separation applies.
        self.by_designer(
            case,
            "issue",
            {"recipient_name": "ผู้รับจำลอง", "lines": [{"line_no": 1, "quantity": "3"}]},
        )
        self.by_designer(
            case,
            "confirm",
            {"delivery": 1, "result": "full", "channel": "phone", "intake_note": "เขตโทรยืนยัน"},
        )
        case.refresh_from_db()
        self.assertEqual(case.status, S.CLOSED)
        decisions = Decision.objects.filter(case=case, actor=self.designer)
        self.assertEqual(decisions.count(), 8)
        for decision in decisions:
            self.assertIn("division", decision.data["support"], decision.action)
            self.assertTrue(decision.reason, decision.action)
        self.assertTrue(AuditEvent.objects.filter(action="case.issue", data__has_key="support"))

    def test_every_step_needs_a_reason(self):
        case = self.submit()
        with self.assertRaises(CommandFailed) as caught:
            self.by_designer(case, "start_triage", reason="")
        self.assertEqual(caught.exception.code, ErrorCode.VALIDATION_ERROR)

    def test_others_stay_bound_by_the_separation_of_duties(self):
        case = self.ready()
        self.act(self.approver, case, "approve", lines((1, "2", "ทดสอบ")))
        self.issuer_also_approver = self.approver
        RoleGrant.objects.create(user=self.approver, role=Role.ISSUER, division=case.owner_division)
        with self.assertRaises(CommandFailed) as caught:
            self.issue(self.approver, case, 2)
        self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)


class VisibilityTests(SupportTestCase):
    def test_page_form_report_and_supervisor_mail(self):
        lead = make_staff("mtrlead", (Role.SUPERVISOR, "SIM-MTR"))
        lead.email = "mtrlead@example.invalid"
        lead.save()
        case = self.ready()
        client = self.client_for(self.designer)
        page = client.get(f"/cases/{case.number}/")
        self.assertContains(page, "อนุมัติ")  # it can act where admins only read
        form = client.get(f"/cases/{case.number}/act/approve/")
        self.assertContains(form, "ผู้ช่วยเหลือระบบต้องบอกเหตุผลทุกครั้ง")
        with override_settings(STAFF_EMAIL_ENABLED=True):
            self.by_designer(case, "approve", lines((1, "3")), reason="ผู้อนุมัติลา")
            event = OutboxEvent.objects.filter(
                event_type=orchestration.EVENT_DECISION, case=case
            ).latest("id")
            planned = staff_email.plan_for_event(event)
        self.assertEqual([d.recipient_user for d in planned], [lead])
        self.assertEqual(planned[0].kind, "staff_support")
        self.assertIn("ผู้อนุมัติลา", NotificationDelivery.objects.get(pk=planned[0].pk).text)
        page = client.get(f"/cases/{case.number}/")
        self.assertContains(page, "ผู้ช่วยเหลือระบบทำแทน SIM-MTR")
        report = self.client_for(self.admin).get("/reports/")
        self.assertContains(report, "การทำแทนกองโดยผู้ช่วยเหลือระบบ")
        self.assertContains(report, "ผู้อนุมัติลา")
        # Staff of another division do not see SIM-MTR's entries.
        other = self.client_for(self.trf_staff).get("/reports/")
        self.assertNotContains(other, "ผู้อนุมัติลา")

    def test_withdrawal_filed_for_a_requester_is_labelled_too(self):
        result = services.submit_case(
            Actor.staff(self.designer),
            idempotency_key=key(),
            payload=withdrawal(requester_id=self.req1.pk, channel="phone", intake_note="เขตโทรมา"),
        )
        decision = Decision.objects.get(case_id=result.case_id, action="submit")
        self.assertEqual(decision.data["support"]["division"], "SIM-TRIAGE")
