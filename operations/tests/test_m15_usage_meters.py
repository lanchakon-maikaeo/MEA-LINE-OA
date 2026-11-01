"""กตฟ.: the five หัวข้อการใช้ไฟฟ้า, each naming meters and their districts (user, 25 Oct 2569 Q3, Q5,
Q11, Q23)."""

from django.test import Client

from operations import services, usage_meters
from operations.contracts import Actor, CommandFailed
from operations.models import Case, District, Role
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import ALICE, buttons, texts
from operations.tests.test_m14_kcw_menu import KcwTestCase

TILE = "ตรวจสอบการใช้ไฟฟ้า"


class UsageTestCase(KcwTestCase):
    def setUp(self):
        super().setUp()
        for code, name in (("D01", "วัดเลียบ"), ("D14", "บางเขน"), ("D15", "บางนา")):
            District.objects.update_or_create(
                code=code, defaults={"name": f"การไฟฟ้านครหลวงเขต{name}", "is_active": True}
            )

    def report(self, meters, key="usage-001", channel_extra=None):
        payload = {
            "kind": "problem",
            "topic": "usage",
            "division_code": "กตฟ.",
            "description": "สงสัยลักใช้ไฟ",
            "lines": [],
            **({"meters": meters} if meters is not None else {}),
            **(channel_extra or {}),
        }
        result = services.submit_case(
            Actor.for_requester(self.requester), idempotency_key=key, payload=payload
        )
        return Case.objects.get(pk=result.case_id)


class TileTests(UsageTestCase):
    def test_the_tile_offers_the_five_topics_with_short_labels(self):
        reply = self.open(TILE)
        self.assertEqual(
            list(buttons(reply)),
            [
                "เครื่องวัดฯ ผิดปกติ",
                "เครื่องวัดฯ ชำรุด",
                "เครื่องวัดฯ ถูกกระทำ",
                "ร้องเรียนไฟแพง",
                "ทบทวนหน่วยไฟฟ้า",
                "ติดตามเรื่อง",
            ],
        )
        shown = {
            item["action"]["label"]: item["action"]["displayText"]
            for item in reply[-1]["quickReply"]["items"]
        }
        self.assertEqual(shown["เครื่องวัดฯ ถูกกระทำ"], "เครื่องวัดฯ มีการกระทำ")
        self.assertEqual(shown["ทบทวนหน่วยไฟฟ้า"], "ทบทวนหน่วยไฟฟ้าจากการร้องเรียน")
        self.assertTrue(all(len(label) <= 20 for label in shown))

    def test_a_report_names_each_meter_and_its_district(self):
        self.open(TILE)
        reply = self.press(ALICE, "เครื่องวัดฯ ถูกกระทำ")
        self.assertIn("ตรวจสอบการใช้ไฟฟ้า · เครื่องวัดฯ มีการกระทำ", texts(reply))
        self.assertIn("พิมพ์หมายเลขเครื่องวัดฯ", texts(reply))
        self.assertIn("4 ถึง 20 ตัว", texts(self.type(ALICE, "ab")))
        reply = self.type(ALICE, "12345678")
        self.assertIn("ติดตั้งอยู่เขตไหน", texts(reply))
        reply = self.type(ALICE, "ลาดพร้าว")
        self.assertIn('ไม่พบเขต "ลาดพร้าว"', texts(reply))
        reply = self.type(ALICE, "เขตบางเขน")
        self.assertIn("12345678 (เขตบางเขน)", texts(reply))
        self.press(ALICE, "เพิ่มเครื่องวัดฯ")
        self.assertIn("อยู่ในเรื่องนี้แล้ว", texts(self.type(ALICE, "12345678")))
        self.press(ALICE, "เพิ่มเครื่องวัดฯ")
        self.type(ALICE, "๘๗๖๕๔๓๒๑")  # Thai digits
        self.type(ALICE, "กฟน. เขต บางนา")
        reply = self.press(ALICE, "ไปต่อ")
        self.assertIn("รายละเอียดเพิ่มเติม", texts(reply))
        self.assertIn("ข้าม", buttons(reply))  # Q11: the details may be skipped
        self.press(ALICE, "ข้าม")
        summary = texts(self.press(ALICE, "ข้าม"))
        self.assertIn("ส่งถึง: กตฟ.", summary)
        self.assertIn("เครื่องวัดฯ: 12345678 (เขตบางเขน), 87654321 (เขตบางนา)", summary)
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual(
            (case.topic, case.owner_division.code, case.menu_label),
            ("usage", "กตฟ.", "ตรวจสอบการใช้ไฟฟ้า · เครื่องวัดฯ มีการกระทำ"),
        )
        self.assertEqual(
            case.meters,
            [{"number": "12345678", "district": "D14"}, {"number": "87654321", "district": "D15"}],
        )
        self.assertIn("เครื่องวัดฯ มีการกระทำ: เครื่องวัดฯ 12345678 (เขตบางเขน)", case.description)
        self.assertEqual(Case.objects.count(), 1)  # Q23: one division, one case
        detail = texts(self.type(ALICE, case.number))
        self.assertIn("เครื่องวัดฯ: 12345678 (เขตบางเขน), 87654321 (เขตบางนา)", detail)
        staff = make_staff("ktf-staff", (Role.STAFF, "กตฟ."))
        client = Client()
        client.force_login(staff)
        self.assertContains(client.get(f"/cases/{case.number}/"), "87654321 (เขตบางนา)")


class DistrictTests(UsageTestCase):
    def test_district_names_match_with_or_without_their_prefixes(self):
        for text in ("วัดเลียบ", "เขตวัดเลียบ", "การไฟฟ้านครหลวงเขตวัดเลียบ", "กฟน. เขต วัดเลียบ"):
            with self.subTest(text=text):
                self.assertEqual(usage_meters.match_district(text).code, "D01")
        for text in ("", "เขต", "วัด", "ลาดพร้าว"):
            with self.subTest(text=text):
                self.assertIsNone(usage_meters.match_district(text))


class SubmissionTests(UsageTestCase):
    def test_a_line_usage_report_needs_a_meter_and_a_known_district_if_any(self):
        with self.assertRaises(CommandFailed):
            self.report(None)
        with self.assertRaises(CommandFailed):
            self.report([{"number": "12345678", "district": "D99"}], key="usage-002")
        with self.assertRaises(CommandFailed):
            self.report([{"number": "1", "district": "D14"}], key="usage-003")
        case = self.report([{"number": "12345678", "district": "D14"}], key="usage-004")
        self.assertEqual(case.meters, [{"number": "12345678", "district": "D14"}])
        # 1 Nov 2569: the LINE flow files a meter whose district was not typed.
        case = self.report([{"number": "12345679", "district": ""}], key="usage-006")
        self.assertEqual(usage_meters.describe(case.meters), "12345679")

    def test_only_a_usage_report_lists_meters(self):
        with self.assertRaises(CommandFailed):
            services.submit_case(
                Actor.for_requester(self.requester),
                idempotency_key="usage-005",
                payload={
                    "kind": "problem",
                    "topic": "system",
                    "division_code": "ศอค.",
                    "description": "ระบบใช้ไม่ได้",
                    "meters": [{"number": "12345678", "district": "D14"}],
                    "lines": [],
                },
            )
