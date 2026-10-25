"""A version with menu_fields reads where a message belongs on the LINE menu (owner, 25 Oct
2569 Q8–Q10): the division and topic, a กตฟ. report's meters and districts, ติดตามสถานะการซ่อม
and the number on a piece of other equipment. Versions without it are unchanged."""

import json

from operations import ai
from operations.management.commands.run_worker import run_once
from operations.models import (
    AIConfigVersion,
    ConversationDraft,
    District,
    Division,
    LineMenuItem,
)
from operations.tests.test_m03_line import ALICE, buttons, texts
from operations.tests.test_m05_ai import answer
from operations.tests.test_m14_typed_ai import TypedTestCase, typed_request

THEFT = "สงสัยลักใช้ไฟ เครื่องวัดหมายเลข 12345678 เขตบางเขน"
REPAIR = "ขอติดตามงานซ่อม เครื่องบีบสายไฟแบบแบตเตอรี่ หมายเลข BB-011"


def menu_request(
    intent, *items, division=None, topic=None, meters=(), numbers=(), sap_number=None, **fields
):
    """A menu version's answer: typed_request plus the menu path. `numbers` gives each item's
    equipment_number in order."""
    entry = typed_request(intent, *items, sap_number=sap_number, **fields)
    entry.update(
        division=division,
        topic=topic,
        meters=[{"number": n, "district": d} for n, d in meters],
    )
    for index, item in enumerate(entry["items"]):
        item["equipment_number"] = numbers[index] if index < len(numbers) else None
    return entry


class MenuTestCase(TypedTestCase):
    def setUp(self):
        super().setUp()
        for code, name in (("D14", "บางเขน"), ("D15", "บางนา")):
            District.objects.update_or_create(
                code=code, defaults={"name": f"การไฟฟ้านครหลวงเขต{name}", "is_active": True}
            )

    def menu_version(self):
        version = ai.create_version(self.dev, self.profiles, typed_fields=True, menu_fields=True)
        AIConfigVersion.objects.filter(status=AIConfigVersion.Status.ACTIVE).update(
            status=AIConfigVersion.Status.RETIRED
        )
        AIConfigVersion.objects.filter(pk=version.pk).update(status=AIConfigVersion.Status.ACTIVE)
        return version

    def read(self, text, *requests, menu=True):
        raw = json.dumps({"requests": list(requests)}, ensure_ascii=False)
        return ai.validate(raw, text, ai._catalog(True, menu))

    def chat(self, text, *requests):
        self.llm.default = answer(*requests)
        self.post(self.text(ALICE, text))
        run_once()
        self.assertEqual(self.claim_and_run_ai(), ["applied"])
        return ConversationDraft.objects.get(status="open")


class SchemaTests(MenuTestCase):
    def test_only_a_menu_version_sees_the_menu_path(self):
        codes = [i.code for i in ai._catalog(True)]
        typed = ai.response_schema(codes, typed=True)
        menu = ai.response_schema(codes, typed=True, menu=True)
        plain = typed["properties"]["requests"]["items"]
        path = menu["properties"]["requests"]["items"]
        self.assertNotIn("division", plain["properties"])
        self.assertNotIn("repair", plain["properties"]["intent"]["enum"])
        self.assertIn("repair", path["properties"]["intent"]["enum"])
        self.assertIn("กตฟ.", path["properties"]["division"]["enum"])
        self.assertIn("เครื่องวัดฯ มีการกระทำ", path["properties"]["topic"]["enum"])
        self.assertIn("meters", path["required"])
        self.assertIn("equipment_number", path["properties"]["items"]["items"]["properties"])
        typed_prompt = ai._messages("x", None, ai._catalog(True))[0]["content"]
        menu_prompt = ai._messages("x", None, ai._catalog(True, True))[0]["content"]
        self.assertNotIn("Menu path", typed_prompt)
        self.assertIn("Menu path", menu_prompt)
        version = ai.create_version(self.dev, self.profiles, typed_fields=True, menu_fields=True)
        self.assertEqual(ai.schema_version_for(version), ai.MENU_SCHEMA_VERSION)
        self.assertTrue(ai.eval_gate_for(version).endswith("+menu-v1"))


class GroundingTests(MenuTestCase):
    def test_a_theft_report_is_a_ktf_topic_with_its_meter_and_district(self):
        got = self.read(
            THEFT,
            menu_request(
                "problem",
                topic="เครื่องวัดฯ มีการกระทำ",
                meters=[("12345678", "บางเขน"), ("99999999", "บางนา")],
                description="สงสัยลักใช้ไฟ",
            ),
        ).requests[0]
        # The topic decides the division (Q10); a meter not in the message is dropped.
        self.assertEqual(got.division, "กตฟ.")
        self.assertEqual(got.meters, [{"number": "12345678", "district": "D14"}])
        got = self.read(
            THEFT,
            menu_request(
                "problem",
                division="กคว.",
                topic="เครื่องวัดฯ มีการกระทำ",
                meters=[("12345678", "บางนา")],
            ),
        ).requests[0]
        self.assertEqual(got.division, "กตฟ.")
        self.assertEqual(got.meters, [{"number": "12345678", "district": ""}])  # not written

    def test_a_ktf_topic_needs_a_sign_that_the_meter_is_a_customers(self):
        # Q10: "มิเตอร์ ... เสีย" or a meter withdrawn on a slip is not shown to be a customer's;
        # the model's guess is dropped and the chat asks whose meter it is.
        for text, division in (
            ("มิเตอร์ 12340000 เสีย", None),
            ("แจ้งปัญหา มิเตอร์ที่เบิกตามใบเบิก 4500131313 หมายเลข 77001234 ไม่หมุน", "กคว."),
        ):
            with self.subTest(text=text):
                got = self.read(
                    text,
                    menu_request("problem", division=division, topic="เครื่องวัดฯ ชำรุด", meters=[]),
                ).requests[0]
                self.assertEqual((got.topic, got.division), ("", ""))
        got = self.read(
            "มิเตอร์ของลูกค้า 12340000 เสีย",
            menu_request("problem", division="กคว.", topic="เครื่องวัดฯ ชำรุด"),
        ).requests[0]
        self.assertEqual((got.topic, got.division), ("เครื่องวัดฯ ชำรุด", "กตฟ."))

    def test_a_lone_serial_of_other_equipment_is_its_number(self):
        item = ("TYPED_OTHER", None, "สว่านไฟฟ้า", ["DR-2001"], None)
        got = self.read(
            "ติดตามงานซ่อมสว่านไฟฟ้า หมายเลข DR-2001", menu_request("repair", item, division="กคอ.")
        ).requests[0]
        self.assertEqual(got.items[0].equipment_number, "DR-2001")

    def test_a_repair_names_its_piece_only_when_the_message_does(self):
        item = ("TYPED_OTHER", None, "เครื่องบีบสายไฟแบบแบตเตอรี่", [], None)
        for number, expected in (("BB-011", "BB-011"), ("BB-999", "")):
            with self.subTest(number=number):
                got = self.read(
                    REPAIR, menu_request("repair", item, division="กคอ.", numbers=[number])
                ).requests[0]
                self.assertEqual((got.intent, got.division), ("repair", "กคอ."))
                self.assertEqual(got.items[0].equipment_number, expected)

    def test_older_versions_cannot_answer_with_the_menu_path(self):
        with self.assertRaises(ai.SchemaError):
            self.read(REPAIR, menu_request("repair"), menu=False)
        got = self.read(
            THEFT, menu_request("problem", topic="เครื่องวัดฯ มีการกระทำ"), menu=False
        ).requests[0]
        self.assertEqual((got.division, got.topic, got.menu_read), ("", "", False))


class ChatTests(MenuTestCase):
    def setUp(self):
        super().setUp()
        self.menu_version()

    def test_free_text_goes_straight_to_the_ktf_topic(self):
        draft = self.chat(
            THEFT,
            menu_request(
                "problem",
                topic="เครื่องวัดฯ มีการกระทำ",
                meters=[("12345678", "บางเขน")],
                description="สงสัยลักใช้ไฟ",
            ),
        )
        self.assertEqual(
            (draft.data["topic"], draft.data["division"], draft.data["menu_label"]),
            ("usage", "กตฟ.", "ตรวจสอบการใช้ไฟฟ้า · เครื่องวัดฯ มีการกระทำ"),
        )
        self.assertEqual(draft.data["meters"], [{"number": "12345678", "district": "D14"}])
        self.assertEqual(draft.data["lines"], [])
        self.assertTrue(draft.step.startswith("field:"))

    def test_a_meter_without_its_district_asks_the_district(self):
        draft = self.chat(
            "เครื่องวัดของลูกค้า 12345678 หน้าปัดแตก",
            menu_request("problem", topic="เครื่องวัดฯ ชำรุด", meters=[("12345678", None)]),
        )
        self.assertEqual((draft.step, draft.data["meter"]), ("district", "12345678"))
        reply = self.say(self.text(ALICE, "บางนา"))
        self.assertIn("12345678 (เขตบางนา)", texts(reply))

    def test_a_repair_of_kkho_equipment_by_its_number(self):
        draft = self.chat(
            REPAIR,
            menu_request(
                "repair",
                ("TYPED_OTHER", None, "เครื่องบีบสายไฟแบบแบตเตอรี่", [], None),
                division="กคอ.",
                numbers=["BB-011"],
            ),
        )
        self.assertEqual(
            (draft.kind, draft.data["topic"], draft.data["division"], draft.data["sap_skipped"]),
            ("problem", "repair", "กคอ.", True),
        )
        # The size was not written: asked (and may be skipped), the rest already filled.
        entry = draft.data["entry"]
        self.assertEqual((draft.step, entry["keys"]), ("entry", ["size"]))
        self.assertEqual(
            (entry["row"]["name"], entry["row"]["equipment_number"]),
            ("เครื่องบีบสายไฟแบบแบตเตอรี่", "BB-011"),
        )
        self.assertEqual(draft.data["menu_label"], "เครื่องมือและอุปกรณ์ · ติดตามสถานะการซ่อม")
        self.press(ALICE, "ข้าม")
        reply = self.press(ALICE, "ไปต่อ")
        self.assertIn("สิ่งที่ต้องการติดตาม", texts(reply))  # no SAP number needed (Q7)

    def test_an_unplaced_meter_asks_whose_meter_it_is(self):
        tile = LineMenuItem.objects.create(
            tab="report", place="tile", position=5, label="ตรวจสอบการใช้ไฟฟ้า", action="topic"
        )
        LineMenuItem.objects.create(
            tab="report",
            place="choice",
            position=2,
            label="เครื่องวัดฯ ชำรุด",
            action="problem",
            topic="usage",
            parent=tile,
        )
        draft = self.chat(
            "มิเตอร์ 12345678 เสีย", menu_request("problem", meters=[("12345678", None)])
        )
        self.assertEqual(draft.step, "which_division")
        reply = self.sent[-1]
        self.assertEqual(list(buttons(reply))[:2], ["มิเตอร์บ้านผู้ใช้ไฟ", "มิเตอร์ของ กคว."])
        reply = self.press(ALICE, "มิเตอร์บ้านผู้ใช้ไฟ")
        self.assertIn("เลือกหัวข้อ", texts(reply))
        reply = self.press(ALICE, "เครื่องวัดฯ ชำรุด")
        self.assertIn("เครื่องวัดฯ 12345678 ติดตั้งอยู่เขตไหน", texts(reply))
        draft.refresh_from_db()
        self.assertEqual(
            (draft.data["topic"], draft.data["division"], draft.data["menu_label"]),
            ("usage", "กตฟ.", "ตรวจสอบการใช้ไฟฟ้า · เครื่องวัดฯ ชำรุด"),
        )


class SplitTests(MenuTestCase):
    """Q19: one message about equipment of two divisions becomes one case per division."""

    def setUp(self):
        super().setUp()
        self.menu_version()
        tile = LineMenuItem.objects.create(tab="report", place="tile", position=1, label="ทดสอบ")
        for position, (group, code) in enumerate((("meter", "กคว."), ("transformer", "กอจ.")), 1):
            division, _ = Division.objects.get_or_create(code=code, defaults={"name": code})
            LineMenuItem.objects.create(
                tab="report",
                place="choice",
                position=position,
                label=group,
                action="equipment",
                topic=group,
                division=division,
                parent=tile,
            )

    def test_rows_of_two_divisions_name_no_division(self):
        meter = ("TYPED_METER", 1, "มิเตอร์", ["11110001"], "5(15)A")
        transformer = ("TYPED_TRF", 1, "หม้อแปลง", ["TR-1601"], "160 kVA")
        draft = self.chat(
            "ขอเบิกมิเตอร์ 5(15)A 1 เครื่อง หมายเลข 11110001 และหม้อแปลง 160 kVA 1 เครื่อง "
            "หมายเลข TR-1601 ใบเบิก 4500777001",
            menu_request("withdrawal", meter, transformer, sap_number="4500777001"),
        )
        self.assertNotIn("division", draft.data)
        draft.delete()
        draft = self.chat(
            "ขอเบิกมิเตอร์ 5(15)A 1 เครื่อง หมายเลข 11110001 ใบเบิก 4500777002",
            menu_request("withdrawal", meter, sap_number="4500777002"),
        )
        self.assertEqual(draft.data["division"], "กคว.")


class GateTests(MenuTestCase):
    def test_a_menu_version_needs_menu_v1_before_a_switch(self):
        from operations import ai_eval
        from operations.tests.test_m14_typed_ai import passing_typed_gate

        version = ai.create_version(self.dev, self.profiles, typed_fields=True, menu_fields=True)
        ai.test_version(self.dev, version)
        report = passing_typed_gate(version)
        report.update(gate=ai.MENU_EVAL_GATE, schema=ai.MENU_SCHEMA_VERSION)
        version = ai.record_eval(version, report)  # typed sets only
        self.assertFalse(version.eval_passed)
        dataset, _, cases = ai_eval.load(ai_eval.DATASETS["menu1"])
        report["profiles"].append(
            {
                "dataset": dataset,
                "dataset_sha256": ai_eval.MENU_SHA256,
                "role": "application",
                "metrics": {**ai_eval.MENU_THRESHOLDS, "cases": len(cases)},
            }
        )
        version = ai.record_eval(version, report)
        self.assertTrue(version.eval_passed)
        self.assertIsNone(ai.activation_blocker(version))
        report["profiles"][-1]["metrics"]["ask_accuracy"] = 0.0  # guessed an unplaced meter
        self.assertFalse(ai_eval.gate_passed(report, version))


class ExampleTests(MenuTestCase):
    """examples-v3 (25 Oct 2569 Q17): installed anywhere by an AI editor, and shown only to
    the versions that read what they answer."""

    def test_an_ai_editor_installs_a_bundled_set_with_a_reason(self):
        from io import StringIO

        from django.core.management import call_command
        from django.core.management.base import CommandError

        from operations.models import AIExample, AuditEvent

        out = StringIO()
        call_command(
            "install_ai_examples", set="v3", username="dev", reason="ตัวอย่างใหม่", stdout=out
        )
        self.assertIn("12 ensured, 12 new", out.getvalue())
        call_command("install_ai_examples", set="v3", username="dev", reason="ซ้ำ", stdout=out)
        self.assertIn("12 ensured, 0 new", out.getvalue())
        self.assertEqual(AIExample.objects.filter(source="curated:examples-v3").count(), 12)
        event = AuditEvent.objects.filter(action="ai.examples_installed").first()
        self.assertEqual((event.data["set"], len(event.data["sha256"])), ("examples-v3", 64))
        with self.assertRaises(CommandError):
            call_command("install_ai_examples", set="v3", username="dev", reason=" ")
        from operations.tests.test_m02_cases import make_staff

        make_staff("not-ai-editor")
        with self.assertRaises(CommandError):
            call_command("install_ai_examples", set="v3", username="not-ai-editor", reason="x")

    def test_menu_examples_reach_menu_versions_only(self):
        ai.install_examples(self.dev, "v3", "ทดสอบ")
        typed = [i.code for i in ai._catalog(True)]
        found = ai.retrieve_examples("สงสัยลักใช้ไฟ เครื่องวัด 1234 เขตบางนา", None, 5, typed)
        self.assertFalse(any(ai._menu_example(e) for e in found))
        found = ai.retrieve_examples("สงสัยลักใช้ไฟ เครื่องวัด 1234 เขตบางนา", None, 5, typed, menu=True)
        theft = next(e for e in found if "อ้อมมิเตอร์" in e.text)
        shown = ai._example_answer(theft, typed=True, menu=True)["requests"][0]
        self.assertEqual(
            (shown["division"], shown["topic"], shown["meters"]),
            ("กตฟ.", "เครื่องวัดฯ มีการกระทำ", [{"number": "40506070", "district": "คลองเตย"}]),
        )
        slip = next(
            e
            for e in ai.retrieve_examples("ขอเบิกมิเตอร์ ใบเบิก 45001", None, 99, typed)
            if "44005566" in e.text
        )
        shown = ai._example_answer(slip, typed=True)["requests"][0]
        self.assertEqual(shown["sap_number"], "4500333444")
        self.assertNotIn("division", shown)
        self.assertEqual(shown["items"][0]["serials"], ["44005566"])
        plain = [i.code for i in ai._catalog(False)]
        found = ai.retrieve_examples("ขอเบิกมิเตอร์ ใบเบิก 45001", None, 99, plain)
        self.assertFalse(any("44005566" in e.text for e in found))  # typed codes: typed only


class UnplacedRowTests(MenuTestCase):
    def test_a_meter_row_waits_while_the_chat_asks_whose_meter_it_is(self):
        # typed-v1 T06 on 25 Oct 2569: the row still asking its size was lost at the question.
        self.menu_version()
        draft = self.chat(
            "มิเตอร์หมายเลข 33005678 หน้าจอไม่แสดงผล",
            menu_request("problem", ("TYPED_METER", None, "มิเตอร์", ["33005678"], None)),
        )
        self.assertEqual(draft.step, "which_division")
        self.assertEqual(draft.data["entry"]["row"]["numbers"], ["33005678"])
        self.assertEqual(draft.data["meters_pending"], ["33005678"])
        reply = self.press(ALICE, "มิเตอร์ของ กคว.")
        draft.refresh_from_db()
        self.assertEqual((draft.step, draft.data["division"]), ("entry", "กคว."))
        self.assertIn("พิมพ์ขนาด", texts(reply))
