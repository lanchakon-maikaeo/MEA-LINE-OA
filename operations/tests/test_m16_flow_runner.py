"""ตัวรัน flow (ADR 0023) กับ flow กคว. รอบใหม่ (27 ต.ค. 2569): once an admin publishes the
Meter, CT, VT flow, a tap on the tile follows it. A เลขติดตาม has one of four kinds chosen
before typing and may repeat; withdrawals and returns go from the number to the summary, which
offers เพิ่มรายละเอียด (users' comments, 30 ต.ค. 2569), and list no items; a flow may still ask a
set's meter, CT and VT sizes and compare the multiplier (SetTests keep the 27 ต.ค. graph); the
summary offers แก้ไข and a filed case offers ส่งใบถัดไป; โน้ตบุ๊กและหัวอ่าน holds a slot 30 minutes
until its summary is confirmed. A draft keeps the version it started with, and the editor's
ลองคุย reads typed text with the same rules without saving anything."""

import json
from datetime import timedelta
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from operations import calendars, line_flows, options
from operations.models import (
    Appointment,
    Calendar,
    CalendarSlot,
    Case,
    ChannelIdentity,
    ConversationDraft,
    LineFlow,
    Role,
    SystemOption,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import ALICE, BOB, buttons, texts
from operations.tests.test_m14_kcw_menu import TILE, KcwTestCase

WITHDRAWAL_KINDS = ["เลขที่ใบเบิก", "เลขใบสั่งงานจาก SAP", "หมายเลขเครื่องวัดฯ", "โครงการ"]
# The 27 ต.ค. 2569 graph that asked a set's sizes and multiplier after the number.
SETS_FLOW = Path(__file__).parent / "data" / "meter_ct_vt_sets_oct30.json"


class FlowTestCase(KcwTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        call_command("seed_line_flows", stdout=StringIO())
        cls.flow_admin = make_staff("flowrun-admin", (Role.ADMIN, None))
        cls.flow = LineFlow.objects.get(key="meter-ct-vt")

    def setUp(self):
        super().setUp()
        SystemOption.objects.update_or_create(key=options.SCREENING, defaults={"value": "off"})
        self.published = line_flows.publish(
            self.flow_admin, line_flows.draft_of(self.flow), "ตามข้อสรุป กคว."
        )

    def open_tile(self, user=ALICE):
        return self.say(self.postback(user, f"m=item&id={self.tile(TILE).pk}"))

    def track(self, *taps, kind="เลขที่ใบเบิก", number="4500012345 /10", user=ALICE):
        self.open_tile(user)
        for label in taps:
            self.press(user, label)
        self.press(user, kind)
        return self.type(user, number)


class WithdrawalTests(FlowTestCase):
    def test_a_meter_withdrawal_takes_a_tracking_number_and_details(self):
        reply = self.open_tile()
        self.assertEqual(
            list(buttons(reply)),
            [
                "มิเตอร์",
                "CT",
                "VT",
                "ชุดมิเตอร์, CT, VT",
                "โน้ตบุ๊กและหัวอ่าน",
                "ขอเครื่องวัดฯ คืน",
                "ยกเลิกร่าง",
            ],
        )
        reply = self.press(ALICE, "มิเตอร์")
        self.assertEqual(list(buttons(reply)), ["ติดตามใบเบิก", "ติดตามใบส่งคืน", "ยกเลิกร่าง"])
        reply = self.press(ALICE, "ติดตามใบเบิก")
        self.assertEqual(list(buttons(reply))[:4], WITHDRAWAL_KINDS)
        reply = self.type(ALICE, "4500012345")  # the kind comes first
        self.assertIn("กรุณาเลือกช่องทางการติดตาม", texts(reply))
        self.press(ALICE, "เลขที่ใบเบิก")
        reply = self.type(ALICE, "๔๕๐๐๐๑๒๓๔๕ /10")
        self.assertEqual(texts(reply).splitlines()[0], "4500012345/10")  # what it read
        # 30 ต.ค. 2569 (P69-000036, P69-000040 and a later comment): the number, then the summary.
        self.assertIn("สรุปเรื่อง: การเบิก", texts(reply))
        self.assertEqual(list(buttons(reply))[:3], ["ยืนยันส่งเรื่อง", "แก้ไข", "เพิ่มรายละเอียด"])
        reply = self.press(ALICE, "เพิ่มรายละเอียด")
        self.assertIn("รายละเอียดเพิ่มเติม", texts(reply))
        reply = self.type(ALICE, "มิเตอร์ 5(100)A 2 เครื่อง ใช้ติดตั้งผู้ใช้ไฟรายใหม่")
        summary = texts(reply)
        for line in (
            "สรุปเรื่อง: การเบิก",
            "ส่งถึง: กคว.",
            "อุปกรณ์: มิเตอร์",
            "เลขที่ใบเบิก: 4500012345/10",
            "รายละเอียด: มิเตอร์ 5(100)A 2 เครื่อง ใช้ติดตั้งผู้ใช้ไฟรายใหม่",
        ):
            self.assertIn(line, summary)
        self.assertEqual(list(buttons(reply))[:2], ["ยืนยันส่งเรื่อง", "แก้ไข"])
        reply = self.press(ALICE, "แก้ไข")
        self.assertEqual(list(buttons(reply)), ["เลขติดตาม", "รายละเอียด", "กลับไปที่สรุป"])
        reply = self.press(ALICE, "รายละเอียด")
        self.assertIn("แก้ รายละเอียดเพิ่มเติม", texts(reply))
        reply = self.type(ALICE, "มิเตอร์ 5(100)A 3 เครื่อง")
        self.assertIn("รายละเอียด: มิเตอร์ 5(100)A 3 เครื่อง", texts(reply))  # back at the summary
        reply = self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertIn(f"เลขเรื่อง: {case.number}", texts(reply))
        self.assertEqual(
            (case.kind, case.owner_division.code, case.sap_number, case.sap_kind, case.purpose),
            ("withdrawal", "กคว.", "4500012345/10", "slip", "มิเตอร์ 5(100)A 3 เครื่อง"),
        )
        self.assertEqual((case.lines.count(), case.status), (0, "awaiting_approval"))
        self.assertEqual(case.intake_data["group"], "meter")
        self.assertEqual(case.menu_label, f"{TILE} · มิเตอร์ · ติดตามใบเบิก")
        self.assertEqual(list(buttons(reply))[0], "ส่งเรื่องเพิ่มเติม")
        reply = self.press(ALICE, "ส่งเรื่องเพิ่มเติม")  # Q7: the same path, at the number
        self.assertEqual(list(buttons(reply))[:4], WITHDRAWAL_KINDS)
        draft = ConversationDraft.objects.get(status="open")
        self.assertEqual(draft.data["flow"]["values"]["case_kind"], "withdrawal")

    def test_the_bot_asks_only_for_the_chosen_kind(self):
        # 29 ต.ค. 2569: โครงการ was asked for "เลขที่ใบเบิก ... เช่น 4500012345/10".
        prompts = {
            "เลขที่ใบเบิก": "กรุณาพิมพ์เลขที่ใบเบิก",
            "เลขใบสั่งงานจาก SAP": "กรุณาพิมพ์เลขใบสั่งงานจาก SAP",
            "หมายเลขเครื่องวัดฯ": "กรุณาพิมพ์หมายเลขเครื่องวัดฯ เช่น 12345678",
            "โครงการ": "กรุณาพิมพ์ชื่อโครงการ",
        }
        for kind, prompt in prompts.items():
            with self.subTest(kind):
                self.open_tile()
                self.press(ALICE, "มิเตอร์")
                self.press(ALICE, "ติดตามใบเบิก")
                self.assertEqual(texts(self.press(ALICE, kind)), prompt)
        self.assertIn("กรุณาตรวจสอบอีกครั้ง", texts(self.type(ALICE, "x")))
        reply = self.track("มิเตอร์", "ติดตามใบเบิก", number="ab")
        self.assertIn("กรุณาตรวจสอบอีกครั้ง", texts(reply))  # the page's words (29 ต.ค. 2569)

    def test_a_project_or_a_meter_number_may_repeat_with_a_note(self):
        project = "ปรับปรุงระบบจำหน่าย ถนนพระราม 4"
        self.track("มิเตอร์", "ติดตามใบเบิก", kind="โครงการ", number=project)
        self.type(ALICE, "มิเตอร์ 5(15)A 10 เครื่อง")
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        first = Case.objects.get()
        self.assertEqual((first.sap_kind, first.sap_number), ("project", project))
        reply = self.track("CT", "ติดตามใบเบิก", kind="โครงการ", number=project)
        self.assertIn(f"เลขนี้กำลังดำเนินการอยู่ในเรื่อง {first.number}", texts(reply))
        self.type(ALICE, "CT 100/5A 3 ตัว")
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        second = Case.objects.exclude(pk=first.pk).get()
        self.assertEqual(second.intake_data["staff_notes"], [f"เลขซ้ำกับเรื่อง {first.number}"])
        reply = self.track("มิเตอร์", "ติดตามใบเบิก", kind="หมายเลขเครื่องวัดฯ", number="ab")
        self.assertIn("เป็นตัวเลข 8 หลัก", texts(reply))  # ADR 0035
        reply = self.type(ALICE, "หมายเลข 1234 5678")
        self.assertEqual(texts(reply).splitlines()[0], "12345678")

    def test_a_return_takes_its_details_from_the_summary(self):
        self.open_tile()
        self.press(ALICE, "มิเตอร์")
        reply = self.press(ALICE, "ติดตามใบส่งคืน")
        self.assertEqual(list(buttons(reply))[0], "เลขที่ใบส่งคืน")
        self.press(ALICE, "เลขที่ใบส่งคืน")
        reply = self.type(ALICE, "4500020001")
        self.assertIn("สรุปเรื่อง: การส่งคืน", texts(reply))
        self.press(ALICE, "เพิ่มรายละเอียด")
        self.type(ALICE, "มิเตอร์ 5(15)A 1 เครื่อง ชำรุด หน้าจอไม่ติด")
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual(
            (case.kind, case.sap_kind, case.lines.count()), ("return", "return_slip", 0)
        )
        self.assertEqual(case.return_reason, "มิเตอร์ 5(15)A 1 เครื่อง ชำรุด หน้าจอไม่ติด")

    def test_the_number_goes_straight_to_the_summary(self):
        reply = self.track("ชุดมิเตอร์, CT, VT", "ติดตามใบเบิก", number="4500033333")
        self.assertNotIn("มิเตอร์ + CT + VT", buttons(reply))  # P69-000040: no set questions
        self.assertIn("สรุปเรื่อง: การเบิก", texts(reply))
        self.assertNotIn("รายละเอียด:", texts(reply))
        self.press(ALICE, "เพิ่มรายละเอียด")
        reply = self.press(ALICE, "กลับไปที่สรุป")  # tapped by mistake: back, nothing added
        self.assertIn("สรุปเรื่อง: การเบิก", texts(reply))
        self.assertNotIn("รายละเอียด:", texts(reply))
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual((case.sap_number, case.purpose), ("4500033333", "-"))  # a case needs one


class SetTests(FlowTestCase):
    def setUp(self):
        super().setUp()
        draft = line_flows.start_draft(self.flow_admin, self.flow)
        draft.graph = json.loads(SETS_FLOW.read_text(encoding="utf-8"))["graph"]
        draft.save(update_fields=["graph"])
        line_flows.publish(self.flow_admin, draft, "ชุดแบบ 27 ต.ค. 2569")

    def fill_set(self, multiplier):
        self.track("ชุดมิเตอร์, CT, VT", "ติดตามใบเบิก", number="4500022222")
        reply = self.press(ALICE, "มิเตอร์ + CT + VT")
        self.assertIn("ชุดประกอบมิเตอร์", texts(reply))
        reply = self.type(ALICE, "11112222, 5(100)A\n11112223, 5(100)A")
        self.assertIn("กรุณาพิมพ์ไม่เกิน 1 บรรทัด", texts(reply))
        self.type(ALICE, "11112222, 5(100)A")
        reply = self.type(ALICE, "100/5a")
        self.assertEqual(texts(reply).splitlines()[0], "100/5A")
        self.assertIn("ขนาด VT", texts(reply))  # the set has a VT: the branch asks it
        reply = self.type(ALICE, "11000/110V")
        self.assertIn("กรุณาระบุเหตุผลในการขอเบิก", texts(reply))  # the sizes are in: no item details
        reply = self.type(ALICE, "ติดตั้งชุดวัดแรงสูง")
        self.assertIn("พิมพ์ตัวคูณที่เขียนในเอกสาร", texts(reply))
        return self.type(ALICE, multiplier)

    def test_the_multiplier_matches_ct_times_vt(self):
        reply = self.fill_set("2,000")
        summary = texts(reply)
        for line in (
            "ชุด: มิเตอร์ + CT + VT",
            "มิเตอร์ · 5(100)A · 1 เครื่อง (หมายเลข 11112222)",
            "ขนาด CT: 100/5A",
            "ขนาด VT: 11000/110V",
            "ตัวคูณ: 2000 (ตรงกับที่คำนวณ)",
        ):
            self.assertIn(line, summary)
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual(case.intake_data["set_kind"], "meter_ct_vt")
        self.assertTrue(case.intake_data["multiplier_check"]["ok"])
        self.assertEqual(case.intake_data["meters"][0]["numbers"], ["11112222"])
        self.assertEqual(case.lines.count(), 0)

    def test_a_wrong_multiplier_is_said_and_can_be_fixed(self):
        reply = self.fill_set("200")
        self.assertIn("ไม่ตรงกับที่คำนวณจากขนาด CT และ VT (2000)", texts(reply))
        self.press(ALICE, "แก้ไข")
        self.press(ALICE, "ตัวคูณ")
        reply = self.type(ALICE, "2000")
        self.assertIn("ตัวคูณ: 2000 (ตรงกับที่คำนวณ)", texts(reply))
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        self.assertTrue(Case.objects.get().intake_data["multiplier_check"]["ok"])

    def test_ct_only_asks_no_meter_and_no_vt(self):
        self.track("ชุดมิเตอร์, CT, VT", "ติดตามใบส่งคืน", kind="เลขที่ใบส่งคืน", number="4500033333")
        reply = self.press(ALICE, "CT อย่างเดียว")
        self.assertIn("ขนาด CT", texts(reply))
        reply = self.type(ALICE, "200/5A")
        self.assertIn("กรุณาระบุเหตุผลในการขอส่งคืนชุดนี้", texts(reply))
        reply = self.type(ALICE, "ถอดคืนหลังย้ายมิเตอร์")
        self.assertNotIn("ใบเบิก", texts(reply))  # a return slip, not a withdrawal one
        reply = self.type(ALICE, "50")
        self.assertIn("ไม่ตรงกับที่คำนวณจากขนาด CT (40)", texts(reply))  # this set has no VT
        self.assertNotIn("VT", texts(reply))
        self.press(ALICE, "แก้ไข")
        self.press(ALICE, "ตัวคูณ")
        reply = self.type(ALICE, "40")
        self.assertIn("ตัวคูณ: 40 (ตรงกับที่คำนวณ)", texts(reply))
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        self.assertEqual(Case.objects.get().kind, "return")


class OtherPathTests(FlowTestCase):
    def setUp(self):
        super().setUp()
        # Staff open each slot, as before ADR 0036 (test_m25_booking_cards has the default times).
        Calendar.objects.filter(key="notebook").update(default_times=[])

    def slot(self, days=2):
        start = timezone.now() + timedelta(days=days)
        return CalendarSlot.objects.create(
            calendar=Calendar.objects.get(key="notebook"),
            starts_at=start,
            ends_at=start + timedelta(hours=1),
        )

    def test_a_notebook_request_holds_a_slot_and_confirms_at_its_summary(self):
        slot = self.slot()
        self.open_tile()
        self.press(ALICE, "โน้ตบุ๊กและหัวอ่าน")
        reply = self.type(ALICE, "โน้ตบุ๊กอ่านมิเตอร์ไม่ได้ ขอลงโปรแกรมใหม่")
        self.assertIn(calendars.short(slot), buttons(reply))
        reply = self.press(ALICE, calendars.short(slot))
        self.assertIn("สรุปเรื่อง: โน้ตบุ๊กและหัวอ่าน", texts(reply))
        self.assertIn(f"ขอนัด: {calendars.label(slot)}", texts(reply))
        self.assertEqual(calendars.free_slots("notebook"), [])  # held while the summary waits
        reply = self.press(ALICE, "ยืนยันส่งเรื่อง")
        self.assertIn("ทำการนัดหมายแล้ว กรุณารอเจ้าหน้าที่ยืนยัน", texts(reply))
        case = Case.objects.get()
        self.assertEqual((case.kind, case.topic), ("problem", "service"))
        appointment = Appointment.objects.get(status="requested")
        self.assertEqual(appointment.case, case)

    def test_a_notebook_request_without_a_free_slot_waits_for_a_call(self):
        self.open_tile()
        self.press(ALICE, "โน้ตบุ๊กและหัวอ่าน")
        reply = self.type(ALICE, "ตรวจหัวอ่าน")
        self.assertIn("ยังไม่มีช่วงเวลาว่าง", texts(reply))
        self.press(ALICE, "ไปต่อ")
        reply = self.press(ALICE, "ยืนยันส่งเรื่อง")
        self.assertIn("เจ้าหน้าที่จะติดต่อกลับเพื่อทำการนัดหมายวันเวลา", texts(reply))
        self.assertNotIn("ขอนัดแล้ว", texts(reply))  # nothing was asked for, so nothing waits

    def test_a_hold_older_than_thirty_minutes_is_lost_if_someone_else_takes_it(self):
        slot = self.slot()
        self.open_tile()
        self.press(ALICE, "โน้ตบุ๊กและหัวอ่าน")
        self.type(ALICE, "ขอติดตั้ง supporting S/W")
        self.press(ALICE, calendars.short(slot))
        Appointment.objects.update(requested_at=timezone.now() - timedelta(minutes=31))
        self.assertEqual(calendars.free_slots("notebook"), [slot])  # released
        self.approve(BOB)
        self.open_tile(BOB)
        self.press(BOB, "โน้ตบุ๊กและหัวอ่าน")
        self.type(BOB, "ตรวจหัวอ่าน")
        self.press(BOB, calendars.short(slot))
        alice_summary = [m for m in self.sent if "สรุปเรื่อง: โน้ตบุ๊ก" in m[-1].get("text", "")][0]
        reply = self.press(ALICE, "ยืนยันส่งเรื่อง", alice_summary)
        self.assertIn("ช่วงเวลาอยู่ระหว่างดำเนินการ", texts(reply))
        self.assertFalse(Case.objects.filter(requester=self.requester).exists())

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

    def test_a_repair_keeps_its_sap_number(self):
        self.open_tile()
        self.press(ALICE, "CT")
        reply = self.press(ALICE, "ติดตามสถานะการซ่อม")
        self.assertIn("พิมพ์เลขใบสั่งงานจาก SAP", texts(reply))
        self.type(ALICE, "4100012345")
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual((case.topic, case.sap_number), ("repair", "4100012345"))

    def test_cancel(self):
        self.track("มิเตอร์", "ติดตามใบเบิก")
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
        self.assertIn("เลขติดตามใบเบิก", texts(reply))
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
        status, answer = ask(rule="tracking", field="tracking", text="ab", kind="meter")
        self.assertIn("เป็นตัวเลข 8 หลัก", answer["problem"])
        status, answer = ask(
            rule="multiplier",
            field="multiplier",
            text="2000",
            values={"ct_size": "100/5A", "vt_size": "11000/110V", "set_kind": "ct_vt"},
        )
        self.assertEqual((answer["computed"], answer["ok"]), ("2000", True))
        self.assertEqual(ask(rule="eval", text="x")[0], 400)
        self.assertEqual(Case.objects.count(), 0)
        staff = Client()
        staff.force_login(make_staff("flowrun-staff", (Role.STAFF, "กคว.")))
        self.assertEqual(staff.post(url, "{}", content_type="application/json").status_code, 403)


class OtherButtonTests(FlowTestCase):
    """28 ต.ค. 2569: every requester button has a draft drawn as the chat asks today; once
    published, the chat follows it to a filed case."""

    def publish(self, label):
        flow = LineFlow.objects.get(tile=label)
        line_flows.publish(self.flow_admin, line_flows.draft_of(flow), "ลองใช้ร่าง")

    def open(self, label, user=ALICE):
        from operations.models import LineMenuItem

        # ปัญหา/ข้อเสนอแนะ is on the guest menu too: the report menu's button.
        tile = LineMenuItem.objects.get(place="tile", tab="report", label=label, is_active=True)
        return self.say(self.postback(user, f"m=item&id={tile.pk}"))

    def test_usage_takes_the_report_in_one_message(self):
        # ADR 0035: meters, district and what was found in one message, filed as before.
        from operations.models import District

        district = District.objects.filter(is_active=True).exclude(code="D00").first()
        short = district.name.replace("การไฟฟ้านครหลวง", "").replace("เขต", "")
        self.publish("ตรวจสอบการใช้ไฟฟ้า")
        reply = self.open("ตรวจสอบการใช้ไฟฟ้า")
        self.assertIn("เครื่องวัดฯ ผิดปกติ", buttons(reply))
        self.assertNotIn("ติดตามเรื่อง", buttons(reply))
        reply = self.press(ALICE, "เครื่องวัดฯ ผิดปกติ")
        self.assertEqual(
            texts(reply),
            "กรุณาพิมพ์รายละเอียด\nหมายเลขเครื่องวัดฯ, การไฟฟ้าเขต, ลักษณะที่พบ\n"
            "เช่น 12345678, วัดเลียบ, PLS ถูกแกะ",
        )
        self.assertIn("เป็นตัวเลข 8 หลัก", texts(self.type(ALICE, "1234567, วัดเลียบ, x")))
        reply = self.type(ALICE, f"12345678 12345679, {short}, PLS ถูกแกะ")
        for line in (
            "เรื่อง: เครื่องวัดฯ ผิดปกติ",
            "หมายเลขเครื่องวัดฯ: 12345678, 12345679",
            f"การไฟฟ้าเขต: {district.name.replace('การไฟฟ้านครหลวง', '')}",
            "ลักษณะที่พบ: PLS ถูกแกะ",
        ):
            self.assertIn(line, texts(reply))
        self.press(ALICE, "แก้ไข")
        reply = self.press(ALICE, "ลักษณะที่พบ")  # Q19: one answer at a time
        reply = self.type(ALICE, "PLS ถูกแกะ ซีลขาด")
        self.assertIn("ลักษณะที่พบ: PLS ถูกแกะ ซีลขาด", texts(reply))
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual((case.topic, case.owner_division.code), ("usage", "กตฟ."))
        self.assertEqual([m["number"] for m in case.meters], ["12345678", "12345679"])
        self.assertEqual({m["district"] for m in case.meters}, {district.code})
        self.assertIn("PLS ถูกแกะ ซีลขาด", case.description)

    def test_usage_asks_only_what_the_message_left_out(self):
        self.publish("ตรวจสอบการใช้ไฟฟ้า")
        self.open("ตรวจสอบการใช้ไฟฟ้า")
        self.press(ALICE, "ร้องเรียนไฟแพง")
        reply = self.type(ALICE, "บิลเดือนนี้สูงผิดปกติ")
        self.assertIn("กรุณาพิมพ์หมายเลขเครื่องวัดฯ", texts(reply))
        reply = self.type(ALICE, "12345678")
        self.assertIn("กรุณาพิมพ์การไฟฟ้าเขต", texts(reply))  # the found text was given
        mine = ChannelIdentity.objects.get(external_id=ALICE).requester.district
        name = mine.name.replace("การไฟฟ้านครหลวง", "")
        self.assertIn(name, buttons(reply))  # Q17: the requester's own district, one tap
        reply = self.press(ALICE, name)
        self.assertIn(f"การไฟฟ้าเขต: {name}", texts(reply))
        self.assertIn("ลักษณะที่พบ: บิลเดือนนี้สูงผิดปกติ", texts(reply))
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        self.assertEqual(Case.objects.get().meters[0]["district"], mine.code)

    # The กคอ. tile asks the job first since the กคอ. grill (Q3): test_m18_kko_flow.

    def test_smg_and_general_reports(self):
        # ADR 0027: ศอค. takes problem reports in its own Google Form; nothing is filed here.
        self.publish("ระบบ SMG, AMR")
        reply = self.open("ระบบ SMG, AMR")
        self.assertEqual(list(buttons(reply)), ["แจ้งปัญหา", "ตรวจสอบปัญหา", "FAQ", "ยกเลิกร่าง"])
        reply = self.press(ALICE, "แจ้งปัญหา")
        self.assertIn("https://docs.google.com/forms/", texts(reply))
        self.assertFalse(Case.objects.exists())
        self.publish("ปัญหา/ข้อเสนอแนะ")
        self.open("ปัญหา/ข้อเสนอแนะ")
        self.press(ALICE, "กองเครื่องวัดหน่วยไฟฟ้า")  # ADR 0030: the division carousel
        self.type(ALICE, "อยากให้แจ้งวันจ่ายล่วงหน้า")
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual((case.topic, case.owner_division.code), ("general", "กคว."))
