"""หน้ารับรองผู้แจ้ง: who each approved requester is, and what an admin can do (14 Oct 2569).

The user asked for: suspend or revoke a requester (no data deleted, admins only, nothing pushed),
read what they typed, and see on the list who each person is, their position from ทะเบียนพนักงาน
and their role in the system ("user, user และ admin"). 23 Oct 2569: the roster has no names, so
nobody is tied to it by a name any more; the employee ID ties them.

Acceptance → tests: the list shows proof, position and role, without anonymous reporters, and
only admins can open a requester (RequesterListTests); a typed name never links anyone, an admin
links by employee ID, corrects the district or removes a link, imports keep it, and the
migration turns names copied from the roster into the employee label (EmployeeLinkTests); an
admin suspends,
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


class EmployeeLinkTests(RequesterTestCase):
    def test_a_typed_name_never_links_and_an_admin_links_by_employee_id(self):
        requester = identity.create_requester(self.admin, "พนักงานจำลอง หนึ่ง", "D01", "ทดสอบ")
        self.assertEqual((requester.employee, requester.employee_link), (None, ""))
        boss = make_staff("boss", (Role.ADMIN, None))
        boss.staff_profile.employee_id = "SYN10001"
        boss.staff_profile.save()
        self.assertEqual(identity.requester_roles([requester])[requester.pk], [])
        self.assertContains(self.client.get(LIST), 'data-identity="none"')

        self.act(requester, "link-employee", employee_id="SYN10001", reason="โทรถามแล้ว")
        requester.refresh_from_db()
        self.assertEqual(requester.employee_link, "admin")
        self.assertEqual(identity.requester_roles([requester])[requester.pk], ["แอดมินระบบ"])
        event = AuditEvent.objects.get(action="requester.employee_linked")
        self.assertEqual((event.reason, event.data["employee"]), ("โทรถามแล้ว", "SYN10001"))
        detail = self.client.get(page(requester.pk))
        self.assertContains(detail, "ผูกกับรหัสพนักงาน SYN10001 · พนักงานช่าง")
        self.assertNotContains(detail, "ชื่อในทะเบียน")
        self.assertContains(self.client.get(LIST), 'data-identity="verified"')

    def test_an_admin_corrects_a_wrong_district(self):
        requester = identity.create_requester(self.admin, "พนักงานจำลอง สาม", "D01", "ทดสอบ")
        with self.assertRaises(ValidationError):
            identity.set_requester_district(self.admin, requester, "D01", "เขตเดิม")
        with self.assertRaises(PermissionDenied):
            identity.set_requester_district(self.meter, requester, "D03", "ไม่ใช่แอดมิน")
        self.act(requester, "district", district="D03", reason="")
        requester.refresh_from_db()
        self.assertEqual((requester.district.code, requester.employee), ("D03", None))
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
        self.act(requester, "link-employee", employee_id="SYN10001", reason="ตรวจแล้ว")
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

    def test_the_migration_turns_names_copied_from_the_roster_into_the_label(self):
        from operations.models import RegistrationRequest, StaffProfile

        convert = import_module("operations.migrations.0056_roster_without_names").convert
        employee = Employee.objects.get(employee_id="SYN10001")
        copied = identity.create_requester(self.admin, "นายสมชาย  ใจดี", "D01", "ทดสอบ")
        typed = identity.create_requester(self.admin, "สมชาย ใจดี", "D01", "ทดสอบ")
        by_name = identity.create_requester(self.admin, "สมชาย ใจดี", "D02", "ทดสอบ")
        Requester.objects.filter(pk=copied.pk).update(employee=employee, employee_link="verified")
        Requester.objects.filter(pk=typed.pk).update(employee=employee, employee_link="admin")
        Requester.objects.filter(pk=by_name.pk).update(employee=employee, employee_link="name")
        line = ChannelIdentity.objects.create(channel="line", external_id="U" + "e" * 32)
        registration = RegistrationRequest.objects.create(
            identity=line,
            submitted_name="นายสมชาย ใจดี",
            requested_district=District.objects.get(code="D01"),
            employee=employee,
            verified_by="otp",
        )
        staff = make_staff(f"line-employee-{employee.pk}")
        StaffProfile.objects.filter(user=staff).update(
            display_name="นายสมชาย ใจดี", employee_id="SYN10001"
        )
        internal = Requester.objects.create(
            display_name="นายสมชาย ใจดี (กคว.)",
            district=District.objects.get(code="D01"),
            approved_by=self.admin,
            is_internal=True,
        )

        convert(
            Requester,
            RegistrationRequest,
            StaffProfile,
            {employee.pk: ("SYN10001", "นายสมชาย ใจดี")},
        )

        def name_of(row):
            row.refresh_from_db()
            return getattr(row, "display_name", None) or row.submitted_name

        self.assertEqual(name_of(copied), "พนักงาน SYN10001")
        self.assertEqual(name_of(typed), "สมชาย ใจดี")  # an admin link keeps the typed name
        by_name.refresh_from_db()
        self.assertEqual((by_name.employee, by_name.employee_link), (None, ""))
        self.assertEqual(name_of(registration), "พนักงาน SYN10001")
        self.assertEqual(StaffProfile.objects.get(user=staff).display_name, "พนักงาน SYN10001")
        self.assertEqual(name_of(internal), "พนักงาน SYN10001 (กคว.)")


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
        self.press(ALICE, "ปัญหาการใช้งานหรืออื่นๆ")  # ADR 0030: the division carousel
        self.type(ALICE, "อยากให้มีที่จอดรถรับของ")
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
