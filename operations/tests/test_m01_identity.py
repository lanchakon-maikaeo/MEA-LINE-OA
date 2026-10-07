import os
import secrets
import threading
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings

from operations import identity
from operations.models import (
    AuditEvent,
    ChannelIdentity,
    District,
    Division,
    RegistrationRequest,
    Role,
    RoleGrant,
    StaffProfile,
)

User = get_user_model()
LINE = ChannelIdentity.Channel.LINE


def make_staff(username, *roles):
    """Staff user with a usable password; roles are (role, division_code) pairs."""
    user = User.objects.create_user(username=username, password=secrets.token_urlsafe(16))
    StaffProfile.objects.create(user=user, display_name=username.title())
    for role, code in roles:
        RoleGrant.objects.create(
            user=user, role=role, division=Division.objects.get(code=code) if code else None
        )
    return user


class IdentityTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("load_districts", stdout=StringIO())
        call_command("seed_demo_units", stdout=StringIO())
        cls.root = identity.bootstrap_admin("root", "Root", secrets.token_urlsafe(16))
        cls.admin = make_staff("admin", (Role.ADMIN, None))
        cls.meter = make_staff("meter", (Role.STAFF, "SIM-MTR"))
        cls.transformer = make_staff("transformer", (Role.STAFF, "SIM-TRF"))
        cls.norole = make_staff("norole")
        cls.mtr = Division.objects.get(code="SIM-MTR")
        cls.trf = Division.objects.get(code="SIM-TRF")

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client


class ReferenceDataTests(IdentityTestCase):
    def test_eighteen_real_districts_load_idempotently_without_official_codes(self):
        call_command("load_districts", stdout=StringIO())
        self.assertEqual(District.objects.count(), 18)
        self.assertEqual(District.objects.filter(official_code__isnull=False).count(), 0)
        self.assertTrue(District.objects.filter(name="การไฟฟ้านครหลวงเขตวัดเลียบ").exists())
        call_command("seed_demo_units", stdout=StringIO())
        self.assertEqual(Division.objects.filter(is_synthetic=True).count(), 6)
        self.assertEqual(Division.objects.get(code="SIM-TRIAGE").kind, "coordination")

    def test_synthetic_units_are_refused_outside_demo_environment(self):
        with override_settings(APP_ENV="production"), self.assertRaises(CommandError):
            call_command("seed_demo_units", stdout=StringIO())


class StaffScopeTests(IdentityTestCase):
    def test_account_without_role_has_no_access(self):
        scope = identity.staff_scope(self.norole)
        self.assertFalse(scope.has_any_access)
        client = self.client_for(self.norole)
        self.assertContains(client.get("/console/"), "ยังไม่ได้รับกองหรือบทบาท")
        self.assertEqual(client.get("/divisions/SIM-MTR/").status_code, 404)
        self.assertEqual(client.get("/console/admin/users/").status_code, 403)

    def test_staff_see_only_their_division_even_by_direct_url(self):
        client = self.client_for(self.meter)
        self.assertContains(client.get("/divisions/SIM-MTR/"), "Meter")
        self.assertEqual(client.get("/divisions/SIM-TRF/").status_code, 404)
        self.assertEqual(client.get("/divisions/NOPE/").status_code, 404)
        scope = identity.staff_scope(self.meter)
        visible = identity.filter_by_division(Division.objects.all(), scope, "pk")
        self.assertEqual(list(visible), [self.mtr])

    def test_admin_sees_every_division_but_developer_rights_are_separate(self):
        scope = identity.staff_scope(self.admin)
        self.assertTrue(identity.can_access_division(scope, self.trf.pk))
        self.assertFalse(scope.is_developer_admin)
        self.assertTrue(identity.staff_scope(self.root).is_developer_admin)
        # A developer-admin grant without admin is not a path to technical settings.
        lone = make_staff("lone", (Role.DEVELOPER_ADMIN, None))
        self.assertFalse(identity.staff_scope(lone).is_developer_admin)

    def test_role_revocation_applies_to_an_open_session(self):
        client = self.client_for(self.meter)
        self.assertEqual(client.get("/divisions/SIM-MTR/").status_code, 200)
        grant = RoleGrant.objects.get(user=self.meter)
        identity.revoke_role(self.admin, grant, "ย้ายงาน")
        self.assertEqual(client.get("/divisions/SIM-MTR/").status_code, 404)

    def test_division_move_and_deactivation_apply_immediately(self):
        client = self.client_for(self.meter)
        identity.change_division(self.admin, self.meter, self.mtr, self.trf, "ย้ายกอง")
        self.assertEqual(client.get("/divisions/SIM-MTR/").status_code, 404)
        self.assertEqual(client.get("/divisions/SIM-TRF/").status_code, 200)
        identity.set_division_active(self.admin, self.trf, False, "ยุบกองทดสอบ")
        self.assertFalse(identity.staff_scope(self.meter).has_any_access)

    def test_suspension_and_session_revocation_end_open_sessions(self):
        client = self.client_for(self.meter)
        identity.revoke_sessions(self.admin, self.meter, "อุปกรณ์หาย")
        self.assertRedirects(
            client.get("/console/"),
            "/accounts/login/?next=/console/",
            fetch_redirect_response=False,
        )
        client = self.client_for(self.meter)
        self.assertEqual(client.get("/console/").status_code, 200)
        identity.set_staff_active(self.admin, self.meter, False, "ลาออก")
        self.assertEqual(client.get("/console/").status_code, 302)
        self.assertFalse(identity.staff_scope(self.meter).has_any_access)
        with self.assertRaises(ValidationError):
            identity.grant_role(self.admin, self.meter, Role.STAFF, self.trf)


class AdminGuardTests(IdentityTestCase):
    def test_account_management_cannot_bypass_developer_role(self):
        original = self.root.password
        actions = (
            lambda: identity.reset_password(self.admin, self.root, "reset"),
            lambda: identity.set_staff_active(self.admin, self.root, False, "suspend"),
            lambda: identity.revoke_sessions(self.admin, self.root, "revoke"),
            lambda: identity.revoke_role(
                self.admin, RoleGrant.objects.get(user=self.root, role=Role.ADMIN), "revoke"
            ),
        )
        for action in actions:
            with self.subTest(action=action), self.assertRaises(PermissionDenied):
                action()
        self.root.refresh_from_db()
        self.assertEqual(self.root.password, original)
        self.assertFalse(AuditEvent.objects.filter(action="staff.password_reset").exists())
        dormant = make_staff("dormant", (Role.DEVELOPER_ADMIN, None))
        with self.assertRaises(PermissionDenied):
            identity.reset_password(self.admin, dormant, "reset")
        with self.assertRaises(PermissionDenied):
            identity.grant_role(self.admin, dormant, Role.ADMIN)
        password = identity.reset_password(self.root, self.root, "own account")
        self.root.refresh_from_db()
        self.assertTrue(self.root.check_password(password))

    def test_only_admins_change_accounts(self):
        for actor in (self.meter, self.norole):
            with self.subTest(actor=actor.username), self.assertRaises(PermissionDenied):
                identity.grant_role(actor, actor, Role.ADMIN)
        client = self.client_for(self.meter)
        response = client.post(
            f"/console/admin/users/{self.meter.pk}/grant/", {"role": Role.ADMIN, "reason": "x"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(identity.staff_scope(self.meter).is_admin)

    def test_developer_admin_is_granted_only_by_developer_admins(self):
        with self.assertRaises(PermissionDenied):
            identity.grant_role(self.admin, self.admin, Role.DEVELOPER_ADMIN)
        identity.grant_role(self.root, self.admin, Role.DEVELOPER_ADMIN, reason="ดูแล AI")
        self.assertTrue(identity.staff_scope(self.admin).is_developer_admin)

    def test_scope_mismatched_or_duplicate_grants_are_rejected(self):
        with self.assertRaises(ValidationError):
            identity.grant_role(self.admin, self.norole, Role.STAFF)
        with self.assertRaises(ValidationError):
            identity.grant_role(self.admin, self.norole, Role.ADMIN, self.mtr)
        with self.assertRaises(ValidationError):
            identity.grant_role(self.admin, self.meter, Role.STAFF, self.mtr)
        with self.assertRaises(IntegrityError), transaction.atomic():
            RoleGrant.objects.create(user=self.norole, role=Role.STAFF)

    def test_last_admin_and_last_developer_admin_cannot_be_removed(self):
        root_admin = RoleGrant.objects.get(user=self.root, role=Role.ADMIN)
        root_dev = RoleGrant.objects.get(user=self.root, role=Role.DEVELOPER_ADMIN)
        with self.assertRaises(ValidationError):
            identity.revoke_role(self.root, root_dev, "ไม่ใช้แล้ว")
        with self.assertRaises(PermissionDenied):
            identity.revoke_role(self.admin, root_admin, "ไม่ใช้แล้ว")
        with self.assertRaises(PermissionDenied):
            identity.set_staff_active(self.admin, self.root, False, "ไม่ใช้แล้ว")
        admin_grant = RoleGrant.objects.get(user=self.admin, role=Role.ADMIN)
        identity.revoke_role(self.root, admin_grant, "เหลือผู้ดูแลคนเดียว")
        with self.assertRaises(ValidationError):
            identity.set_staff_active(self.root, self.root, False, "ไม่ใช้แล้ว")

    def test_reason_is_required_and_audit_records_actor_without_secrets(self):
        with self.assertRaises(ValidationError):
            identity.revoke_sessions(self.admin, self.meter, "  ")
        password = identity.reset_password(self.admin, self.meter, "ลืมรหัส")
        event = AuditEvent.objects.get(action="staff.password_reset")
        self.assertEqual((event.actor, event.reason), (self.admin, "ลืมรหัส"))
        self.assertNotIn(password, str(event.data))
        with self.assertRaises(ValueError):
            event.save()
        with self.assertRaises(ValueError):
            event.delete()

    def test_admin_console_flow_creates_account_without_role(self):
        client = self.client_for(self.admin)
        response = client.post(
            "/console/admin/users/new/", {"username": "newbie", "display_name": "ผู้ใช้ใหม่"}
        )
        self.assertContains(response, "แสดงครั้งเดียว")
        newbie = User.objects.get(username="newbie")
        self.assertFalse(identity.staff_scope(newbie).has_any_access)
        response = client.post(
            f"/console/admin/users/{newbie.pk}/grant/",
            {"role": Role.STAFF, "division": self.trf.pk, "reason": "รับเข้ากอง"},
        )
        self.assertRedirects(response, f"/console/admin/users/{newbie.pk}/")
        self.assertIn(self.trf.pk, identity.staff_scope(newbie).division_roles)
        self.assertEqual(
            client.post(f"/console/admin/users/{newbie.pk}/revoke/", {"grant_id": "x"}).status_code,
            404,
        )

    def test_admin_pages_render_and_console_shows_thai_role_names(self):
        client = self.client_for(self.admin)
        for url in (
            "/console/",
            "/console/admin/users/",
            "/console/admin/users/new/",
            f"/console/admin/users/{self.meter.pk}/",
            "/console/admin/registrations/",
            "/console/admin/divisions/",
            "/divisions/SIM-TRF/",
        ):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 200)
        self.assertContains(self.client_for(self.meter).get("/console/"), "เจ้าหน้าที่กอง")

    def test_admin_actions_require_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        response = client.post(f"/console/admin/users/{self.meter.pk}/suspend/", {"reason": "x"})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(User.objects.get(pk=self.meter.pk).is_active)


class PasswordTests(IdentityTestCase):
    def test_temporary_password_forces_change_and_ends_old_sessions(self):
        old_client = self.client_for(self.meter)
        password = identity.reset_password(self.admin, self.meter, "ลืมรหัส")
        self.assertEqual(old_client.get("/console/").status_code, 302)
        client = Client()
        self.assertTrue(client.login(username="meter", password=password))
        self.assertRedirects(
            client.get("/console/"), "/accounts/password/change/", fetch_redirect_response=False
        )
        new_password = secrets.token_urlsafe(16)
        response = client.post(
            "/accounts/password/change/",
            {
                "old_password": password,
                "new_password1": new_password,
                "new_password2": new_password,
            },
        )
        self.assertRedirects(response, "/console/")
        self.assertEqual(client.get("/console/").status_code, 200)

    def test_login_page_and_logout(self):
        client = Client()
        password = identity.reset_password(self.admin, self.norole, "ตั้งรหัส")
        wrong = secrets.token_urlsafe(16)
        response = client.post("/accounts/login/", {"username": "norole", "password": wrong})
        self.assertContains(response, "ไม่ถูกต้อง")
        response = client.post("/accounts/login/", {"username": "norole", "password": password})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(client.post("/accounts/logout/").status_code, 302)
        self.assertEqual(client.get("/console/").status_code, 302)


class BootstrapTests(IdentityTestCase):
    def test_bootstrap_refuses_when_an_admin_exists(self):
        with self.assertRaises(PermissionDenied):
            identity.bootstrap_admin("second", "Second", secrets.token_urlsafe(16))
        with patch.dict(os.environ, {"BOOT_PW": secrets.token_urlsafe(16)}):
            with self.assertRaises(CommandError):
                call_command(
                    "bootstrap_admin",
                    username="second",
                    display_name="Second",
                    password_env="BOOT_PW",  # pragma: allowlist secret
                    stdout=StringIO(),
                )
        self.assertFalse(User.objects.filter(username="second").exists())


class RegistrationTests(IdentityTestCase):
    def register(self, line_id, name="สมชาย", district="D01"):
        return identity.submit_registration(LINE, line_id, name, district)

    def test_registration_waits_for_admin_and_district_choice_grants_nothing(self):
        registration = self.register("U-a")
        self.assertEqual(registration.status, "pending")
        self.assertIsNone(identity.resolve_requester(LINE, "U-a"))
        self.assertEqual(self.register("U-a").pk, registration.pk)
        with self.assertRaises(ValidationError):
            self.register("U-b", district="D99")
        requester = identity.approve_registration(self.admin, registration)
        self.assertEqual(identity.resolve_requester(LINE, "U-a"), requester)
        self.assertEqual(requester.district.code, "D01")
        with self.assertRaises(ValidationError):
            self.register("U-a")

    def test_requesters_in_the_same_district_see_only_their_own(self):
        first = identity.approve_registration(self.admin, self.register("U-a", "ก"))
        second = identity.approve_registration(self.admin, self.register("U-b", "ข"))
        self.assertEqual(first.district, second.district)
        own = identity.filter_by_requester(
            RegistrationRequest.objects.all(), first, "identity__requester"
        )
        self.assertEqual([r.submitted_name for r in own], ["ก"])
        self.assertEqual(identity.registration_status(LINE, "U-b").submitted_name, "ข")
        self.assertIsNone(identity.registration_status(LINE, "U-unknown"))
        empty = identity.filter_by_requester(RegistrationRequest.objects.all(), None)
        self.assertFalse(empty.exists())

    def test_reject_needs_reason_and_only_admins_decide(self):
        registration = self.register("U-a")
        with self.assertRaises(PermissionDenied):
            identity.approve_registration(self.meter, registration)
        client = self.client_for(self.meter)
        response = client.post(f"/console/admin/registrations/{registration.pk}/approve/")
        self.assertEqual(response.status_code, 403)
        with self.assertRaises(ValidationError):
            identity.reject_registration(self.admin, registration, "")
        identity.reject_registration(self.admin, registration, "ไม่ใช่พนักงานเขต")
        with self.assertRaises(ValidationError):
            identity.approve_registration(self.admin, registration)
        self.assertIsNone(identity.resolve_requester(LINE, "U-a"))

    def test_revoked_identity_or_suspended_requester_loses_access_and_relink_needs_approval(self):
        requester = identity.approve_registration(self.admin, self.register("U-a"))
        identity.set_requester_active(self.admin, requester, False, "ย้ายหน่วยงาน")
        self.assertIsNone(identity.resolve_requester(LINE, "U-a"))
        identity.set_requester_active(self.admin, requester, True, "กลับมา")
        identity_row = ChannelIdentity.objects.get(external_id="U-a")
        identity.revoke_identity(self.admin, identity_row, "บัญชี LINE ถูกขโมย")
        self.assertIsNone(identity.resolve_requester(LINE, "U-a"))
        again = self.register("U-a", district="D02")
        self.assertEqual(again.status, "pending")
        self.assertIsNone(identity.resolve_requester(LINE, "U-a"))
        new_requester = identity.approve_registration(self.admin, again)
        self.assertNotEqual(new_requester.pk, requester.pk)
        self.assertEqual(new_requester.district.code, "D02")

    def test_admin_console_lists_and_approves_registrations(self):
        registration = self.register("U-a", "สมหญิง")
        client = self.client_for(self.admin)
        self.assertContains(client.get("/console/admin/registrations/"), "สมหญิง")
        response = client.post(f"/console/admin/registrations/{registration.pk}/approve/")
        self.assertRedirects(response, "/console/admin/registrations/")
        self.assertIsNotNone(identity.resolve_requester(LINE, "U-a"))


class ConcurrentAdminRemovalTests(TransactionTestCase):
    def setUp(self):
        self.first = identity.bootstrap_admin(
            "first", "First", secrets.token_urlsafe(16), developer=False
        )
        self.second = make_staff("second", (Role.ADMIN, None))

    def test_last_admin_is_protected_without_any_developer_admin(self):
        identity.set_staff_active(self.first, self.second, False, "ลาออก")
        own = RoleGrant.objects.get(user=self.first, role=Role.ADMIN)
        with self.assertRaises(ValidationError):
            identity.revoke_role(self.first, own, "ถอนตัวเอง")
        self.assertIsNone(RoleGrant.objects.get(pk=own.pk).revoked_at)

    def test_two_admins_removing_each_other_leave_one_admin(self):
        barrier = threading.Barrier(2)
        errors = []

        def remove(actor, target):
            try:
                barrier.wait()
                grant = RoleGrant.objects.get(user=target, role=Role.ADMIN)
                identity.revoke_role(actor, grant, "แข่งกัน")
            except (ValidationError, PermissionDenied) as exc:
                errors.append(exc)
            finally:
                connection.close()

        threads = [
            threading.Thread(target=remove, args=(self.first, self.second)),
            threading.Thread(target=remove, args=(self.second, self.first)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        active = RoleGrant.objects.filter(role=Role.ADMIN, revoked_at__isnull=True).count()
        self.assertEqual(active, 1)
        self.assertEqual(len(errors), 1)
