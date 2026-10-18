"""หน้ารับรองผู้แจ้ง: who each approved requester is, and what an admin can do (14 Oct 2569).

The user asked for: suspend or revoke a requester (no data deleted, admins only, nothing pushed),
read what they typed, and see on the list who each person is, their position from ทะเบียนพนักงาน
("map จากรายชื่อในอนาคต": tied by a name that matches one employee) and their role in the system
("user, user และ admin").

Acceptance → tests: the list shows proof, position and role, without anonymous reporters, and
only admins can open a requester (RequesterListTests); a name that matches exactly one employee
gives the position only; a shared name or another district waits for an admin, who confirms an
employee, corrects the district or fixes a link (EmployeeMatchTests); an admin suspends,
opens again or revokes with a reason, nothing is deleted or pushed, and a revoked LINE account
registers again (SuspendRevokeTests); an admin reads what was typed as this requester, never
what the same LINE account typed anonymously, and every opening is audited (MessagesTests).
"""

from importlib import import_module

from django.apps import apps
from django.core.exceptions import PermissionDenied, ValidationError

from operations import employees, identity, options
from operations.models import (
    AuditEvent,
    ChannelIdentity,
    ConversationMessage,
    District,
    Division,
    Employee,
    NotificationDelivery,
    Requester,
    Role,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import ALICE, BOB, CAROL, texts
from operations.tests.test_m13_line_people import DIRECTORY, PeopleTestCase

LIST = "/console/admin/registrations/"


def page(pk, part=""):
    return f"/console/admin/requesters/{pk}/{part}"


class RequesterTestCase(PeopleTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def act(self, requester, action, **data):
        return self.client.post(page(requester.pk, f"{action}/"), data)

    def line(self, user):
        return ChannelIdentity.objects.get(external_id=user)


class RequesterListTests(RequesterTestCase):
    def test_the_list_shows_proof_position_and_role_without_anonymous_reporters(self):
        Employee.objects.filter(employee_id="SYN10001").update(
            division=Division.objects.get(code="กคว.")
        )
        options.change(None, options.REGISTRATION_CHECK, "employee_auto", "ทดสอบ")
        self.register(ALICE)
        self.type(ALICE, "SYN10001")
        self.assertIn("ยืนยันตัวตนแล้ว", texts(self.type(ALICE, "01/01/2530")))
        verified = Requester.objects.get(channelidentity__external_id=ALICE)
        self.assertEqual(
            (verified.employee.employee_id, verified.employee_link), ("SYN10001", "verified")
        )
        staff = self.approve(BOB)
        ChannelIdentity.objects.filter(external_id=BOB).update(staff_user=self.admin)
        anonymous = identity.anonymous_requester(
            ChannelIdentity.objects.create(channel="line", external_id=CAROL)
        )

        listing = self.client.get(LIST)
        self.assertContains(listing, "ยืนยันรหัสพนักงาน SYN10001")
        self.assertContains(listing, "พนักงานช่าง")
        self.assertContains(listing, "ผู้ใช้ LINE OA · แอดมินระบบ")
        self.assertContains(listing, page(verified.pk))
        self.assertContains(listing, page(staff.pk))
        self.assertNotContains(listing, page(anonymous.pk))
        self.assertEqual(self.client.get(page(anonymous.pk)).status_code, 404)

    def test_deciders_who_are_not_admins_see_the_list_but_cannot_open_a_requester(self):
        requester = self.approve(ALICE)
        options.change(None, options.REGISTRATION, "staff", "ทดสอบ")
        self.client.force_login(self.meter)
        listing = self.client.get(LIST)
        self.assertContains(listing, requester.display_name)
        self.assertNotContains(listing, page(requester.pk))
        self.assertEqual(self.client.get(page(requester.pk)).status_code, 403)
        self.assertEqual(self.client.get(page(requester.pk, "messages/")).status_code, 403)
        self.assertEqual(self.act(requester, "suspend", reason="x").status_code, 403)
        self.assertEqual(Requester.objects.get(pk=requester.pk).status, "active")
        self.assertFalse(AuditEvent.objects.filter(action="requester.messages_viewed").exists())


class EmployeeMatchTests(RequesterTestCase):
    def test_a_name_matching_one_employee_gives_the_position_but_no_role(self):
        requester = identity.create_requester(
            self.admin, "นายพนักงานจำลอง   หนึ่ง", "D01", "ไม่ใช้ LINE"
        )
        self.assertEqual(
            (requester.employee.employee_id, requester.employee_link), ("SYN10001", "name")
        )
        boss = make_staff("boss", (Role.ADMIN, None))
        boss.staff_profile.employee_id = "SYN10001"
        boss.staff_profile.save()
        self.assertEqual(identity.requester_roles([requester])[requester.pk], [])
        detail = self.client.get(page(requester.pk))
        self.assertContains(detail, "ชื่อตรงทะเบียน SYN10001 (ยังไม่ได้ยืนยัน)")
        self.assertContains(detail, "พนักงานช่าง")
        self.assertContains(detail, "รอยืนยัน")
        self.assertContains(detail, "รับรองพนักงานคนนี้")
        self.assertContains(self.client.get(LIST), 'data-identity="name"')

        # Case 2: an admin checks the person, then confirms the employee the name matched.
        self.act(requester, "link-employee", employee_id="SYN10001", reason="โทรถามแล้ว")
        requester.refresh_from_db()
        self.assertEqual(requester.employee_link, "admin")
        self.assertEqual(identity.requester_roles([requester])[requester.pk], ["แอดมินระบบ"])
        event = AuditEvent.objects.get(action="requester.employee_confirmed")
        self.assertEqual((event.reason, event.data["employee"]), ("โทรถามแล้ว", "SYN10001"))
        identity.link_requester_employee(self.admin, requester, "SYN10001", "ซ้ำ")
        self.assertContains(self.client.get(LIST), 'data-identity="verified"')

    def test_no_match_for_a_shared_name_or_another_district(self):
        requester = identity.create_requester(self.admin, "พนักงานจำลอง หนึ่ง", "D01", "ทดสอบ")
        elsewhere = identity.create_requester(self.admin, "พนักงานจำลอง สาม", "D01", "ทดสอบ")
        self.assertEqual(requester.employee_link, "name")
        more = (
            "employee_id,name,district_code,division_code,position,check\n"
            "SYN10003,พนักงานจำลอง หนึ่ง,D02,,พนักงานธุรการ,02/02/2530\n"
            "SYN10004,พนักงานจำลอง สาม,D03,,พนักงานช่าง,03/03/2530\n"
        )
        summary = employees.import_csv(None, more, "วันเกิด", synthetic=True)
        self.assertEqual((summary["matched"], summary["unmatched"], summary["review"]), (0, 1, 2))
        requester.refresh_from_db()
        elsewhere.refresh_from_db()
        self.assertEqual((requester.employee, requester.employee_link), (None, ""))
        self.assertEqual((elsewhere.employee, elsewhere.employee_link), (None, ""))
        self.assertTrue(AuditEvent.objects.filter(action="requester.employee_unmatched").exists())

        # Case 1: both wait for an admin, with why, and every employee with the name to choose.
        listing = self.client.get(LIST)
        self.assertContains(listing, "ต้องตรวจ 2 ราย")
        self.assertContains(listing, 'data-identity="review"', count=2)
        self.assertContains(listing, "ชื่อนี้มี 2 คนในทะเบียน")
        d03 = District.objects.get(code="D03").name
        self.assertContains(listing, f"ทะเบียนระบุ{d03}")
        shared = self.client.get(page(requester.pk))
        self.assertContains(shared, "SYN10001")
        self.assertContains(shared, "SYN10003")
        self.act(elsewhere, "link-employee", employee_id="SYN10004", reason="ย้ายมาจากเขตอื่น")
        elsewhere.refresh_from_db()
        self.assertEqual(
            (elsewhere.employee.employee_id, elsewhere.employee_link), ("SYN10004", "admin")
        )

    def test_an_admin_corrects_a_wrong_district_and_the_name_is_checked_again(self):
        more = (
            "employee_id,name,district_code,division_code,position,check\n"
            "SYN10004,พนักงานจำลอง สาม,D03,,พนักงานช่าง,03/03/2530\n"
        )
        employees.import_csv(None, more, "วันเกิด", synthetic=True)
        requester = identity.create_requester(self.admin, "พนักงานจำลอง สาม", "D01", "ทดสอบ")
        self.assertIsNone(requester.employee)
        with self.assertRaises(ValidationError):
            identity.set_requester_district(self.admin, requester, "D01", "เขตเดิม")
        with self.assertRaises(PermissionDenied):
            identity.set_requester_district(self.meter, requester, "D03", "ไม่ใช่แอดมิน")
        self.act(requester, "district", district="D03", reason="")
        requester.refresh_from_db()
        self.assertEqual(requester.district.code, "D03")
        self.assertEqual(
            (requester.employee.employee_id, requester.employee_link), ("SYN10004", "name")
        )
        event = AuditEvent.objects.get(action="requester.district_changed")
        self.assertEqual((event.data["previous"], event.data["district"]), ("D01", "D03"))
        self.assertContains(
            self.client.get(page(requester.pk)), "แอดมินแก้หน่วยงาน (เขต/ฝ่าย) · D01 → D03"
        )
        internal = Requester.objects.create(
            display_name="ผอ. (กคว.)",
            district=requester.district,
            approved_by=self.admin,
            is_internal=True,
        )
        with self.assertRaises(ValidationError):
            identity.set_requester_district(self.admin, internal, "D01", "ผู้แจ้งภายใน")

    def test_an_admin_link_or_unlink_is_kept_by_later_imports(self):
        requester = identity.create_requester(self.admin, "พนักงานจำลอง หนึ่ง", "D01", "ทดสอบ")
        with self.assertRaises(ValidationError):
            identity.link_requester_employee(self.admin, requester, "NOPE", "ผิดรหัส")
        self.act(requester, "unlink-employee", reason="คนละคน")
        requester.refresh_from_db()
        self.assertEqual((requester.employee, requester.employee_link), (None, "unlinked"))
        employees.import_csv(None, DIRECTORY, "วันเกิด", synthetic=True)
        requester.refresh_from_db()
        self.assertIsNone(requester.employee)
        self.act(requester, "link-employee", employee_id="SYN20002", reason="ย้ายมาจากกอง")
        employees.import_csv(None, DIRECTORY, "วันเกิด", synthetic=True)
        requester.refresh_from_db()
        self.assertEqual(
            (requester.employee.employee_id, requester.employee_link), ("SYN20002", "admin")
        )
        history = self.client.get(page(requester.pk))
        self.assertContains(history, "แอดมินยกเลิกการผูกทะเบียนพนักงาน")
        self.assertContains(history, "คนละคน")


class LineAudienceTests(RequesterTestCase):
    def test_the_approval_page_opens_the_bot_to_everyone_who_registers(self):
        stranger = "U" + "d" * 32  # not on the test host's LINE_ALLOWED_RECIPIENTS
        self.assertEqual(self.say(self.event(stranger, "follow")), [])
        listing = self.client.get(LIST)
        self.assertContains(listing, "ใครคุยกับบอทได้")
        self.assertContains(listing, "เฉพาะบัญชีทดสอบ")

        self.client.force_login(self.meter)
        options.change(None, options.REGISTRATION, "staff", "ทดสอบ")
        self.assertNotContains(self.client.get(LIST), 'name="key" value="line_audience"')
        self.client.force_login(self.admin)
        response = self.client.post(
            "/console/admin/options/",
            {"key": "line_audience", "value": "everyone", "reason": "", "next": "registrations"},
        )
        self.assertEqual(options.get(options.LINE_AUDIENCE), "everyone")
        self.assertRedirects(response, LIST)
        self.assertContains(self.client.get(LIST), "ทุกคน: ทักมาลงทะเบียนได้")
        self.assertTrue(texts(self.type(stranger, "สวัสดีครับ")))  # the bot answers now
        event = AuditEvent.objects.get(action="option.changed", data__key="line_audience")
        self.assertEqual((event.actor, event.data["value"]), (self.admin, "everyone"))


class SuspendRevokeTests(RequesterTestCase):
    def test_suspend_and_open_again_need_a_reason_and_push_nothing(self):
        requester = self.approve(ALICE)
        self.act(requester, "suspend", reason="")

        self.assertEqual(Requester.objects.get(pk=requester.pk).status, "suspended")
        self.assertIn("ถูกระงับ", texts(self.type(ALICE, "สวัสดี")))
        self.assertIsNone(identity.resolve_requester("line", ALICE))
        with self.assertRaises(ValidationError):
            identity.set_requester_active(self.admin, requester, False, "ซ้ำ")
        self.act(requester, "activate", reason="กลับมาแล้ว")
        self.assertEqual(identity.resolve_requester("line", ALICE), requester)
        reasons = AuditEvent.objects.filter(target_type="Requester", target_id=str(requester.pk))
        self.assertEqual(
            sorted(reasons.values_list("action", "reason")),
            [("requester.activated", "กลับมาแล้ว"), ("requester.suspended", "")],
        )
        self.assertFalse(NotificationDelivery.objects.exists())

    def test_revoke_is_final_keeps_the_history_and_the_staff_link(self):
        requester = self.approve(ALICE)
        self.type(ALICE, "สวัสดีครับ")
        self.act(requester, "revoke", reason="ลาออก")
        self.assertEqual(Requester.objects.get(pk=requester.pk).status, "active")
        self.act(requester, "revoke", reason="ลาออก", confirm="1")
        requester.refresh_from_db()
        self.assertEqual((requester.status, self.line(ALICE).status), ("revoked", "revoked"))
        self.assertTrue(ConversationMessage.objects.filter(requester=requester).exists())
        with self.assertRaises(ValidationError):
            identity.set_requester_active(self.admin, requester, True, "กลับมา")
        event = AuditEvent.objects.get(action="requester.revoked")
        self.assertEqual((event.reason, event.data["line_accounts"]), ("ลาออก", 1))
        self.assertFalse(NotificationDelivery.objects.exists())
        self.assertContains(self.client.get(LIST), "ถอนการรับรองแล้ว (1)")
        self.assertContains(self.client.get(page(requester.pk)), "จัดการเพิ่มไม่ได้")

        self.assertIn("ต้องลงทะเบียนและรับรองใหม่", texts(self.type(ALICE, "สวัสดี")))
        again = identity.submit_registration("line", ALICE, "สมหญิง ใหม่", "D02")
        renewed = identity.approve_registration(self.admin, again)
        self.assertNotEqual(renewed.pk, requester.pk)

        staff = self.approve(BOB)
        ChannelIdentity.objects.filter(external_id=BOB).update(staff_user=self.meter)
        identity.revoke_requester(self.admin, staff, "ใช้ LINE ในฐานะเจ้าหน้าที่อย่างเดียว")
        self.assertEqual(
            (self.line(BOB).status, self.line(BOB).staff_user), ("revoked", self.meter)
        )


class MessagesTests(RequesterTestCase):
    def anonymous_then_approved(self):
        """The same LINE account reports a suggestion anonymously, then registers."""
        options.change(None, options.ANONYMOUS, "on", "ทดสอบ")
        self.say(self.event(ALICE, "follow"))
        self.press(ALICE, "ไม่ระบุตัวตน")
        self.press(ALICE, "ข้อเสนอแนะ")
        self.type(ALICE, "อยากให้มีที่จอดรถรับของ")
        self.press(ALICE, "ข้าม")
        self.press(ALICE, "ข้าม")
        self.say(self.location(ALICE))
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        registration = identity.submit_registration("line", ALICE, "สมหญิง ทดสอบ", "D01")
        requester = identity.approve_registration(self.admin, registration)
        self.type(ALICE, "<b>ขอบคุณ</b>")
        return requester

    def test_admin_reads_only_what_was_typed_as_this_requester_and_it_is_audited(self):
        requester = self.anonymous_then_approved()
        mine = ConversationMessage.objects.get(text="<b>ขอบคุณ</b>")
        hidden = ConversationMessage.objects.get(text="อยากให้มีที่จอดรถรับของ")
        self.assertEqual(mine.requester, requester)
        self.assertTrue(hidden.requester.is_anonymous)

        self.assertContains(self.client.get(page(requester.pk)), "1 ข้อความ")
        shown = self.client.get(page(requester.pk, "messages/"))
        self.assertContains(shown, "&lt;b&gt;ขอบคุณ&lt;/b&gt;")
        self.assertNotContains(shown, "อยากให้มีที่จอดรถรับของ")
        event = AuditEvent.objects.get(action="requester.messages_viewed")
        self.assertEqual((event.actor, event.data["shown"]), (self.admin, 1))

    def test_the_migration_gives_old_messages_the_requester_they_were_typed_as(self):
        requester = self.anonymous_then_approved()
        ConversationMessage.objects.update(requester=None)
        backfill = import_module("operations.migrations.0028_requester_admin").backfill
        backfill(apps, None)
        self.assertEqual(
            ConversationMessage.objects.get(text="<b>ขอบคุณ</b>").requester_id, requester.pk
        )
        self.assertTrue(
            ConversationMessage.objects.get(text="อยากให้มีที่จอดรถรับของ").requester.is_anonymous
        )
