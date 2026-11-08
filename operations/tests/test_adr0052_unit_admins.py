"""แอดมินแผนกและแอดมินกลุ่มงาน (ADR 0052, grill 8 พ.ย. 2569 Q1 ถึง Q20).

Acceptance → tests: the role names one แผนก or กลุ่มงาน, from the pick or the สังกัด, and only
an admin sets it (RoleTests); a แอดมินแผนก sees and acts on their แผนก's cases only, a
แอดมินกลุ่มงาน on their กลุ่มงาน's, while สมาชิกกอง still see the whole กอง (ScopeTests); a case
carries its กลุ่มงาน, which follows its แผนก and, with no unit card, its ผู้รับผิดชอบ
(PlacementTests); a handoff waits for the unit whose card takes it (HandoffTests); the กอง's
pages open and show the unit's cases only (PageTests).

The example is the owner's: a meter withdrawal goes to แผนกคลังเครื่องวัด of กคว., and inside it
to กลุ่มงานคลังเครื่องวัดไฟฟ้า or another กลุ่มงาน of the แผนก.
"""

import uuid
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.utils import timezone

from operations import calendars, identity, services, units, web_actions
from operations.contracts import Actor, CommandFailed, ErrorCode
from operations.models import (
    Appointment,
    Calendar,
    CalendarSlot,
    Case,
    Division,
    Handoff,
    Role,
    Section,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m13_org import OrgTestCase, withdrawal
from operations.tests.test_m21_org_units import UnitTestCase

KCW = "กคว."


class MeterUnits:
    """แผนกคลังเครื่องวัด with two กลุ่มงาน, and its admins."""

    @classmethod
    def meter_units(cls):
        cls.kcw_division = Division.objects.get(code=KCW)
        cls.stock = Section.objects.get(name="แผนกคลังเครื่องวัด")
        cls.testing = Section.objects.get(name="แผนกทดสอบเครื่องวัด")
        cls.electric, _ = units.create_group(cls.admin, cls.stock, "กลุ่มงานคลังเครื่องวัดไฟฟ้า")
        cls.other_group, _ = units.create_group(cls.admin, cls.stock, "กลุ่มงานคลังอุปกรณ์")
        cls.kcw_head = make_staff("kcw-head", (Role.SUPERVISOR, KCW))
        cls.stock_admin = make_staff("stock-admin")
        identity.set_role(cls.admin, cls.stock_admin, Role.SECTION_ADMIN, section=cls.stock)
        cls.electric_admin = make_staff("electric-admin")
        identity.set_role(
            cls.admin, cls.electric_admin, Role.WORK_GROUP_ADMIN, work_group=cls.electric
        )
        cls.other_admin = make_staff("other-admin")
        identity.set_role(
            cls.admin, cls.other_admin, Role.WORK_GROUP_ADMIN, work_group=cls.other_group
        )

    def meter_case(self, quantity="1"):
        _, case = self.submit(withdrawal(("MTR_SYN_A", quantity)))
        case.refresh_from_db()
        return case

    def visible(self, user):
        return set(services.visible_cases(Actor.staff(user)).values_list("number", flat=True))

    def form(self, user, case, action, payload):
        case.refresh_from_db()
        return services.perform_form_action(
            Actor.staff(user),
            case.pk,
            action,
            expected_revision=case.revision,
            idempotency_key=f"test-{uuid.uuid4().hex}",
            payload=payload,
        )

    def refused(self, user, case, action, payload=None):
        with self.assertRaises(CommandFailed) as caught:
            self.act(user, case, action, payload)
        return caught.exception.code


class RoleTests(MeterUnits, UnitTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.meter_units()

    def test_the_role_names_one_unit_and_holds_no_กอง(self):
        grant = self.stock_admin.role_grants.get(revoked_at__isnull=True)
        self.assertEqual(
            (grant.role, grant.division, grant.section, grant.work_group),
            (Role.SECTION_ADMIN, self.kcw_division, self.stock, None),
        )
        scope = identity.staff_scope(self.stock_admin)
        self.assertEqual(scope.sections, {self.stock.pk})
        self.assertEqual(scope.division_roles, {})  # Q3: no membership of the whole กอง
        self.assertTrue(scope.unit_only and scope.has_any_access)
        scope = identity.staff_scope(self.electric_admin)
        self.assertEqual((scope.work_groups, scope.sections), ({self.electric.pk}, frozenset()))

    def test_without_a_pick_the_role_takes_the_สังกัด(self):
        units.move_person(self.admin, self.kcw, units.Unit.of(group=self.electric))
        identity.set_role(self.admin, self.kcw, Role.SECTION_ADMIN)  # Q7
        grant = self.kcw.role_grants.get(revoked_at__isnull=True)
        self.assertEqual((grant.section, grant.work_group), (self.stock, None))
        identity.set_role(self.admin, self.kcw, Role.WORK_GROUP_ADMIN)
        grant = self.kcw.role_grants.get(revoked_at__isnull=True)  # one role per person
        self.assertEqual((grant.section, grant.work_group), (None, self.electric))
        with self.assertRaises(ValidationError):  # no กลุ่มงาน in the สังกัด
            identity.set_role(self.admin, self.kcw2, Role.WORK_GROUP_ADMIN)
        with self.assertRaises(PermissionDenied):  # Q7: only an admin sets it
            identity.set_role(self.kcw_head, self.kcw2, Role.SECTION_ADMIN, section=self.stock)

    def test_the_user_page_picks_the_unit(self):
        client = Client()
        client.force_login(self.admin)
        client.post(
            f"/console/admin/users/{self.kcw2.pk}/grant/",
            {"role": Role.WORK_GROUP_ADMIN, "unit": f"g{self.other_group.pk}", "reason": "ทดสอบ"},
        )
        grant = self.kcw2.role_grants.get(revoked_at__isnull=True)
        self.assertEqual((grant.role, grant.work_group), (Role.WORK_GROUP_ADMIN, self.other_group))


class ScopeTests(MeterUnits, UnitTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.meter_units()
        units.add_work(cls.admin, units.Unit.of(group=cls.electric), "withdrawal", "meter")
        units.move_person(cls.admin, cls.kcw, units.Unit.of(group=cls.electric))
        units.move_person(cls.admin, cls.kcw2, units.Unit.of(section=cls.testing))

    def test_each_admin_sees_their_unit_and_members_see_the_กอง(self):
        electric = self.meter_case()
        self.assertEqual((electric.section, electric.work_group), (self.stock, self.electric))
        elsewhere = self.meter_case("2")
        self.act(self.kcw_head, elsewhere, "set_section", {"section": str(self.testing.pk)})
        loose = self.meter_case("3")
        self.act(self.kcw_head, loose, "set_section", {"section": ""})
        self.assertEqual(self.visible(self.stock_admin), {electric.number})  # Q2, Q5
        self.assertEqual(self.visible(self.electric_admin), {electric.number})
        self.assertEqual(self.visible(self.other_admin), set())
        self.assertEqual(  # Q10: สมาชิกกอง still see the whole กอง
            self.visible(self.kcw2), {electric.number, elsewhere.number, loose.number}
        )
        with self.assertRaises(CommandFailed) as caught:  # Q3: other cases look unknown
            services.get_case(Actor.staff(self.stock_admin), elsewhere.pk)
        self.assertEqual(caught.exception.code, ErrorCode.NOT_FOUND)

    def test_the_unit_admin_receives_and_assigns_inside_the_unit(self):
        case = self.meter_case()
        offers = [o.name for o in web_actions.available(self.electric_admin, case)]
        self.assertIn("approve", offers)  # Q2: ดำเนินการ
        self.assertIn("assign", offers)
        self.form(self.electric_admin, case, "approve", {"priority": "P3", "notice": "รับแล้ว"})
        case.refresh_from_db()
        self.assertEqual(case.status, Case.Status.IN_PROGRESS)
        st = web_actions.situation(self.electric_admin, case)
        people = {label for _, label in web_actions._members(st)}
        self.assertIn("Kcw", people)  # Q6: only the people of the unit
        self.assertNotIn("Kcw2", people)
        self.assertEqual(
            self.refused(self.electric_admin, case, "assign", {"assignee_id": self.kcw2.pk}),
            ErrorCode.VALIDATION_ERROR,
        )
        self.act(self.electric_admin, case, "assign", {"assignee_id": self.kcw.pk})
        self.assertEqual(Case.objects.get(pk=case.pk).assignee, self.kcw)
        # A แอดมินกอง still assigns anyone of the กอง, the unit admins included.
        self.act(self.kcw_head, case, "assign", {"assignee_id": self.stock_admin.pk})
        self.assertEqual(Case.objects.get(pk=case.pk).assignee, self.stock_admin)

    def test_moving_a_case_stays_top_down(self):
        case = self.meter_case()
        # Q13: a แอดมินกลุ่มงาน moves it to another กลุ่มงาน of the แผนก, never out of the แผนก.
        self.assertEqual(
            self.refused(
                self.electric_admin, case, "set_section", {"section": str(self.testing.pk)}
            ),
            ErrorCode.FORBIDDEN,
        )
        self.act(self.electric_admin, case, "set_work_group", {"work_group": self.other_group.pk})
        self.assertEqual(self.visible(self.electric_admin), set())
        self.assertEqual(self.visible(self.other_admin), {case.number})
        # A แอดมินแผนก sends it to another แผนก, and it leaves their hands (Q20: no กลุ่มงาน).
        self.act(self.stock_admin, case, "set_section", {"section": str(self.testing.pk)})
        case.refresh_from_db()
        self.assertEqual((case.section, case.work_group), (self.testing, None))
        self.assertEqual(self.visible(self.stock_admin), set())

    def test_the_responsible_person_keeps_their_case(self):
        case = self.meter_case()
        self.act(self.kcw_head, case, "set_section", {"section": str(self.testing.pk)})
        self.act(self.kcw_head, case, "assign", {"assignee_id": self.kcw.pk})
        self.act(self.kcw_head, case, "assign", {"assignee_id": self.other_admin.pk})
        self.assertEqual(self.visible(self.other_admin), {case.number})  # Q16
        case.refresh_from_db()
        offers = [o.name for o in web_actions.available(self.other_admin, case)]
        self.assertIn("accept_assignment", offers)
        self.assertNotIn("assign", offers)  # no แอดมินกอง buttons outside the unit
        self.act(self.other_admin, case, "accept_assignment")
        self.assertEqual(
            self.refused(self.other_admin, case, "assign", {"assignee_id": self.kcw.pk}),
            ErrorCode.FORBIDDEN,
        )


class PlacementTests(MeterUnits, UnitTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.meter_units()

    def test_the_card_of_the_แผนก_leaves_the_กลุ่มงาน_empty(self):
        units.add_work(self.admin, units.Unit.of(section=self.stock), "withdrawal", "meter")
        case = self.meter_case()
        self.assertEqual((case.section, case.work_group), (self.stock, None))
        self.assertEqual(self.visible(self.stock_admin), {case.number})  # Q5
        self.assertEqual(self.visible(self.electric_admin), set())

    def test_no_card_takes_the_สังกัด_of_the_responsible_person(self):
        case = self.meter_case()
        self.assertIsNone(case.section)
        units.move_person(self.admin, self.kcw, units.Unit.of(group=self.electric))
        self.act(self.kcw_head, case, "assign", {"assignee_id": self.kcw.pk})  # Q18
        case.refresh_from_db()
        self.assertEqual((case.section, case.work_group), (self.stock, self.electric))
        self.assertEqual(self.visible(self.electric_admin), {case.number})
        self.assertEqual(case.decisions.get(action="assign").data["section"], self.stock.name)


class HandoffTests(MeterUnits, OrgTestCase):
    """New cases are screened here, as in the handoff tests of the real structure."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.meter_units()
        cls.repair = Section.objects.get(name="แผนกซ่อมบำรุงหม้อแปลง")
        units.add_work(cls.admin, units.Unit.of(section=cls.repair), "withdrawal", "meter")
        cls.repair_admin = make_staff("repair-admin")
        identity.set_role(cls.admin, cls.repair_admin, Role.SECTION_ADMIN, section=cls.repair)

    def test_the_unit_whose_card_takes_it_sees_and_accepts_the_handoff(self):
        _, case = self.submit(withdrawal(("MTR_SYN_A", "1")))
        self.assertEqual(case.owner_division.code, KCW)
        self.act(self.kcw, case, "start_triage")
        self.act(
            self.kcw,
            case,
            "request_handoff",
            {"to_division": "กองอุปกรณ์ฯ", "reason_category": "wrong_division", "reason": "ทดสอบ"},
        )
        handoff = Handoff.objects.get(case=case, status=Handoff.Status.PENDING)
        self.assertEqual((handoff.to_section, handoff.to_work_group), (self.repair, None))
        self.assertEqual(self.visible(self.repair_admin), {case.number})  # Q17
        self.assertEqual(self.visible(self.stock_admin), set())
        self.act(self.repair_admin, case, "accept_handoff")
        case.refresh_from_db()
        self.assertEqual((case.owner_division.code, case.section), ("กองอุปกรณ์ฯ", self.repair))
        self.assertEqual(self.visible(self.repair_admin), {case.number})


class PageTests(MeterUnits, UnitTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.meter_units()
        units.add_work(cls.admin, units.Unit.of(group=cls.electric), "withdrawal", "meter")

    def test_the_queue_of_the_กอง_shows_the_unit_only(self):
        mine = self.meter_case()
        other = self.meter_case("2")
        self.act(self.kcw_head, other, "set_work_group", {"work_group": self.other_group.pk})
        client = Client()
        client.force_login(self.electric_admin)
        page = client.get(f"/divisions/{KCW}/", {"tab": "host"})
        self.assertEqual(page.status_code, 200)
        self.assertEqual([row.case.number for row in page.context["rows"]], [mine.number])
        self.assertEqual(client.get(f"/cases/{other.number}/").status_code, 404)
        self.assertEqual(client.get(f"/divisions/{KCW}/warehouse/").status_code, 200)
        self.assertEqual(client.get("/divisions/กตฟ./").status_code, 404)

    def test_the_calendar_shows_who_booked_only_for_the_unit(self):
        mine = self.meter_case()
        other = self.meter_case("2")
        self.act(self.kcw_head, other, "set_work_group", {"work_group": self.other_group.pk})
        other.refresh_from_db()
        calendar = Calendar.objects.create(
            key="adr0052-pickup", name="นัดรับมิเตอร์", division=self.kcw_division
        )
        day = timezone.localdate() + timedelta(days=1)
        nine = timezone.make_aware(timezone.datetime(day.year, day.month, day.day, 9))
        slots = [
            CalendarSlot.objects.create(
                calendar=calendar,
                starts_at=nine + timedelta(hours=h),
                ends_at=nine + timedelta(hours=h + 1),
            )
            for h in (0, 2)
        ]
        ours = Appointment.objects.create(calendar=calendar, slot=slots[0], case=mine)
        theirs = Appointment.objects.create(calendar=calendar, slot=slots[1], case=other)
        client = Client()
        client.force_login(self.electric_admin)
        page = client.get(
            f"/divisions/{KCW}/calendar/",
            {"cal": calendar.key, "month": day.replace(day=1).isoformat(), "day": day.isoformat()},
        )
        self.assertEqual(page.status_code, 200)  # Q19: the page opens
        self.assertEqual([a.pk for a in page.context["waiting"]], [ours.pk])
        html = page.content.decode()
        self.assertIn(mine.number, html)
        self.assertNotIn(other.number, html)
        self.assertIn("ไม่ว่าง", html)
        self.assertNotIn('value="add"', html)  # no slot opening for a unit admin
        self.assertTrue(calendars.may_decide(self.electric_admin, ours))
        self.assertFalse(calendars.may_decide(self.electric_admin, theirs))
        calendars.decide(self.electric_admin, ours, "confirm")
        with self.assertRaises(PermissionDenied):
            calendars.decide(self.electric_admin, theirs, "confirm")
