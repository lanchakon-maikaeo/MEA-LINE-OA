"""เจ้าหน้าที่ทำงานใน LINE (16 ต.ค. 2569 Q8, Q10, Q15): staff link LINE with their employee ID and
a code sent to the roster e-mail; the งานของกอง and สั่งงาน tabs answer in chat; งานสั่ง and case
steps run as a few questions and one confirm, and long forms open on the web.
"""

import re
from datetime import timedelta

from django.core import mail
from django.utils import timezone

from operations import identity, options, staff_line, staff_work, work_orders
from operations.models import (
    ChannelIdentity,
    Division,
    LineMenuItem,
    NotificationDelivery,
    Role,
    StaffProfile,
    WorkOrder,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import ALICE, BOB, buttons, texts
from operations.tests.test_m13_roster import RosterTestCase

S = WorkOrder.Status
A = LineMenuItem.Action


class StaffChatTestCase(RosterTestCase):
    def setUp(self):
        super().setUp()
        self.load()  # the roster: 510001 one@example.test, 510002 two@example.test
        self.kae = Division.objects.get(code="กองอุปกรณ์ฯ")
        self.head = make_staff("kaehead", (Role.SUPERVISOR, "กองอุปกรณ์ฯ"))
        StaffProfile.objects.filter(user=self.head).update(employee_id="510001")
        self.director = make_staff("director")
        identity.grant_role(self.admin, self.director, Role.DEPARTMENT_SUPERVISOR, None, "ผอ.")

    def say(self, *args, **kwargs):
        # Code mails leave after the commit, as in production.
        with self.captureOnCommitCallbacks(execute=True):
            return super().say(*args, **kwargs)

    def link(self, user, line_user):
        self.type(line_user, f"เชื่อม {staff_line.issue_code(user)}")

    def tile(self, action):
        item = LineMenuItem.objects.filter(action=action).order_by("pk").first()
        return self.say(self.postback(ALICE, f"m=item&id={item.pk}"))


class LinkTests(StaffChatTestCase):
    def test_an_employee_id_and_the_mailed_code_link_line(self):
        reply = self.type(ALICE, "เชื่อมบัญชีเจ้าหน้าที่")
        self.assertIn("รหัสพนักงาน", texts(reply))
        reply = self.type(ALICE, "510001")
        self.assertIn("ส่งรหัส 6 หลัก", texts(reply))
        self.assertEqual(mail.outbox[-1].to, ["one@example.test"])
        code = re.search(r"\d{6}", mail.outbox[-1].body).group(0)
        wrong = "000000" if code != "000000" else "111111"
        self.assertIn("รหัสไม่ถูก", texts(self.type(ALICE, wrong)))
        reply = self.type(ALICE, code)  # the link note, then the งานของกอง list
        self.assertIn("เชื่อม LINE กับบัญชีเจ้าหน้าที่แล้ว", texts(reply))
        self.assertIn("งานของกอง", texts(reply))
        self.assertEqual(ChannelIdentity.objects.get(external_id=ALICE).staff_user, self.head)

    def test_an_id_without_a_staff_account_gets_no_code(self):
        self.type(BOB, "เชื่อมบัญชีเจ้าหน้าที่")
        reply = self.type(BOB, "510002")  # in the roster, but no staff account carries it
        self.assertIn("ไม่พบบัญชีเจ้าหน้าที่", texts(reply))
        self.assertEqual(mail.outbox, [])

    def test_the_web_code_only_option_still_works(self):
        options.change(None, options.STAFF_LINK, "code", "เฉพาะรหัสจากเว็บ")
        self.assertIn("บัญชีของฉัน", texts(self.type(ALICE, "เชื่อมบัญชีเจ้าหน้าที่")))


class WorkTabTests(StaffChatTestCase):
    def test_a_received_order_is_acknowledged_and_finished_in_chat(self):
        self.link(self.head, ALICE)
        self.assertIn("ยังไม่มีงานสั่ง", texts(self.tile(A.WORK_ORDERS)))
        order = work_orders.create(self.director, "เช็กสต็อกหม้อแปลง (ทดสอบ)", self.kae)
        push = NotificationDelivery.objects.get(
            recipient_user=self.head, channel=NotificationDelivery.Channel.LINE_PUSH
        )
        self.assertIn(f"ดูงาน {order.number}", buttons([push.message]))
        reply = self.tile(A.WORK_ORDERS)
        detail = self.press(ALICE, order.number, reply)
        self.assertIn("เช็กสต็อกหม้อแปลง", texts(detail))
        self.press(ALICE, "รับทราบ", detail)
        self.assertEqual(WorkOrder.objects.get().status, S.ACKNOWLEDGED)
        self.press(ALICE, "ทำเสร็จแล้ว")
        self.type(ALICE, "นับครบแล้ว 12 ลูก")
        order.refresh_from_db()
        self.assertEqual((order.status, order.result), (S.DONE, "นับครบแล้ว 12 ลูก"))

    def test_find_a_case_or_order_by_number(self):
        self.link(self.head, ALICE)
        order = work_orders.create(self.director, "งานทดสอบ", self.kae)
        self.tile(A.WORK_FIND)
        reply = self.type(ALICE, order.number.lower())
        self.assertIn(f"งานสั่ง {order.number}", texts(reply))

    def test_the_web_button_opens_the_site(self):
        self.link(self.head, ALICE)
        reply = self.tile(A.OPEN_WEB)
        self.assertTrue(any(v.startswith("http") for v in buttons(reply).values()))

    def test_people_who_did_not_link_staff_get_a_plain_answer(self):
        self.approve(ALICE)
        self.assertIn("เจ้าหน้าที่ที่เชื่อม LINE", texts(self.tile(A.WORK_NEW)))


class OrderTabTests(StaffChatTestCase):
    def test_a_supervisor_orders_work_with_text_and_confirmation(self):
        self.link(self.director, ALICE)
        self.tile(A.ORDER_NEW)
        summary = self.type(ALICE, "ให้ทั้งกอง กองอุปกรณ์ฯ ตรวจนับเครื่องมือพรุ่งนี้ ด่วน")
        self.assertIn("ตรวจก่อนสั่งงาน", texts(summary))
        self.assertFalse(WorkOrder.objects.exists())
        self.type(ALICE, "ยืนยัน")
        order = WorkOrder.objects.get()
        self.assertEqual(
            (order.division, order.assignee, order.is_urgent, order.issued_by),
            (self.kae, None, True, self.director),
        )
        self.assertEqual(order.due_on, timezone.localdate() + timedelta(days=1))
        self.assertIn(order.number, texts(self.sent[-1]))

    def test_the_order_tab_is_for_supervisors_only(self):
        member = make_staff("kaemember", (Role.STAFF, "กองอุปกรณ์ฯ"))
        self.link(member, ALICE)
        self.assertIn("เฉพาะ Supervisor", texts(self.tile(A.ORDER_NEW)))

    def test_typed_dates_read_thai_day_and_month(self):
        ahead = timezone.localdate() + timedelta(days=10)
        self.assertEqual(staff_work._typed_date(f"{ahead.day}/{ahead.month}"), ahead)
        self.assertEqual(
            staff_work._typed_date(f"{ahead.day}/{ahead.month}/{ahead.year + 543}"), ahead
        )
        self.assertIsNone(staff_work._typed_date("32/13"))
        past = timezone.localdate() - timedelta(days=400)
        self.assertIsNone(staff_work._typed_date(f"{past.day}/{past.month}/{past.year}"))

    def test_today_and_late_summaries_answer(self):
        self.link(self.director, ALICE)
        order = work_orders.create(self.director, "งานค้าง", self.kae, due_on=timezone.localdate())
        WorkOrder.objects.filter(pk=order.pk).update(
            due_on=timezone.localdate() - timedelta(days=2)
        )
        self.assertIn("งานสั่งเกินกำหนด 1 รายการ", texts(self.tile(A.ORDER_LATE)))
        self.assertIn("งานสั่ง: ค้าง 1", texts(self.tile(A.ORDER_TODAY)))
