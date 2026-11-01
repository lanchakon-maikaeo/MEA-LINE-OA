"""ADR 0034 (grill กอจ. 29 ต.ค. 2569): the กอจ. button files ติดตามใบเบิก and ติดตามใบส่งคืน
cases like กคว., each equipment button names its group so a unit can take it, and
งานซ่อมสวิตช์ตัดตอน answers from ซจ.'s sheet before it sends anything to ซจ."""

from django.utils import timezone

from operations import line_flows, sheet_lookup, units
from operations.models import Case, Division, LineFlow, LineMenuItem, Section, SheetSource, UnitWork
from operations.tests.test_m03_line import ALICE, buttons, texts
from operations.tests.test_m16_flow_runner import FlowTestCase

KOJ_TILE = "หม้อแปลง LBS RMU"


class KojCaseTestCase(FlowTestCase):
    def setUp(self):
        super().setUp()
        flow = LineFlow.objects.get(key="koj-tracking")
        line_flows.publish(self.flow_admin, line_flows.draft_of(flow), "ADR 0034")
        self.koj = Division.objects.get(code__in=["กอจ.", "กองอุปกรณ์ฯ"])

    def open_koj(self, user=ALICE):
        tile = LineMenuItem.objects.get(place="tile", tab="report", label=KOJ_TILE, is_active=True)
        return self.say(self.postback(user, f"m=item&id={tile.pk}"))


class FilingTests(KojCaseTestCase):
    def test_the_first_menu_has_the_agreed_buttons(self):
        reply = self.open_koj()
        self.assertIn("กรุณาเลือกอุปกรณ์ที่ต้องการติดตาม", texts(reply))
        self.assertEqual(
            list(buttons(reply)),
            ["หม้อแปลง", "LBS", "RMU", "ตู้ metering", "อุปกรณ์อื่น", "งานซ่อมสวิตช์ตัดตอน", "ยกเลิกร่าง"],
        )
        reply = self.press(ALICE, "อุปกรณ์อื่น")
        self.assertEqual(
            list(buttons(reply))[:4], ["bushing box", "VT ประกอบ LBS", "LV Cap.", "พิมพ์ชื่ออุปกรณ์"]
        )

    def test_a_transformer_withdrawal_is_filed_with_its_group(self):
        self.open_koj()
        reply = self.press(ALICE, "หม้อแปลง")
        self.assertEqual(list(buttons(reply))[:2], ["ติดตามใบเบิก", "ติดตามใบส่งคืน"])
        reply = self.press(ALICE, "ติดตามใบเบิก")
        self.assertEqual(
            list(buttons(reply))[:4],
            ["เลขที่ใบเบิก", "เลขใบสั่งงานจาก SAP", "เลขอื่น เช่น M/N", "ไม่มีหมายเลข"],
        )
        self.press(ALICE, "เลขที่ใบเบิก")
        reply = self.type(ALICE, "I/69.01.01.001.69.01.001")  # a กอจ. number keeps its dots
        # 30 ต.ค. 2569: the summary comes right after the number, as in กคว.
        self.assertIn("เพิ่มรายละเอียด", buttons(reply))
        reply = self.press(ALICE, "เพิ่มรายละเอียด")
        self.assertIn("รายละเอียดเพิ่มเติม", texts(reply))
        reply = self.type(ALICE, "3 เฟส 500 kVA 1 ลูก ใช้ติดตั้งงานใหม่")
        for line in (
            "สรุปเรื่อง: การเบิก",
            "ส่งถึง: กอจ.",
            "อุปกรณ์: หม้อแปลง",
            "เลขที่ใบเบิก: I/69.01.01.001.69.01.001",
        ):
            self.assertIn(line, texts(reply))
        reply = self.press(ALICE, "ยืนยันส่งเรื่อง")
        self.assertIn("ยื่นเรื่องแล้ว", texts(reply))
        case = Case.objects.get()
        self.assertEqual(
            (case.kind, case.owner_division, case.sap_kind, case.sap_number),
            ("withdrawal", self.koj, "slip", "I/69.01.01.001.69.01.001"),
        )
        self.assertEqual(case.intake_data["group"], "transformer")
        self.assertIn("transformer", units.case_groups(case))

    def test_a_number_repeats_only_in_the_same_division(self):
        # 30 ต.ค. 2569 Q1: a กอจ. slip was warned of a กคว. case with the same number.
        self.track("มิเตอร์", "ติดตามใบเบิก", number="12345678")
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        kcw = Case.objects.get()

        def file():
            self.open_koj()
            self.press(ALICE, "RMU")
            self.press(ALICE, "ติดตามใบเบิก")
            self.press(ALICE, "เลขที่ใบเบิก")
            return self.type(ALICE, "12345678")

        reply = file()
        self.assertIn("ส่งถึง: กอจ.", texts(reply))
        self.assertNotIn("คุณเคยส่งเลขนี้", texts(reply))
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        koj = Case.objects.exclude(pk=kcw.pk).get()
        self.assertEqual(koj.owner_division, self.koj)
        self.assertNotIn("staff_notes", koj.intake_data)
        reply = file()
        self.assertIn(f"หมายเหตุ: คุณเคยส่งเลขนี้ในเรื่อง {koj.number} ซึ่งยังไม่ปิด", texts(reply))
        self.assertNotIn(kcw.number, texts(reply))

    def test_a_unit_takes_the_group_its_button_names(self):
        section = Section.objects.create(division=self.koj, name="แผนกหม้อแปลงและคาปาซิเตอร์ ทดสอบ")
        UnitWork.objects.create(division=self.koj, section=section, kind="return", group="lv_cap")
        self.open_koj()
        self.press(ALICE, "อุปกรณ์อื่น")
        self.press(ALICE, "LV Cap.")
        self.press(ALICE, "ติดตามใบส่งคืน")
        reply = self.press(ALICE, "ใบโอนขอคืน")
        self.assertEqual(texts(reply), "กรุณาพิมพ์เลขใบโอนขอคืน")
        reply = self.type(ALICE, "TR-2569-0042")
        self.assertIn("ใบโอนขอคืน: TR-2569-0042", texts(reply))
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual((case.kind, case.sap_kind), ("return", "return_transfer"))
        self.assertEqual(units.section_for(case), section)

    def test_another_number_and_a_typed_equipment(self):
        self.open_koj()
        self.press(ALICE, "อุปกรณ์อื่น")
        self.press(ALICE, "พิมพ์ชื่ออุปกรณ์")
        reply = self.type(ALICE, "ล่อฟ้าแรงสูง")
        self.assertIn("อุปกรณ์ที่พิมพ์ชื่อ", texts(reply))
        self.press(ALICE, "ติดตามใบเบิก")
        reply = self.press(ALICE, "เลขอื่น เช่น M/N")
        self.assertEqual(texts(reply), "กรุณาพิมพ์เลขที่ใช้ติดตาม เช่น M/N 123/2569")
        self.type(ALICE, "M/N 123/2569")
        reply = self.type(ALICE, "10 ตัว ใช้งานบำรุงรักษา")
        self.assertIn("อุปกรณ์: ล่อฟ้าแรงสูง", texts(reply))
        self.assertNotIn("อุปกรณ์: อุปกรณ์อื่น", texts(reply))
        self.assertIn("เลขอื่น: M/N 123/2569", texts(reply))
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual((case.sap_kind, case.sap_number), ("other_number", "M/N 123/2569"))
        self.assertEqual(case.intake_data["equipment_name"], "ล่อฟ้าแรงสูง")

    def test_no_number_asks_the_other_details(self):
        self.open_koj()
        self.press(ALICE, "ตู้ metering")
        self.press(ALICE, "ติดตามใบเบิก")
        reply = self.press(ALICE, "ไม่มีหมายเลข")
        self.assertEqual(texts(reply), "กรุณาระบุรายละเอียดอื่นๆ\nเช่น ขนาด จำนวน และวันที่ส่งใบ")
        reply = self.type(ALICE, "ตู้ metering 1 ตู้ ส่งใบเบิก 28 ต.ค.")
        self.assertIn("สรุปเรื่อง: การเบิก", texts(reply))
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual((case.sap_number, case.intake_data["group"]), ("", "metering"))
        self.assertTrue(case.intake_data["no_number"])


class SwitchRepairTests(KojCaseTestCase):
    def setUp(self):
        super().setUp()
        self.source = SheetSource.objects.get(key="koj-switch-repairs")

    def ask(self, text):
        self.open_koj()
        reply = self.press(ALICE, "งานซ่อมสวิตช์ตัดตอน")
        self.assertEqual(texts(reply), "กรุณาพิมพ์เลขหนังสือแจ้ง หรือเลข WO, WBS, เลขรับ, ใบแจ้งซ่อม")
        self.assertNotIn("ส่งให้ ซจ. ตรวจ", buttons(reply))
        return self.type(ALICE, text)

    def test_the_sheet_is_set_up_as_agreed(self):
        s = self.source
        self.assertEqual(
            (s.enabled, s.key_column, s.status_column, s.done_column, s.district_column),
            (True, "E,K", "AF", "AE", ""),
        )
        self.assertEqual([c["column"] for c in s.extra_columns], ["R", "G", "M", "Z"])
        self.assertFalse(s.ai_words)
        sheet_lookup.check_source(s)
        read = [*sheet_lookup.key_columns(s), s.status_column, s.done_column]
        read += [c["column"] for c in s.extra_columns]
        for private in ("H", "I", "J", "AA", "AG"):  # Q23: names, places, contacts, notes
            self.assertNotIn(private, read)

    def load(self):
        sheet_lookup.apply_columns(
            self.source,
            ["ซจ.123/2569", "ซจ.124/2569"],  # E หนังสือแจ้งเลขที่
            ["รอซ่อม", "ปฏิบัติงานแล้ว"],  # AF สถานะงาน
            extras=[["ซ่อม RMU", "ซ่อม LBS"], ["1/10/2569", "2/10/2569"], [], []],
            dones=["", "x"],  # AE ยกเลิก
            more_keys=[["4100012345", "I/69.01.01.001.69.01.001"]],  # K
        )
        SheetSource.objects.filter(pk=self.source.pk).update(last_synced_at=timezone.now())

    def test_a_number_in_either_column_answers_from_the_sheet(self):
        self.load()
        said = texts(self.ask("4100012345"))
        self.assertIn("เลขงานซ่อม 4100012345: รอซ่อม", said)
        self.assertIn("สรุปลักษณะงาน ซ่อม RMU", said)
        self.assertIn("อยากทราบเลขงานซ่อมอื่นอีกไหม", said)
        self.assertNotIn("ส่งให้ ซจ. ตรวจ", buttons(self.type(ALICE, "ซจ.123/2569")))
        said = texts(self.type(ALICE, "I/69.01.01.001.69.01.001"))
        self.assertIn("ยกเลิกงานแล้ว", said)  # AE has a value
        self.assertFalse(Case.objects.exists())

    def test_a_number_not_found_goes_to_ซจ_with_the_phone(self):
        self.load()
        SheetSource.objects.filter(pk=self.source.pk).update(contact="02-000-0000")
        reply = self.ask("4199999999")
        self.assertIn("ไม่พบเลขงานซ่อม 4199999999", texts(reply))
        self.assertIn("หรือโทร 02-000-0000", texts(reply))
        reply = self.press(ALICE, "ส่งให้ ซจ. ตรวจ")
        self.assertIn("ส่งถึง: กอจ. แผนกซ่อมบำรุงสวิตช์ตัดตอนระบบจำหน่าย", texts(reply))
        self.assertIn("เลขงานซ่อม: 4199999999", texts(reply))
        self.assertNotIn("แก้ไข", buttons(reply))
        reply = self.press(ALICE, "ยืนยันส่งเรื่อง")
        self.assertIn("ส่งให้ ซจ. ตรวจแล้ว", texts(reply))
        case = Case.objects.get()
        self.assertEqual((case.topic, case.owner_division), ("repair", self.koj))
        self.assertIn("เลขงานซ่อม: 4199999999", case.description)
        self.assertEqual(case.lines.count(), 0)
        self.assertEqual((case.sap_kind, case.sap_number), ("other_number", "4199999999"))

    def test_without_a_phone_no_phone_line(self):
        self.load()
        self.assertNotIn("หรือโทร", texts(self.ask("4199999999")))
