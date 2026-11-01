"""Administrative chat commands never inherit permissions from a menu or notification."""

from operations import admin_line, employees, identity, staff_line
from operations.models import ChannelIdentity, NotificationDelivery, RegistrationRequest
from operations.tests.test_m03_line import ALICE, BOB, texts
from operations.tests.test_m13_staff_chat import StaffChatTestCase


class AdminLineTests(StaffChatTestCase):
    def pending(self):
        return identity.submit_registration("line", "new-applicant", "ผู้สมัคร ทดสอบ", "D01")

    def test_admin_can_opt_in_review_approve_and_reject_in_chat(self):
        self.link(self.admin, ALICE)
        self.assertIn("เปิดรับแจ้ง", texts(self.type(ALICE, "รับแจ้งสมัคร เปิด")))
        registration = self.pending()
        alert = NotificationDelivery.objects.get(kind="staff_registration")
        self.assertNotIn(registration.submitted_name, alert.text)
        self.assertEqual(staff_line.recipient_problem(alert), "")
        admin_line.notify(registration)
        self.assertEqual(NotificationDelivery.objects.filter(kind="staff_registration").count(), 1)
        reply = self.type(ALICE, f"ดูคำขอ {registration.pk}")
        self.assertIn(registration.submitted_name, texts(reply))
        self.assertIn("บันทึกผล", texts(self.type(ALICE, f"approve {registration.pk}")))
        self.type(ALICE, f"approve {registration.pk} ตรวจเอกสารแล้ว")
        registration.refresh_from_db()
        self.assertEqual(registration.status, RegistrationRequest.Status.APPROVED)
        self.assertEqual(registration.decided_by, self.admin)
        self.assertEqual(NotificationDelivery.objects.filter(kind="staff_registration").count(), 2)
        self.type(ALICE, f"approve {registration.pk} ซ้ำ")
        self.assertEqual(NotificationDelivery.objects.filter(kind="staff_registration").count(), 2)
        other = identity.submit_registration("line", "another-applicant", "คนที่ สอง", "D01")
        self.type(ALICE, f"reject {other.pk} เอกสารไม่ครบ")
        other.refresh_from_db()
        self.assertEqual(other.status, RegistrationRequest.Status.REJECTED)

    def test_non_admin_cannot_approve_or_subscribe(self):
        self.link(self.head, BOB)
        registration = self.pending()
        reply = self.type(BOB, f"approve {registration.pk} ขอสิทธิ์")
        self.assertIn("เฉพาะแอดมิน", texts(reply))
        self.assertIn("เฉพาะแอดมิน", texts(self.type(BOB, "รับแจ้งสมัคร เปิด")))
        registration.refresh_from_db()
        self.assertEqual(registration.status, RegistrationRequest.Status.PENDING)

    def test_opt_out_and_unlink_prevent_queued_delivery(self):
        self.link(self.admin, ALICE)
        admin_line.set_notifications(self.admin, self.admin, True)
        self.pending()
        alert = NotificationDelivery.objects.get(kind="staff_registration")
        admin_line.set_notifications(self.admin, self.admin, False)
        self.assertTrue(staff_line.recipient_problem(alert))
        admin_line.set_notifications(self.admin, self.admin, True)
        ChannelIdentity.objects.filter(pk=alert.identity_id).update(staff_user=None)
        alert.refresh_from_db()
        self.assertTrue(staff_line.recipient_problem(alert))

    def verified_employee(self):
        from operations.models import Employee

        employee = Employee.objects.get(employee_id="510002")
        employee.division = self.kae
        employee.save()
        registration = identity.submit_registration(
            "line",
            "verified-applicant",
            employees.label(employee),
            "D01",
            employee=employee,
            verified_by="otp",
        )
        return identity.auto_approve_registration(registration)

    def test_admin_grants_and_revokes_supervisor_in_line_after_verification(self):
        from operations.models import Role, RoleGrant

        self.link(self.admin, ALICE)
        requester = self.verified_employee()
        reply = self.type(ALICE, f"เพิ่มสิทธิ์ {requester.pk} supervisor กองอุปกรณ์ฯ")
        self.assertIn("เพิ่มสิทธิ์", texts(reply))
        user = requester.channelidentity_set.get().staff_user
        grant = RoleGrant.objects.get(user=user, role=Role.SUPERVISOR, revoked_at=None)
        self.assertIn(f"สิทธิ์ #{grant.pk}", texts(self.type(ALICE, f"ดูผู้ใช้ {requester.pk}")))
        self.type(ALICE, f"ถอนสิทธิ์ {grant.pk}")
        grant.refresh_from_db()
        self.assertIsNotNone(grant.revoked_at)
        external = identity.approve_registration(self.admin, self.pending())
        reply = self.type(ALICE, f"เพิ่มสิทธิ์ {external.pk} supervisor กองอุปกรณ์ฯ ทดสอบ")
        self.assertIn("ต้องยืนยันตัวตน", texts(reply))

    def test_web_admin_manages_notifications_and_line_employee_role(self):
        from django.test import Client

        from operations.models import Role, RoleGrant

        client = Client()
        client.force_login(self.admin)
        response = client.post(
            f"/console/admin/users/{self.admin.pk}/registration-notifications/",
            {"enabled": "1"},
        )
        self.assertEqual(response.status_code, 302)
        self.admin.staff_profile.refresh_from_db()
        self.assertTrue(self.admin.staff_profile.notify_registrations)
        requester = self.verified_employee()
        response = client.get(f"/console/admin/requesters/{requester.pk}/")
        self.assertContains(response, "เปลี่ยนบทบาท")
        response = client.post(
            f"/console/admin/requesters/{requester.pk}/grant-role/",
            {"role": Role.SUPERVISOR, "division": self.kae.pk, "reason": "แต่งตั้งหัวหน้า"},
        )
        self.assertEqual(response.status_code, 302)
        user = requester.channelidentity_set.get().staff_user
        self.assertTrue(
            RoleGrant.objects.filter(user=user, role=Role.SUPERVISOR, revoked_at=None).exists()
        )
        client.force_login(self.head)
        self.assertEqual(client.get(f"/console/admin/requesters/{requester.pk}/").status_code, 403)
