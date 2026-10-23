"""Regressions for the LINE redesign agreed on 16 October 2026."""

from types import SimpleNamespace

from django.core.management import call_command
from django.test import TestCase

from operations import employees, options, staff_work
from operations.models import Case, ConversationDraft, LineMenuItem, WorkOrder
from operations.tests.test_m03_line import ALICE, buttons, texts
from operations.tests.test_m10_web import FriendlyFlowTests, WebTestCase
from operations.tests.test_m13_line_menu import MenuTestCase
from operations.tests.test_m13_staff_chat import StaffChatTestCase


class MenuRegressionTests(MenuTestCase):
    def test_disabled_parent_rejects_old_child_button(self):
        parent = self.tile("ระบบ SMG, AMR")
        child = parent.choices.get(label="การจัดทำบิล")
        parent.is_active = False
        parent.save()
        reply = self.say(self.postback(ALICE, f"m=item&id={child.pk}"))
        self.assertIn("เมนูนี้ปิดแล้ว", texts(reply))
        self.assertFalse(ConversationDraft.objects.filter(status="open").exists())


class StaffConfirmationTests(StaffChatTestCase):
    def test_old_confirmation_cannot_submit_new_order(self):
        self.link(self.head, ALICE)

        def summary(title):
            self.tile(LineMenuItem.Action.ORDER_NEW)
            return self.type(ALICE, f"ให้ทั้งกอง กองอุปกรณ์ฯ {title} พรุ่งนี้")

        old = summary("งาน A")
        latest = summary("งาน B")
        reply = self.say(self.postback(ALICE, buttons(old)["ยืนยัน"]))
        self.assertIn("ฉบับเก่า", texts(reply))
        self.assertFalse(WorkOrder.objects.exists())
        self.say(self.postback(ALICE, buttons(latest)["ยืนยัน"]))
        self.assertIn("งาน B", WorkOrder.objects.get().title)


class TriageRegressionTests(WebTestCase):
    def test_chat_triage_confirms_priority_and_completes_together(self):
        options.change(None, options.STAFF_LINE_ACTIONS, "act", "test")
        case = FriendlyFlowTests.at_owner(self)
        turn = SimpleNamespace(
            conversation=SimpleNamespace(
                prompt_data={
                    "case": str(case.pk),
                    "action": "complete_triage",
                    "revision": case.revision,
                    "values": {"priority": "P2", "reason": "ไฟดับบางส่วน"},
                }
            ),
            inbox=SimpleNamespace(pk=987654),
            replies=[],
        )
        turn.set_prompt = lambda *args, **kwargs: setattr(turn.conversation, "prompt_data", kwargs)
        turn.say = lambda message, *args: turn.replies.append(message)
        turn.button = lambda label, *args, **kwargs: (label, "test-token")
        staff_work.step_go(turn, self.trf_staff)
        case.refresh_from_db()
        self.assertNotEqual(case.status, Case.Status.TRIAGE)
        self.assertTrue(case.decisions.filter(action="set_priority").exists())
        self.assertTrue(case.decisions.filter(action="complete_triage").exists())
        self.assertFalse(any("บันทึกไม่ได้" in message for message in turn.replies))


class FreshMenuTests(TestCase):
    def test_org_import_binds_seed_and_preserves_explicit_routes(self):
        LineMenuItem.objects.update(division=None)
        call_command("load_org", verbosity=0)
        choices = LineMenuItem.objects.filter(place="choice")
        self.assertTrue(choices.exists())
        self.assertFalse(choices.filter(division__isnull=True).exists())
        child = choices.first()
        original = child.division
        alternate = LineMenuItem.objects.exclude(division=original).exclude(division=None).first()
        child.division = alternate.division
        child.save()
        call_command("load_org", verbosity=0)
        child.refresh_from_db()
        self.assertEqual(child.division_id, alternate.division_id)


class RegistrationRedesignTests(StaffChatTestCase):
    def test_other_registers_by_name_and_cannot_submit_withdrawal(self):
        from operations import identity, services
        from operations.contracts import Actor, CommandFailed, ErrorCode
        from operations.tests.test_m02_cases import withdrawal
        from operations.tests.test_m03_line import BOB

        self.type(BOB, "เริ่ม")
        self.type(BOB, "อื่น ๆ")
        reply = self.type(BOB, "ผู้แจ้ง ทดสอบ")
        self.assertIn("ลงทะเบียนแล้ว", texts(reply))
        requester = identity.resolve_requester("line", BOB)
        self.assertTrue(requester.is_external)
        with self.assertRaises(CommandFailed) as caught:
            services.submit_case(
                Actor.for_requester(requester),
                payload=withdrawal(),
                idempotency_key="external-other-1",
            )
        self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)
        reply = self.say(self.postback(BOB, "m=new"))
        self.assertNotIn("เบิก", buttons(reply))

    def test_verified_employee_is_linked_as_member_without_supervisor(self):
        from operations import identity
        from operations.models import ChannelIdentity, Employee, Role, RoleGrant

        employee = Employee.objects.get(employee_id="510002")
        employee.division = self.kae
        employee.save()
        row = ChannelIdentity.objects.create(channel="line", external_id="new-employee")
        original = identity.external_requester(row, "ชื่อก่อนยืนยัน")
        registration = identity.submit_registration(
            "line",
            row.external_id,
            employees.label(employee),
            "D01",
            employee=employee,
            verified_by="otp",
        )
        requester = identity.auto_approve_registration(registration)
        row.refresh_from_db()
        self.assertEqual(requester.pk, original.pk)
        self.assertFalse(requester.is_external)
        self.assertIsNotNone(row.staff_user_id)
        self.assertFalse(row.staff_user.has_usable_password())
        grants = RoleGrant.objects.filter(user=row.staff_user, revoked_at__isnull=True)
        self.assertEqual(
            list(grants.values_list("role", "division_id")), [(Role.STAFF, self.kae.pk)]
        )

    def test_employee_with_no_division_cannot_gain_membership(self):
        from operations import identity
        from operations.models import ChannelIdentity, Employee, RegistrationRequest

        employee = Employee.objects.get(employee_id="510002")
        employee.division = None
        employee.save()
        row = ChannelIdentity.objects.create(channel="line", external_id="unplaced-employee")
        registration = identity.submit_registration(
            "line",
            row.external_id,
            employees.label(employee),
            "D01",
            employee=employee,
            verified_by="otp",
        )
        requester = identity.auto_approve_registration(registration)
        self.assertFalse(requester.is_external)
        self.assertEqual(identity.auto_approve_registration(registration).pk, requester.pk)
        registration.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual(registration.status, RegistrationRequest.Status.APPROVED)
        self.assertIsNone(row.staff_user_id)


class ServiceMenuFlowTests(MenuTestCase):
    def setUp(self):
        super().setUp()
        from operations import line_menu_catalog

        line_menu_catalog.sync()

    def test_meter_withdrawal_uses_its_division_and_category(self):
        self.open("Meter, CT, VT")
        self.type(ALICE, "เบิกอุปกรณ์")
        draft = ConversationDraft.objects.get(status="open")
        self.assertEqual(draft.kind, Case.Kind.WITHDRAWAL)
        self.assertEqual(draft.data["division"], "กคว.")
        self.assertEqual(draft.step, "meter_group")
        self.type(ALICE, "มิเตอร์")
        draft.refresh_from_db()
        self.assertEqual(draft.data["line"]["category"], "meter")

    def test_every_equipment_service_uses_the_same_four_groups(self):
        from operations import line_menu_catalog

        line_menu_catalog.sync()
        for label in ("Meter, CT, VT", "หม้อแปลง LBS RMU", "เครื่องมือและอุปกรณ์"):
            with self.subTest(label=label):
                self.open(label)
                reply = self.press(ALICE, "เบิกอุปกรณ์")
                self.assertEqual(list(buttons(reply))[:4], ["มิเตอร์", "CT", "VT", "อุปกรณ์อื่น"])
                self.press(ALICE, "ยกเลิกร่าง")

    def test_meter_groups_click_type_search_and_back(self):
        from operations.models import EquipmentType

        for group in ("CT", "VT"):
            EquipmentType.objects.create(
                code=f"{group}_TEST",
                name=f"{group} ทดสอบ",
                line_group=group.lower(),
                category="transformer",
                unit="เครื่อง",
            )
        self.open("Meter, CT, VT")
        reply = self.type(ALICE, "ส่งคืนอุปกรณ์")
        self.assertEqual(list(buttons(reply))[:4], ["มิเตอร์", "CT", "VT", "อุปกรณ์อื่น"])
        reply = self.press(ALICE, "CT")
        self.assertIn("CT ทดสอบ", buttons(reply))
        self.assertNotIn("VT ทดสอบ", buttons(reply))
        self.press(ALICE, "ย้อนกลับ")
        reply = self.type(ALICE, "๓")
        self.assertIn("VT ทดสอบ", buttons(reply))
        self.press(ALICE, "ย้อนกลับ")
        reply = self.type(ALICE, "ct")
        self.assertIn("CT ทดสอบ", buttons(reply))
        self.press(ALICE, "CT ทดสอบ")
        self.type(ALICE, "2")
        draft = ConversationDraft.objects.get(status="open")
        self.assertEqual(draft.data["division"], "กคว.")
        self.assertEqual(draft.data["lines"][0]["item_code"], "CT_TEST")

    def test_missing_group_does_not_offer_unrelated_material(self):
        self.open("Meter, CT, VT")
        self.type(ALICE, "เบิกอุปกรณ์")
        reply = self.type(ALICE, "CT")
        self.assertIn("ยังไม่มีรายการที่ใช้งานได้", texts(reply))
        self.assertEqual(list(buttons(reply)), ["ย้อนกลับ", "หน้าหลัก", "ยกเลิกร่าง"])

    def test_group_filter_is_kept_when_catalogue_requires_search(self):
        from operations.models import EquipmentType

        for number in range(14):
            EquipmentType.objects.create(
                code=f"CT_{number}",
                name=f"CT รุ่น {number}",
                line_group="ct",
                category="meter",
                unit="เครื่อง",
            )
        EquipmentType.objects.create(
            code="VT_99", name="VT รุ่น 99", line_group="vt", category="meter", unit="เครื่อง"
        )
        self.open("Meter, CT, VT")
        self.type(ALICE, "เบิกอุปกรณ์")
        self.type(ALICE, "CT")
        self.assertEqual(ConversationDraft.objects.get(status="open").step, "item_search")
        reply = self.type(ALICE, "99")
        self.assertNotIn("VT รุ่น 99", buttons(reply))
        reply = self.type(ALICE, "CT_13")
        self.assertIn("CT รุ่น 13", buttons(reply))

    def test_usage_menu_has_three_report_types_and_own_tracking(self):
        reply = self.open("ตรวจสอบการใช้ไฟฟ้า")
        self.assertEqual(
            set(buttons(reply)),
            {
                "แจ้งละเมิดใช้ไฟฟ้า",
                "ตรวจสอบค่าไฟแพง",
                "แจ้งมิเตอร์ชำรุด",
                "ติดตามเรื่อง",
            },
        )
        self.type(ALICE, "แจ้งมิเตอร์ชำรุด")
        draft = ConversationDraft.objects.get(status="open")
        self.assertEqual(draft.kind, Case.Kind.PROBLEM)
        self.assertEqual(draft.data["division"], "กตฟ.")
        self.assertEqual(draft.data["topic"], Case.Topic.USAGE)

    def test_rich_menu_label_can_be_typed_without_a_prior_suggestion(self):
        reply = self.type(ALICE, "ตรวจสอบการใช้ไฟฟ้า")
        self.assertIn("แจ้งมิเตอร์ชำรุด", buttons(reply))
        self.type(ALICE, "แจ้งมิเตอร์ชำรุด")
        self.assertEqual(ConversationDraft.objects.get(status="open").data["division"], "กตฟ.")

    def test_anonymous_cannot_open_equipment_options_by_typing_a_tile(self):
        from operations.tests.test_m03_line import BOB

        self.say(self.event(BOB, "follow"))
        self.type(BOB, "ไม่ระบุตัวตน")
        reply = self.type(BOB, "Meter, CT, VT")
        self.assertNotIn("เบิกอุปกรณ์", buttons(reply))
        self.assertNotIn("ส่งคืนอุปกรณ์", buttons(reply))
        self.assertIn("เฉพาะปัญหา", texts(reply))
