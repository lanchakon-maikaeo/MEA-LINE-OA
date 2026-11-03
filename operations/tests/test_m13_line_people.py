"""Who is on the other side of the LINE chat (ADR 0010; 10 Oct 2569 Q1, Q3–Q8).

Acceptance → tests: an employee ID with its proof approves a registration by itself when
ตัวเลือกระบบ says so, and anything else still reaches a person (RegistrationTests); a LINE user
who does not register may report a problem or suggestion, with no location (AnonymousTests); staff
link LINE with a web code or an employee check, see and do their division's work there, get only
the important alerts without the requester's details, and lose the link when suspended
(StaffLineTests).
"""

import uuid

from django.test import Client, override_settings

from operations import employees, identity, options, services, staff_line
from operations.models import (
    AuditEvent,
    Case,
    ChannelIdentity,
    Conversation,
    District,
    Division,
    Employee,
    NotificationDelivery,
    RegistrationRequest,
    Requester,
    Role,
    StaffProfile,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import ALICE, BOB, LineTestCase, buttons, texts
from operations.tests.test_m13_org import real_world

DIRECTORY = (
    "employee_id,name,district_code,division_code,position,check\n"
    "SYN10001,พนักงานจำลอง หนึ่ง,D01,,พนักงานช่าง,01/01/2530\n"
    "SYN20002,ผอ.จำลอง กองอุปกรณ์ฯ,,กองอุปกรณ์ฯ,ผู้อำนวยการกอง,06/06/2516\n"
)


class PeopleTestCase(LineTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        real_world(cls)
        employees.import_csv(None, DIRECTORY, "วันเกิด (วว/ดด/ปปปป)", synthetic=True)

    def location(self, user, lat=13.7, lng=100.5):
        return self.event(
            user,
            "message",
            message={
                "id": str(next(self.message_ids)),
                "type": "location",
                "latitude": lat,
                "longitude": lng,
                "address": "จุดทดสอบ",
            },
        )

    def register(self, user, name="พนักงานจำลอง หนึ่ง"):
        """As an employee when the check asks for one (15 ต.ค. 2569 Q12), else name and เขต."""
        self.say(self.event(user, "follow"))
        if "พนักงาน กฟน." in buttons(self.sent[-1]):
            return self.press(user, "พนักงาน กฟน.")
        return self.as_other(user, name)

    def as_other(self, user, name="พนักงานจำลอง หนึ่ง"):
        self.type(user, name)
        return self.press(user, District.objects.get(code="D01").name)


class RegistrationTests(PeopleTestCase):
    def test_the_directory_keeps_only_a_hash_of_the_proof(self):
        row = employees.Employee.objects.get(employee_id="SYN10001")
        self.assertNotIn("2530", row.check_hash)
        self.assertNotIn("พนักงานจำลอง หนึ่ง", str(list(Employee.objects.values())))  # no names
        self.assertEqual(row.check_hash, employees.digest("๐๑/๐๑/๒๕๓๐"))
        event = AuditEvent.objects.get(action="employees.imported")
        self.assertNotIn("SYN10001", str(event.data))

    def test_a_matching_employee_is_approved_by_the_system(self):
        Employee.objects.filter(employee_id="SYN10001").update(
            division=Division.objects.get(code="กคว.")
        )
        options.change(None, options.REGISTRATION_CHECK, "employee_auto", "ทดสอบ")
        reply = self.register(ALICE)
        self.assertIn("รหัสพนักงาน", texts(reply))
        self.assertIn("วันเกิด", texts(self.type(ALICE, "SYN10001")))
        reply = self.type(ALICE, "01/01/2530")
        self.assertIn("ยืนยันตัวตนแล้ว", texts(reply))
        line = ChannelIdentity.objects.get(external_id=ALICE)
        self.assertEqual(line.status, ChannelIdentity.Status.ACTIVE)
        registration = RegistrationRequest.objects.get(identity=line)
        self.assertEqual(registration.decided_by.username, identity.SYSTEM_USERNAME)
        self.assertTrue(AuditEvent.objects.filter(action="registration.auto_approved").exists())

    def test_an_employee_is_shown_by_the_first_name_and_has_no_name_to_correct(self):
        options.change(None, options.REGISTRATION_CHECK, "employee_auto", "ทดสอบ")
        self.register(ALICE)
        self.type(ALICE, "SYN20002")  # the roster gives no unit, so it is asked
        reply = self.type(ALICE, "06/06/2516")
        self.assertIn("ผอ.จำลอง", texts(reply))  # ADR 0041: the roster's first name
        self.assertNotIn("แก้ชื่อ", buttons(reply))
        again = self.type(ALICE, "ไม่แน่ใจว่าอยู่หน่วยงานไหน")  # typed: the list again
        self.assertIn("เลือกหน่วยงาน", texts(again))
        self.assertNotIn("แก้ชื่อ", buttons(again))
        self.press(ALICE, District.objects.get(code="D01").name)
        requester = ChannelIdentity.objects.get(external_id=ALICE).requester
        self.assertEqual(requester.display_name, "ผอ.จำลอง")
        self.assertEqual(
            (requester.employee.employee_id, requester.employee_link), ("SYN20002", "verified")
        )

    def test_wrong_proofs_stop_and_go_to_a_person(self):
        options.change(None, options.REGISTRATION_CHECK, "employee_auto", "ทดสอบ")
        self.register(ALICE)
        for _ in range(2):
            self.type(ALICE, "SYN10001")
            self.assertIn("ลองได้อีก", texts(self.type(ALICE, "31/12/2500")))
        self.type(ALICE, "SYN10001")
        reply = self.type(ALICE, "31/12/2500")
        self.assertIn("ส่งให้เจ้าหน้าที่ตรวจแทน", texts(reply))
        self.assertIn("พิมพ์ชื่อ-นามสกุล", texts(reply))  # a person checks: the name and เขต
        self.as_other(ALICE)
        registration = RegistrationRequest.objects.get(identity__external_id=ALICE)
        self.assertEqual((registration.status, registration.employee), ("pending", None))

    def test_another_way_is_always_offered(self):
        options.change(None, options.REGISTRATION_CHECK, "employee_review", "ทดสอบ")
        self.register(ALICE)
        self.assertIn("พิมพ์ชื่อ-นามสกุล", texts(self.press(ALICE, "ใช้วิธีอื่น")))
        reply = self.as_other(ALICE)
        self.assertIn("ระหว่างรอแจ้งปัญหา", texts(reply))
        self.assertEqual(RegistrationRequest.objects.get().status, "pending")

    def test_registration_asks_no_location(self):
        # 31 ต.ค. 2569: the step is gone, whatever the old option holds.
        options.change(None, options.REGISTRATION_LOCATION, "required", "ทดสอบ")
        self.register(BOB)
        self.press(BOB, "ใช้วิธีอื่น")
        reply = self.as_other(BOB, "ผู้ลงทะเบียน ทดสอบ")
        self.assertNotIn("ตำแหน่ง", texts(reply))
        self.assertIn("ส่งคำขอให้แอดมินตรวจแล้ว", texts(reply))
        registration = RegistrationRequest.objects.get(identity__external_id=BOB)
        self.assertEqual((registration.status, registration.latitude), ("pending", None))
        page = Client()
        page.force_login(self.admin)
        self.assertNotContains(page.get("/console/admin/options/"), "ตำแหน่งของผู้ลงทะเบียน")

    def test_a_registration_left_at_the_old_location_step_starts_again(self):
        # 3 พ.ย. 2569: the location step is gone; whoever was left there chooses the type again.
        self.register(BOB)
        Conversation.objects.filter(identity__external_id=BOB).update(
            prompt="reg_location",
            prompt_data={"name": "ผู้ลงทะเบียน ทดสอบ", "district": "D01", "checked": True},
        )
        reply = self.say(self.location(BOB))
        self.assertIn("เป็นพนักงาน กฟน. ไหม", texts(reply))
        self.assertFalse(RegistrationRequest.objects.filter(identity__external_id=BOB).exists())


class UnverifiedTagTests(PeopleTestCase):
    """Zero trust (31 ต.ค. 2569 Q2): staff see which reporter names were not checked against
    ทะเบียนพนักงาน; the reporter can still file."""

    def page(self, requester):
        result = services.submit_case(
            services.Actor.for_requester(requester),
            idempotency_key=uuid.uuid4().hex,
            payload={"kind": "problem", "topic": "suggestion", "description": "ทดสอบป้าย"},
        )
        client = Client()
        client.force_login(self.admin)
        return client.get(f"/cases/{Case.objects.get(pk=result.case_id).number}/")

    def test_a_typed_name_is_tagged_and_a_proved_employee_is_not(self):
        line = ChannelIdentity.objects.create(channel="line", external_id=BOB)
        outsider = identity.external_requester(line, "คนนอก ทดสอบ")
        self.assertContains(self.page(outsider), "ยังไม่ยืนยันตัวตน")
        options.change(None, options.REGISTRATION_CHECK, "employee_auto", "ทดสอบ")
        self.register(ALICE)
        self.type(ALICE, "SYN10001")
        self.type(ALICE, "01/01/2530")
        employee = ChannelIdentity.objects.get(external_id=ALICE).requester
        page = self.page(employee)
        self.assertContains(page, employee.display_name)
        self.assertNotContains(page, "ยังไม่ยืนยันตัวตน")


class AnonymousTests(PeopleTestCase):
    def test_an_unregistered_user_reports_a_suggestion_without_a_location(self):
        # 31 ต.ค. 2569: no step asks for a location; one sent anyway is not kept.
        options.change(None, options.ANONYMOUS, "on", "ทดสอบ")
        self.say(self.event(ALICE, "follow"))
        start = self.press(ALICE, "ไม่ระบุตัวตน")
        self.assertNotIn("ตำแหน่ง", texts(start))
        self.press(ALICE, "ปัญหาการใช้งานหรืออื่นๆ")  # ADR 0030: the division carousel
        summary = self.type(ALICE, "อยากให้มีที่จอดรถรับของ (ทดสอบ)")
        self.assertIn("แจ้งแบบไม่ระบุตัวตน", texts(summary))
        self.assertIn("ระบบไม่ได้ใช้ตำแหน่ง", texts(self.say(self.location(ALICE))))
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertTrue(case.requester.is_anonymous)
        self.assertEqual((case.owner_label, case.latitude), ("แอดมิน", None))
        self.assertEqual(ChannelIdentity.objects.get(external_id=ALICE).status, "pending")
        client = Client()
        client.force_login(self.admin)  # the admins hold it (ADR 0030)
        page = client.get(f"/cases/{case.number}/")
        self.assertContains(page, "ไม่ระบุตัวตน")
        self.assertNotContains(page, "google.com/maps")
        self.assertNotContains(page, "ยังไม่ยืนยันตัวตน")  # no name is shown at all

    def test_anonymous_reporters_cannot_withdraw_and_are_limited(self):
        options.change(None, options.ANONYMOUS, "on", "ทดสอบ")
        line = ChannelIdentity.objects.create(channel="line", external_id=BOB)
        anonymous = identity.anonymous_requester(line)
        actor = services.Actor.for_requester(anonymous)
        with self.assertRaises(services.CommandFailed):
            services.submit_case(
                actor,
                idempotency_key="anon-withdrawal",
                payload={
                    "kind": "withdrawal",
                    "purpose": "x",
                    "lines": [{"item_code": "MTR_SYN_A", "quantity": "1"}],
                },
            )
        problem = {"kind": "problem", "topic": "suggestion", "description": "ทดสอบ"}
        for n in range(3):  # no location needed (31 ต.ค. 2569)
            services.submit_case(actor, idempotency_key=f"anon-report-{n}", payload=problem)
        with self.assertRaises(services.CommandFailed):
            services.submit_case(actor, idempotency_key="anon-report-over", payload=problem)


@override_settings(LINE_TYPED_SLIPS=True)  # ADR 0032: the earlier typed intake
class StaffLineTests(PeopleTestCase):
    def head(self):
        user = make_staff("kaehead", (Role.SUPERVISOR, "กองอุปกรณ์ฯ"), (Role.STAFF, "กองอุปกรณ์ฯ"))
        StaffProfile.objects.filter(user=user).update(employee_id="SYN20002")
        return user

    def test_a_web_code_links_line_and_wrong_codes_stop(self):
        user = self.head()
        self.assertIn("ไม่ถูกต้อง", texts(self.type(ALICE, "เชื่อม 000000")))
        code = staff_line.issue_code(user)
        self.assertIn("เชื่อม LINE กับบัญชีเจ้าหน้าที่แล้ว", texts(self.type(ALICE, f"เชื่อม {code}")))
        self.assertEqual(ChannelIdentity.objects.get(external_id=ALICE).staff_user, user)
        self.assertIn("ไม่ถูกต้อง", texts(self.type(BOB, f"เชื่อม {code}")))  # used once
        for _ in range(4):
            self.type(BOB, "เชื่อม 111111")
        self.assertIn("ครบจำนวน", texts(self.type(BOB, "เชื่อม 222222")))

    def test_an_employee_check_links_line_when_the_option_allows(self):
        user = self.head()
        self.type(ALICE, "เชื่อมบัญชีเจ้าหน้าที่")
        self.assertIn("บัญชีของฉัน", texts(self.sent[-1]))  # web code only by default
        options.change(None, options.STAFF_LINK, "code_or_employee", "ทดสอบ")
        self.type(ALICE, "เชื่อมบัญชีเจ้าหน้าที่")
        self.type(ALICE, "SYN20002")
        reply = self.type(ALICE, "06/06/2516")
        self.assertIn("เชื่อม LINE กับบัญชีเจ้าหน้าที่แล้ว", texts(reply))
        self.assertEqual(ChannelIdentity.objects.get(external_id=ALICE).staff_user, user)

    def linked(self, user, line_user=ALICE):
        code = staff_line.issue_code(user)
        self.type(line_user, f"เชื่อม {code}")

    def submit(self, payload):
        requester = Requester.objects.create(
            display_name="ผู้แจ้งลับ", district=District.objects.get(code="D01"), approved_by=self.admin
        )
        result = services.submit_case(
            services.Actor.for_requester(requester),
            idempotency_key=f"test-{uuid.uuid4().hex}",
            payload=payload,
        )
        return Case.objects.get(pk=result.case_id)

    def test_staff_see_and_take_their_divisions_work_in_line(self):
        user = self.head()
        self.linked(user)
        case = self.submit({"kind": "problem", "topic": "transformer", "description": "เสียงดัง"})
        reply = self.type(ALICE, "งานของกอง")
        self.assertIn("งานของกอง 1 รายการ", texts(reply))
        self.assertIn(case.number, buttons(reply))
        options.change(None, options.STAFF_LINE_ACTIONS, "view", "ดูอย่างเดียว")
        detail = self.press(ALICE, case.number, reply)
        self.assertIn("เสียงดัง", texts(detail))
        self.assertNotIn("เริ่มคัดกรอง", buttons(detail))
        options.change(None, options.STAFF_LINE_ACTIONS, "act", "ทำใน LINE")  # the default
        detail = self.press(ALICE, case.number, reply)
        self.press(ALICE, "เริ่มคัดกรอง", detail)  # nothing to ask: one tap
        self.assertEqual(Case.objects.get(pk=case.pk).status, "triage")
        detail = self.press(ALICE, case.number, reply)
        self.press(ALICE, "ขอข้อมูลเพิ่ม", detail)
        self.type(ALICE, "ขอรูปหม้อแปลง")
        self.press(ALICE, "บันทึก")
        self.assertEqual(Case.objects.get(pk=case.pk).status, "awaiting_info")

    def test_a_requester_line_can_also_be_staff(self):
        self.approve(ALICE)
        user = self.head()
        self.linked(user)
        self.say(self.postback(ALICE, "m=new"))
        self.assertIn("เบิก", buttons(self.sent[-1]))  # the requester side still works
        self.assertIn("งานของกอง", texts(self.type(ALICE, "งานของกอง")))

    def test_alerts_are_short_private_and_go_to_the_head(self):
        user = self.head()
        member = make_staff("kaemember", (Role.STAFF, "กองอุปกรณ์ฯ"))
        self.linked(user)
        self.linked(member, BOB)
        case = self.submit(
            {"kind": "problem", "topic": "transformer", "description": "ไฟลุกที่หม้อแปลง รายละเอียดลับ"}
        )
        planned = staff_line.plan("staff_hazard", f"hazard:{case.pk}", case, [user, member])
        self.assertEqual([d.recipient_user for d in planned], [user])
        text = planned[0].text
        self.assertIn(case.number, text)
        self.assertNotIn("รายละเอียดลับ", text)
        self.assertNotIn("ผู้แจ้งลับ", text)
        self.assertEqual(staff_line.recipient_problem(planned[0]), "")
        options.change(None, options.STAFF_LINE_BUDGET, "0", "ปิด")
        self.assertEqual(staff_line.plan("staff_hazard", "again", case, [user]), [])
        identity.set_staff_active(self.admin, user, False, "ลาออก")
        self.assertIsNone(ChannelIdentity.objects.get(external_id=ALICE).staff_user)
        self.assertEqual(
            staff_line.recipient_problem(NotificationDelivery.objects.get(pk=planned[0].pk)),
            "staff LINE no longer linked or account inactive",
        )

    def test_staff_report_in_their_divisions_name(self):
        user = self.head()
        self.linked(user)
        menu = self.type(ALICE, "งานของกอง")
        self.press(ALICE, "แจ้งปัญหา", menu)
        self.press(ALICE, "กองอุปกรณ์การจำหน่าย")  # ADR 0030: the division carousel
        self.type(ALICE, "ควรเพิ่มชั้นวางของในคลัง (ทดสอบ)")
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertTrue(case.requester.is_internal)
        self.assertEqual(case.requester.division.code, "กองอุปกรณ์ฯ")
        self.assertEqual((case.topic, case.owner_division.code), ("general", "กองอุปกรณ์ฯ"))
        self.assertEqual(case.menu_label, "ปัญหา/ข้อเสนอแนะ")  # 31 Oct 2569 Q13: on the tab
