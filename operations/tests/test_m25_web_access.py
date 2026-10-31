"""สิทธิ์บนเว็บ 31 ต.ค. 2569 (ADR 0039): ตั้งสิทธิ์ให้พนักงานที่บัญชีเจ้าหน้าที่เดิมถูกระงับ, ทุกกองและ
ปัญหาและข้อเสนอแนะของ Supervisor ระดับฝ่าย, และผู้ดูข้อมูลของกองและระดับฝ่ายที่ดูได้อย่างเดียว"""

import re

from django.test import Client

from operations import forms, identity, services, staff_email
from operations.contracts import Actor, CommandFailed
from operations.models import AuthorityGrant, Division, Role, RoleGrant
from operations.tests import test_m13_admin_line
from operations.tests import test_m19_problem_tile as tile_tests
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m13_staff_chat import StaffChatTestCase


class SuspendedStaffAccountTests(StaffChatTestCase):
    verified_employee = test_m13_admin_line.AdminLineTests.verified_employee

    def test_grant_role_for_an_employee_whose_old_staff_account_was_suspended(self):
        # Production 31 Oct: employee 2635435 linked LINE on 17 Oct, the admin suspended that
        # account on 19 Oct, the LINE data was wiped on 29 Oct and the person signed up again.
        requester = self.verified_employee()
        old = identity.grant_requester_role(self.admin, requester, Role.STAFF, self.kae, "ทดลอง")
        identity.grant_role(self.admin, old, Role.DEPARTMENT_SUPERVISOR, None, "ทดลอง")
        identity.set_staff_active(self.admin, old, False, "ทดลองระงับ")
        client = Client()
        client.force_login(self.admin)
        response = client.post(
            f"/console/admin/requesters/{requester.pk}/grant-role/",
            {"role": Role.SUPERVISOR, "division": self.kae.pk, "reason": "แต่งตั้งหัวหน้า"},
            follow=True,
        )
        self.assertNotContains(response, "บัญชีเจ้าหน้าที่ถูกระงับ ให้แอดมินตรวจ")
        # The person is not suspended today, so the old account opens again with only the
        # roles given now (user, 31 Oct 2569 Q9); the old roles stay in its history.
        old.refresh_from_db()
        self.assertTrue(old.is_active)
        self.assertEqual(
            set(RoleGrant.objects.filter(user=old, revoked_at=None).values_list("role", flat=True)),
            {Role.STAFF, Role.SUPERVISOR},
        )
        self.assertTrue(
            RoleGrant.objects.filter(user=old, role=Role.DEPARTMENT_SUPERVISOR)
            .exclude(revoked_at=None)
            .exists()
        )
        self.assertEqual(requester.channelidentity_set.get().staff_user, old)


def posts(response):
    """The forms a page posts, apart from signing out."""
    forms = re.findall(r"<form\b[^>]*>", response.content.decode())
    return [f for f in forms if 'method="post"' in f and "/accounts/logout/" not in f]


class DepartmentAccessTests(tile_tests.PickTestCase):
    """ADR 0039: ทุกกอง and ปัญหาและข้อเสนอแนะ for ฝ่าย, and the two viewer roles."""

    def setUp(self):
        super().setUp()
        self.kcw_case, _ = self.file("กองเครื่องวัดหน่วยไฟฟ้า", "มิเตอร์ กคว. ดับ")
        self.koj_case, _ = self.file("กองอุปกรณ์การจำหน่าย", "หม้อแปลง กอจ. ร้อน")
        self.held, _ = self.file("ปัญหาการใช้งานหรืออื่นๆ", "เข้าเว็บไม่ได้")
        self.assertTrue(self.held.held_by_admin)
        self.director = make_staff("fajo-director", (Role.DEPARTMENT_SUPERVISOR, None))
        self.watcher = make_staff("fajo-watcher", (Role.DEPARTMENT_VIEWER, None))
        self.kcw_viewer = make_staff("kcw-viewer", (Role.DIVISION_VIEWER, "กคว."))

    def web(self, user):
        client = Client()
        client.force_login(user)
        return client

    def test_department_supervisor_sees_every_division_and_sends_the_admins_cases(self):
        web = self.web(self.director)
        queue = web.get("/queue/")
        self.assertContains(queue, self.kcw_case.number)
        self.assertContains(queue, self.koj_case.number)
        page = web.get("/console/admin/problems/")
        self.assertContains(page, self.held.number)
        self.assertContains(page, 'id="bulk-send"')
        held_page = web.get(f"/cases/{self.held.number}/")
        self.assertContains(held_page, "ส่งให้กอง")
        self.assertTrue(posts(held_page))  # the check below sees forms when there are some
        sent = web.post(
            "/console/admin/problems/send/",
            {"confirm": "1", "case": [self.held.number], "to_division": "กคว.", "reason": "มิเตอร์"},
            follow=True,
        )
        self.assertContains(sent, "ส่ง 1 เรื่องให้ กคว. แล้ว")
        self.held.refresh_from_db()
        self.assertEqual((self.held.held_by_admin, self.held.owner_division.code), (False, "กคว."))
        self.assertContains(web.get("/console/admin/problems.csv"), "เข้าเว็บไม่ได้")

    def test_department_viewer_reads_everything_and_changes_nothing(self):
        web = self.web(self.watcher)
        queue = web.get("/queue/")
        self.assertContains(queue, self.kcw_case.number)
        self.assertContains(queue, self.koj_case.number)
        self.assertContains(queue, "บัญชีนี้ดูได้อย่างเดียว")
        self.assertEqual(posts(queue), [])
        page = web.get("/console/admin/problems/")
        self.assertContains(page, self.held.number)
        self.assertEqual(posts(page), [])
        for case in (self.held, self.kcw_case):
            detail = web.get(f"/cases/{case.number}/")
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(posts(detail), [])
        self.assertContains(web.get("/console/admin/problems.csv"), "เข้าเว็บไม่ได้")
        self.assertEqual(web.get("/reports/").status_code, 200)
        refused = web.post(
            "/console/admin/problems/send/",
            {"confirm": "1", "case": [self.held.number], "to_division": "กคว.", "reason": "ลอง"},
        )
        self.assertEqual(refused.status_code, 403)
        with self.assertRaises(CommandFailed):
            services.perform_action(
                Actor.staff(self.watcher),
                self.kcw_case.pk,
                "take",
                expected_revision=self.kcw_case.revision,
                idempotency_key="viewer-take",
            )
        self.held.refresh_from_db()
        self.assertTrue(self.held.held_by_admin)

    def test_division_viewer_reads_only_its_division(self):
        web = self.web(self.kcw_viewer)
        own = web.get("/divisions/กคว./")
        self.assertContains(own, self.kcw_case.number)
        self.assertContains(own, "บัญชีนี้ดูได้อย่างเดียว")
        self.assertEqual(web.get("/divisions/กอจ./").status_code, 404)
        self.assertEqual(web.get(f"/cases/{self.koj_case.number}/").status_code, 404)
        self.assertEqual(web.get(f"/cases/{self.held.number}/").status_code, 404)
        self.assertEqual(web.get("/queue/").status_code, 403)
        self.assertEqual(web.get("/console/admin/problems/").status_code, 403)
        pages = (
            "/divisions/กคว./",
            f"/cases/{self.kcw_case.number}/",
            "/divisions/กคว./warehouse/",
            "/divisions/กคว./warehouse/schedule/",
            "/divisions/กคว./calendar/",
            "/orders/",
            "/reports/",
            "/console/",
        )
        for url in pages:
            response = web.get(url, follow=True)
            self.assertEqual((url, response.status_code), (url, 200))
            self.assertEqual((url, posts(response)), (url, []))
        refused = web.post("/divisions/กคว./warehouse/", {"numbers": self.kcw_case.number})
        self.assertEqual(refused.status_code, 404)
        scope = identity.staff_scope(self.kcw_viewer)
        self.assertFalse(scope.has_any_access)  # so no LINE staff menu and no alerts
        kcw = Division.objects.get(code="กคว.")
        self.assertNotIn(self.kcw_viewer, staff_email.members(kcw.pk))
        self.assertEqual(identity.sync_membership_authority(self.kcw_viewer), 0)
        self.assertFalse(AuthorityGrant.objects.filter(user=self.kcw_viewer).exists())

    def test_a_viewer_who_is_a_member_acts_in_their_own_division_only(self):
        identity.grant_role(
            self.boss, self.watcher, Role.STAFF, Division.objects.get(code="กอจ."), "ทดสอบ"
        )
        scope = identity.staff_scope(self.watcher)
        koj, kcw = (Division.objects.get(code=c).pk for c in ("กอจ.", "กคว."))
        self.assertTrue(identity.holds(scope, koj))
        self.assertFalse(identity.holds(scope, kcw))
        self.assertIn(kcw, scope.viewed)
        self.assertNotIn(koj, scope.viewed)

    def test_two_supervisors_in_one_division_and_two_for_the_department(self):
        kcw = Division.objects.get(code="กคว.")
        first, second = make_staff("kcw-head-1"), make_staff("kcw-head-2")
        for user in (first, second):
            identity.grant_role(self.boss, user, Role.SUPERVISOR, kcw, "หัวหน้า")
        identity.grant_role(self.boss, first, Role.DEPARTMENT_SUPERVISOR, None, "ผอ.")
        self.assertEqual(
            RoleGrant.objects.filter(role=Role.SUPERVISOR, division=kcw, revoked_at=None).count(), 2
        )
        self.assertEqual(
            RoleGrant.objects.filter(role=Role.DEPARTMENT_SUPERVISOR, revoked_at=None).count(), 2
        )

    def test_admins_give_the_viewer_roles_on_the_staff_page(self):
        choices = dict(forms.GrantRoleForm().fields["role"].choices)
        self.assertEqual(choices[Role.DIVISION_VIEWER], "ผู้ดูข้อมูลของกอง")
        self.assertEqual(choices[Role.DEPARTMENT_VIEWER], "ผู้ดูข้อมูลระดับฝ่าย")
