"""flow ของ กคอ. (grill กคอ. 28 ต.ค. 2569, ADR 0029): the tile asks what to follow, a slip may
be a เลขที่ใบเบิก, an SAP work order or an M/N, or have no number at all; คม.'s truck table names
each ฟข. by its abbreviation until an admin maps it, the truck is the default with มารับเอง as
the one button, returns are received rather than issued and carry คม.'s own stage names; and
repair tracking answers from คม.'s sheet, only the asker's ฟข. rows and never the codes, or
goes to คม. as a case when the piece is not there or the sheet is not set up."""

from datetime import timedelta
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from operations import line_flows, services, sheet_lookup, typed_input, warehouse
from operations.contracts import Actor
from operations.models import (
    Appointment,
    AuditEvent,
    AuthorityGrant,
    Case,
    CaseMessage,
    CaseMilestone,
    ConversationDraft,
    District,
    Division,
    DivisionSchedule,
    LineFlow,
    Role,
    SheetEntry,
    SheetSource,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import ALICE, buttons, texts
from operations.tests.test_m16_flow_runner import FlowTestCase

S = CaseMilestone.Stage
TILE = "เครื่องมือและอุปกรณ์"


class KkoTestCase(FlowTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.kko = make_staff("kko-clerk", (Role.STAFF, "กคอ."))
        cls.kko_division = Division.objects.get(code="กคอ.")
        cls.kko_approver = make_staff("kko-approver", (Role.STAFF, "กคอ."))
        AuthorityGrant.objects.create(
            user=cls.kko_approver,
            activity=AuthorityGrant.Activity.WITHDRAWAL_APPROVAL,
            division=None,
            valid_from=timezone.now() - timedelta(minutes=1),
            reason="ทดสอบ",
            approval_reference="TEST-KKO",
            granted_by=cls.flow_admin,
        )

    def setUp(self):
        super().setUp()
        flow = LineFlow.objects.get(key="kco-tools")
        line_flows.publish(self.flow_admin, line_flows.draft_of(flow), "ตามข้อสรุป กคอ.")

    def open_kko(self, user=ALICE):
        return self.say(self.postback(user, f"m=item&id={self.tile(TILE).pk}"))

    def file(self, job, kind=None, number=None, details=None, user=ALICE):
        self.open_kko(user)
        self.press(user, job)
        if kind is None:
            self.press(user, "ไม่มีหมายเลข")
            self.type(user, details or "คีมตัดสาย 2 อัน ส่งใบเบิก 27 ต.ค.")
        else:
            self.press(user, kind)
            self.type(user, number)  # 30 ต.ค. 2569: the summary comes right after the number
        self.press(user, "ยืนยันส่งเรื่อง")
        case = Case.objects.order_by("-submitted_at").first()
        self.assertEqual(case.status, Case.Status.AWAITING_APPROVAL)  # ADR 0044: รอรับเรื่อง
        services.perform_form_action(  # ดำเนินการ, at ปกติ
            Actor.staff(self.kko_approver),
            case.pk,
            "approve",
            expected_revision=case.revision,
            idempotency_key=f"start-{case.pk}",
            payload={"priority": "P3"},
        )
        case.refresh_from_db()
        self.assertEqual(case.status, Case.Status.IN_PROGRESS)
        return case


class TileTests(KkoTestCase):
    def test_the_tile_asks_what_to_follow(self):
        reply = self.open_kko()
        self.assertIn("กรุณาเลือกเครื่องมือเครื่องใช้ที่ต้องการติดตาม", texts(reply))
        self.assertEqual(
            list(buttons(reply)), ["ติดตามใบเบิก", "ติดตามใบส่งคืน", "ติดตามงานซ่อม", "ยกเลิกร่าง"]
        )

    def test_an_mn_number_is_a_kind_of_slip(self):
        reply = self.open_kko()
        reply = self.press(ALICE, "ติดตามใบเบิก")
        self.assertEqual(
            list(buttons(reply))[:4],
            ["เลขที่ใบเบิก", "เลขใบสั่งงานจาก SAP", "เลขที่ M/N", "ไม่มีหมายเลข"],
        )
        self.press(ALICE, "เลขที่ M/N")
        reply = self.type(ALICE, "คม. 123/2569")
        self.assertIn("เลขที่ M/N: คม. 123/2569", texts(reply))
        self.assertIn("ส่งถึง: กคอ.", texts(reply))
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual(
            (case.kind, case.owner_division.code, case.sap_kind, case.sap_number),
            ("withdrawal", "กคอ.", "memo", "คม. 123/2569"),
        )

    def test_a_slip_without_a_number_says_what_and_when(self):
        reply = self.open_kko()
        self.press(ALICE, "ติดตามใบเบิก")
        reply = self.press(ALICE, "ไม่มีหมายเลข")
        self.assertEqual(texts(reply), "กรุณาพิมพ์รายละเอียด")  # the page's words (29 ต.ค. 2569)
        reply = self.type(ALICE, "คีมตัดสาย 2 อัน ส่งใบเบิก 27 ต.ค.")
        self.assertIn("เลขติดตาม: ไม่มี เจ้าหน้าที่จะหาเอกสารจากรายละเอียด", texts(reply))
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual((case.sap_kind, case.sap_number), ("slip", ""))
        self.assertEqual(case.purpose, "คีมตัดสาย 2 อัน ส่งใบเบิก 27 ต.ค.")
        self.assertTrue(case.intake_data.get("no_number"))

    def test_the_memo_rule(self):
        self.assertEqual(
            typed_input.tracking_rule(" ฝอจ.คม.  ๑๒/2569 ", "memo")[0], "ฝอจ.คม. 12/2569"
        )
        self.assertTrue(typed_input.tracking_rule("!", "memo")[2])


class TruckTests(KkoTestCase):
    def setUp(self):
        super().setUp()
        call_command("load_truck_table", stdout=StringIO())
        self.schedule = DivisionSchedule.objects.get(division=self.kko_division)
        self.district = self.requester.district

    def test_the_table_names_ฟข_by_abbreviation_until_mapped(self):
        self.assertEqual(self.schedule.delivery, "truck")
        self.assertEqual(self.schedule.truck_days["ฟขอ."], [1, 2, 3])
        self.assertEqual(warehouse.truck_weekdays(self.schedule, self.district), [])
        self.assertIn("ฟขป.", warehouse.unmapped_abbreviations(self.schedule))
        admin = self.flow_admin
        with self.assertRaises(ValidationError):  # a reason is needed
            warehouse.save_short_names(admin, {self.district.code: "ฟขป"}, "")
        with self.assertRaises(ValidationError):  # one abbreviation, one district
            warehouse.save_short_names(admin, {"D01": "ฟขป.", "D02": "ฟขป."}, "ทดสอบ")
        warehouse.save_short_names(admin, {self.district.code: "ฟขป"}, "ตามตารางรถ")
        self.district.refresh_from_db()
        self.assertEqual(self.district.short_name, "ฟขป.")
        self.assertEqual(warehouse.truck_weekdays(self.schedule, self.district), [0])
        self.assertNotIn("ฟขป.", warehouse.unmapped_abbreviations(self.schedule))
        self.assertTrue(AuditEvent.objects.filter(action="district.short_name").exists())

    def test_keyed_goes_on_the_next_truck_day_with_collection_as_the_one_button(self):
        District.objects.filter(pk=self.district.pk).update(short_name="ฟขป.")  # Mondays
        case = self.file("ติดตามใบเบิก", "เลขที่ใบเบิก", "4500090001")
        tuesday = timezone.localdate() - timedelta(days=timezone.localdate().weekday() + 6)
        warehouse.record(self.kko, [case], S.RECEIVED, tuesday)
        done, skipped = warehouse.record(self.kko, [case], S.KEYED, tuesday)
        self.assertEqual((len(done), skipped), (1, []))
        monday = tuesday + timedelta(days=6)
        self.assertEqual(CaseMilestone.objects.get(case=case, stage=S.KEYED).planned_on, monday)
        chosen = Appointment.objects.get(case=case)
        self.assertEqual((chosen.method, chosen.status), ("truck", "confirmed"))
        held = CaseMessage.objects.get(case=case, status="held").text
        self.assertIn("รถ กคอ. จะนำของไปส่งถึงเขต", held)
        reply = self.type(ALICE, "สวัสดี")
        bubble = [m for m in reply if case.number in m.get("text", "")]
        self.assertEqual(
            [b for b in buttons(bubble) if b.startswith(("รถส่ง", "มารับ"))], ["มารับเอง"]
        )
        reply = self.press(ALICE, "มารับเอง", bubble)
        self.assertIn("มารับเอง ยังไม่มีช่วงว่าง กคอ. จะนัดกลับ", texts(reply))
        self.assertEqual(Appointment.objects.get(case=case, status="requested").method, "pickup")

    def test_a_ฟข_without_a_truck_day_hears_the_contact(self):
        case = self.file("ติดตามใบเบิก", "เลขที่ใบเบิก", "4500090002")
        today = timezone.localdate()
        warehouse.record(self.kko, [case], S.RECEIVED, today)
        warehouse.record(self.kko, [case], S.KEYED, today, planned_on=today)
        held = CaseMessage.objects.get(case=case, status="held").text
        self.assertIn("ยังไม่มีวันรถของเขตนี้ สอบถามวันส่งได้ที่ 704-5001 ถึง 5011", held)

    def test_a_return_is_received_and_closes_with_its_own_stage_names(self):
        case = self.file("ติดตามใบส่งคืน", "เลขที่ใบส่งคืน", "4500020001")
        filed = [m for m in self.sent if "ยื่นเรื่องแล้ว" in texts(m)][-1]
        self.assertNotIn("TE-Delivery", texts(filed))  # no one ฟข.'s truck day for everyone
        today = timezone.localdate()
        for stage in CaseMilestone.ORDER:
            done, skipped = warehouse.record(self.kko, [case], stage, today)
            self.assertEqual((len(done), skipped), (1, []))
        case.refresh_from_db()
        self.assertEqual(case.status, "awaiting_confirmation")  # ADR 0055: then ยืนยันผล
        self.assertFalse(Appointment.objects.filter(case=case).exists())  # no delivery offer
        texts_held = list(CaseMessage.objects.filter(case=case).values_list("text", flat=True))
        self.assertEqual(len(texts_held), 1)
        self.assertTrue(texts_held[0].startswith(f"{case.number} รับเข้าคลังแล้ว\n"))
        self.assertIn("ยืนยันผล", texts_held[0])
        self.assertEqual(warehouse.current(case).label, "รับเข้าคลังแล้ว")

    def test_the_schedule_page_keeps_unmapped_rows_and_saves_stage_names(self):
        client = Client()
        client.force_login(self.kko)
        page = client.get("/divisions/กคอ./warehouse/schedule/").content.decode()
        self.assertIn("ฟขป.", page)
        # ADR 0056: กคอ. goes by its truck day, so the page has no กำหนดจ่ายตามขนาด of กคว.
        self.assertNotIn('data-sc-part="sla"', page)
        self.assertNotIn("ชุดมิเตอร์ CT VT", page)
        r = client.post(
            "/divisions/กคอ./warehouse/schedule/",
            {
                "reason": "ทดสอบ",
                "delivery": "truck",
                "lead_days": "1",
                "contact": "704-5001",
                "stage_return_keyed": "รถมารับแล้ว",
            },
        )
        self.assertEqual(r.status_code, 302)
        self.schedule.refresh_from_db()
        self.assertEqual(self.schedule.truck_days["ฟขธ."], [1, 4])
        self.assertEqual((self.schedule.lead_days, self.schedule.contact), (1, "704-5001"))
        self.assertEqual(self.schedule.stage_labels, {"return": {"keyed": "รถมารับแล้ว"}})
        self.assertEqual(self.schedule.issue_weekdays, [0, 1, 2, 3, 4])  # not on the form: kept


class RepairSheetTests(KkoTestCase):
    def setUp(self):
        super().setUp()
        self.source = SheetSource.objects.get(key="kko-repairs")
        District.objects.filter(pk=self.requester.district_id).update(short_name="ฟขป.")

    def ask(self, text):
        self.open_kko()
        self.press(ALICE, "ติดตามงานซ่อม")
        return self.type(ALICE, text)

    def test_numbers_are_cleaned_the_same_way(self):
        self.assertEqual(
            sheet_lookup.codes_in("safety belt, nc-801\nคีม\t BB 011\nNC801 อีกที"),
            [("SAFETY BELT", "NC801"), ("คีม", "BB011")],
        )
        self.assertEqual(sheet_lookup.codes_in("NC801 ซ่อมเสร็จยัง"), [("", "NC801")])

    def test_without_a_sheet_what_was_typed_goes_to_คม(self):
        self.open_kko()
        reply = self.press(ALICE, "ติดตามงานซ่อม")
        self.assertNotIn("ส่งให้ คม. ตรวจ", buttons(reply))  # offered once a number is typed
        reply = self.ask("SAFETY BELT, NC801")
        said = texts(reply)
        self.assertIn("ส่งให้เจ้าหน้าที่ตรวจแทน", said)
        self.assertIn("สรุปเรื่อง: ติดตามงานซ่อม", said)
        self.assertIn("เครื่องมือ: SAFETY BELT, NC801", said)
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual((case.owner_division.code, case.topic), ("กคอ.", "repair"))
        self.assertIn("SAFETY BELT, NC801", case.description)
        row = case.lines.get().details[0]
        self.assertEqual((row["name"], row["equipment_number"]), ("SAFETY BELT", "NC801"))

    def load(self):
        """Rows shaped like คม.'s sheet: one number for many tools, rows not in date order."""
        SheetSource.objects.filter(pk=self.source.pk).update(
            enabled=True, last_synced_at=timezone.now()
        )
        self.source.refresh_from_db()
        mine = self.requester.district.name.replace("การไฟฟ้านครหลวงเขต", "")
        dates = ["14/06/2021", "17/11/2023", "09/03/2023", "02/02/2023", "01/01/2023"]
        sheet_lookup.apply_columns(
            self.source,
            ["NC801", "NC001", "NC001", "NC001", "XY900"],
            ["11", "11", "22", "", "22"],
            districts=[mine, mine, mine, mine, "ธนบุรี"],
            names=[
                "SAFETY BELT",
                "CLIP ON VOLT AMMETER 600A.600V.",
                "CLIP ON VOLT AMMETER 600A.600V.",
                "HV.DETECTOR (NOISY TESTER)",
                "ถุงมือยาง",
            ],
            orders=dates,
            dones=["16/06/2022", "", "", "", ""],
            extras=[dates, ["", "04/12/2023", "11/05/2023", "", ""]],
        )

    def test_the_sheet_answers_the_askers_rows_in_words(self):
        self.load()
        reply = self.ask("SAFETY BELT, NC801\nclip on, NC-001\nถุงมือ, XY900")
        said = texts(reply)
        # back with ฟข. (วันรับกลับ has a date): the done word, not รอส่งคืน
        self.assertIn("หมายเลขอุปกรณ์ NC801 (SAFETY BELT): ซ่อมเสร็จ ส่งคืน ฟข. แล้ว", said)
        # the latest row of the tool whose name fits, by วันเข้า (not by row)
        self.assertIn(
            "หมายเลขอุปกรณ์ NC001 (CLIP ON VOLT AMMETER 600A.600V.): ซ่อมเสร็จ รอส่งคืน"
            " · วันเข้า 17/11/2023 · วันเสร็จ 04/12/2023",
            said,
        )
        self.assertNotIn("HV.DETECTOR", said)
        self.assertIn("ไม่พบหมายเลขอุปกรณ์ XY900 (ถุงมือ) ในรายการของเขตคุณ", said)  # another ฟข.
        self.assertNotIn(": 11", said)
        self.assertNotIn("22", said.replace("2023", ""))
        self.assertIn("ส่งให้ คม. ตรวจ", list(buttons(reply)))
        self.assertFalse(Case.objects.exists())
        reply = self.press(ALICE, "ส่งให้ คม. ตรวจ")
        self.assertIn("สรุปเรื่อง: ติดตามงานซ่อม", texts(reply))

    def test_a_name_that_fits_no_tool_shows_every_tool_of_the_number(self):
        self.load()
        said = texts(self.ask("คีม, NC001"))
        self.assertIn("ไม่พบ คีม ที่หมายเลข NC001 ในเขตคุณ แสดงทุกเครื่องมือที่ใช้หมายเลขนี้", said)
        self.assertIn("NC001 (CLIP ON VOLT AMMETER 600A.600V.): ซ่อมเสร็จ รอส่งคืน", said)
        self.assertIn("NC001 (HV.DETECTOR (NOISY TESTER)): กำลังซ่อม", said)

    def test_a_near_miss_offers_the_askers_closest_rows(self):
        # ADR 0046: NC8O1 (a letter O) is not NC801; the closest rows of ฟข. come as buttons.
        self.assertEqual(sheet_lookup.code_key("nc_801\u200b"), "NC801")
        self.assertTrue(sheet_lookup._slip("NC810", "NC801"))  # neighbours swapped
        self.assertTrue(sheet_lookup._slip("NC8O1", "NC801"))  # a letter O for the zero
        self.assertFalse(sheet_lookup._slip("NC811", "NC801"))
        self.assertEqual(sheet_lookup.name_key("ทํางาน"), sheet_lookup.name_key("ทำงาน"))
        self.load()
        reply = self.ask("safety belt, NC8O1")
        said = texts(reply)
        self.assertIn("ไม่พบหมายเลขอุปกรณ์ NC8O1", said)
        self.assertIn("กดหมายเลขที่ใกล้เคียงด้านล่างเพื่อค้นอีกครั้ง", said)
        near = [
            i["action"]
            for m in reply
            for i in m.get("quickReply", {}).get("items", [])
            if i["action"]["type"] == "message"
        ]
        self.assertEqual(
            near[0], {"type": "message", "label": "NC801 SAFETY BELT", "text": "SAFETY BELT, NC801"}
        )
        self.assertLessEqual(len(near), sheet_lookup.NEAR_K)
        # NC001 is two tools: one button with the number alone, whose answer lists both
        self.assertIn({"type": "message", "label": "NC001", "text": "NC001"}, near)
        self.assertNotIn("XY900", str(near))  # another ฟข.'s row
        self.assertIn("ส่งให้ คม. ตรวจ", list(buttons(reply)))
        said = texts(self.type(ALICE, near[0]["text"]))
        self.assertIn("หมายเลขอุปกรณ์ NC801 (SAFETY BELT): ซ่อมเสร็จ ส่งคืน ฟข. แล้ว", said)
        reply = self.type(ALICE, "ZZ9999")
        self.assertNotIn("ใกล้เคียง", texts(reply))

    def test_dates_and_names(self):
        self.assertEqual(sheet_lookup.iso_date("17/11/2566"), "2023-11-17")
        self.assertEqual(sheet_lookup.iso_date("17/11/2023"), "2023-11-17")
        self.assertEqual(sheet_lookup.iso_date("รอ"), "")
        self.assertTrue(sheet_lookup.name_matches("clip on", "CLIP ON VOLT AMMETER 600A.600V."))
        self.assertTrue(sheet_lookup.name_matches("คีม", "คีมตัดชนิดมีเฟืองทด"))
        self.assertFalse(sheet_lookup.name_matches("คีม", "HV.DETECTOR (NOISY TESTER)"))

    def test_bare_date_numbers_become_dates(self):
        # ซจ.'s date cells are formula results with no date format (30 ต.ค. 2569).
        self.assertEqual(sheet_lookup.serial_date("46027"), "05/01/2569")
        self.assertEqual(sheet_lookup.serial_date("244389"), "09/02/2569")  # 2569 typed as AD
        for kept in ("12345", "999999", "-600374", "20-21/02/2569", "", None):
            self.assertEqual(sheet_lookup.serial_date(kept), kept)
        self.source.extra_columns = [
            {"column": "G", "label": "วันที่ ซจ. รับงาน"},
            {"column": "H", "label": "จำนวน"},
        ]
        sheet_lookup.apply_columns(
            self.source, ["NC801"], ["11"], districts=["x"], extras=[["46027"], ["46027"]]
        )
        entry = SheetEntry.objects.get(source=self.source, number="NC801")
        self.assertEqual(entry.extra, [["วันที่ ซจ. รับงาน", "05/01/2569"], ["จำนวน", "46027"]])

    def test_a_code_sheet_may_show_every_district(self):
        # ADR 0034: the ฟข. column is the sheet owner's choice; คม.'s sheet keeps one.
        sheet_lookup.check_source(self.source)
        self.assertTrue(self.source.district_column)
        self.source.district_column = ""
        sheet_lookup.check_source(self.source)


class RepairWordingTests(KkoTestCase):
    """Q28, Q30, Q37: typed text finds the piece in the sheet; the AI may word it only with a
    passing กคอ. run in its evidence, and a reply that shows a code is refused."""

    def setUp(self):
        super().setUp()
        SheetSource.objects.filter(key="kko-repairs").update(
            enabled=True, last_synced_at=timezone.now()
        )
        self.source = SheetSource.objects.get(key="kko-repairs")
        mine = self.requester.district.name.replace("การไฟฟ้านครหลวงเขต", "")
        sheet_lookup.apply_columns(
            self.source,
            ["NC801", "XY900"],
            ["11", "22"],
            districts=[mine, "ธนบุรี"],
            names=["SAFETY BELT", "ถุงมือยาง"],
            orders=["14/06/2021", "01/01/2023"],
        )

    def test_typed_text_with_a_piece_in_the_sheet_is_answered_from_it(self):
        reply = self.type(ALICE, "NC801 ซ่อมเสร็จยังครับ")
        said = texts(reply)
        self.assertIn("หมายเลขอุปกรณ์ NC801 (SAFETY BELT): ซ่อมเสร็จ รอส่งคืน", said)
        self.assertEqual(list(buttons(reply))[:2], ["ถามชิ้นอื่น", "จบ"])
        reply = self.type(ALICE, "XY900 ล่ะ")  # another ฟข.'s piece: still in the lookup
        self.assertIn("ไม่พบหมายเลขอุปกรณ์ XY900 ในรายการของเขตคุณ", texts(reply))

    def test_a_piece_of_another_ฟข_is_not_taken_from_typed_text(self):
        reply = self.type(ALICE, "XY900 ซ่อมเสร็จยัง")
        self.assertNotIn("หมายเลขอุปกรณ์ XY900", texts(reply))
        self.assertFalse(ConversationDraft.objects.filter(status="open", step="flow").exists())

    def test_the_ai_words_a_repair_only_with_a_passing_kko_run(self):
        from unittest.mock import patch

        from cryptography.fernet import Fernet
        from django.test import override_settings

        from operations import ai, ai_sheet_status
        from operations.models import AIConfigVersion
        from operations.tests.test_m17_sheet_lookup import RAW_KEY, FakeWording

        llm = FakeWording()
        key = Fernet.generate_key().decode()
        with (
            override_settings(AI_SECRET_KEY=key, AI_ENABLED=True),
            patch.object(ai, "_post", side_effect=llm),
        ):
            dev = make_staff("kko-ai-dev", (Role.ADMIN, None), (Role.DEVELOPER_ADMIN, None))
            credential = ai.save_credential(
                dev, "thaillm", "https://thaillm.or.th/api/v1", RAW_KEY, "ทดสอบ"
            )
            profile = ai.create_profile(dev, credential, "sheet-model")
            version = ai.create_version(
                dev, [profile], "เรียบเรียง", task=AIConfigVersion.Task.SHEET_STATUS
            )
            version = ai.test_version(dev, version)
            report = ai_sheet_status.run_gate(version)
            version = ai.record_eval(version, report)
            self.assertTrue(ai_sheet_status.code_gate_passed(report["kko"], version), report["kko"])
            ai.activate(dev, version, "ผ่านการประเมิน")
            self.assertEqual(ai_sheet_status.usable(code=True), version)
            AIConfigVersion.objects.filter(pk=version.pk).update(
                eval_result={k: v for k, v in report.items() if k != "kko"}
            )
            self.assertEqual(ai_sheet_status.usable(), version)  # SMG keeps its evidence
            self.assertIsNone(ai_sheet_status.usable(code=True))
            llm.careless = True
            careless = ai_sheet_status.run_gate(version)
            self.assertFalse(ai_sheet_status.code_gate_passed(careless["kko"], version))

    def test_the_wording_check_refuses_a_code(self):
        import json

        from operations import ai_sheet_status

        item = sheet_lookup.lookup(self.source, ["NC801"], self.requester.district)[0]
        bad = json.dumps({"reply": "หมายเลขอุปกรณ์ NC801 ผล 11 ซ่อมเสร็จ", "follow_up": "ถามต่อไหม"})
        self.assertIn("code", ai_sheet_status.validate(bad, [item])[2])
        good = json.dumps({"reply": "หมายเลขอุปกรณ์ NC801 ซ่อมเสร็จ รอส่งคืน", "follow_up": "ถามต่อไหม"})
        self.assertEqual(ai_sheet_status.validate(good, [item])[2], [])
        sent = ai_sheet_status._messages([item])[-1]["content"]
        self.assertIn("ซ่อมเสร็จ รอส่งคืน", sent)
        self.assertNotIn('"11"', sent)
        self.assertNotIn("hidden", sent)

    def test_another_code_sheet_is_worded_with_its_own_label(self):
        # 30 ต.ค. 2569: ซจ.'s jobs, whose numbers mix letters and digits.
        import json

        from operations import ai_sheet_status

        item = {
            "number": "MSฟขต604217735",
            "found": True,
            "count": 1,
            "statuses": ["กำลังดำเนินการ"],
            "extras": [["วันที่นัดหมาย", "04/11/2569"]],
            "kind": "code",
            "label": "เลขงานซ่อม",
            "name": "",
        }
        sent = json.loads(ai_sheet_status._messages([item])[-1]["content"])["items"][0]
        self.assertEqual(sent["label"], "เลขงานซ่อม")

        def reply(text):
            return json.dumps({"reply": text, "follow_up": "ถามเลขอื่นไหม"})

        good = "เลขงานซ่อม MSฟขต604217735 กำลังดำเนินการ นัดหมาย 04/11/2569"
        self.assertEqual(ai_sheet_status.validate(reply(good), [item])[2], [])
        other = good.replace("กำลัง", "และ 999888777 กำลัง")
        self.assertIn("other_number", ai_sheet_status.validate(reply(other), [item])[2])
