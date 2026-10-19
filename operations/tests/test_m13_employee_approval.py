"""Employee approval does not depend on membership of an internal division."""

from django.core.exceptions import ValidationError

from operations import identity, line_menu, services
from operations.contracts import Actor
from operations.models import (
    AuditEvent,
    ChannelIdentity,
    Division,
    Employee,
    RegistrationRequest,
    Role,
    RoleGrant,
)
from operations.tests.test_m02_cases import return_payload, withdrawal
from operations.tests.test_m13_line_people import PeopleTestCase


class EmployeeApprovalTests(PeopleTestCase):
    def pending(self, external_id="verified-no-division", verified_by="otp"):
        employee = Employee.objects.get(employee_id="SYN10001")
        self.assertIsNone(employee.division_id)
        return identity.submit_registration(
            "line",
            external_id,
            employee.name,
            "D01",
            employee=employee,
            verified_by=verified_by,
        )

    def test_worker_retries_verified_pending_and_employee_can_withdraw_and_return(self):
        registration = self.pending()
        self.assertEqual(identity.approve_verified_registrations(), 1)
        registration.refresh_from_db()
        requester = registration.identity.requester
        self.assertEqual(registration.status, RegistrationRequest.Status.APPROVED)
        self.assertFalse(requester.is_external)
        self.assertIsNone(registration.identity.staff_user_id)
        self.assertEqual(line_menu.page_for_identity(registration.identity), "employee-report")
        for number, payload in enumerate([withdrawal(), return_payload()]):
            services.submit_case(
                Actor.for_requester(requester),
                payload=payload,
                idempotency_key=f"employee-no-division-{number}",
            )
        self.assertEqual(requester.cases.count(), 2)
        self.assertEqual(identity.approve_verified_registrations(), 0)
        self.assertEqual(AuditEvent.objects.filter(action="registration.auto_approved").count(), 1)

    def test_worker_does_not_approve_name_only_or_revoked_identity(self):
        self.pending("name-only", verified_by="")
        revoked = self.pending("revoked-employee")
        ChannelIdentity.objects.filter(pk=revoked.identity_id).update(status="revoked")
        self.assertEqual(identity.approve_verified_registrations(), 0)
        self.assertEqual(RegistrationRequest.objects.filter(status="pending").count(), 2)

    def test_manual_confirmation_finishes_pending_registration_without_reason(self):
        registration = self.pending(verified_by="")
        requester = identity.external_requester(registration.identity, registration.submitted_name)
        result = identity.link_requester_employee(
            self.admin, requester, registration.employee.employee_id, ""
        )
        result.refresh_from_db()
        registration.refresh_from_db()
        self.assertEqual(registration.status, RegistrationRequest.Status.APPROVED)
        self.assertEqual(registration.identity.requester_id, requester.pk)
        self.assertFalse(result.is_external)
        self.assertEqual(identity.approve_registration(self.admin, registration).pk, requester.pk)

    def test_inactive_employee_is_not_approved(self):
        registration = self.pending()
        Employee.objects.filter(pk=registration.employee_id).update(is_active=False)
        with self.assertRaises(ValidationError):
            identity.auto_approve_registration(registration)

    def test_admin_can_assign_division_and_supervisor_later_without_roster_division(self):
        registration = self.pending()
        requester = identity.auto_approve_registration(registration)
        division = Division.objects.filter(is_active=True).first()
        user = identity.grant_requester_role(self.admin, requester, Role.SUPERVISOR, division)
        registration.identity.refresh_from_db()
        self.assertEqual(registration.identity.staff_user_id, user.pk)
        self.assertEqual(line_menu.page_for_identity(registration.identity), "sup-report")
        self.assertTrue(
            RoleGrant.objects.filter(
                user=user, role=Role.SUPERVISOR, division=division, revoked_at__isnull=True
            ).exists()
        )
        requester.refresh_from_db()
        self.assertFalse(requester.is_external)

    def test_worker_repairs_verified_legacy_external_flag(self):
        registration = self.pending()
        requester = identity.auto_approve_registration(registration)
        requester.is_external = True
        requester.save(update_fields=["is_external"])
        identity.approve_verified_registrations()
        requester.refresh_from_db()
        self.assertFalse(requester.is_external)
        self.assertEqual(
            AuditEvent.objects.filter(action="requester.employee_access_restored").count(), 1
        )
        identity.approve_verified_registrations()
        self.assertEqual(
            AuditEvent.objects.filter(action="requester.employee_access_restored").count(), 1
        )

    def test_confirming_an_external_reporter_uses_roster_unit_and_allows_withdrawal(self):
        employee = Employee.objects.get(employee_id="SYN10001")
        line = ChannelIdentity.objects.create(channel="line", external_id="external-to-employee")
        requester = identity.external_requester(line, employee.name)
        requester = identity.link_requester_employee(
            self.admin, requester, employee.employee_id, ""
        )
        self.assertEqual(requester.district, employee.district)
        self.assertFalse(requester.is_external)
        services.submit_case(
            Actor.for_requester(requester),
            payload=withdrawal(),
            idempotency_key="confirmed-external-withdrawal",
        )
        self.assertEqual(requester.cases.count(), 1)
