from unittest.mock import patch

from django.test import Client
from django.urls import reverse

from operations import identity, work_orders
from operations.models import LineMenuItem, Role, WorkOrder
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import ALICE, buttons, texts
from operations.tests.test_m13_staff_chat import StaffChatTestCase


class TextOrderTests(StaffChatTestCase):
    def setUp(self):
        super().setUp()
        self.link(self.director, ALICE)

    def proposal(self, **changes):
        return {
            "title": "ตรวจมิเตอร์",
            "recipient": "Kaehead",
            "division": "กองอุปกรณ์ฯ",
            "due": "",
            "urgent": False,
            **changes,
        }

    def test_two_line_orders_appear_on_web_for_issuer_recipient_and_admin(self):
        titles = ("สั่งงาน ศอค", "สั่งงานมีเครื่องวัดชำรุด")
        for title in titles:
            self.type(ALICE, "สั่งงาน")
            with patch(
                "operations.staff_orders_chat.extract", return_value=self.proposal(title=title)
            ):
                self.type(ALICE, f"ให้ Kaehead กองอุปกรณ์ฯ {title}")
            self.type(ALICE, "ยืนยัน")
        orders = list(WorkOrder.objects.order_by("pk"))
        self.assertEqual([order.title for order in orders], list(titles))
        self.assertEqual({order.channel for order in orders}, {WorkOrder.Channel.LINE})
        recipient = make_staff("recipient", (Role.STAFF, self.kae.code))
        for order in orders:
            work_orders.act(self.head, order, "assign_person", assignee=recipient)
        for user in (self.director, recipient, self.admin):
            with self.subTest(user=user.username):
                client = Client()
                client.force_login(user)
                page = client.get(reverse("division-detail", args=[self.kae.code]))
                self.assertContains(page, "ผ่าน LINE", count=2)
                self.assertContains(page, "สั่งโดย Director", count=2)
                if user == self.admin:
                    self.assertContains(client.get(reverse("queue-all")), orders[0].number)
                for order in orders:
                    self.assertContains(page, order.number)
                    self.assertContains(page, order.title)
                    detail = client.get(reverse("order-detail", args=[order.number]))
                    self.assertContains(detail, order.title)

    def test_ai_proposal_is_only_created_after_confirmation(self):
        with patch("operations.staff_orders_chat.extract", return_value=self.proposal()):
            summary = self.type(ALICE, "ให้ Kaehead กองอุปกรณ์ฯ ตรวจมิเตอร์")
        self.assertIn("ไม่กำหนดวัน", texts(summary))
        self.assertFalse(WorkOrder.objects.exists())
        self.type(ALICE, "ยืนยัน")
        order = WorkOrder.objects.get()
        self.assertEqual(order.assignee, self.head)
        self.assertEqual(order.title, "ตรวจมิเตอร์")
        self.assertIsNone(order.due_on)
        self.say(self.postback(ALICE, buttons(summary)["ยืนยัน"]))
        self.assertEqual(WorkOrder.objects.count(), 1)

    def test_ai_cannot_invent_a_recipient_or_use_another_division(self):
        self.tile(LineMenuItem.Action.ORDER_NEW)
        with patch("operations.staff_orders_chat.extract", return_value=self.proposal()):
            reply = self.type(ALICE, "ตรวจมิเตอร์")
        self.assertNotIn("ยืนยัน", buttons(reply))
        with patch(
            "operations.staff_orders_chat.extract", return_value=self.proposal(division="กคว.")
        ):
            reply = self.type(ALICE, "ให้ Kaehead กคว. ตรวจมิเตอร์")
        self.assertNotIn("ยืนยัน", buttons(reply))
        self.type(ALICE, "ยืนยัน")
        self.assertFalse(WorkOrder.objects.exists())

    def test_revoking_supervisor_invalidates_confirmation(self):
        with patch("operations.staff_orders_chat.extract", return_value=self.proposal()):
            summary = self.type(ALICE, "ให้ Kaehead กองอุปกรณ์ฯ ตรวจมิเตอร์")
        # ADR 0041: the new role replaces แอดมินฝ่าย, so the old summary cannot issue.
        identity.set_role(self.admin, self.director, Role.SUPERVISOR, self.kae, "คงสิทธิ์กอง")
        self.say(self.postback(ALICE, buttons(summary)["ยืนยัน"]))
        self.assertFalse(WorkOrder.objects.exists())

    def test_local_edits_replace_date_urgency_and_title_before_confirmation(self):
        with patch("operations.staff_orders_chat.extract", return_value=None):
            old = self.type(ALICE, "ให้ทั้งกอง กองอุปกรณ์ฯ ตรวจมิเตอร์พรุ่งนี้ ด่วน")
            self.type(ALICE, "แก้งาน ตรวจตู้มิเตอร์")
            self.type(ALICE, "ไม่กำหนดวัน")
            summary = self.type(ALICE, "ปกติ")
        self.assertIn("ไม่กำหนดวัน", texts(summary))
        self.assertIn("ความเร่งด่วน\nปกติ", texts(summary))
        self.say(self.postback(ALICE, buttons(old)["ยืนยัน"]))
        self.assertFalse(WorkOrder.objects.exists())
        self.say(self.postback(ALICE, buttons(summary)["ยืนยัน"]))
        order = WorkOrder.objects.get()
        self.assertEqual(order.title, "ตรวจตู้มิเตอร์")
        self.assertIsNone(order.due_on)
        self.assertFalse(order.is_urgent)
