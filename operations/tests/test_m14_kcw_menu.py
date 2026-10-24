"""เมนู กคว. ที่เลือกอุปกรณ์ก่อน ช่องที่ผู้ใช้พิมพ์ และเลขใบเบิก/ใบส่งคืน SAP (user, 23 Oct 2569).

Acceptance → tests: the กคว. tile asks for the equipment on a card, then ติดตามใบเบิก /
ติดตามใบส่งคืน / แจ้งปัญหา on a second card, while the other tiles stay as they were
(CardTests); a withdrawal or return starts with the SAP slip number and takes typed rows, in one
line split by "/" or field by field, checked against the quantity, the other rows and open
cases (TypedRowTests); someone who may only report problems skips the second card and may skip
every field (ProblemTests); another path into กคว. asks the SAP number and the missing fields
before the summary, as ตัวเลือกระบบ says (OtherPathTests); staff see, search and correct the
number, and admins change the fields, units and the division switch (StaffAndAdminTests).
"""

import uuid

from django.test import Client

from operations import line_menu_catalog, options, services
from operations.contracts import Actor, CommandFailed
from operations.models import (
    AuditEvent,
    Case,
    ConversationDraft,
    Division,
    EntryField,
    EquipmentType,
    Role,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import ALICE, BOB, buttons, texts
from operations.tests.test_m13_line_menu import MenuTestCase

TILE = "Meter, CT, VT"


def card_texts(reply):
    """Header texts of the Flex cards in one reply."""
    found = []
    for message in reply:
        if message["type"] == "flex":
            for bubble in message["contents"]["contents"]:
                found += [c["text"] for c in bubble.get("header", {}).get("contents", [])]
    return "\n".join(found)


class KcwTestCase(MenuTestCase):
    def setUp(self):
        super().setUp()
        line_menu_catalog.sync()
        Division.objects.filter(code="กคว.").update(requires_sap=True)

    def start(self, group="มิเตอร์", action="ติดตามใบเบิก", user=ALICE):
        self.say(self.postback(user, f"m=item&id={self.tile(TILE).pk}"))
        self.press(user, group)
        return self.press(user, action)

    def submit(self, requester, key, sap, kind="withdrawal"):
        payload = {
            "kind": kind,
            "division_code": "กคว.",
            "sap_number": sap,
            "lines": [{"item_code": "MTR_SYN_A", "quantity": "1", "serials": []}],
        }
        if kind == "withdrawal":
            payload["purpose"] = "ติดตั้ง"
        else:
            payload.update(condition="ชำรุด", reason="เปลี่ยน")
            payload["lines"][0]["serials"] = ["SER-0001"]
        result = services.submit_case(
            Actor.for_requester(requester), idempotency_key=key, payload=payload
        )
        return Case.objects.get(pk=result.case_id)


class SetupTests(MenuTestCase):
    def test_the_structure_data_asks_kcw_and_the_migration_seeds_the_fields(self):
        import json

        from operations.management.commands import load_org

        document = json.loads(load_org.DEFAULT.read_text(encoding="utf-8"))
        flagged = [row["code"] for row in document["divisions"] if row.get("requires_sap")]
        self.assertEqual(flagged, ["กคว."])
        coordinator = {"code": "TEST-COORD", "name": "ผู้ประสานทดสอบ", "sections": []}
        new = {"code": "TEST-SAP", "name": "กองทดสอบ", "sections": [], "requires_sap": True}
        load_org.load({"coordinator": coordinator, "divisions": [new]})
        self.assertTrue(Division.objects.get(code="TEST-SAP").requires_sap)  # set on creation
        shared = dict(EquipmentType.objects.exclude(entry_group="").values_list("code", "unit"))
        self.assertEqual(
            shared,
            {"TYPED_METER": "เครื่อง", "TYPED_CT": "ตัว", "TYPED_VT": "ตัว", "TYPED_OTHER": "หน่วย"},
        )
        self.assertEqual(
            list(EntryField.objects.filter(group="meter").values_list("key", flat=True)),
            ["numbers", "size", "quantity"],
        )


class CardTests(KcwTestCase):
    def test_the_kcw_tile_asks_for_the_equipment_first_on_cards(self):
        reply = self.open(TILE)
        self.assertEqual(reply[0]["type"], "flex")
        self.assertIn("เลือกอุปกรณ์", card_texts(reply))
        self.assertEqual(
            [b for b in buttons(reply) if b != "หน้าหลัก"],
            ["มิเตอร์", "CT", "VT", "อุปกรณ์อื่น", "ติดตามเรื่อง"],
        )
        reply = self.press(ALICE, "CT")
        self.assertIn(f"{TILE} · CT", card_texts(reply))
        self.assertEqual(
            list(buttons(reply)), ["ย้อนกลับ", "หน้าหลัก", "ติดตามใบเบิก", "ติดตามใบส่งคืน", "แจ้งปัญหา"]
        )
        self.type(ALICE, "ติดตามใบส่งคืน")  # typing a card label works like tapping it
        draft = ConversationDraft.objects.get(status="open")
        self.assertEqual(
            (draft.kind, draft.step, draft.data["division"], draft.data["typed"]),
            ("return", "sap", "กคว.", "ct"),
        )
        self.assertEqual(draft.data["menu_label"], f"{TILE} · CT · ติดตามใบส่งคืน")

    def test_the_other_equipment_tiles_stay_as_they_were(self):
        reply = self.open("หม้อแปลง LBS RMU")
        self.assertIn("เลือกเรื่องที่ต้องการ", texts(reply))
        self.assertEqual(list(buttons(reply)), ["เบิกอุปกรณ์", "ส่งคืนอุปกรณ์", "ติดตามเรื่อง"])
        reply = self.press(ALICE, "เบิกอุปกรณ์")
        self.assertEqual(list(buttons(reply))[:4], ["มิเตอร์", "CT", "VT", "อุปกรณ์อื่น"])
        self.assertNotIn("TYPED_METER", str(reply))  # shared items are never in a list

    def test_tracking_lists_the_kcw_cases(self):
        case = self.submit(self.requester, "kcw-track-001", "4500000001")
        self.open(TILE)
        reply = self.press(ALICE, "ติดตามเรื่อง")
        self.assertIn(case.number, buttons(reply))


class TypedRowTests(KcwTestCase):
    def test_a_withdrawal_follows_its_sap_slip_with_typed_rows(self):
        reply = self.start()
        self.assertIn("เลขใบเบิก SAP", texts(reply))
        self.assertIn("4 ถึง 20 ตัว", texts(self.type(ALICE, "ab")))
        reply = self.type(ALICE, "4500012345")
        self.assertIn("หมายเลขเครื่องวัด / ขนาด / จำนวน", texts(reply))
        reply = self.type(ALICE, "M-1001 M-1002 / 5(100)A / 2")
        self.assertIn("เพิ่มรายการ", buttons(reply))
        self.press(ALICE, "เพิ่มรายการ")
        reply = self.press(ALICE, "CT")
        self.assertIn("ขนาด / จำนวน", texts(reply))
        self.type(ALICE, "100/5A/3")  # a CT size has its own "/"
        self.press(ALICE, "ไปต่อ")
        self.type(ALICE, "ติดตั้งมิเตอร์ใหม่")
        summary = texts(self.press(ALICE, "ข้าม"))
        self.assertIn("เลขใบเบิก SAP: 4500012345", summary)
        self.assertIn("มิเตอร์ · 5(100)A · 2 เครื่อง (หมายเลข M-1001, M-1002)", summary)
        self.assertIn("CT · 100/5A · 3 ตัว", summary)
        self.assertIn("ส่งถึง: กคว.", summary)
        # Rows of two kinds of equipment: the topic keeps the tile and the action only.
        self.assertIn(f"หัวข้อ: {TILE} · ติดตามใบเบิก", summary)
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual(
            (case.kind, case.sap_number, case.owner_division.code, case.menu_label),
            ("withdrawal", "4500012345", "กคว.", f"{TILE} · ติดตามใบเบิก"),
        )
        meter, ct = case.lines.order_by("line_no")
        self.assertEqual((meter.item.code, meter.requested_quantity), ("TYPED_METER", 2))
        self.assertEqual(
            meter.details,
            [
                {
                    "group": "meter",
                    "numbers": ["M-1001", "M-1002"],
                    "size": "5(100)A",
                    "quantity": "2",
                }
            ],
        )
        self.assertEqual(meter.serials, [])  # a withdrawal's meters are recorded at issue
        self.assertEqual(
            (ct.item.code, ct.requested_quantity, ct.details[0]["size"]), ("TYPED_CT", 3, "100/5A")
        )

    def test_field_by_field_and_the_numbers_must_match_the_quantity(self):
        self.start()
        self.type(ALICE, "4500012346")
        self.assertIn("พิมพ์ขนาด", texts(self.type(ALICE, "M-2001")))
        self.type(ALICE, "15(45)A")
        reply = self.type(ALICE, "2")
        self.assertIn("หมายเลขเครื่องวัดมี 1 เลข แต่จำนวน 2", texts(reply))
        reply = self.type(ALICE, "M-2001 M-2002/15(45)A/2 เครื่อง")
        self.assertIn("เพิ่มรายการ", buttons(reply))
        self.press(ALICE, "เพิ่มรายการ")
        self.press(ALICE, "มิเตอร์")
        reply = self.type(ALICE, "M-2002/15(45)A/1")
        self.assertIn("หมายเลข M-2002 อยู่ในรายการก่อนหน้าแล้ว", texts(reply))
        draft = ConversationDraft.objects.get(status="open")
        self.assertEqual(draft.data["lines"][0]["quantity"], "2")

    def test_back_and_a_required_field_cannot_be_skipped(self):
        self.start()
        self.type(ALICE, "4500012347")
        reply = self.type(ALICE, "M-3001")
        self.assertNotIn("ข้าม", buttons(reply))  # size is required for a withdrawal
        reply = self.press(ALICE, "ย้อนกลับ")
        self.assertIn("หมายเลขเครื่องวัด", texts(reply))

    def test_a_sap_number_on_an_open_case_is_refused_without_telling_whose(self):
        mine = self.submit(self.requester, "kcw-dup-0001", "4500077777")
        other = self.submit(self.approve(BOB), "kcw-dup-0002", "4500088888")
        self.start()
        reply = self.type(ALICE, "4500077777")
        self.assertIn(f"เลขนี้อยู่ในเรื่อง {mine.number}", texts(reply))
        reply = self.type(ALICE, "4500088888")
        self.assertIn("เลขนี้มีเรื่องที่ยังไม่จบอยู่แล้ว", texts(reply))
        self.assertNotIn(other.number, texts(reply))
        Case.objects.filter(pk=other.pk).update(status=Case.Status.CLOSED)
        self.assertIn("หมายเลขเครื่องวัด", texts(self.type(ALICE, "4500088888")))

    def test_a_return_counts_its_meter_numbers_and_refuses_one_already_returned(self):
        self.start(action="ติดตามใบส่งคืน")
        self.assertIn("เลขใบส่งคืน SAP", texts(self.sent[-1]))
        self.type(ALICE, "5000012345")
        self.type(ALICE, "R-0001 R-0002 / 5(100)A / 2")
        self.press(ALICE, "ไปต่อ")
        self.type(ALICE, "ชำรุด")
        self.type(ALICE, "เปลี่ยนมิเตอร์ใหม่")
        summary = texts(self.press(ALICE, "ข้าม"))
        self.assertIn("เลขใบส่งคืน SAP: 5000012345", summary)
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        line = Case.objects.get().lines.get()
        self.assertEqual((line.serials, line.requested_quantity), (["R-0001", "R-0002"], 2))
        self.start(action="ติดตามใบส่งคืน")
        self.type(ALICE, "5000012399")
        reply = self.type(ALICE, "R-0002/5(100)A/1")
        self.assertIn("อยู่ในเรื่องส่งคืนอื่นที่ยังไม่จบ", texts(reply))


class ProblemTests(KcwTestCase):
    def test_an_outside_reporter_skips_card_two_and_may_skip_every_field(self):
        self.requester.is_external = True
        self.requester.save(update_fields=["is_external"])
        self.open(TILE)  # not refused: the tile also takes problem reports
        reply = self.press(ALICE, "มิเตอร์")
        self.assertIn("แจ้งได้เฉพาะปัญหา", texts(reply))
        self.assertNotIn("ติดตามใบเบิก", buttons(reply))
        self.press(ALICE, "ข้าม")
        reply = self.press(ALICE, "ข้าม")
        self.assertIn("ไปต่อ", buttons(reply))
        self.press(ALICE, "ไปต่อ")
        self.type(ALICE, "มิเตอร์หมุนเร็วผิดปกติ")
        self.press(ALICE, "ข้าม")
        summary = texts(self.press(ALICE, "ข้าม"))
        self.assertNotIn("SAP", summary)
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual(
            (case.kind, case.owner_division.code, case.sap_number), ("problem", "กคว.", "")
        )
        self.assertEqual(case.lines.get().details, [{"group": "meter"}])

    def test_a_requester_reports_a_problem_with_a_meter_number(self):
        reply = self.start(action="แจ้งปัญหา")
        self.assertIn("หมายเลขเครื่องวัด / ขนาด", texts(reply))
        self.assertNotIn("จำนวน", texts(reply))
        self.type(ALICE, "P-0001 / 5(15)A")
        self.press(ALICE, "ไปต่อ")
        self.type(ALICE, "จอไม่แสดงผล")
        self.press(ALICE, "ข้าม")
        self.press(ALICE, "ข้าม")
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        line = Case.objects.get().lines.get()
        self.assertEqual((line.serials, line.requested_quantity), (["P-0001"], None))


class OtherPathTests(KcwTestCase):
    def withdraw_by_command(self):
        self.say(self.postback(ALICE, "m=equipment&kind=withdrawal"))
        self.press(ALICE, "มิเตอร์")
        self.press(ALICE, "MTR_SYN_A")
        self.type(ALICE, "2")
        self.press(ALICE, "ไปต่อ")
        self.type(ALICE, "เปลี่ยนมิเตอร์")
        return self.press(ALICE, "ข้าม")

    def test_a_command_into_kcw_asks_the_sap_number_and_the_missing_fields(self):
        reply = self.withdraw_by_command()
        self.assertIn("เลขใบเบิก SAP", texts(reply))
        reply = self.type(ALICE, "4500055555")
        self.assertIn("พิมพ์ หมายเลขเครื่องวัด / ขนาด", texts(reply))
        summary = texts(self.type(ALICE, "N-0001 N-0002/5(100)A"))
        self.assertIn("เลขใบเบิก SAP: 4500055555", summary)
        self.assertIn("5(100)A", summary)
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual((case.owner_division.code, case.sap_number), ("กคว.", "4500055555"))
        self.assertEqual(case.lines.get().details[0]["numbers"], ["N-0001", "N-0002"])

    def test_with_the_catalog_option_only_the_sap_number_is_asked(self):
        options.change(None, options.SAP_LINE_ENTRY, "catalog", "ทดสอบ")
        self.withdraw_by_command()
        summary = texts(self.type(ALICE, "4500055556"))
        self.assertIn("สรุปเรื่อง", summary)
        self.assertIn("เลขใบเบิก SAP: 4500055556", summary)

    def test_a_division_without_the_switch_asks_no_sap_number(self):
        Division.objects.filter(code="กคว.").update(requires_sap=False)
        self.assertIn("สรุปเรื่อง", texts(self.withdraw_by_command()))
        reply = self.start()
        self.assertIn("หมายเลขเครื่องวัด / ขนาด / จำนวน", texts(reply))  # the card skips it too

    def test_a_line_submission_needs_the_number_staff_intake_does_not(self):
        payload = {
            "kind": "withdrawal",
            "division_code": "กคว.",
            "purpose": "ติดตั้ง",
            "lines": [{"item_code": "MTR_SYN_A", "quantity": "1"}],
        }
        with self.assertRaises(CommandFailed):
            services.submit_case(
                Actor.for_requester(self.requester), idempotency_key="kcw-need-001", payload=payload
            )
        with self.assertRaises(CommandFailed):
            services.submit_case(
                Actor.for_requester(self.requester),
                idempotency_key="kcw-need-002",
                payload={**payload, "sap_number": "4500 12"},
            )


class StaffAndAdminTests(KcwTestCase):
    def setUp(self):
        super().setUp()
        self.kcw_staff = make_staff("kcw-staff", (Role.STAFF, "กคว."))
        self.client = Client()
        self.client.force_login(self.kcw_staff)

    def test_staff_see_search_and_correct_the_sap_number(self):
        case = self.submit(self.requester, "kcw-staff-01", "4500012345")
        page = self.client.get(f"/cases/{case.number}/")
        self.assertContains(page, "เลขใบเบิก SAP")
        self.assertContains(page, "4500012345")
        self.assertRedirects(
            self.client.get("/cases/?q=4500012345"),
            f"/cases/{case.number}/",
            fetch_redirect_response=False,
        )
        response = self.client.post(
            f"/cases/{case.number}/act/set_sap_number/",
            {
                "sap_number": "4500012399",
                "revision": case.revision,
                "idempotency_key": uuid.uuid4().hex,
            },
        )
        self.assertEqual(response.status_code, 302, getattr(response, "context", None))
        case.refresh_from_db()
        self.assertEqual(case.sap_number, "4500012399")
        self.assertContains(self.client.get(f"/cases/{case.number}/"), "4500012345 → 4500012399")

    def test_a_requester_finds_a_case_by_its_sap_number_in_line(self):
        case = self.submit(self.requester, "kcw-find-001", "4500044444")
        self.say(self.postback(ALICE, "m=mine"))
        self.press(ALICE, "ค้นหาเลขเรื่อง")
        reply = self.type(ALICE, "4500044444")
        self.assertIn(f"เรื่อง {case.number}", texts(reply))
        self.assertIn("เลขใบเบิก SAP: 4500044444", texts(reply))

    def test_admins_relabel_fields_add_one_and_change_a_unit(self):
        admin = Client()
        admin.force_login(self.admin)
        size = EntryField.objects.get(group="meter", key="size")
        data = {"action": "fields", f"label-{size.pk}": "ขนาดมิเตอร์", f"position-{size.pk}": "2"}
        data[f"active-{size.pk}"] = "1"
        data["unit-ct"] = "ลูก"
        self.assertEqual(admin.post("/console/admin/items/", data).status_code, 302)
        admin.post(
            "/console/admin/items/",
            {"action": "add_field", "group": "meter", "label": "ยี่ห้อ", "kinds": ["withdrawal"]},
        )
        size.refresh_from_db()
        self.assertEqual((size.label, size.required), ("ขนาดมิเตอร์", False))
        self.assertEqual(EquipmentType.objects.get(code="TYPED_CT").unit, "ลูก")
        self.assertTrue(AuditEvent.objects.filter(action="entry_field.added").exists())
        self.start()
        reply = self.type(ALICE, "4500033333")
        self.assertIn("หมายเลขเครื่องวัด / ขนาดมิเตอร์ / จำนวน / ยี่ห้อ", texts(reply))
        page = admin.get("/console/admin/items/")
        self.assertContains(page, "ช่องที่ผู้ใช้ LINE พิมพ์")
        self.assertNotContains(page, "<td>TYPED_METER</td>")  # not in the item list

    def test_the_division_page_switches_the_sap_question(self):
        admin = Client()
        admin.force_login(self.admin)
        kcw = Division.objects.get(code="กคว.")
        self.assertContains(admin.get("/console/admin/divisions/"), "เลิกถามเลข SAP")
        admin.post(f"/console/admin/divisions/{kcw.pk}/sap-off/", {"reason": ""})
        kcw.refresh_from_db()
        self.assertFalse(kcw.requires_sap)
        self.assertTrue(AuditEvent.objects.filter(action="division.requires_sap").exists())
