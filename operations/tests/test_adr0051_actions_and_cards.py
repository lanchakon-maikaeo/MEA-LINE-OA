"""ADR 0051: the warehouse and appointment steps are buttons in สิ่งที่ทำได้ whose forms open in
the action slot, the next stage leading (Q1, Q8 to Q10); สาธิตการใช้งาน shows an example of that
form (Q11); the queue keeps one main button and a menu (Q12); the chart's unit settings open from
the branch (Q4), and a person's card links to the account page and back (Q5 to Q7)."""

import re

from django.test import Client
from django.urls import reverse

from operations.models import Division, DivisionSchedule, Role, StaffProfile
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m13_org import OrgTestCase
from operations.tests.test_m16_kcw_work import WorkTestCase
from operations.tests.test_m21_org_units import on_chart


class WarehouseStepTests(WorkTestCase):
    def setUp(self):
        super().setUp()
        DivisionSchedule.objects.update_or_create(
            division=self.division,
            defaults={"sla": [{"group": "meter", "size": "", "days": 2}], "issue_weekdays": []},
        )
        self.client = Client()
        self.client.force_login(self.kcw)

    def test_the_next_stage_leads_and_its_form_opens_in_the_slot(self):
        case = self.withdraw("4500510001")
        page = self.client.get(f"/cases/{case.number}/").content.decode()
        lead = re.search(r'<a class="btn btn--block"[^>]*>([^<]*)</a>', page)
        self.assertEqual(lead.group(1), "บันทึกรับใบเบิกแล้ว")
        self.assertIn(reverse("warehouse", args=["กคว."]), page)  # ไปหน้างานคลัง in the panel
        stage = reverse("case-stage", args=[case.number])
        self.assertNotIn(f'action="{stage}"', page)  # the form opens only from its button
        form = self.client.get(f"/cases/{case.number}/stage/", HTTP_HX_REQUEST="true")
        self.assertContains(form, f'action="{stage}"')
        self.assertEqual(
            self.client.get(f"/cases/{case.number}/stage/?stage=issued").status_code, 403
        )

    def test_a_case_waiting_to_be_received_says_why_instead(self):
        case = self.older_waiting("4500510002")
        page = self.client.get(f"/cases/{case.number}/").content.decode()
        self.assertIn("บันทึกขั้นงานคลังได้หลังรับเรื่องแล้ว", page)
        self.assertNotIn("บันทึกรับใบเบิกแล้ว", page)
        self.assertEqual(self.client.get(f"/cases/{case.number}/stage/").status_code, 403)

    def test_the_tour_shows_an_example_that_cannot_be_sent(self):
        case = self.withdraw("4500510003")
        demo = self.client.get(f"/cases/{case.number}/stage/?demo=1", HTTP_HX_REQUEST="true")
        self.assertContains(demo, "data-demo")
        self.assertNotContains(demo, "csrfmiddlewaretoken")

    def test_the_queue_has_one_main_button_and_a_menu(self):
        page = self.client.get(reverse("division-detail", args=["กคว."])).content.decode()
        menu = page[page.index('data-tour="queue-menu"') :]
        self.assertIn(reverse("warehouse", args=["กคว."]), menu[: menu.index("</details>")])


class OrgCardTests(OrgTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.requester = on_chart(Division.objects.get(code="กคว."), "", cls.admin)
        cls.person = make_staff("org-0051-person", (Role.STAFF, "กคว."))
        StaffProfile.objects.filter(user=cls.person).update(
            employee_id=cls.requester.employee.employee_id
        )

    def test_unit_settings_open_from_the_branch_and_people_link_to_their_account(self):
        client = Client()
        client.force_login(self.admin)
        page = client.get(reverse("admin-org")).content.decode()
        self.assertNotIn("ot__gear", page)
        self.assertIn('class="ot__set"', page)
        self.assertIn(reverse("admin-user-detail", args=[self.person.pk]), page)
        account = client.get(reverse("admin-user-detail", args=[self.person.pk]))
        self.assertContains(account, f"#op-{self.requester.employee.pk}")
