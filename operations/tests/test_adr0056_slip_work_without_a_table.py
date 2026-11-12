"""ADR 0056 (12 พ.ย. 2569): a ติดตามใบเบิก or ติดตามใบส่งคืน case is worked by its warehouse
stages in every กอง, table or not; กอจ. keeps no กำหนดจ่ายตามขนาด and no วันส่งรถ, so its page
has neither.

Acceptance → tests: a กอจ. return filed without a number shows the stage first on its page, is
worked through the four stages, waits for the requester and closes on ยืนยันผล; the queue's menu
opens งานคลัง once the กอง holds a slip case; the table page of กอจ. shows only ชื่อขั้น and the
contact, and saves them.
"""

from django.test import Client
from django.urls import reverse
from django.utils import timezone

from operations import case_menu, services, warehouse
from operations.contracts import Actor
from operations.models import Case, CaseMilestone, DivisionSchedule, Role
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import ALICE, buttons
from operations.tests.test_m24_koj_cases import KojCaseTestCase


class KojSlipWorkTests(KojCaseTestCase):
    def setUp(self):
        super().setUp()
        self.head = make_staff("koj-head-56", (Role.SUPERVISOR, self.koj.code))
        self.web = Client()
        self.web.force_login(self.head)

    def file_return(self):
        self.open_koj()
        self.press(ALICE, "LBS")
        self.press(ALICE, "ติดตามใบส่งคืน")
        self.press(ALICE, "ไม่มีหมายเลข")
        self.type(ALICE, "LBS 1 ชุด ส่งใบส่งคืน 3 พ.ย.")
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        services.perform_form_action(  # ADR 0044: ดำเนินการ, then the stages are its work
            Actor.staff(self.head),
            case.pk,
            "approve",
            expected_revision=case.revision,
            idempotency_key=f"start-{case.pk}",
            payload={"priority": "P3"},
        )
        case.refresh_from_db()
        return case

    def test_a_return_is_worked_by_its_stages_and_closes_on_the_requesters_answer(self):
        case = self.file_return()
        self.assertFalse(DivisionSchedule.objects.filter(division=self.koj).exists())
        menu = case_menu.build(self.head, case)
        self.assertEqual((menu.first.kind, menu.first.name), ("stage", "received"))
        page = self.web.get(reverse("case-detail", args=[case.number])).content.decode()
        self.assertIn("งานคลังและนัด", page)
        self.assertIn(reverse("warehouse", args=[self.koj.code]), page)
        today = timezone.localdate()
        for stage in CaseMilestone.ORDER:
            done, skipped = warehouse.record(self.head, [case], stage, today)
            self.assertEqual((len(done), skipped), (1, []))
        case.refresh_from_db()
        self.assertEqual(case.status, Case.Status.AWAITING_CONFIRMATION)
        reply = self.type(ALICE, "ขอบคุณ")
        self.assertIn("ยืนยันผล", buttons(reply))
        self.press(ALICE, "ยืนยันผล", reply)
        case.refresh_from_db()
        self.assertEqual(case.status, Case.Status.CLOSED)

    def test_the_queue_menu_opens_งานคลัง_once_the_กอง_holds_a_slip(self):
        link = reverse("warehouse", args=[self.koj.code])
        queue = reverse("division-detail", args=[self.koj.code])
        self.assertNotIn(link, self.web.get(queue).content.decode())
        self.file_return()
        self.assertIn(link, self.web.get(queue).content.decode())

    def test_the_table_page_of_กอจ_has_only_stage_names_and_the_contact(self):
        url = reverse("warehouse-schedule", args=[self.koj.code])
        page = self.web.get(url).content.decode()
        self.assertNotIn('data-sc-part="sla"', page)
        self.assertNotIn('data-sc-part="truck"', page)
        self.assertNotIn("มิเตอร์", page)
        self.assertIn('data-sc-part="stages"', page)
        saved = self.web.post(
            url,
            {"reason": "ตั้งชื่อขั้น", "contact": "0 2555 0100", "stage_return_received": "รับใบส่งคืนแล้ว"},
        )
        self.assertEqual(saved.status_code, 302)
        schedule = DivisionSchedule.objects.get(division=self.koj)
        self.assertEqual(schedule.contact, "0 2555 0100")
        self.assertEqual(schedule.stage_labels, {"return": {"received": "รับใบส่งคืนแล้ว"}})
        self.assertEqual((schedule.sla, schedule.truck_days), ([], {}))
        work = self.web.get(reverse("warehouse", args=[self.koj.code])).content.decode()
        self.assertNotIn("ยังไม่มีตารางกำหนดจ่าย", work)
