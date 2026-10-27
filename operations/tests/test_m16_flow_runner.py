"""ตัวรัน flow (ADR 0023, grill กคว. 27 ต.ค. 2569): once an admin publishes the Meter, CT, VT
flow, a tap on the tile follows it. Withdrawals take the slip or SAP work order number as typed
("/" kept) and one row per line split by ","; sets ask meter, CT and VT and compare the
multiplier; โน้ตบุ๊กและหัวอ่าน books a slot that the request holds; ขอเครื่องวัดฯ กลับคืน takes
MEA numbers. A draft keeps the version it started with, and the editor's ลองคุย reads typed text
with the same rules without saving anything."""

import json
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from operations import calendars, line_flows
from operations.models import (
    Appointment,
    Calendar,
    CalendarSlot,
    Case,
    ConversationDraft,
    LineFlow,
    Role,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import ALICE, BOB, buttons, texts
from operations.tests.test_m14_kcw_menu import TILE, KcwTestCase


class FlowTestCase(KcwTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        call_command("seed_line_flows", stdout=StringIO())
        cls.flow_admin = make_staff("flowrun-admin", (Role.ADMIN, None))
        cls.flow = LineFlow.objects.get(key="meter-ct-vt")

    def setUp(self):
        super().setUp()
        self.published = line_flows.publish(
            self.flow_admin, line_flows.draft_of(self.flow), "ตามข้อสรุป กคว."
        )

    def open_tile(self, user=ALICE):
        return self.say(self.postback(user, f"m=item&id={self.tile(TILE).pk}"))

    def slip(self, *taps, number="4500012345 /10", user=ALICE):
        self.open_tile(user)
        for label in taps:
            self.press(user, label)
        self.press(user, "เลขที่ใบเบิก")
        return self.type(user, number)


class WithdrawalTests(FlowTestCase):
    def test_a_meter_withdrawal_follows_the_published_flow(self):
        reply = self.open_tile()
        self.assertEqual(
            list(buttons(reply)),
            ["มิเตอร์", "CT", "VT", "ชุดมิเตอร์ CT VT", "โน้ตบุ๊กและหัวอ่าน", "ขอเครื่องวัดฯ คืน", "ยกเลิกร่าง"],
        )
        reply = self.press(ALICE, "มิเตอร์")
        self.assertEqual(list(buttons(reply)), ["ติดตามใบเบิก", "ติดตามใบส่งคืน", "ยกเลิกร่าง"])
        reply = self.press(ALICE, "ติดตามใบเบิก")
        self.assertEqual(list(buttons(reply))[:2], ["เลขที่ใบเบิก", "ใบสั่งงาน SAP"])
        reply = self.type(ALICE, "4500012345")  # the kind of number comes first
        self.assertIn("กดเลือกก่อน", texts(reply))
        self.press(ALICE, "เลขที่ใบเบิก")
        reply = self.type(ALICE, "๔๕๐๐๐๑๒๓๔๕ /10")
        self.assertIn("อ่านเป็น: 4500012345/10", texts(reply))
        self.assertIn("หมายเลขเครื่องวัดฯ, ขนาด", texts(reply))
        reply = self.type(ALICE, "12345678 5(100)A")  # no comma: asked again with the guide
        self.assertIn("บรรทัดละเครื่อง", texts(reply))
        reply = self.type(ALICE, "12345678 ， 5(100)a\nหมายเลข 12345679,5(15)a")
        self.assertIn("อ่านเป็น: 12345678, 5(100)A\n12345679, 5(15)A", texts(reply))
        self.press(ALICE, "ไปต่อ")
        self.type(ALICE, "ติดตั้งผู้ใช้ไฟรายใหม่")
        reply = self.press(ALICE, "ข้าม")
        summary = texts(reply)
        for line in (
            "สรุปเรื่อง: เบิก",
            "ส่งถึง: กคว.",
            "เลขที่ใบเบิก: 4500012345/10",
            "1. มิเตอร์ · 5(100)A · 1 เครื่อง (หมายเลข 12345678)",
            "วัตถุประสงค์: ติดตั้งผู้ใช้ไฟรายใหม่",
        ):
            self.assertIn(line, summary)
        reply = self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertIn(f"เลขเรื่อง: {case.number}", texts(reply))
        self.assertEqual(
            (case.kind, case.owner_division.code, case.sap_number, case.sap_kind, case.purpose),
            ("withdrawal", "กคว.", "4500012345/10", "slip", "ติดตั้งผู้ใช้ไฟรายใหม่"),
        )
        self.assertEqual(case.menu_label, f"{TILE} · มิเตอร์ · ติดตามใบเบิก")
        line = case.lines.get()
        self.assertEqual((line.requested_quantity, line.details[1]["numbers"]), (2, ["12345679"]))
        draft = ConversationDraft.objects.get()
        self.assertEqual((draft.status, draft.step, draft.case), ("submitted", "flow", case))

    def test_a_work_order_and_a_number_already_in_an_open_case(self):
        self.slip("CT", "ติดตามใบเบิก", number="4500099999")
        self.type(ALICE, "100/5A, 3")
        self.press(ALICE, "ไปต่อ")
        self.type(ALICE, "ใช้งาน")
        self.press(ALICE, "ข้าม")
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        self.assertEqual(Case.objects.get().lines.get().details[0]["size"], "100/5A")
        reply = self.slip("VT", "ติดตามใบเบิก", number="4500099999")  # exact text: taken
        self.assertIn("เลขนี้อยู่ในเรื่อง", texts(reply))
        self.open_tile()
        self.press(ALICE, "มิเตอร์")
        self.press(ALICE, "ติดตามใบส่งคืน")
        self.assertEqual(list(buttons(self.sent[-1]))[:2], ["เลขที่ใบส่งคืน", "ใบสั่งงาน SAP"])
        self.press(ALICE, "ใบสั่งงาน SAP")
        reply = self.type(ALICE, "4500099999/20")  # another slash number is another document
        self.assertIn("หมายเลขเครื่องวัดฯ, ขนาด", texts(reply))

    def test_a_meter_number_in_another_open_case_is_kept_with_a_warning(self):
        self.slip("มิเตอร์", "ติดตามใบเบิก", number="4500011111")
        self.type(ALICE, "55001001, 5(15)A")
        self.press(ALICE, "ไปต่อ")
        self.type(ALICE, "ใช้งาน")
        self.press(ALICE, "ข้าม")
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        self.slip("มิเตอร์", "ติดตามใบเบิก", number="4500011112")
        reply = self.type(ALICE, "55001001, 5(15)A")
        self.assertIn("อยู่ในเรื่องอื่นที่ยังไม่จบ", texts(reply))
        self.press(ALICE, "ไปต่อ")
        self.type(ALICE, "ใช้งาน")
        reply = self.press(ALICE, "ข้าม")
        self.assertIn("หมายเหตุ: หมายเลข 55001001", texts(reply))
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        newest = Case.objects.order_by("-submitted_at").first()
        self.assertTrue(newest.intake_data["warnings"])


class SetTests(FlowTestCase):
    def fill_set(self, multiplier):
        self.slip("ชุดมิเตอร์ CT VT", "ติดตามใบเบิก", number="4500022222")
        reply = self.press(ALICE, "มิเตอร์ + CT + VT")
        self.assertIn("ชุดหนึ่งมีมิเตอร์ 1 เครื่อง", texts(reply))
        reply = self.type(ALICE, "11112222, 5(100)A\n11112223, 5(100)A")
        self.assertIn("ไม่เกิน 1 บรรทัด", texts(reply))
        self.type(ALICE, "11112222, 5(100)A")
        reply = self.type(ALICE, "100/5A, 3")
        self.assertIn("ชุด · VT", texts(reply))  # the set has a VT: the branch asks it
        self.type(ALICE, "11000/110V, 3")
        return self.type(ALICE, multiplier)

    def test_the_multiplier_matches_ct_times_vt(self):
        reply = self.fill_set("2,000")
        self.assertNotIn("ไม่ตรง", texts(reply))
        self.type(ALICE, "ติดตั้งชุดวัดแรงสูง")
        reply = self.press(ALICE, "ข้าม")
        self.assertIn("ชุด: มิเตอร์ + CT + VT", texts(reply))
        self.assertIn("ตัวคูณ: 2000 (ตรงกับที่คำนวณ)", texts(reply))
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual(case.intake_data["set_kind"], "meter_ct_vt")
        self.assertTrue(case.intake_data["multiplier_check"]["ok"])
        self.assertEqual(case.lines.count(), 3)  # meter, CT and VT in one case

    def test_a_wrong_multiplier_is_said_and_kept_for_staff(self):
        reply = self.fill_set("200")
        self.assertIn("ไม่ตรงกับที่คำนวณจาก CT และ VT (2000)", texts(reply))
        self.type(ALICE, "ติดตั้ง")
        self.press(ALICE, "ข้าม")
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        check = Case.objects.get().intake_data["multiplier_check"]
        self.assertEqual((check["given"], check["computed"], check["ok"]), ("200", "2000", False))

    def test_ct_only_asks_the_meter_it_goes_with_and_no_vt(self):
        self.slip("ชุดมิเตอร์ CT VT", "ติดตามใบส่งคืน", number="4500033333")
        reply = self.press(ALICE, "CT อย่างเดียว")
        self.assertIn("ใช้คู่กัน", texts(reply))
        self.type(ALICE, "11112222, 11112223")
        self.type(ALICE, "200/5A, 3")
        reply = self.type(ALICE, "40")
        self.assertIn("สภาพของที่ส่งคืน", texts(reply))  # a return: no purpose asked
        reply = self.type(ALICE, "ถอดคืนหลังย้ายมิเตอร์")
        self.assertIn("ใช้คู่กับหมายเลขเครื่องวัดฯ: 11112222, 11112223", texts(reply))
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual((case.kind, case.sap_kind), ("return", "slip"))


class OtherPathTests(FlowTestCase):
    def test_a_notebook_request_holds_a_slot_until_staff_decide(self):
        calendar = Calendar.objects.get(key="notebook")
        start = timezone.now() + timedelta(days=2)
        slot = CalendarSlot.objects.create(
            calendar=calendar, starts_at=start, ends_at=start + timedelta(hours=1)
        )
        self.open_tile()
        self.press(ALICE, "โน้ตบุ๊กและหัวอ่าน")
        reply = self.type(ALICE, "โน้ตบุ๊กอ่านมิเตอร์ไม่ได้ ขอลงโปรแกรมใหม่")
        self.assertIn(calendars.short(slot), buttons(reply))
        reply = self.press(ALICE, calendars.short(slot))
        self.assertIn("ขอนัดแล้ว รอเจ้าหน้าที่ยืนยัน", texts(reply))
        case = Case.objects.get()
        self.assertEqual((case.kind, case.topic), ("problem", "service"))
        appointment = Appointment.objects.get()
        self.assertEqual((appointment.case, appointment.status), (case, "requested"))
        self.assertEqual(calendars.free_slots("notebook"), [])  # unconfirmed counts as taken
        bob = self.approve(BOB)
        self.say(self.postback(BOB, f"m=item&id={self.tile(TILE).pk}"))
        self.press(BOB, "โน้ตบุ๊กและหัวอ่าน")
        reply = self.type(BOB, "ขอติดตั้ง supporting S/W")
        self.assertIn("ยังไม่มีช่วงเวลาว่าง", texts(reply))
        self.assertTrue(bob)

    def test_meters_back_by_mea_number(self):
        self.open_tile()
        reply = self.press(ALICE, "ขอเครื่องวัดฯ คืน")
        self.assertIn("ขอเครื่องวัดฯ กลับคืน", texts(reply))  # the full name in the chat
        self.type(ALICE, "MEA123456 ,MEA123457")
        reply = self.press(ALICE, "ข้าม")
        self.assertIn("MEA No.: MEA123456, MEA123457", texts(reply))
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual((case.topic, case.owner_division.code), ("meter_back", "กคว."))
        self.assertIn("MEA123457", case.description)
        self.assertEqual(case.intake_data["mea_numbers"], ["MEA123456", "MEA123457"])

    def test_a_repair_keeps_its_sap_number(self):
        self.open_tile()
        self.press(ALICE, "CT")
        self.press(ALICE, "ติดตามสถานะการซ่อม")
        self.type(ALICE, "4100012345")
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual((case.topic, case.sap_number), ("repair", "4100012345"))

    def test_start_again_and_cancel(self):
        self.slip("มิเตอร์", "ติดตามใบเบิก")
        self.type(ALICE, "12345678, 5(100)A")
        self.press(ALICE, "ไปต่อ")
        self.type(ALICE, "ใช้งาน")
        self.press(ALICE, "ข้าม")
        reply = self.press(ALICE, "กรอกใหม่")
        self.assertIn("เริ่มกรอกใหม่", texts(reply))
        self.assertEqual(ConversationDraft.objects.get().data["flow"]["rows"], [])
        self.press(ALICE, "ยกเลิกร่าง")
        self.assertEqual(ConversationDraft.objects.get().status, "discarded")


class VersionTests(FlowTestCase):
    def test_a_draft_keeps_its_version_and_an_unpublished_tile_keeps_the_old_card(self):
        self.open_tile()
        self.press(ALICE, "มิเตอร์")
        second = line_flows.start_draft(self.flow_admin, self.flow)
        graph = json.loads(json.dumps(second.graph))
        for node in graph["nodes"]:
            if node["id"] == "c2m":
                node["title"] = "รุ่นสอง"
        second = line_flows.save_draft(self.flow_admin, second, graph, second.revision)
        line_flows.publish(self.flow_admin, second, "เปลี่ยนชื่อ")
        reply = self.press(ALICE, "ติดตามใบเบิก")  # the started draft goes on with version 1
        self.assertIn("เลขที่ใบเบิกหรือใบสั่งงาน SAP", texts(reply))
        self.open_tile()  # a new tap starts the new version; the first draft stays open
        reply = self.press(ALICE, "มิเตอร์")
        self.assertIn("รุ่นสอง", texts(reply))
        LineFlow.objects.filter(pk=self.flow.pk).update(tile="")
        ConversationDraft.objects.update(status="discarded")
        reply = self.open_tile()
        self.assertIn("อุปกรณ์อื่น", buttons(reply))  # the card from code (no flow for it)


class TryTests(FlowTestCase):
    def test_the_editor_reads_typed_text_with_the_chat_rules(self):
        client = Client()
        client.force_login(self.flow_admin)
        url = "/console/admin/flows/meter-ct-vt/try/"

        def ask(**body):
            r = client.post(url, json.dumps(body), content_type="application/json")
            return r.status_code, r.json()

        status, answer = ask(rule="meter_row", field="rows.meter", text="12345678 ,5(100)a")
        self.assertEqual((status, answer["shown"]), (200, "12345678, 5(100)A"))
        self.assertIn("มิเตอร์ · 5(100)A", answer["rows"][0]["text"])
        status, answer = ask(
            rule="multiplier",
            field="multiplier",
            text="2000",
            rows=[{"group": "ct", "size": "100/5A"}, {"group": "vt", "size": "11000/110V"}],
        )
        self.assertEqual((answer["computed"], answer["ok"]), ("2000", True))
        self.assertEqual(ask(rule="eval", text="x")[0], 400)
        self.assertEqual(Case.objects.count(), 0)
        staff = Client()
        staff.force_login(make_staff("flowrun-staff", (Role.STAFF, "กคว.")))
        self.assertEqual(staff.post(url, "{}", content_type="application/json").status_code, 403)
