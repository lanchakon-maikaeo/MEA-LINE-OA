"""สมาชิกกองกับ Supervisor (16 ต.ค. 2569 Q3–Q5): with ตัวเลือก "หน้าที่ในกอง" = member every
member does every step and holds the division's authority; the old duties merge into สมาชิกกอง;
a แอดมินกอง assigns work in that division and a แอดมินฝ่าย in every division.
"""

import importlib

from django.apps import apps
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from operations import identity, options, policy, queues, web_actions
from operations.models import AuthorityGrant, Case, Division, Role, RoleGrant
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m07_fulfilment import lines
from operations.tests.test_m13_org import OrgTestCase, withdrawal

A = AuthorityGrant.Activity
S = Case.Status
merge_duties = importlib.import_module("operations.migrations.0042_merge_duties").merge_duties


class MemberTestCase(OrgTestCase):
    def setUp(self):
        super().setUp()
        self.kcw_division = Division.objects.get(code="กคว.")
        self.ktf_division = Division.objects.get(code="กตฟ.")

    def triaged(self):
        _, case = self.submit(withdrawal(("MTR_SYN_A", "1")))
        self.act(self.kcw, case, "start_triage")
        self.act(self.kcw, case, "set_priority", {"priority": "P3"})
        self.act(self.kcw, case, "complete_triage")
        return Case.objects.get(pk=case.pk)

    def member(self, username, division, role=Role.STAFF):
        user = make_staff(username)
        identity.grant_role(self.admin, user, role, division, "สมาชิกใหม่")
        return user


class MemberTests(MemberTestCase):
    def test_options_start_with_members_doing_every_step_and_no_separation(self):
        self.assertEqual(options.get(options.DIVISION_WORK), "member")
        self.assertEqual(options.get(options.SEPARATION), "off")

    def test_a_member_does_every_step_and_holds_the_division_authority(self):
        member = self.member("member", self.kcw_division)
        scope = identity.staff_scope(member)
        for role in (Role.APPROVER, Role.ISSUER, Role.RECEIVER, Role.INSPECTOR, Role.RESOLVER):
            self.assertTrue(identity.holds(scope, self.kcw_division.pk, [role]))
        self.assertFalse(identity.holds(scope, self.kcw_division.pk, [Role.SUPERVISOR]))
        for activity in identity.DIVISION_AUTHORITY:
            self.assertTrue(policy.has_authority(member, activity, self.kcw_division.pk))
        self.assertFalse(policy.has_authority(member, A.WITHDRAWAL_APPROVAL, self.ktf_division.pk))
        # Leaving the division ends the authority; the rows stay for the record.
        grant = RoleGrant.objects.get(user=member, revoked_at__isnull=True)
        identity.revoke_role(self.admin, grant, "ย้ายงาน")
        self.assertFalse(policy.has_authority(member, A.WITHDRAWAL_APPROVAL, self.kcw_division.pk))
        kept = AuthorityGrant.objects.filter(user=member, revoked_at__isnull=True)
        self.assertTrue(kept.exists())

    def test_one_member_takes_a_withdrawal_from_triage_to_approval(self):
        member = self.member("member", self.kcw_division)
        _, case = self.submit(withdrawal(("MTR_SYN_A", "2")))
        self.assertEqual(case.owner_division, self.kcw_division)
        self.act(member, case, "start_triage")
        self.act(member, case, "set_priority", {"priority": "P3"})
        self.act(member, case, "complete_triage")
        self.act(member, case, "approve", lines((1, "2", "ครบ")))
        self.assertEqual(Case.objects.get(pk=case.pk).status, S.AWAITING_EXECUTION)

    def test_coordinator_members_decide_routes_for_every_division(self):
        member = self.member("faoj", Division.objects.get(code="ฝอจ."))
        self.assertEqual(policy.routing_scope(member), "all")

    def test_the_old_duties_are_offered_only_when_the_work_is_split(self):
        user = make_staff("dutyless")
        with self.assertRaises(ValidationError):
            identity.grant_role(self.admin, user, Role.APPROVER, self.kcw_division, "ผู้อนุมัติ")
        client = Client()
        client.force_login(self.admin)
        page = client.get(reverse("admin-user-detail", args=[user.pk]))
        self.assertNotContains(page, '<option value="approver"')
        self.assertContains(page, '<option value="department_supervisor"')
        options.change(self.admin, options.DIVISION_WORK, "roles", "แยกหน้าที่อีกครั้ง")
        identity.grant_role(self.admin, user, Role.APPROVER, self.kcw_division, "ผู้อนุมัติ")
        page = client.get(reverse("admin-user-detail", args=[user.pk]))
        self.assertContains(page, '<option value="approver"')

    def test_splitting_the_work_again_turns_membership_authority_off(self):
        member = self.member("member", self.kcw_division)
        options.change(self.admin, options.DIVISION_WORK, "roles", "แยกหน้าที่อีกครั้ง")
        scope = identity.staff_scope(member)
        self.assertFalse(identity.holds(scope, self.kcw_division.pk, [Role.APPROVER]))
        self.assertFalse(policy.has_authority(member, A.WITHDRAWAL_APPROVAL, self.kcw_division.pk))
        options.change(self.admin, options.DIVISION_WORK, "member", "กลับมาเป็นสมาชิก")
        self.assertTrue(policy.has_authority(member, A.WITHDRAWAL_APPROVAL, self.kcw_division.pk))

    def test_the_division_page_lists_each_person_once(self):
        self.member("head", self.kcw_division, Role.SUPERVISOR)
        client = Client()
        client.force_login(self.kcw)
        page = client.get(reverse("division-detail", args=["กคว."]))
        members = page.context["members"]
        self.assertEqual(
            sorted((m["name"], m["label"]) for m in members),
            [("Head", "แอดมินกอง"), ("Kcw", "สมาชิกกอง"), ("Kcw2", "สมาชิกกอง")],
        )


class SupervisorTests(MemberTestCase):
    def test_a_division_supervisor_assigns_only_in_their_division(self):
        head = self.member("head", self.kcw_division, Role.SUPERVISOR)
        case = self.triaged()
        self.act(head, case, "assign", {"assignee_id": self.kcw2.pk, "reason": "รับไป"})
        self.assertEqual(Case.objects.get(pk=case.pk).assignee, self.kcw2)
        scope = identity.staff_scope(head)
        self.assertFalse(identity.holds(scope, self.ktf_division.pk, [Role.SUPERVISOR]))

    def test_the_department_supervisor_oversees_every_division(self):
        director = make_staff("director")
        identity.grant_role(self.admin, director, Role.DEPARTMENT_SUPERVISOR, None, "ผอ. ฝอจ.")
        scope = identity.staff_scope(director)
        self.assertTrue(scope.is_department_supervisor)
        self.assertTrue(identity.holds(scope, self.ktf_division.pk, [Role.SUPERVISOR]))
        self.assertFalse(identity.holds(scope, self.ktf_division.pk))  # not a member
        self.assertIn(self.kcw_division, queues.own_divisions(scope))
        case = self.triaged()
        state = web_actions.situation(director, case)
        self.assertIn("assign", {o.name for o in web_actions.available(director, case)})
        self.assertFalse(state.member(case.owner_division_id))
        self.act(director, case, "assign", {"assignee_id": self.kcw2.pk, "reason": "ฝากดู"})
        self.assertEqual(Case.objects.get(pk=case.pk).assignee, self.kcw2)
        self.act(director, case, "assign", {"assignee_id": self.kcw.pk, "reason": "คืนคนเดิม"})
        with self.assertRaises(ValidationError):
            identity.grant_role(
                self.admin, make_staff("x"), Role.DEPARTMENT_SUPERVISOR, self.kcw_division, "ผิด"
            )


class MergeTests(MemberTestCase):
    def test_the_old_duties_merge_into_one_membership(self):
        old = make_staff("old", (Role.APPROVER, "กคว."), (Role.ISSUER, "กคว."))
        merge_duties(apps, None)
        active = RoleGrant.objects.filter(user=old, revoked_at__isnull=True)
        self.assertEqual(list(active.values_list("role", flat=True)), [Role.STAFF])
        self.assertEqual(RoleGrant.objects.filter(user=old, revoked_at__isnull=False).count(), 2)
        rows = AuthorityGrant.objects.filter(user=old, member_of=self.kcw_division)
        self.assertEqual(
            sorted(rows.values_list("activity", flat=True)), sorted(identity.DIVISION_AUTHORITY)
        )
        merge_duties(apps, None)  # running again adds nothing
        self.assertEqual(AuthorityGrant.objects.filter(user=old).count(), 4)
