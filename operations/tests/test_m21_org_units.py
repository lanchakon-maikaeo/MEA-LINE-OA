"""ผังหน่วยงาน (ADR 0031, grill หน่วยงานและ DED Center 29 ต.ค. 2569).

Acceptance → tests: the สังกัด shows กอง › แผนก › กลุ่มงาน with N/A where a level is missing
(PathTests); a new case goes at once to the ผู้รับผิดชอบประจำงาน of the unit that takes its work,
skipping people who are away or lost their role, and only cases the system assigned move on by
themselves (AssignTests); important LINE alerts go to the person responsible, else the unit's
ผู้รับแจ้งเรื่องสำคัญ from small to big, else the Supervisors (LineTests); the roster gives the
สังกัด and a new กลุ่มงาน takes its people (RosterTests); the chart is admins' only and answers
the page (PageTests); registration asks for no location and the old lists moved onto
the cards (MigrationTests).
"""

import uuid
from importlib import import_module

from django.apps import apps
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client

from operations import identity, options, staff_line, units
from operations.models import (
    Case,
    ChannelIdentity,
    Division,
    EmergencyContact,
    Employee,
    Role,
    RoleGrant,
    Section,
    SystemOption,
    UnitRecipient,
)
from operations.templatetags.units_ui import unit_path
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m13_org import OrgTestCase

KAE = "กองอุปกรณ์ฯ"


def line(user):
    return ChannelIdentity.objects.create(
        channel="line", external_id=f"U{uuid.uuid4().hex}", staff_user=user
    )


def on_chart(division, section_name, approver):
    """Someone in the roster who registered on LINE: the chart shows only their units."""
    from operations.models import District, Requester

    employee = Employee.objects.create(
        employee_id=f"E-{uuid.uuid4().hex[:6]}", division=division, section_name=section_name
    )
    requester = Requester.objects.create(
        display_name="ผู้ใช้ LINE",
        district=District.objects.first(),
        approved_by=approver,
        employee=employee,
        employee_link=Requester.EmployeeLink.VERIFIED,
    )
    ChannelIdentity.objects.create(
        channel="line", external_id=f"U{uuid.uuid4().hex}", requester=requester, status="active"
    )
    return requester


class UnitTestCase(OrgTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # ADR 0025: new cases wait for the owner's approval without screening.
        SystemOption.objects.update_or_create(key=options.SCREENING, defaults={"value": "off"})
        cls.division = Division.objects.get(code=KAE)
        cls.repair = Section.objects.get(name="แผนกซ่อมบำรุงหม้อแปลง")
        cls.caps = Section.objects.get(name="แผนกหม้อแปลงและคาปาซิเตอร์")
        cls.head = make_staff("kaehead", (Role.SUPERVISOR, KAE))

    def problem(self, text="หม้อแปลงมีเสียงดังผิดปกติ"):
        _, case = self.submit({"kind": "problem", "topic": "transformer", "description": text})
        return case

    def work(self, section, *owners, kind="problem", group="transformer"):
        work, _ = units.add_work(self.admin, units.Unit.of(section=section), kind, group)
        if owners:
            units.set_owners(self.admin, work, [u.pk for u in owners])
        return work


class PathTests(UnitTestCase):
    def test_three_slots_with_na_and_short_names(self):
        self.assertEqual(units.path_labels(self.kae.staff_profile), ["N/A", "N/A", "N/A"])
        units.move_person(self.admin, self.kae, units.Unit.of(section=self.repair))
        self.kae.staff_profile.refresh_from_db()
        self.assertEqual(
            units.path_labels(self.kae.staff_profile), [KAE, "แผนกซ่อมบำรุงหม้อแปลง", "N/A"]
        )
        units.set_short_name(self.admin, units.Unit.of(section=self.repair), "ซม.")
        units.set_short_name(self.admin, units.Unit.of(division=self.division), "กอจ.")
        self.kae.refresh_from_db()
        html = unit_path(self.kae)
        self.assertIn("กอจ.", html)
        self.assertIn("ซม.", html)
        self.assertIn("unit-path__part--na", html)
        self.assertIn("แผนกซ่อมบำรุงหม้อแปลง", html)  # the full names on hover
        # Only a กอง where the person holds a role; only admins.
        with self.assertRaises(ValidationError):
            units.move_person(self.admin, self.ktf, units.Unit.of(section=self.repair))
        with self.assertRaises(PermissionDenied):
            units.move_person(self.kae, self.kae2, units.Unit.of(section=self.repair))


class AssignTests(UnitTestCase):
    def test_a_new_case_goes_to_the_work_owner_at_once(self):
        work = self.work(self.repair, self.kae)
        case = self.problem()
        self.assertEqual(case.status, Case.Status.AWAITING_APPROVAL)  # ADR 0044: รอรับเรื่อง
        case.refresh_from_db()
        self.assertEqual((case.assignee, case.auto_assigned), (self.kae, True))
        self.assertEqual((case.section, case.unit_work), (self.repair, work))
        decision = case.decisions.get(action="assign")
        self.assertIsNone(decision.actor)
        self.assertIn("ปัญหา/ข้อเสนอแนะ · หม้อแปลง", decision.reason)
        client = Client()
        client.force_login(self.kae)
        page = client.get(f"/cases/{case.number}/").content.decode()
        self.assertIn("ระบบมอบหมายให้ Kae", page)
        self.assertIn("ระบบมอบตามงานที่หน่วยรับ", page)

    def test_without_a_matching_work_nobody_is_assigned(self):
        self.work(self.repair, self.kae, group="meter")
        case = self.problem()
        case.refresh_from_db()
        self.assertIsNone(case.assignee)
        self.assertIsNone(case.section)

    def test_the_first_owner_takes_it_and_the_next_only_when_the_first_cannot_act(self):
        # ADR 0054 Q13: no "ไม่อยู่" any more; the order matters only once the first loses the กอง.
        self.work(self.repair, self.kae, self.kae2)
        first = self.problem()
        first.refresh_from_db()
        self.assertEqual(first.assignee, self.kae)

    def test_a_revoked_role_moves_the_case_or_frees_it(self):
        self.work(self.repair, self.kae, self.kae2)
        case = self.problem()
        grant = RoleGrant.objects.get(user=self.kae, division=self.division)
        identity.revoke_role(self.admin, grant, "ย้ายงาน")
        case.refresh_from_db()
        self.assertEqual((case.assignee, case.auto_assigned), (self.kae2, True))
        grant = RoleGrant.objects.get(user=self.kae2, division=self.division, revoked_at=None)
        identity.revoke_role(self.admin, grant, "ย้ายงาน")
        case.refresh_from_db()
        self.assertEqual((case.assignee, case.auto_assigned), (None, False))

    def test_a_person_s_choice_is_kept(self):
        self.work(self.repair, self.kae, self.kae2)
        case = self.problem()
        self.act(self.head, case, "assign", {"assignee_id": self.kae2.pk, "reason": "ถนัด"})
        case.refresh_from_db()
        self.assertFalse(case.auto_assigned)
        identity.revoke_role(
            self.admin, RoleGrant.objects.get(user=self.kae, division=self.division), "ย้ายงาน"
        )
        case.refresh_from_db()
        self.assertEqual(case.assignee, self.kae2)  # a person's pick never follows the system

    def test_a_new_section_or_a_new_owner_list_moves_system_picks(self):
        self.work(self.repair, self.kae)
        caps = self.work(self.caps, self.kae2)
        case = self.problem()
        self.act(self.kae, case, "accept_assignment")  # ADR 0049: รับงาน before any other step
        self.act(self.kae, case, "set_section", {"section": str(self.caps.pk)})
        case.refresh_from_db()
        self.assertEqual((case.section, case.assignee), (self.caps, self.kae2))
        self.assertTrue(case.auto_assigned)
        self.assertEqual(units.set_owners(self.admin, caps, [self.kae.pk]), 1)
        case.refresh_from_db()
        self.assertEqual(case.assignee, self.kae)

    def test_the_admin_order_decides_between_two_units(self):
        first = self.work(self.repair, self.kae)
        second = self.work(self.caps, self.kae2)
        _, others = units.add_work(
            self.admin, units.Unit.of(division=self.division), "problem", "transformer"
        )
        self.assertEqual(len(others), 2)  # warned: two units already take it
        units.move_work(self.admin, second, -1)
        case = self.problem()
        case.refresh_from_db()
        self.assertEqual((case.unit_work, case.assignee), (second, self.kae2))
        units.remove_work(self.admin, second)
        again = self.problem("หม้อแปลงร้อน")
        again.refresh_from_db()
        self.assertEqual(again.unit_work, first)


class LineTests(UnitTestCase):
    def test_assignee_then_unit_receivers_small_to_big_then_supervisors(self):
        work = self.work(self.repair)
        people = lambda: units.line_people(self.division, work)  # noqa: E731
        self.assertIsNone(people())  # nobody with LINE
        line(self.head)
        self.assertEqual(people(), [self.head])
        for user in (self.kae, self.kae2):
            line(user)
        units.set_recipients(self.admin, units.Unit.of(division=self.division), [self.kae2.pk])
        self.assertEqual(people(), [self.kae2])
        units.set_recipients(self.admin, units.Unit.of(section=self.repair), [self.kae.pk])
        self.assertEqual(people(), [self.kae])
        self.assertEqual(units.line_people(self.division, work, assignee=self.kae2), [self.kae2])
        with self.assertRaises(ValidationError):  # LINE first
            units.set_recipients(
                self.admin, units.Unit.of(section=self.repair), [self.kae_resolver.pk]
            )

    def test_an_important_alert_reaches_the_unit_before_the_division(self):
        self.work(self.repair)
        for user in (self.kae, self.kae2, self.head):
            line(user)
        units.set_recipients(self.admin, units.Unit.of(section=self.repair), [self.kae2.pk])
        case = self.problem()
        members = [self.kae, self.kae2, self.head]
        self.assertEqual(staff_line._important_people(case, members), [self.kae2])
        case.assignee = self.kae
        self.assertEqual(staff_line._important_people(case, members), [self.kae])


class RosterTests(UnitTestCase):
    def test_the_roster_gives_the_unit_and_a_new_group_takes_its_people(self):
        employee = Employee.objects.create(
            employee_id="E-1001",
            division=self.division,
            section_name="แผนกซ่อมบำรุงหม้อแปลง",
            work_group="กลุ่มงานหม้อแปลงจำหน่าย",
        )
        profile = self.kae.staff_profile
        profile.employee_id = "E-1001"
        profile.save(update_fields=["employee_id"])
        units.bind_from_roster(self.kae, employee)
        profile.refresh_from_db()
        self.assertEqual((profile.section, profile.work_group), (self.repair, None))
        group, joined = units.create_group(self.admin, self.repair, "กลุ่มงานหม้อแปลงจำหน่าย", "หจ.")
        profile.refresh_from_db()
        self.assertEqual((joined, profile.work_group), (1, group))
        self.assertEqual(units.path_labels(profile), [KAE, "แผนกซ่อมบำรุงหม้อแปลง", "หจ."])
        # ADR 0041 (Q3): the roster wins again; an admin's move lasts until then.
        units.move_person(self.admin, self.kae, units.Unit.of(division=self.division))
        profile.refresh_from_db()
        self.assertIsNone(profile.section)
        units.bind_from_roster(self.kae, employee)
        profile.refresh_from_db()
        self.assertEqual((profile.section, profile.work_group), (self.repair, group))
        with self.assertRaises(ValidationError):
            units.create_group(self.admin, self.repair, "กลุ่มงานหม้อแปลงจำหน่าย")


class PageTests(UnitTestCase):
    def test_admins_only_see_and_change_the_chart(self):
        work = self.work(self.repair, self.kae)
        line(self.kae)
        client = Client()
        client.force_login(self.kae)
        self.assertEqual(client.get("/console/admin/org/").status_code, 403)
        client.force_login(self.admin)
        page = client.get("/console/admin/org/").content.decode()
        self.assertNotIn("ปัญหา/ข้อเสนอแนะ · หม้อแปลง", page)  # ADR 0050 Q3: nobody there yet
        on_chart(self.division, self.repair.name, self.admin)
        page = client.get("/console/admin/org/").content.decode()
        self.assertIn("ผังบุคลากร", page)
        self.assertIn("ปัญหา/ข้อเสนอแนะ · หม้อแปลง", page)  # on the unit's card
        self.assertNotIn("ยังไม่ได้จัดหน่วย", page)  # ADR 0044: the roster places everyone
        response = client.post(
            "/console/admin/org/",
            {
                "action": "owners",
                "work": work.pk,
                "owner": [self.kae2.pk, self.kae.pk, ""],
                "unit": f"s{self.repair.pk}",
            },
        )
        self.assertRedirects(response, "/console/admin/org/#org-tree")
        self.assertEqual([o.user for o in work.owners.order_by("order")], [self.kae2, self.kae])
        client.post(
            "/console/admin/org/",
            {"action": "move_person", "user": self.kae.pk, "target": f"s{self.repair.pk}"},
        )
        self.kae.staff_profile.refresh_from_db()
        self.assertEqual(self.kae.staff_profile.section, self.repair)
        self.assertContains(client.get("/console/admin/"), "ผังบุคลากร")

    def test_the_old_alert_forms_point_to_the_chart(self):
        client = Client()
        client.force_login(self.admin)
        self.assertContains(client.get("/console/admin/divisions/"), "ดูในผังบุคลากร")
        self.assertEqual(client.post(f"/koj/recipients/{self.kae.pk}/").status_code, 404)
        self.assertContains(client.get(f"/console/admin/users/{self.kae.pk}/"), "ผังบุคลากร")


class MigrationTests(UnitTestCase):
    def test_location_is_not_asked_and_emergency_contacts_move_to_the_card(self):
        self.assertEqual(options.get(options.REGISTRATION_LOCATION), "off")
        SystemOption.objects.update_or_create(
            key=options.REGISTRATION_LOCATION, defaults={"value": "required"}
        )
        EmergencyContact.objects.create(division=self.division, user=self.kae)
        self.kae.staff_profile.section = self.repair
        self.kae.staff_profile.save(update_fields=["section"])
        import_module("operations.migrations.0079_org_units").forwards(apps, None)
        self.assertEqual(options.get(options.REGISTRATION_LOCATION), "off")
        row = UnitRecipient.objects.get()
        self.assertEqual((row.division, row.section, row.user), (self.division, None, self.kae))
        self.kae.staff_profile.refresh_from_db()
        self.kae2.staff_profile.refresh_from_db()
        self.assertEqual(self.kae.staff_profile.division, self.division)
        self.assertEqual(self.kae2.staff_profile.division, self.division)  # its only role
        self.admin.staff_profile.refresh_from_db()
        self.assertIsNone(self.admin.staff_profile.division)
