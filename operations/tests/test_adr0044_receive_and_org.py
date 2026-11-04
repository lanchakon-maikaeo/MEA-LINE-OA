"""รับเรื่อง, ผังบุคลากร and the merged account (ADR 0044, grill 3 พ.ย. 2569).

Acceptance → tests: a new case waits at รอรับเรื่อง while an emergency starts at once, and
ดำเนินการ belongs to the division's แอดมินกอง, แอดมินฝ่าย and people an admin granted it, starting
at ปกติ (ReceiveTests); the queue card names the reporter and the picks, and วันนี้ keeps cases
filed today (QueueTests); a web account takes over the LINE account's link, role and work
(MergeTests); the chart picks a unit by the file's paths, names a unit outside ฝอจ., and its
cards save a person's unit and ตำแหน่ง, and it opens at the ฝ่าย level (OrgTests); the chat
groups a run of messages by sender and day (ChatTests); a งานสั่ง starts with one ดำเนินการ that
settles ด่วน or ปกติ, its other steps open their forms, and the requester's result card says ยืนยัน
(OrderTests, 4 Nov 2569).
"""

import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client
from django.utils import timezone

from operations import (
    approvals,
    case_chat,
    conversation,
    identity,
    orders_web,
    problem_chat,
    queues,
    roster,
    staff_work,
    web_actions,
    work_orders,
)
from operations.models import (
    AuthorityGrant,
    Case,
    CaseMessage,
    ChannelIdentity,
    Division,
    Employee,
    Requester,
    Role,
    RoleGrant,
    RosterUnitName,
    StaffProfile,
    WorkOrder,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m13_work_orders import OrderTestCase
from operations.tests.test_m16_no_screening import NoScreeningTestCase

User = get_user_model()


class ReceiveTests(NoScreeningTestCase):
    def test_a_new_case_waits_and_an_emergency_starts_at_once(self):
        self.assertEqual(Case.Status.AWAITING_APPROVAL.label, "รอรับเรื่อง")
        quiet = self.problem("ขอสอบถามการลงโปรแกรมอ่านมิเตอร์")
        self.assertEqual(quiet.status, "awaiting_approval")
        hazard = self.problem("สายไฟขาดห้อย มีประกายไฟ")
        self.assertEqual(hazard.status, "in_progress")

    def test_who_may_press_ดำเนินการ(self):
        case = self.problem("มิเตอร์ไม่หมุน")
        member = make_staff("kcw-member", (Role.STAFF, "กคว."))
        head = make_staff("kcw-head", (Role.SUPERVISOR, "กคว."))
        elsewhere = Division.objects.exclude(pk=case.owner_division_id).first()
        other = make_staff("other-head", (Role.SUPERVISOR, elsewhere.code))
        department = make_staff("dept-head", (Role.DEPARTMENT_SUPERVISOR, None))
        self.assertFalse(approvals.may_start(member, case.owner_division_id))
        self.assertFalse(approvals.may_start(other, case.owner_division_id))
        for user in (head, department, self.approver):  # approver: an admin's explicit grant
            self.assertTrue(approvals.may_start(user, case.owner_division_id), user.username)
        # A grant that comes with a membership is not an admin's choice of person.
        AuthorityGrant.objects.filter(user=self.approver).update(
            member_of_id=case.owner_division_id
        )
        self.assertFalse(approvals.may_start(self.approver, case.owner_division_id))

    def test_ดำเนินการ_starts_the_work_at_ปกติ(self):
        case = self.problem("มิเตอร์ไม่หมุน")
        head = make_staff("kcw-head", (Role.SUPERVISOR, "กคว."))
        offers = web_actions.available(head, case)
        self.assertEqual((offers[0].name, offers[0].label), ("approve", "ดำเนินการ"))
        form = web_actions.build_form("approve", web_actions.situation(head, case), {})
        level = next(f for f in form.fields if f.name == "priority")
        self.assertEqual(level.initial, approvals.NORMAL)
        client = Client()
        client.force_login(head)
        case.refresh_from_db()
        client.post(
            f"/cases/{case.number}/act/approve/",
            {
                "priority": "P3",
                "notice": "เริ่มดำเนินการแล้ว",
                "revision": case.revision,
                "idempotency_key": "adr0044-start-1",
            },
        )
        case.refresh_from_db()
        self.assertEqual(case.status, "in_progress")
        self.assertEqual(case.triage.effective_priority, "P3")


class QueueTests(NoScreeningTestCase):
    def test_the_reporter_line(self):
        case = self.problem("มิเตอร์ไม่หมุน")
        employee = Employee.objects.create(
            employee_id="2600504",
            name="สมชาย ใจดี",
            position="ช่างเทคนิคไฟฟ้า 5",
            department="ฝ่ายตัวอย่าง",
            division_name="กองตัวอย่าง",
        )
        Requester.objects.filter(pk=case.requester_id).update(
            employee=employee, display_name="สมชาย ใจดี"
        )
        case.refresh_from_db()
        self.assertEqual(
            queues.reporter(case), "สมชาย ใจดี (2600504) · ช่างเทคนิคไฟฟ้า 5 · ฝ่ายตัวอย่าง กองตัวอย่าง"
        )
        names = {"ฝ่ายตัวอย่าง": "ฝต."}
        self.assertTrue(queues.reporter(case, names).endswith("· ฝต. กองตัวอย่าง"))
        Requester.objects.filter(pk=case.requester_id).update(is_anonymous=True)
        case.refresh_from_db()
        self.assertEqual(queues.reporter(case), "ผู้แจ้งไม่ระบุตัวตน")

    def test_today_keeps_cases_filed_today(self):
        today = self.problem("มิเตอร์ไม่หมุน")
        older = self.problem("มิเตอร์หมุนช้า")
        Case.objects.filter(pk=older.pk).update(submitted_at=timezone.now() - timedelta(days=2))
        kept = queues.apply_filters(Case.objects.all(), queues.Filters(today=True))
        self.assertIn(today, kept)
        self.assertNotIn(older, kept)
        self.assertTrue(queues.read_filters({"today": "1"}, [Role.SUPERVISOR]).today)


class MergeTests(NoScreeningTestCase):
    def test_a_web_account_takes_over_the_line_account(self):
        employee = Employee.objects.create(employee_id="2600504", name="สมชาย ใจดี")
        old = User.objects.create_user(username=f"{identity.LINE_ACCOUNT}{employee.pk}")
        StaffProfile.objects.create(user=old, display_name="สมชาย", employee_id="2600504")
        RoleGrant.objects.create(
            user=old, role=Role.STAFF, division=Division.objects.get(code="กคว.")
        )
        link = ChannelIdentity.objects.create(
            channel="line", external_id=f"U{uuid.uuid4().hex}", staff_user=old
        )
        case = self.problem("มิเตอร์ไม่หมุน")
        Case.objects.filter(pk=case.pk).update(assignee=old)
        web = make_staff("user-4")
        identity.merge_accounts(self.flow_admin, employee, web)
        link.refresh_from_db()
        old.refresh_from_db()
        self.assertEqual(link.staff_user, web)
        self.assertFalse(old.is_active)
        self.assertEqual(StaffProfile.objects.get(user=web).employee_id, "2600504")
        self.assertTrue(RoleGrant.objects.filter(user=web, role=Role.STAFF).exists())
        self.assertEqual(Case.objects.get(pk=case.pk).assignee, web)
        other = make_staff("user-5")
        with self.assertRaises(ValidationError):  # the ID now sits on a web account
            identity.merge_accounts(self.flow_admin, employee, other)


class OrgTests(NoScreeningTestCase):
    def setUp(self):
        super().setUp()
        self.rows = [
            ("ฝ่ายนอก", "กองนอก", "แผนกนอก"),
            ("ฝ่ายนอก", "กองนอก", "แผนกสอง"),
        ]
        for n, (department, division, section) in enumerate(self.rows):
            Employee.objects.create(
                employee_id=f"90000{n}",
                name=f"คนที่ {n}",
                department=department,
                division_name=division,
                section_name=section,
            )

    def test_a_path_may_stop_at_the_ฝ่าย(self):
        place = roster.place_of("ฝ่ายนอก")
        self.assertEqual(
            (place["department"], place["division_name"], place["section_name"]), ("ฝ่ายนอก", "", "")
        )
        self.assertIsNone(place["division_id"])
        self.assertEqual(roster.place_of("ฝ่ายนอก › กองนอก")["section_name"], "")
        self.assertIsNone(roster.place_of("ฝ่ายอื่น"))

    def test_a_unit_outside_takes_a_short_name(self):
        roster.set_unit_name(self.flow_admin, "ฝ่ายนอก", "ฝน.", "ทดสอบ")
        self.assertEqual(roster.unit_names(), {"ฝ่ายนอก": "ฝน."})
        employee = Employee.objects.get(employee_id="900000")
        self.assertEqual(roster.employee_path(employee, roster.unit_names())[0], "ฝน.")
        roster.set_unit_name(self.flow_admin, "ฝ่ายนอก", "", "ทดสอบ")
        self.assertFalse(RosterUnitName.objects.exists())
        with self.assertRaises(ValidationError):
            roster.set_unit_name(self.flow_admin, "ฝ่ายอื่น", "ฝอ.", "ทดสอบ")

    def test_the_card_saves_a_persons_unit_and_position(self):
        client = Client()
        client.force_login(self.flow_admin)
        page = client.get("/console/admin/org/").content.decode()
        self.assertIn("ผังบุคลากร", page)
        self.assertIn("นำเข้าทะเบียนพนักงานครั้งถัดไป", page)
        self.assertNotIn("ยังไม่ได้จัดหน่วย", page)
        employee = Employee.objects.get(employee_id="900000")
        client.post(
            "/console/admin/org/",
            {
                "action": "set_person",
                "employee": employee.pk,
                "unit-0": "ฝ่ายนอก",
                "unit-1": "กองนอก",
                "unit-2": "แผนกสอง",
                "position": "ช่างเทคนิคไฟฟ้า 6",
            },
        )
        employee.refresh_from_db()
        self.assertEqual(employee.section_name, "แผนกสอง")
        self.assertEqual(employee.position, "ช่างเทคนิคไฟฟ้า 6")
        client.post(
            "/console/admin/org/", {"action": "unit_name", "path": "ฝ่ายนอก", "value": "ฝน."}
        )
        self.assertEqual(roster.unit_names(), {"ฝ่ายนอก": "ฝน."})

    def test_the_tree_opens_at_the_ฝ่าย_level(self):
        client = Client()
        client.force_login(self.flow_admin)
        tree = client.get("/console/admin/org/").context["tree"]
        line = tree["children"][0]
        self.assertTrue(line["open"])  # a สายงาน shows its ฝ่าย
        self.assertTrue(all(not c["open"] for c in line["children"]))  # one click per level
        self.assertEqual(line["key"], f"›{line['label']}")


class ChatTests(NoScreeningTestCase):
    def test_a_run_of_messages_shows_the_name_once_and_each_day_starts_a_line(self):
        case = self.problem("มิเตอร์ไม่หมุน")
        now = timezone.now()
        for who, at in (
            ("requester", now - timedelta(days=1)),
            ("requester", now - timedelta(days=1) + timedelta(minutes=1)),
            ("staff", now - timedelta(days=1) + timedelta(minutes=2)),
            ("staff", now),
        ):
            m = CaseMessage.objects.create(
                case=case,
                direction=who,
                text="ข้อความ",
                author=self.approver if who == "staff" else None,
                status="sent" if who == "staff" else "received",
            )
            CaseMessage.objects.filter(pk=m.pk).update(created_at=at)
        thread = case_chat.thread(case)
        chat = [i for i in thread if i["who"] != "status"][-4:]
        self.assertEqual([i["first"] for i in chat], [True, False, True, True])
        self.assertEqual(sum(bool(i["day"]) for i in thread), 2)  # yesterday and today
        self.assertTrue(chat[0]["day"])
        self.assertEqual(chat[0]["name"], self.requester.display_name)
        self.assertEqual(chat[2]["name"], self.approver.staff_profile.display_name)


class LineStepTests(NoScreeningTestCase):
    def test_a_date_is_picked_with_lines_date_picker_and_a_note_can_be_skipped(self):
        picker = {"type": "datetimepicker", "label": "เลือกวันที่", "data": "t=x", "mode": "date"}
        message = conversation.text_message("วันที่", [picker, ("ข้าม", "t=y")])
        actions = [item["action"]["type"] for item in message["quickReply"]["items"]]
        self.assertEqual(actions, ["datetimepicker", "postback"])
        form = web_actions.ActionForm(
            "pause",
            "พักเรื่อง",
            [
                web_actions.Field("until", "ถึงวันที่", "date", True),
                web_actions.Field("reason", "เหตุผล", "textarea"),
            ],
        )
        self.assertEqual([f.name for f in staff_work._askable(form)], ["until", "reason"])
        self.assertEqual(staff_work._iso_date("2026-11-15").isoformat(), "2026-11-15")
        self.assertIsNone(staff_work._iso_date("15/11"))


class OrderTests(OrderTestCase):
    def test_ดำเนินการ_starts_an_order_and_settles_its_urgency(self):
        order = self.order(assignee=self.kcw, urgent=True)
        member = Client()
        member.force_login(self.kcw)
        page = member.get(f"/orders/{order.number}/").content.decode()
        self.assertIn('name="urgent" value="1" checked', page)  # the issuer's choice comes first
        self.assertIn('popovertarget="os-send_back"', page)  # other steps open their form
        self.assertNotIn("ตัวเลือกอื่น", page)
        self.assertNotIn("รับทราบ", page)
        member.post(f"/orders/{order.number}/do/start/", {"urgent": "0"})
        order.refresh_from_db()
        self.assertEqual((order.status, order.is_urgent), (WorkOrder.Status.IN_PROGRESS, False))
        self.assertEqual(
            [(j["label"], j["state"]) for j in orders_web._journey(order)],
            [
                ("มอบหมาย", "done"),
                ("กำลังดำเนินการ", "done"),
                ("เสร็จสิ้น", "current"),
                ("ปิดงาน", "todo"),
            ],
        )

    def test_an_order_acknowledged_before_the_change_still_starts(self):
        order = self.order(assignee=self.kcw)
        WorkOrder.objects.filter(pk=order.pk).update(status=WorkOrder.Status.ACKNOWLEDGED)
        order.refresh_from_db()
        self.assertIn("start", dict(work_orders.steps_for(self.kcw, order)))
        self.assertEqual(orders_web._journey(order)[1]["state"], "current")

    def test_the_requesters_result_card_says_ยืนยัน(self):
        case = Case(topic=Case.Topic.PROBLEM)
        self.assertEqual(problem_chat.labels(case), ("ยืนยัน", "ยังไม่เรียบร้อย"))
