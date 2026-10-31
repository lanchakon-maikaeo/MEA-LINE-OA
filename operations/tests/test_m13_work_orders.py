"""งานสั่ง (16 ต.ค. 2569 Q6, Q7, Q14): only a Supervisor issues work; มอบหมาย → รับทราบ →
กำลังทำ → เสร็จ, with send-back and a Supervisor's close or cancel; the doer hears at assignment,
the issuer at รับทราบ, done or send-back, and reminders come once near and once past the due date.
"""

from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from operations import identity, options, work_orders
from operations.models import (
    ChannelIdentity,
    Division,
    NotificationDelivery,
    Role,
    WorkOrder,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m13_org import OrgTestCase

S = WorkOrder.Status
N = NotificationDelivery
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


@override_settings(STAFF_EMAIL_ENABLED=True)
class OrderTestCase(OrgTestCase):
    def setUp(self):
        super().setUp()
        self.kcw_division = Division.objects.get(code="กคว.")
        self.ktf_division = Division.objects.get(code="กตฟ.")
        self.director = make_staff("director")
        identity.grant_role(self.admin, self.director, Role.DEPARTMENT_SUPERVISOR, None, "ผอ.")
        self.head = make_staff("head")
        identity.grant_role(self.admin, self.head, Role.SUPERVISOR, self.kcw_division, "หัวหน้า")
        for user in (self.director, self.head, self.kcw, self.kcw2, self.ktf):
            user.email = f"{user.username}@example.test"
            user.save(update_fields=["email"])

    def notices(self, kind, user=None):
        rows = N.objects.filter(kind=kind)
        return rows.filter(recipient_user=user) if user else rows

    def order(self, **extra):
        return work_orders.create(
            self.director, "ตรวจนับมิเตอร์คงคลัง (ทดสอบ)", self.kcw_division, **extra
        )


class IssueTests(OrderTestCase):
    def test_only_department_supervisors_issue_orders(self):
        with self.assertRaises(PermissionDenied):
            work_orders.create(self.kcw, "งานทดสอบ", self.kcw_division)
        with self.assertRaises(PermissionDenied):
            work_orders.create(self.head, "งานทดสอบ", self.ktf_division)
        with self.assertRaises(PermissionDenied):
            work_orders.create(self.head, "งานทดสอบ", self.kcw_division)
        order = work_orders.create(self.director, "งานทดสอบ", self.kcw_division)
        self.assertRegex(order.number, r"^T\d\d-000001$")
        other = work_orders.create(self.director, "งานทดสอบ", self.ktf_division)
        self.assertEqual(other.number[-6:], "000002")
        with self.assertRaises(ValidationError):
            work_orders.create(self.director, "งานทดสอบ", self.kcw_division, assignee=self.ktf)
        with self.assertRaises(ValidationError):
            work_orders.create(self.director, "  ", self.kcw_division)
        with self.assertRaises(ValidationError):
            work_orders.create(self.director, "งานทดสอบ", self.kcw_division, channel="unknown")

    def test_web_creation_is_department_only_for_get_and_post(self):
        for user in (self.head, self.kcw, self.admin):
            with self.subTest(user=user.username):
                client = Client()
                client.force_login(user)
                self.assertEqual(client.get(reverse("order-new")).status_code, 403)
                page = client.get(reverse("division-detail", args=[self.kcw_division.code]))
                self.assertNotContains(page, "สั่งงานถึงกองนี้")
                response = client.post(
                    reverse("order-new"),
                    {"title": "งานที่ไม่มีสิทธิ์", "target": f"d:{self.kcw_division.pk}"},
                )
                self.assertEqual(response.status_code, 403)
        self.assertFalse(WorkOrder.objects.exists())

    def test_revoked_department_role_cannot_use_a_stale_user_to_create(self):
        from operations.models import RoleGrant

        self.assertTrue(work_orders.may_issue(self.director))
        grant = RoleGrant.objects.get(user=self.director, role=Role.DEPARTMENT_SUPERVISOR)
        identity.revoke_role(self.admin, grant, "ถอนสิทธิ์ทดสอบ")
        self.assertFalse(work_orders.may_issue(self.director))
        with self.assertRaises(PermissionDenied):
            work_orders.create(self.director, "งานจากร่างเดิม", self.kcw_division)
        self.assertFalse(WorkOrder.objects.exists())

    def test_division_work_goes_to_its_supervisor_and_person_work_to_the_person(self):
        self.order()
        self.assertEqual({d.recipient_user for d in self.notices("staff_order_new")}, {self.head})
        N.objects.all().delete()
        self.order(assignee=self.kcw2)
        self.assertEqual({d.recipient_user for d in self.notices("staff_order_new")}, {self.kcw2})
        N.objects.all().delete()
        options.change(self.admin, options.STAFF_NOTIFY, "division", "ให้ทุกคนรู้")
        self.order()
        self.assertEqual(
            {d.recipient_user for d in self.notices("staff_order_new")},
            {self.head, self.kcw, self.kcw2},
        )

    def test_a_linked_line_gets_the_message_too(self):
        ChannelIdentity.objects.create(
            channel="line", external_id="U" + "c" * 32, staff_user=self.kcw2, is_following=True
        )
        order = self.order(assignee=self.kcw2)
        self.assertEqual(order.channel, WorkOrder.Channel.WEB)
        line = self.notices("staff_order_new", self.kcw2).get(channel=N.Channel.LINE_PUSH)
        self.assertIn(order.number, line.text)
        self.assertIn(f"/orders/{order.number}/", line.text)


class FlowTests(OrderTestCase):
    def test_acknowledge_start_finish_and_close(self):
        order = self.order()
        self.assertEqual(dict(work_orders.steps_for(self.ktf, order)), {})  # another division
        work_orders.act(self.kcw, order, "acknowledge")
        self.assertEqual(self.notices("staff_order_ack", self.director).count(), 1)
        work_orders.act(self.kcw, order, "start")
        self.assertEqual(self.notices("staff_order_ack").count(), 1)  # told once
        with self.assertRaises(ValidationError):
            work_orders.act(self.kcw, order, "done")
        order = work_orders.act(self.kcw, order, "done", result="นับครบ 120 ตัว")
        self.assertEqual((order.status, order.result), (S.DONE, "นับครบ 120 ตัว"))
        self.assertEqual(self.notices("staff_order_done", self.director).count(), 1)
        with self.assertRaises(PermissionDenied):
            work_orders.act(self.kcw, order, "close")
        order = work_orders.act(self.director, order, "close")
        self.assertEqual(order.status, S.CLOSED)
        self.assertEqual(
            [row["what"] for row in work_orders.timeline(order)],
            ["สั่งงาน", "รับทราบ", "เริ่มทำ", "ทำเสร็จแล้ว", "ปิดงาน"],
        )

    def test_only_the_named_person_acts_on_their_order(self):
        order = self.order(assignee=self.kcw2)
        with self.assertRaises(PermissionDenied):
            work_orders.act(self.kcw, order, "acknowledge")
        work_orders.act(self.kcw2, order, "acknowledge")

    def test_the_division_supervisor_names_a_person(self):
        order = self.order()
        order = work_orders.act(self.head, order, "assign_person", assignee=self.kcw)
        self.assertEqual(order.assignee, self.kcw)
        self.assertEqual(self.notices("staff_order_new", self.kcw).count(), 1)

    def test_sent_back_work_is_given_again_or_cancelled(self):
        order = self.order(assignee=self.kcw)
        order = work_orders.act(self.kcw, order, "send_back", reason="งานนี้เป็นของ กตฟ.")
        self.assertEqual(order.status, S.SENT_BACK)
        self.assertIn("งานนี้เป็นของ กตฟ.", self.notices("staff_order_back", self.director).get().text)
        order = work_orders.act(
            self.director, order, "reassign", division=self.ktf_division, assignee=None
        )
        self.assertEqual(
            (order.status, order.division, order.assignee), (S.ASSIGNED, self.ktf_division, None)
        )
        self.assertEqual(self.notices("staff_order_new", self.ktf).count(), 1)
        order = work_orders.act(self.director, order, "cancel")
        self.assertEqual(order.status, S.CANCELLED)


class ReminderTests(OrderTestCase):
    def test_one_reminder_near_the_due_date_and_one_after_it(self):
        today = timezone.localdate()
        order = self.order(assignee=self.kcw, due_on=today + timedelta(days=1))
        self.assertEqual(work_orders.remind(), 1)
        self.assertEqual(work_orders.remind(), 0)
        self.assertEqual(self.notices("staff_order_soon", self.kcw).count(), 1)
        WorkOrder.objects.filter(pk=order.pk).update(due_on=today - timedelta(days=1))
        self.assertEqual(work_orders.remind(), 1)
        self.assertEqual(
            {d.recipient_user for d in self.notices("staff_order_late")}, {self.kcw, self.director}
        )
        self.assertEqual(work_orders.remind(), 0)

    def test_a_finished_order_is_not_reminded_and_its_notice_is_skipped(self):
        order = self.order(assignee=self.kcw, due_on=timezone.localdate())
        work_orders.act(self.director, order, "cancel", reason="ไม่ต้องทำ")
        self.assertEqual(work_orders.remind(), 0)
        delivery = self.notices("staff_order_new", self.kcw).get()
        self.assertEqual(work_orders.recipient_problem(delivery), "order ended")


class WebTests(OrderTestCase):
    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client

    def test_orders_use_normal_scoped_queues_and_legacy_list_redirects(self):
        received = self.order(assignee=self.kcw)
        other = work_orders.create(self.director, "ตรวจมิเตอร์ชำรุด", self.ktf_division)
        ended = self.order()
        work_orders.act(self.director, ended, "cancel")
        for user in (self.admin, self.director, self.head, self.kcw):
            with self.subTest(user=user.username):
                client = self.client_for(user)
                page = client.get(reverse("division-detail", args=[self.kcw_division.code]))
                self.assertEqual(page.context["tab"], "host")
                self.assertContains(page, received.number)
                self.assertNotContains(page, ended.number)
                self.assertNotContains(page, other.number)
                if user not in (self.admin, self.director):
                    self.assertEqual(
                        client.get(reverse("order-detail", args=[other.number])).status_code, 404
                    )
                self.assertNotContains(page, 'href="/orders/"')
        admin = self.client_for(self.admin)
        self.assertRedirects(admin.get(reverse("orders")), reverse("queue-all") + "?tab=host")
        self.assertContains(admin.get(reverse("queue-all")), other.number)
        personal = self.client_for(self.director).get(
            reverse("division-detail", args=[self.kcw_division.code]), {"tab": "mine"}
        )
        self.assertNotContains(personal, received.number)
        closed = admin.get(reverse("queue-all"), {"tab": "closed"})
        self.assertContains(closed, ended.number)

    def test_a_supervisor_issues_with_a_file_and_the_member_finishes_it(self):
        director = self.client_for(self.director)
        page = director.get(reverse("order-new"))
        self.assertContains(page, f'value="d:{self.kcw_division.pk}"')
        self.assertNotContains(
            self.client_for(self.kcw).get(reverse("order-new")), "สั่งถึง", status_code=403
        )
        response = director.post(
            reverse("order-new"),
            {
                "title": "ส่งรายงานคงคลัง (ทดสอบ)",
                "target": f"u:{self.kcw_division.pk}:{self.kcw.pk}",
                "due_on": (timezone.localdate() + timedelta(days=3)).isoformat(),
                "urgent": "1",
                "files": SimpleUploadedFile("stock.png", PNG, content_type="image/png"),
            },
        )
        order = WorkOrder.objects.get()
        self.assertRedirects(response, reverse("order-detail", args=[order.number]))
        self.assertTrue(order.is_urgent)
        self.assertEqual(order.channel, WorkOrder.Channel.WEB)
        self.assertEqual(order.attachments.count(), 1)
        member = self.client_for(self.kcw)
        listing = member.get(reverse("division-detail", args=[self.kcw_division.code]))
        self.assertContains(listing, order.number)
        self.assertNotContains(listing, 'href="/orders/"')
        page = member.get(reverse("order-detail", args=[order.number]))
        self.assertContains(page, "รับทราบ")
        member.post(reverse("order-action", args=[order.number, "acknowledge"]))
        member.post(reverse("order-action", args=[order.number, "done"]), {"result": "ส่งแล้ว"})
        self.assertEqual(WorkOrder.objects.get().status, S.DONE)
        self.assertEqual(
            self.client_for(self.ktf).get(reverse("order-detail", args=[order.number])).status_code,
            404,
        )
        found = director.get(reverse("case-search"), {"q": order.number.lower()})
        self.assertRedirects(found, reverse("order-detail", args=[order.number]))
        tabs = self.client_for(self.director).get(
            reverse("division-detail", args=[self.kcw_division.code])
        )
        self.assertContains(tabs, order.number)
