"""ADR 0054 (grill 10 พ.ย. 2569): a case from LINE passes down from the แอดมินฝ่าย to the
แอดมินกอง and on to the แอดมินแผนก; Push Message picks each group's LINE and email apart; the
ผู้รับผิดชอบ gets the case card.

Acceptance → tests: a แอดมินฝ่าย hands a case only to the owner กอง's แอดมินกอง, the same on
the web, in LINE and at the service, and to anyone in the กอง once it has none; a แอดมินกอง sees
the แอดมินแผนก and แอดมินกลุ่มงาน first, the case's แผนก ahead; an assignee who may assign passes
the case on without รับงาน; nobody is away any more (ChainTests). A new case reaches the owner
กอง's แอดมินกอง by LINE and email, with only the view button, and the แอดมินฝ่าย by email too;
each group's email has its own switch; the case's แอดมินกอง and แอดมินแผนก start off and follow
their own switches; the page shows a LINE and an email column (RecipientTests). The ผู้รับผิดชอบ
gets the case card with รับงาน, มอบหมายต่อ when they may, ขอคืนงาน and ดูเรื่อง while the lock
screen keeps the short text; the card and the web page show the proven รหัสพนักงาน and the
ตำแหน่ง (CardTests).
"""

from datetime import timedelta

from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from operations import identity, options, staff_email, staff_work, web_actions
from operations.contracts import CommandFailed
from operations.models import (
    Case,
    Employee,
    NotificationDelivery,
    Requester,
    Role,
    RoleGrant,
    Section,
    StaffProfile,
)
from operations.tests.test_adr0050_duty_alerts import ANN_LINE, HEAD_LINE, AlertTestCase
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import buttons, texts
from operations.tests.test_m26_push_and_duty import on_line
from operations.tests.test_m28_accept_and_cards import AcceptTestCase, act, names

N = NotificationDelivery


class ChainTestCase(AlertTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.member = make_staff("kcw-member-54", (Role.STAFF, "กคว."))
        cls.stock = Section.objects.get(name="แผนกคลังเครื่องวัด")
        cls.testing = Section.objects.get(name="แผนกทดสอบเครื่องวัด")
        cls.stock_admin = make_staff("stock-admin-54")
        identity.set_role(cls.boss, cls.stock_admin, Role.SECTION_ADMIN, section=cls.stock)
        cls.testing_admin = make_staff("testing-admin-54")
        identity.set_role(cls.boss, cls.testing_admin, Role.SECTION_ADMIN, section=cls.testing)

    def choices(self, user, case):
        """The web's list and LINE's, which must be the same people in the same order."""
        st = web_actions.situation(user, case)
        web = [pk for pk, _ in web_actions._members(st)]
        self.assertEqual(web, [pk for pk, _ in staff_work.assignees(case, st.scope)])
        return web

    def in_section(self, case, section):
        Case.objects.filter(pk=case.pk).update(section=section)
        case.refresh_from_db()
        return case


class ChainTests(ChainTestCase):
    def test_the_department_admin_hands_it_only_to_the_kong_admin(self):
        case = self.new_case()
        self.assertEqual(self.choices(self.ann, case), [str(self.head.pk)])
        with self.assertRaises(CommandFailed):
            act(self.ann, case, "assign", assignee_id=self.member.pk)
        self.assertEqual(
            act(self.ann, case, "assign", assignee_id=self.head.pk).assignee, self.head
        )

    def test_a_kong_without_a_kong_admin_lists_everyone(self):
        RoleGrant.objects.filter(user=self.head).update(revoked_at=timezone.now())
        case = self.new_case()
        self.assertIn(str(self.member.pk), self.choices(self.ann, case))
        act(self.ann, case, "assign", assignee_id=self.member.pk)

    def test_the_kong_admin_sees_the_unit_admins_first(self):
        case = self.in_section(self.new_case(), self.testing)
        order = self.choices(self.head, case)
        self.assertEqual(order[:2], [str(self.testing_admin.pk), str(self.stock_admin.pk)])
        self.assertIn(str(self.member.pk), order[2:])

    def test_the_kong_admin_passes_it_on_without_accepting(self):
        case = act(self.ann, self.new_case(), "assign", assignee_id=self.head.pk)
        self.assertTrue(case.awaits_acceptance)
        self.assertIn("assign", names(self.head, case))
        case = act(self.head, case, "assign", assignee_id=self.stock_admin.pk)
        self.assertEqual((case.assignee, case.awaits_acceptance), (self.stock_admin, True))

    def test_nobody_is_away_any_more(self):
        client = Client()
        client.force_login(self.head)
        self.assertNotContains(client.get(reverse("account")), "ไม่อยู่ถึงวันที่")
        tomorrow = timezone.localdate() + timedelta(days=1)
        StaffProfile.objects.filter(user=self.head).update(away_until=tomorrow)  # an old date
        self.assertEqual(self.choices(self.ann, self.new_case()), [str(self.head.pk)])


@override_settings(STAFF_EMAIL_ENABLED=True)
class RecipientTests(ChainTestCase):
    def channels(self, user, kind):
        return set(
            N.objects.filter(kind=kind, recipient_user=user).values_list("channel", flat=True)
        )

    def test_a_new_case_reaches_the_kong_admin_by_line_and_email(self):
        self.link(self.ann, ANN_LINE)
        self.link(self.head, HEAD_LINE)
        self.new_case()
        both = {N.Channel.LINE_PUSH, N.Channel.EMAIL}
        self.assertEqual(self.channels(self.head, "staff_new_case"), both)
        self.assertEqual(self.channels(self.ann, "staff_new_case"), both)
        self.assertEqual(self.channels(self.cat, "staff_new_case"), {N.Channel.EMAIL})
        copy = self.labels(self.head)  # the view button only
        self.assertEqual((len(copy), copy[0][:8]), (1, "ดูเรื่อง"))

    def test_each_group_has_its_own_email_switch(self):
        self.link(self.head, HEAD_LINE)
        options.change(None, "push:staff_new_case:division_admin:email", "off", "")
        self.new_case()
        self.assertEqual(self.channels(self.head, "staff_new_case"), {N.Channel.LINE_PUSH})

    def test_the_case_admins_start_off_and_follow_their_own_switches(self):
        self.link(self.head, HEAD_LINE)
        on_line(self.stock_admin)
        self.stock_admin.email = "stock-admin@example.test"
        self.stock_admin.save(update_fields=["email"])
        options.change(None, "push:staff_reopen:duty", "off", "")
        options.change(None, "push:staff_reopen:duty:email", "off", "")
        case = self.in_section(self.new_case(), self.stock)
        staff_email.plan("staff_reopen", "reopen-1", case, [])
        self.assertFalse(N.objects.filter(kind="staff_reopen").exists())
        options.change(None, "push:staff_reopen:unit_admin", "on", "")
        options.change(None, "push:staff_reopen:host_admin:email", "on", "")
        staff_email.plan("staff_reopen", "reopen-2", case, [])
        self.assertEqual(self.channels(self.stock_admin, "staff_reopen"), {N.Channel.LINE_PUSH})
        self.assertEqual(self.channels(self.head, "staff_reopen"), {N.Channel.EMAIL})
        self.assertFalse(self.channels(self.testing_admin, "staff_reopen"))  # not its แผนก

    def test_the_page_shows_a_line_and_an_email_column(self):
        client = Client()
        client.force_login(self.boss)
        page = client.get(reverse("admin-push"))
        self.assertContains(page, "แอดมินกองเจ้าภาพ")
        self.assertContains(page, "แอดมินแผนกหรือกลุ่มงานของเรื่อง")
        self.assertContains(page, 'form="pf-pushstaff_assignedhost_adminemail"')
        self.assertNotContains(page, "ผู้รับชุดนี้ใช้ทั้ง LINE และอีเมล")


class CardTests(AcceptTestCase):
    def prove(self, case, link=Requester.EmployeeLink.VERIFIED):
        requester = case.requester
        requester.employee, _ = Employee.objects.get_or_create(
            employee_id="2630617", defaults={"name": "ชนันพร", "position": "วิศวกร 6"}
        )
        requester.employee_link = link
        requester.save(update_fields=["employee", "employee_link"])
        return requester

    def told(self, case, user):
        self.alerts(case, "staff_assigned")
        return N.objects.get(
            case=case, kind="staff_assigned", recipient_user=user, channel=N.Channel.LINE_PUSH
        )

    def test_the_assignee_gets_the_case_card_with_its_steps(self):
        case = self.withdraw("4500070541")
        requester = self.prove(case)
        case = self.assign(case, self.head, by=self.duty)
        told = self.told(case, self.head)
        self.assertEqual(told.message["type"], "flex")
        self.assertEqual(told.message["altText"], told.text[:400])  # the lock screen
        self.assertNotIn(requester.display_name, told.message["altText"])
        steps = ["รับงาน", "มอบหมายต่อ", "ขอคืนงาน", f"ดูเรื่อง {case.number}"]
        self.assertEqual(list(buttons([told.message])), steps)
        shown = texts([told.message])
        self.assertIn("รหัสพนักงาน: 2630617", shown)
        self.assertIn("ตำแหน่ง: วิศวกร 6", shown)
        case = self.assign(case, self.kcw, by=self.head)  # Q6: no รับงาน first
        steps = ["รับงาน", "ขอคืนงาน", f"ดูเรื่อง {case.number}"]
        self.assertEqual(list(buttons([self.told(case, self.kcw).message])), steps)

    def test_the_web_page_shows_the_proven_id_as_the_card_does(self):
        case = self.withdraw("4500070542")
        self.prove(case)
        client = Client()
        client.force_login(self.head)
        page = client.get(reverse("case-detail", args=[case.number]))
        self.assertContains(page, "รหัสพนักงาน 2630617")
        self.assertContains(page, "วิศวกร 6")
        self.prove(case, Requester.EmployeeLink.UNLINKED)
        case.refresh_from_db()
        page = client.get(reverse("case-detail", args=[case.number]))
        self.assertNotContains(page, "รหัสพนักงาน 2630617")
        rows = "\n".join(staff_work.case_rows(case))
        self.assertNotIn("รหัสพนักงาน", rows)
        self.assertIn("ยังไม่ยืนยันตัวตน", rows)
        self.assertIn("ตำแหน่ง: วิศวกร 6", rows)
