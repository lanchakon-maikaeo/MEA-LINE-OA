"""Clickable LINE navigation and locally configurable material groups (18 Oct 2026)."""

import re
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, override_settings

from operations import conversation, registry, services, staff_orders_chat, work_orders
from operations.contracts import Actor
from operations.models import (
    Attachment,
    Case,
    ChannelIdentity,
    EquipmentType,
    RegistrationRequest,
    Role,
    RoleGrant,
    WorkOrder,
)
from operations.tests import test_m11_registry as registry_tests
from operations.tests import test_m13_admin_line as admin_tests
from operations.tests import test_m13_line_menu as menu_tests
from operations.tests import test_m13_staff_chat as staff_tests
from operations.tests.test_m02_cases import make_staff, withdrawal
from operations.tests.test_m03_line import ALICE, BOB, buttons, texts

NUMBER = re.compile(r"\d{2}-\d{6}$")  # a case or order number button (ADR 0045)


@override_settings(LINE_TYPED_SLIPS=True)  # ADR 0032: the earlier typed intake
class TrackingTests(menu_tests.MenuTestCase):
    def create_case(self, requester, key):
        result = services.submit_case(
            Actor.for_requester(requester), idempotency_key=f"tracking-{key}", payload=withdrawal()
        )
        return Case.objects.get(pk=result.case_id)

    def test_pagination_filters_and_old_buttons_never_show_other_requesters(self):
        mine = [self.create_case(self.requester, f"mine-{n}") for n in range(7)]
        Case.objects.filter(pk=mine[0].pk).update(status=Case.Status.CLOSED)
        other = self.create_case(self.approve(BOB), "other")
        first = self.type(ALICE, "เรื่องของฉัน")
        self.assertIn("6 เรื่อง", texts(first))
        self.assertEqual(sum(bool(NUMBER.search(label)) for label in buttons(first)), 5)
        second = self.press(ALICE, "ถัดไป")
        self.assertEqual(sum(bool(NUMBER.search(label)) for label in buttons(second)), 1)
        self.assertNotIn(other.number, texts(first) + texts(second))
        closed = self.press(ALICE, "เรื่องที่จบแล้ว")
        self.assertIn(mine[0].number, buttons(closed))
        self.assertIn("1 เรื่อง", texts(closed))
        refused = self.say(self.postback(BOB, buttons(first)["ถัดไป"]))
        self.assertNotIn(mine[1].number, texts(refused))
        self.type(ALICE, "เรื่องของฉัน")
        self.press(ALICE, "ค้นหาเลขเรื่อง")
        self.assertIn("ไม่พบเรื่อง", texts(self.type(ALICE, other.number)))

    def test_explicit_information_target_keeps_file_on_selected_case(self):
        first = self.create_case(self.requester, "first")
        second = self.create_case(self.requester, "second")
        owner = make_staff("info-owner", (Role.STAFF, first.owner_division.code))
        for case in (first, second):
            for action, payload in [
                ("start_triage", {}),
                ("request_info", {"reason": "ขอรูปอุปกรณ์"}),
            ]:
                case.refresh_from_db()
                services.perform_action(
                    Actor.staff(owner),
                    case.pk,
                    action,
                    expected_revision=case.revision,
                    idempotency_key=f"info-{case.pk}-{action}",
                    payload=payload,
                )
        self.say(self.postback(ALICE, f"c={first.number}"))
        self.press(ALICE, "แนบหลักฐาน")
        self.say(self.image(ALICE))
        self.assertEqual(Attachment.objects.get().case_id, first.pk)

    def test_draft_back_button_replaces_answer_and_keeps_draft_navigation(self):
        self.say(self.postback(ALICE, "m=new"))
        self.press(ALICE, "เบิก")
        self.press(ALICE, "มิเตอร์")
        self.press(ALICE, "MTR_SYN_A")
        self.type(ALICE, "1")
        self.press(ALICE, "ไปต่อ")
        self.type(ALICE, "วัตถุประสงค์เดิม")
        self.press(ALICE, "ย้อนกลับ")
        self.type(ALICE, "วัตถุประสงค์ใหม่")
        summary = self.press(ALICE, "ข้าม")
        self.assertIn("วัตถุประสงค์ใหม่", texts(summary))
        self.assertNotIn("วัตถุประสงค์เดิม", texts(summary))
        # 25 Oct 2569 Q2: "หน้าหลัก" is the rich menu's; the draft keeps ยกเลิกร่าง.
        self.assertNotIn("หน้าหลัก", buttons(summary))
        self.assertIn("ยกเลิกร่าง", buttons(summary))


@override_settings(LINE_TYPED_SLIPS=True)  # ADR 0032: the earlier typed intake
class SimpleServiceTests(menu_tests.MenuTestCase):
    def test_generic_withdrawal_entry_also_uses_four_groups(self):
        self.say(self.postback(ALICE, "m=new"))
        reply = self.press(ALICE, "เบิก")
        self.assertEqual(list(buttons(reply))[:4], ["มิเตอร์", "CT", "VT", "อุปกรณ์อื่น"])

    def test_equipment_shortcuts_use_four_groups_and_keep_registry_routing(self):
        item = EquipmentType.objects.get(code="TRF_SYN_A")
        item.line_group = "ct"
        item.save()
        reply = self.type(ALICE, "เบิกอุปกรณ์")
        self.assertEqual(list(buttons(reply))[:4], ["มิเตอร์", "CT", "VT", "อุปกรณ์อื่น"])
        draft = conversation.ConversationDraft.objects.get(status="open")
        self.assertEqual(draft.kind, "withdrawal")
        self.assertNotIn("division", draft.data)
        self.press(ALICE, "CT")
        self.press(ALICE, item.name)
        self.type(ALICE, "1")
        self.press(ALICE, "ไปต่อ")
        self.type(ALICE, "ทดสอบการเบิก CT")
        self.press(ALICE, "ข้าม")
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        self.assertEqual(Case.objects.get().owner_division.code, "กองอุปกรณ์ฯ")

    def test_other_includes_unmapped_transformers_and_return_accepts_typed_group(self):
        reply = self.say(self.postback(ALICE, "m=equipment&kind=return"))
        self.assertIn("เลือกหมวดอุปกรณ์", texts(reply))
        choices = self.type(ALICE, "๔")
        self.assertIn(EquipmentType.objects.get(code="TRF_SYN_A").name, buttons(choices))
        draft = conversation.ConversationDraft.objects.get(status="open")
        self.assertEqual(draft.kind, "return")

    def test_external_cannot_forge_equipment_but_can_report_system_problems(self):
        from operations import identity, line_menu_catalog

        line_menu_catalog.sync()
        line = ChannelIdentity.objects.create(channel="line", external_id=BOB)
        identity.external_requester(line, "ผู้แจ้งภายนอก")
        refused = self.say(self.postback(BOB, "m=equipment&kind=withdrawal"))
        self.assertIn("ยืนยันตัวตนพนักงาน", texts(refused))
        self.assertFalse(conversation.ConversationDraft.objects.filter(identity=line).exists())
        # Typing แจ้งปัญหา opens the division carousel (ADR 0030); the system card still works.
        self.say(self.postback(BOB, "m=problem&topic=system"))
        self.press(BOB, "ปัญหาการจัดทำบิล")
        draft = conversation.ConversationDraft.objects.get(identity=line, status="open")
        self.assertEqual((draft.kind, draft.data["topic"]), ("problem", "system"))

    def test_equipment_problem_uses_four_groups_and_actual_item_topic(self):
        reply = self.say(self.postback(ALICE, "m=problem&topic=equipment"))
        self.assertEqual(list(buttons(reply))[:4], ["มิเตอร์", "CT", "VT", "อุปกรณ์อื่น"])
        self.press(ALICE, "อุปกรณ์อื่น")
        self.press(ALICE, EquipmentType.objects.get(code="TRF_SYN_A").name)
        draft = conversation.ConversationDraft.objects.get(status="open")
        self.assertEqual((draft.kind, draft.data["topic"]), ("problem", "transformer"))

    def test_anonymous_system_problem_keeps_anonymity_and_goes_to_the_summary(self):
        from operations import identity, line_menu_catalog, options

        line_menu_catalog.sync()
        options.change(None, options.ANONYMOUS, "on", "ทดสอบ")
        line = ChannelIdentity.objects.create(channel="line", external_id=BOB)
        identity.anonymous_requester(line)
        self.say(self.postback(BOB, "m=problem&topic=system"))
        self.press(BOB, "ปัญหาการจัดทำบิล")
        self.type(BOB, "แจ้งปัญหาระบบจัดทำบิล ทดสอบ")
        self.press(BOB, "ข้าม")
        draft = conversation.ConversationDraft.objects.get(identity=line, status="open")
        self.assertTrue(draft.data["anonymous"])
        self.assertTrue(draft.requester.is_anonymous)
        self.assertEqual(draft.step, "summary")  # no location step (31 ต.ค. 2569)


class MaterialGroupTests(registry_tests.RegistryTestCase):
    def test_admin_maps_menu_group_without_sap_and_cannot_change_registry_category(self):
        item = EquipmentType.objects.get(code="TRF_SYN_A")
        item.official_code = ""
        item.save()
        client = Client()
        client.force_login(self.admin)
        response = client.post(
            "/console/admin/items/",
            {"action": "codes", f"group-{item.pk}": "ct", f"code-{item.pk}": ""},
        )
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(
            (item.line_group, item.category, item.official_code), ("ct", "transformer", "")
        )
        self.assertIn(item, conversation._items("meter", "ct"))
        self.assertNotIn(item, conversation._items("meter", "vt"))
        self.assertContains(client.get("/console/admin/items/"), "หมวดเมนู LINE")
        with self.assertRaises(PermissionDenied):
            registry.set_line_group(self.reg, item, "vt")
        with self.assertRaises(ValidationError):
            registry.set_line_group(self.admin, item, "invented")

    def test_invalid_bulk_mapping_rolls_back_earlier_changes(self):
        items = list(EquipmentType.objects.order_by("code")[:2])
        client = Client()
        client.force_login(self.admin)
        client.post(
            "/console/admin/items/",
            {"action": "codes", f"group-{items[0].pk}": "ct", f"group-{items[1].pk}": "invalid"},
        )
        items[0].refresh_from_db()
        self.assertEqual(items[0].line_group, "")


class AdminButtonsTests(admin_tests.AdminLineTests):
    def test_more_menu_does_not_include_deferred_admin_controls(self):
        self.link(self.admin, ALICE)
        self.link(self.head, BOB)
        choices = buttons(self.say(self.postback(ALICE, "m=roles")))
        self.assertEqual(set(choices), {"สั่งงาน"})  # ADR 0041: แอดมินระบบ orders work too
        self.assertNotIn("แอดมิน", buttons(self.say(self.postback(BOB, "m=roles"))))

    def test_approval_button_and_role_wizard_require_current_admin_rights(self):
        self.link(self.admin, ALICE)
        registration = self.pending()
        self.type(ALICE, "แอดมิน")
        self.press(ALICE, "คำขอรอรับรอง")
        self.press(ALICE, f"คำขอ #{registration.pk}")
        self.press(ALICE, "รับรอง")
        registration.refresh_from_db()
        self.assertEqual(registration.status, RegistrationRequest.Status.APPROVED)
        requester = self.verified_employee()
        self.type(ALICE, f"ดูผู้ใช้ {requester.pk}")
        self.press(ALICE, "เพิ่มสิทธิ์")
        choices = self.press(ALICE, "แอดมินกอง")
        while "กองอุปกรณ์ฯ" not in buttons(choices):
            choices = self.press(ALICE, "ถัดไป")
        summary = self.press(ALICE, "กองอุปกรณ์ฯ")
        self.assertIn("ยืนยัน", buttons(summary))
        self.press(ALICE, "ยืนยัน")
        grant = RoleGrant.objects.get(
            user=requester.channelidentity_set.get().staff_user, role="supervisor", revoked_at=None
        )
        self.type(ALICE, f"ดูผู้ใช้ {requester.pk}")
        self.press(ALICE, f"ถอนสิทธิ์ #{grant.pk}")
        self.press(ALICE, "ยืนยัน")
        grant.refresh_from_db()
        self.assertIsNotNone(grant.revoked_at)
        self.say(self.postback(ALICE, buttons(summary)["ยืนยัน"]))
        grant.refresh_from_db()
        self.assertIsNotNone(grant.revoked_at)

    def test_revoked_admin_cannot_use_old_approval_button(self):
        from django.utils import timezone

        self.link(self.admin, ALICE)
        registration = self.pending()
        self.type(ALICE, f"ดูคำขอ {registration.pk}")
        RoleGrant.objects.filter(user=self.admin, role=Role.ADMIN).update(revoked_at=timezone.now())
        self.press(ALICE, "รับรอง")
        registration.refresh_from_db()
        self.assertEqual(registration.status, RegistrationRequest.Status.PENDING)


class StaffButtonsTests(staff_tests.StaffChatTestCase):
    def test_rich_menu_shortcuts_open_personal_cases_and_the_current_roles_menu(self):
        self.link(self.head, ALICE)
        roles = self.say(self.postback(ALICE, "m=roles"))
        self.assertEqual(buttons(roles), {})
        tracking = self.say(self.postback(ALICE, "m=mine"))
        self.assertIn("เรื่องที่ยังไม่จบ", texts(tracking))
        self.assertNotIn("งานที่ฉันสั่ง", buttons(tracking))
        self.assertIn("เรื่องที่ยังไม่จบ", texts(self.type(ALICE, "ติดตามเรื่องของฉัน")))
        self.assertEqual(buttons(self.type(ALICE, "เมนูตามสิทธิ์")), {})

    def test_personal_tracking_hides_other_staff_reports_in_the_same_division(self):
        member = make_staff("personal-member", (Role.STAFF, self.kae.code))
        cases = {}
        for user, line_id in ((self.head, ALICE), (member, BOB)):
            self.link(user, line_id)
            self.say(self.postback(line_id, "m=mine"))
            requester = ChannelIdentity.objects.get(external_id=line_id).requester
            result = services.submit_case(
                Actor.for_requester(requester),
                idempotency_key=f"personal-{user.pk}",
                payload={
                    "kind": "problem",
                    "topic": "suggestion",
                    "description": "ข้อเสนอแนะจำลองของเจ้าหน้าที่",
                    "division_code": self.kae.code,
                },
            )
            cases[line_id] = Case.objects.get(pk=result.case_id)

        mine = self.say(self.postback(ALICE, "m=mine"))
        self.assertIn(cases[ALICE].number, buttons(mine))
        self.assertNotIn(cases[BOB].number, texts(mine))
        self.assertNotIn(cases[BOB].number, buttons(mine))

    def test_staff_order_list_pages_and_supervisor_cancel_button(self):
        self.link(self.director, ALICE)
        orders = [
            work_orders.create(self.director, f"งาน {n}", self.kae, self.head) for n in range(7)
        ]
        self.type(ALICE, "งานของกอง")
        first = self.press(ALICE, "งานที่ฉันสั่ง")
        self.assertEqual(sum(bool(NUMBER.search(label)) for label in buttons(first)), 5)
        second = self.press(ALICE, "ถัดไป")
        self.assertEqual(sum(bool(NUMBER.search(label)) for label in buttons(second)), 2)
        self.assertIn(orders[-1].number, texts(first) + texts(second))
        menu = self.say(self.postback(ALICE, "m=roles"))
        self.press(ALICE, "สั่งงาน", menu)
        with patch("operations.staff_orders_chat.extract", return_value=None):
            summary = self.type(ALICE, "ให้ทั้งกอง กองอุปกรณ์ฯ ตรวจมิเตอร์")
        self.press(ALICE, "ยกเลิก")
        self.say(self.postback(ALICE, buttons(summary)["ยืนยัน"]))
        self.assertEqual(WorkOrder.objects.count(), 7)

    def test_supervisor_picks_recipient_then_confirms(self):
        self.link(self.director, ALICE)
        self.type(ALICE, "สั่งงาน")
        with patch("operations.staff_orders_chat.extract", return_value=None):
            self.type(ALICE, "ตรวจมิเตอร์")
            choices = self.press(ALICE, "เลือกผู้รับ")
            self.press(ALICE, "กองอุปกรณ์ฯ", choices)
            summary = self.press(ALICE, staff_orders_chat.who(self.head, self.kae))
        self.assertIn("ยืนยัน", buttons(summary))
        self.press(ALICE, "ยืนยัน")
        self.assertEqual(WorkOrder.objects.get().assignee, self.head)

    def test_assign_person_pages_and_cancellation_without_reason(self):
        self.link(self.head, ALICE)
        people = [make_staff(f"member-{n}", (Role.STAFF, self.kae.code)) for n in range(7)]
        order = work_orders.create(self.director, "ตรวจอุปกรณ์", self.kae)
        self.type(ALICE, order.number)
        reply = self.press(ALICE, "มอบหมายผู้รับผิดชอบ")
        label = f"{people[-1].pk} {work_orders.name_of(people[-1])}"[:20]
        while label not in buttons(reply):
            reply = self.press(ALICE, "ถัดไป")
        self.press(ALICE, label)
        order.refresh_from_db()
        self.assertEqual(order.assignee, people[-1])
        self.press(ALICE, "ยกเลิกงาน")
        self.press(ALICE, "ไม่ระบุเหตุผล")
        order.refresh_from_db()
        self.assertEqual(order.status, WorkOrder.Status.CANCELLED)
        self.assertEqual(order.reason, "")
