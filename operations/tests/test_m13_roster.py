"""ทะเบียนพนักงานจากไฟล์ขององค์กร และการยืนยันตัวตนด้วยอีเมล (15 ต.ค. 2569 Q10–Q16, Q24–Q29).

Acceptance → tests: the roster comes in from .csv or .xlsx through a preview that maps columns
and ฝ่าย, never reads the name or phone columns (names left the roster, 23 Oct 2569) and keeps
e-mails only hashed and sealed (ImportTests);
an employee registers in LINE with their ID and a code sent to the roster e-mail or the e-mail
typed, approved by the system and logged with the method, while anything else still reaches a
person (LineEmailTests).
"""

import io
import re

from django.core import mail
from django.test import Client, override_settings

from operations import options, roster, sealing
from operations.models import (
    AuditEvent,
    ChannelIdentity,
    District,
    Division,
    EmailCode,
    Employee,
    RegistrationRequest,
    Requester,
    Role,
    RosterSetting,
    RosterUpload,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import ALICE, BOB, LineTestCase, buttons, texts
from operations.tests.test_m13_org import real_world

HEADERS = [
    "รหัสพนักงาน",
    "ชื่อ-สกุล",
    "ตำแหน่ง",
    "สายงาน",
    "ฝ่าย",
    "กอง",
    "แผนก",
    "กลุ่มงาน",
    "อีเมล",
    "เบอร์ภายใน",
    "เบอร์สวัสดิการ กฟน.",
]
ROWS = [
    ["510001", "นางสาวทดสอบ หนึ่ง", "วิศวกร 6", "สายงานจำลอง", "การไฟฟ้านครหลวงเขตวัดเลียบ",
     "กองจำลอง", "แผนกจำลอง", "กลุ่มจำลอง", "one@example.test", "1234", "0811111111"],
    ["510002", "นายทดสอบ สอง", "พนักงานช่าง 5", "สายงานจำลอง", "ฝ่ายอุปกรณ์งานจำหน่าย (จำลอง)",
     "กองจำลอง", "", "", "two@example.test", "5678", "0822222222"],
    ["510002", "ซ้ำ", "", "", "", "", "", "", "", "", ""],
    ["", "ไม่มีรหัส", "", "", "", "", "", "", "", "", ""],
]  # fmt: skip


def as_csv(headers=HEADERS, rows=ROWS):
    out = io.StringIO()
    out.write("ทะเบียนพนักงาน (ข้อมูลจำลอง)\n")  # a title row above the headers
    for row in [headers, *rows]:
        out.write(",".join(row) + "\n")
    return out.getvalue().encode("utf-8-sig")


def as_xlsx(headers=HEADERS, rows=ROWS):
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.append(headers)
    for row in rows:
        sheet.append([int(v) if v.isdigit() and len(v) == 6 else v for v in row])
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


class RosterTestCase(LineTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        real_world(cls)

    def load(self, data=None, name="roster.csv", choices=None, deactivate=False):
        upload = roster.stage(self.admin, name, data or as_csv())
        headers, _ = roster.opened(upload)
        columns = roster.guess_columns(headers)
        return roster.import_upload(
            self.admin, upload, columns, choices or {}, deactivate, "ทะเบียนจำลองเพื่อทดสอบ"
        )


class ImportTests(RosterTestCase):
    def test_the_preview_maps_columns_and_reports_problems(self):
        upload = roster.stage(self.admin, "roster.csv", as_csv())
        headers, rows = roster.opened(upload)
        columns = roster.guess_columns(headers)
        self.assertEqual(columns["employee_id"], "รหัสพนักงาน")
        self.assertEqual(columns["email"], "อีเมล")
        self.assertNotIn("เบอร์ภายใน", columns.values())
        self.assertNotIn("ชื่อ-สกุล", columns.values())  # names are no field
        view = roster.preview(headers, rows, columns)
        self.assertEqual(
            (view["usable"], view["duplicates"], view["incomplete"]), (2, ["510002"], [6])
        )
        units = {u["text"]: u for u in view["units"]}
        self.assertEqual(units["การไฟฟ้านครหลวงเขตวัดเลียบ"]["match"].code, "D01")
        self.assertIsNone(units["ฝ่ายอุปกรณ์งานจำหน่าย (จำลอง)"]["match"])
        self.assertNotIn("one@example.test", upload.sealed)  # waiting rows are sealed

    def test_import_keeps_emails_sealed_and_never_reads_phones(self):
        summary = self.load(choices={roster.unit_key("ฝ่ายอุปกรณ์งานจำหน่าย (จำลอง)"): roster.NEW_UNIT})
        self.assertEqual((summary["created"], summary["emails"]), (2, 2))
        one = Employee.objects.get(employee_id="510001")
        self.assertEqual(
            (one.position, one.department, one.district.code),
            ("วิศวกร 6", "การไฟฟ้านครหลวงเขตวัดเลียบ", "D01"),
        )
        self.assertEqual(one.email_hash, roster.email_digest("ONE@example.test "))
        self.assertNotIn("one@", one.email_sealed)
        self.assertEqual(sealing.unseal(one.email_sealed), "one@example.test")
        two = Employee.objects.get(employee_id="510002")
        self.assertEqual(two.district.name, "ฝ่ายอุปกรณ์งานจำหน่าย (จำลอง)")  # a new unit
        stored = str(list(Employee.objects.values()))
        self.assertNotIn("0811111111", stored)
        self.assertNotIn("1234", stored)
        self.assertNotIn("ทดสอบ หนึ่ง", stored)
        self.assertFalse(RosterUpload.objects.exists())  # the staged file is gone
        event = AuditEvent.objects.get(action="employees.imported")
        self.assertNotIn("510001", str(event.data))
        self.assertNotIn("example.test", str(event.data))
        setting = RosterSetting.objects.get()
        self.assertNotIn("name", setting.columns)
        self.assertEqual(
            setting.units[roster.unit_key("ฝ่ายอุปกรณ์งานจำหน่าย (จำลอง)")], two.district.code
        )

    def test_xlsx_with_numeric_ids_and_a_later_import_closes_the_missing(self):
        self.load(as_xlsx(), "roster.xlsx")
        self.assertTrue(Employee.objects.filter(employee_id="510001").exists())
        line = ChannelIdentity.objects.create(channel="line", external_id=BOB)
        requester = Requester.objects.create(
            display_name="นายทดสอบ สอง",
            district=District.objects.get(code="D01"),
            approved_by=self.admin,
            employee=Employee.objects.get(employee_id="510002"),
            employee_link=Requester.EmployeeLink.VERIFIED,
        )
        line.requester = requester
        line.save()
        summary = self.load(as_csv(rows=ROWS[:1]), deactivate=True)
        self.assertEqual(summary["closed"], 1)
        self.assertFalse(Employee.objects.get(employee_id="510002").is_active)
        client = Client()
        client.force_login(self.admin)
        page = client.get("/console/admin/registrations/")
        self.assertContains(page, "ไม่อยู่ในทะเบียนล่าสุด")
        self.assertContains(page, "พนักงานช่าง 5 · ฝ่ายอุปกรณ์งานจำหน่าย (จำลอง)")

    def test_the_admin_page_reads_previews_and_imports(self):
        client = Client()
        client.force_login(self.admin)
        file = io.BytesIO(as_xlsx())
        file.name = "roster.xlsx"
        page = client.post(
            "/console/admin/employees/", {"action": "upload", "file": file}, follow=True
        )
        self.assertContains(page, "ตรวจก่อนนำเข้า")
        self.assertContains(page, "ฝ่ายอุปกรณ์งานจำหน่าย (จำลอง)")
        upload = RosterUpload.objects.get()
        form = {"upload": str(upload.pk), "action": "import", "reason": "ทดสอบนำเข้า"}
        form.update({f"col_{f}": h for f, h in roster.guess_columns(HEADERS).items()})
        form["unit_key"] = roster.unit_key("ฝ่ายอุปกรณ์งานจำหน่าย (จำลอง)")
        form["unit_choice"] = ""
        page = client.post("/console/admin/employees/", form, follow=True)
        self.assertContains(page, "นำเข้าทะเบียนพนักงานแล้ว")
        self.assertIsNone(Employee.objects.get(employee_id="510002").district)
        staff = make_staff("kcwview", (Role.STAFF, "กคว."))
        client.force_login(staff)
        self.assertEqual(client.get("/console/admin/employees/").status_code, 403)

    @override_settings(APP_ENV="production", PII_SECRET_KEY="")
    def test_no_key_on_the_server_means_no_import(self):
        self.assertFalse(sealing.available())
        with self.assertRaisesMessage(Exception, "PII_SECRET_KEY"):
            roster.stage(self.admin, "roster.csv", as_csv())


class LineEmailTests(RosterTestCase):
    def setUp(self):
        super().setUp()
        self.load()
        Employee.objects.filter(employee_id="510001").update(
            division=Division.objects.get(code="กคว.")
        )
        options.change(None, options.REGISTRATION_CHECK, "employee_auto", "ทดสอบ")

    def say(self, *args, **kwargs):
        # The code mail leaves after the commit, as in production.
        with self.captureOnCommitCallbacks(execute=True):
            return super().say(*args, **kwargs)

    def start(self, user=ALICE, employee_id="510001"):
        reply = self.say(self.event(user, "follow"))
        self.assertIn("เลือกประเภทผู้ใช้", texts(reply))
        self.assertIn("อื่น ๆ", buttons(reply))
        self.assertIn("รหัสพนักงาน", texts(self.press(user, "พนักงาน กฟน.")))
        return self.type(user, employee_id)

    def code(self):
        return re.search(r"\d{6}", mail.outbox[-1].body).group(0)

    def test_a_code_from_the_roster_email_approves_by_itself(self):
        reply = self.start()
        self.assertNotIn("ทดสอบ หนึ่ง", texts(reply))  # the ID alone shows no name
        self.assertIn("ส่งรหัส 6 หลัก", texts(self.press(ALICE, "ส่งรหัสทางอีเมล")))
        self.assertEqual(mail.outbox[-1].to, ["one@example.test"])
        self.assertNotIn("ทดสอบ", mail.outbox[-1].body)  # no name in the mail
        self.assertNotIn("http", mail.outbox[-1].body)
        self.assertIn("ยืนยันตัวตนแล้ว", texts(self.type(ALICE, self.code())))
        registration = RegistrationRequest.objects.get()
        self.assertEqual((registration.status, registration.verified_by), ("approved", "otp"))
        self.assertEqual(registration.submitted_name, "พนักงาน 510001")  # the ID, no name
        self.assertEqual(registration.requested_district.code, "D01")
        self.assertNotEqual(EmailCode.objects.get().code_hash, self.code())

    def test_the_typed_email_approves_or_waits_as_the_option_says(self):
        self.start()
        self.press(ALICE, "พิมพ์อีเมลเอง")
        self.assertIn("ไม่ตรง", texts(self.type(ALICE, "other@example.test")))
        self.assertIn("ยืนยันตัวตนแล้ว", texts(self.type(ALICE, "One@Example.test")))
        self.assertEqual(RegistrationRequest.objects.get().verified_by, "email")
        options.change(None, options.EMAIL_TYPED, "review", "ให้แอดมินดูก่อน")
        self.start(BOB, "510002")
        self.press(BOB, "พิมพ์อีเมลเอง")
        reply = self.type(BOB, "two@example.test")
        self.assertIn("เลือกหน่วยงาน (เขต/ฝ่าย)", texts(reply))  # the roster gives no เขต for this ฝ่าย
        reply = self.press(BOB, District.objects.get(code="D01").name)
        self.assertIn("ยืนยันตัวตนแล้ว", texts(reply))
        waiting = RegistrationRequest.objects.get(identity__external_id=BOB)
        self.assertEqual(
            (waiting.status, waiting.verified_by, waiting.employee.employee_id),
            ("approved", "email", "510002"),
        )

    def test_wrong_codes_run_out_and_the_day_has_a_limit(self):
        self.start()
        self.press(ALICE, "ส่งรหัสทางอีเมล")
        good = self.code()
        wrong = "000000" if good != "000000" else "111111"
        self.assertIn("ลองได้อีก 2", texts(self.type(ALICE, wrong)))
        self.assertIn("ลองได้อีก 1", texts(self.type(ALICE, wrong)))
        self.assertIn("ใช้ไม่ได้แล้ว", texts(self.type(ALICE, wrong)))
        self.assertIn("เลือกวิธี", texts(self.type(ALICE, good)))  # that code is spent
        self.press(ALICE, "ส่งรหัสทางอีเมล")  # the second code of the day
        self.press(ALICE, "ส่งรหัสใหม่")  # the third
        self.assertIn("ครบจำนวน", texts(self.press(ALICE, "ส่งรหัสใหม่")))

    def test_an_unknown_id_is_counted_and_another_way_reaches_a_person(self):
        self.say(self.event(ALICE, "follow"))
        self.press(ALICE, "พนักงาน กฟน.")
        for left in (2, 1):
            self.assertIn(f"ลองได้อีก {left}", texts(self.type(ALICE, "999999")))
        reply = self.type(ALICE, "999998")
        self.assertIn("เจ้าหน้าที่ตรวจแทน", texts(reply))
        self.type(ALICE, "สมหญิง ทดสอบ")
        reply = self.press(ALICE, District.objects.get(code="D01").name)
        self.assertIn("ระหว่างรอแจ้งปัญหา", texts(reply))
        registration = RegistrationRequest.objects.get()
        self.assertEqual((registration.status, registration.employee), ("pending", None))

    def test_others_register_with_a_name_and_can_report_immediately(self):
        self.say(self.event(ALICE, "follow"))
        self.assertIn("พิมพ์ชื่อของคุณ", texts(self.press(ALICE, "อื่น ๆ")))
        reply = self.type(ALICE, "ผู้รับเหมา ทดสอบ")
        self.assertIn("ลงทะเบียนแล้ว", texts(reply))
        row = ChannelIdentity.objects.get(external_id=ALICE)
        self.assertTrue(row.requester.is_external)
        self.assertFalse(RegistrationRequest.objects.exists())

    def test_legacy_approval_option_does_not_hide_registration_choices(self):
        options.change(None, options.REGISTRATION_CHECK, "approval", "ค่าเก่า")
        reply = self.say(self.event(ALICE, "follow"))
        self.assertEqual(set(buttons(reply)), {"พนักงาน กฟน.", "อื่น ๆ", "ไม่ระบุตัวตน"})
