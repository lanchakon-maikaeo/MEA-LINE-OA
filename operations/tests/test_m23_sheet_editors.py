"""ชีทที่ระบบอ่าน (ADR 0033, 29 ต.ค. 2569).

Acceptance → tests: both kinds of sheet (ค้นในชีท of the LINE flows and งานติดตาม กอจ.) sit on
one page and the old addresses lead to it (OnePageTests); an admin names ผู้ดูแลชีท one person at
a time for a กอง, แผนก or กลุ่มงาน, who then sees and changes every sheet of that unit and of the
units below it and nothing else, and loses it when the admin takes it back (EditorTests); a new
sheet takes a pasted link, and ทดสอบอ่าน shows the first rows, or the tabs when the tab is wrong,
without saving (FormTests).

grill 29 ต.ค. 2569 ("ไม่เอาการตั้งค่าที่เป็นลักษณะแต่ละชีทมาปนกับชีทอื่น"): a ค้นสถานะ sheet's
form shows the columns of its แบบชีท only, saving clears those of the other แบบ, and ทดสอบอ่าน
shows the line the chat would answer for the first row; a กอจ. sheet shows the link and tab only
for Google Sheets and takes its CSV file on save (TypeTests).
"""

from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from operations.models import (
    AuditEvent,
    KojSource,
    SheetEditor,
    SheetSource,
    WorkGroup,
)
from operations.tests.test_m21_org_units import UnitTestCase

PAGE = "/console/sheets/"
SHEET_ID = "1" + "a" * 40


def _unit_key(source):
    if source is None:
        return ""
    if source.work_group_id:
        return f"g{source.work_group_id}"
    return f"s{source.section_id}" if source.section_id else f"d{source.division_id}"


def lookup_form(source=None, **changes):
    form = {
        "do": "save",
        "kind": "lookup",
        "source": source.key if source else "",
        "name": source.name if source else "ชีทใหม่",
        "link": source.spreadsheet_id if source else SHEET_ID,
        "sheet_name": source.sheet_name if source else "ข้อมูล",
        "key_column": "J",
        "status_column": "R",
        "first_row": "2",
        "match": "digits",
        "number_label": "เลข UI",
        # The form sends the sheet's unit back as it is unless someone moves it.
        "unit": _unit_key(source),
        "reason": "ทดสอบผู้ดูแลชีท",
    }
    return {**form, **changes}


class SheetTestCase(UnitTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.group = WorkGroup.objects.create(section=cls.repair, name="กลุ่มงานทดสอบชีท")
        common = {"spreadsheet_id": SHEET_ID, "sheet_name": "ข้อมูล", "division": cls.division}
        cls.mine = SheetSource.objects.create(
            key="repair-sheet", name="ชีทสถานะของแผนกซ่อม", section=cls.repair, **common
        )
        cls.top = SheetSource.objects.create(key="kae-sheet", name="ชีทสถานะระดับกอง", **common)
        cls.other = SheetSource.objects.create(
            key="caps-sheet", name="ชีทสถานะของแผนกคาปา", section=cls.caps, **common
        )
        cls.koj = KojSource.objects.create(
            name="ใบเบิกของกลุ่มงานทดสอบ",
            provider="csv",
            service="withdrawal",
            default_equipment="transformer",
            columns={"row_key": "ID", "reference": "ใบเบิก", "status": "สถานะ", "district": "เขต"},
            division=cls.division,
            section=cls.repair,
            work_group=cls.group,
        )

    def as_user(self, user):
        client = Client()
        client.force_login(user)
        return client

    def grant(self, unit_key, user=None):
        response = self.as_user(self.admin).post(
            PAGE,
            {
                "do": "grant",
                "person": (user or self.kae).pk,
                "unit": unit_key,
                "reason": "ให้ดูแลชีทของหน่วย",
            },
        )
        self.assertEqual(response.status_code, 302)


class OnePageTests(SheetTestCase):
    def test_both_kinds_sit_on_one_page_and_the_old_addresses_lead_to_it(self):
        client = self.as_user(self.admin)
        with patch(
            "operations.koj_sheets.reader_email", return_value="reader@x.iam.gserviceaccount.com"
        ):
            page = client.get(PAGE).content.decode()
        for name in (self.mine.name, self.top.name, self.other.name, self.koj.name):
            self.assertIn(name, page)
        self.assertIn("ค้นสถานะในแชต", page)
        self.assertIn("ติดตามใบเบิก", page)
        self.assertIn("reader@x.iam.gserviceaccount.com", page)
        for old in ("/koj/", "/console/admin/sheets/"):
            response = client.get(old)
            self.assertEqual((response.status_code, response.url), (302, PAGE))
        # The admin menu points at the one page.
        self.assertIn(f'href="{PAGE}"', client.get("/console/admin/").content.decode())
        self.assertEqual(self.as_user(self.kae).get(PAGE).status_code, 403)


class EditorTests(SheetTestCase):
    def test_a_section_editor_changes_the_sheets_of_the_section_and_below_only(self):
        self.grant(f"s{self.repair.pk}")
        self.assertTrue(AuditEvent.objects.filter(action="sheet_editor.granted").exists())
        client = self.as_user(self.kae)
        self.assertIn(f'href="{PAGE}"', client.get("/console/").content.decode())
        page = client.get(PAGE).content.decode()
        self.assertIn(self.mine.name, page)
        self.assertIn(self.koj.name, page)  # its กลุ่มงาน is below the แผนก
        self.assertNotIn(self.top.name, page)
        self.assertNotIn(self.other.name, page)

        client.post(PAGE, lookup_form(self.mine, sheet_name="รวมข้อมูล"))
        self.mine.refresh_from_db()
        self.assertEqual(self.mine.sheet_name, "รวมข้อมูล")
        self.assertEqual(self.mine.updated_by, self.kae)

        response = client.post(PAGE, lookup_form(self.other, sheet_name="แท็บอื่น"))
        self.assertEqual(response.status_code, 403)
        self.other.refresh_from_db()
        self.assertEqual(self.other.sheet_name, "ข้อมูล")

        # Not up to the กอง: the unit list holds the แผนก and its กลุ่มงาน only.
        client.post(PAGE, lookup_form(self.mine, unit=f"d{self.division.pk}"))
        self.mine.refresh_from_db()
        self.assertEqual(self.mine.section, self.repair)

        response = client.post(
            PAGE, {"do": "grant", "person": self.kae.pk, "unit": f"d{self.division.pk}"}
        )
        self.assertEqual(response.status_code, 403)

    def test_an_editor_changes_a_koj_sheet_of_the_unit(self):
        self.grant(f"s{self.repair.pk}")
        response = self.as_user(self.kae).post(
            PAGE,
            {
                "do": "save",
                "kind": "koj",
                "source": self.koj.pk,
                "name": "ใบเบิกของกลุ่มงาน (แก้แล้ว)",
                "provider": "csv",
                "service": "return",
                "default_equipment": "transformer",
                "column_row_key": "ID",
                "column_reference": "ใบคืน",
                "column_status": "สถานะ",
                "column_district": "เขต",
                "unit": f"g{self.group.pk}",
                "reason": "เปลี่ยนเป็นใบส่งคืน",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.koj.refresh_from_db()
        self.assertEqual((self.koj.service, self.koj.columns["reference"]), ("return", "ใบคืน"))
        self.assertEqual(self.koj.work_group, self.group)

    def test_an_admin_takes_the_right_back(self):
        self.grant(f"g{self.group.pk}")
        client = self.as_user(self.kae)
        self.assertEqual(client.get(PAGE).status_code, 200)
        editor = SheetEditor.objects.get(user=self.kae)
        response = self.as_user(self.admin).post(
            PAGE, {"do": "revoke", "editor": editor.pk, "reason": "ย้ายงาน"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(SheetEditor.objects.exists())
        self.assertTrue(AuditEvent.objects.filter(action="sheet_editor.revoked").exists())
        self.assertEqual(client.get(PAGE).status_code, 403)


class FormTests(SheetTestCase):
    def test_a_new_sheet_takes_a_pasted_link(self):
        self.grant(f"s{self.repair.pk}")
        link = "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz_0123456789-x/edit#gid=0"
        response = self.as_user(self.kae).post(
            PAGE, lookup_form(name="ชีทสถานะของกลุ่มงาน", link=link, unit=f"g{self.group.pk}")
        )
        self.assertEqual(response.status_code, 302)
        source = SheetSource.objects.get(name="ชีทสถานะของกลุ่มงาน")
        self.assertEqual(source.spreadsheet_id, "1AbCdEfGhIjKlMnOpQrStUvWxYz_0123456789-x")
        self.assertEqual(source.work_group, self.group)
        self.assertTrue(source.key)

    def test_trying_a_sheet_shows_the_first_rows_and_saves_nothing(self):
        client = self.as_user(self.admin)
        columns = [["เลข UI", "", "90000001"], ["สถานะ", "", "รับเรื่องแล้ว"]]
        form = {**lookup_form(self.mine, sheet_name="รวมข้อมูล"), "do": "test"}
        with patch("operations.koj_sheets.read_columns", return_value=columns) as read:
            page = client.post(PAGE, form).content.decode()
        self.assertEqual(read.call_args.args[:3], (SHEET_ID, "รวมข้อมูล", ["J", "R"]))
        self.assertIn("เลข UI 90000001 สถานะจาก ฝอจ.: รับเรื่องแล้ว", page)
        self.mine.refresh_from_db()
        self.assertEqual(self.mine.sheet_name, "ข้อมูล")

        with (
            patch("operations.koj_sheets.read_columns", side_effect=ValidationError("อ่านไม่ได้")),
            patch("operations.koj_sheets.tab_names", return_value=["ชีต1", "รวมข้อมูล 2569"]),
        ):
            page = client.post(PAGE, form).content.decode()
        self.assertIn("รวมข้อมูล 2569", page)


class TypeTests(SheetTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.tools = SheetSource.objects.create(
            key="tool-sheet",
            name="ชีทซ่อมเครื่องมือของแผนก",
            spreadsheet_id=SHEET_ID,
            sheet_name="ข้อมูล",
            division=cls.division,
            section=cls.repair,
            match=SheetSource.Match.CODE,
            key_column="G",
            status_column="M",
            district_column="B",
            name_column="D",
            order_column="J",
            done_column="L",
            done_words={"11": "ซ่อมเสร็จ ส่งคืนแล้ว"},
            number_label="หมายเลขอุปกรณ์",
        )
        cls.mine.owner_columns = "B,M"
        cls.mine.save()

    def tool_form(self, **changes):
        form = {"match": "code", "number_label": "หมายเลขอุปกรณ์", "key_column": "G"}
        form |= {"status_column": "M", "district_column": "B", "name_column": "D"}
        form |= {"order_column": "J", "done_column": "L"}
        return lookup_form(self.tools, **{**form, **changes})

    def test_a_sheet_shows_the_columns_of_its_type_only(self):
        client = self.as_user(self.admin)
        page = client.get(f"{PAGE}?lookup={self.mine.key}").content.decode()
        self.assertIn('data-when="match=code" hidden disabled', page)
        self.assertNotIn('data-when="match=digits" hidden', page)
        page = client.get(f"{PAGE}?lookup={self.tools.key}").content.decode()
        self.assertIn('data-when="match=digits" hidden disabled', page)
        self.assertNotIn('data-when="match=code" hidden', page)

    def test_changing_the_type_clears_the_columns_of_the_other_type(self):
        client = self.as_user(self.admin)
        client.post(PAGE, self.tool_form(match="digits", owner_columns="C"))
        self.tools.refresh_from_db()
        self.assertEqual(self.tools.match, "digits")
        self.assertEqual(self.tools.owner_columns, "C")
        cleared = [self.tools.district_column, self.tools.name_column, self.tools.order_column]
        cleared += [self.tools.done_column, self.tools.done_words]
        self.assertEqual(cleared, ["", "", "", "", {}])

        client.post(PAGE, lookup_form(self.mine, match="code", district_column="C"))
        self.mine.refresh_from_db()
        self.assertEqual((self.mine.match, self.mine.owner_columns), ("code", ""))
        self.assertEqual(self.mine.district_column, "C")

    def test_trying_a_sheet_shows_what_the_chat_would_answer(self):
        sheet = {
            "G": ["หมายเลข", "NC801"],
            "M": ["สถานะ", "11"],
            "B": ["ฟข.", "บางเขน"],
            "D": ["ชื่อเครื่องมือ", "คีมตัดสาย"],
            "J": ["วันเข้า", "1/10/2569"],
            "L": ["วันรับกลับ", ""],
            "O": ["หมายเหตุ", "รออะไหล่"],
        }
        form = {
            **self.tool_form(extra_columns="O=หมายเหตุ", status_words="11=ซ่อมเสร็จ รอส่งคืน"),
            "do": "test",
        }
        with patch(
            "operations.koj_sheets.read_columns",
            side_effect=lambda _sheet, _tab, wanted, *rows: [sheet[c] for c in wanted],
        ) as read:
            page = self.as_user(self.admin).post(PAGE, form).content.decode()
        self.assertEqual(read.call_args.args[2], ["G", "M", "B", "D", "J", "L", "O"])
        self.assertIn("หมายเลขอุปกรณ์ NC801 (คีมตัดสาย): ซ่อมเสร็จ รอส่งคืน · หมายเหตุ รออะไหล่", page)

    def test_a_csv_koj_sheet_hides_the_link_and_takes_its_file_on_save(self):
        client = self.as_user(self.admin)
        page = client.get(f"{PAGE}?koj={self.koj.pk}").content.decode()
        self.assertIn('data-when="provider=google" hidden disabled', page)
        self.assertNotIn('data-when="provider=csv" hidden', page)
        upload = SimpleUploadedFile(
            "koj.csv", "ID,ใบเบิก,สถานะ,เขต\nr1,W-1,รอเบิก,D01\n".encode(), content_type="text/csv"
        )
        response = client.post(
            PAGE,
            {
                "do": "save",
                "kind": "koj",
                "source": self.koj.pk,
                "name": self.koj.name,
                "provider": "csv",
                "service": "withdrawal",
                "default_equipment": "transformer",
                "column_row_key": "ID",
                "column_reference": "ใบเบิก",
                "column_status": "สถานะ",
                "column_district": "เขต",
                "unit": f"g{self.group.pk}",
                "reason": "นำเข้ารอบแรก",
                "file": upload,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.koj.records.count(), 1)
