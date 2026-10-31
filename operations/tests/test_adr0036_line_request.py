"""ADR 0036 (grill 30 ต.ค. 2569): what a requester types reaches the case only after they tap
ติดต่อเจ้าหน้าที่ on its card in เรื่องของฉัน, or after staff wrote first (ContactStaffTests);
photos sent while filing or chatting join the case (FileTests); and the case card shows what
the requester picked, the number by the kind they chose, what they typed and their photos,
which open in the browser (PageTests, KojPageTests)."""

import threading
from unittest.mock import patch

from django.test import Client, override_settings

from operations import case_chat, line_api
from operations.models import Case, CaseMessage, Division, Role
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import ALICE, PNG, FakeClamd, buttons, texts
from operations.tests.test_m16_kcw_work import WorkTestCase
from operations.tests.test_m24_koj_cases import KojCaseTestCase


def newest_case():
    return Case.objects.order_by("-submitted_at").first()


class PhotoMixin:
    """The runner fetches a LINE photo (a PNG here) and scans it with a fake ClamAV."""

    def setUp(self):
        super().setUp()
        clamd = FakeClamd()
        threading.Thread(target=clamd.serve_forever, daemon=True).start()
        self.addCleanup(clamd.server_close)
        self.addCleanup(clamd.shutdown)
        scanner = override_settings(CLAMAV_HOST="127.0.0.1", CLAMAV_PORT=clamd.server_address[1])
        scanner.enable()
        self.addCleanup(scanner.disable)
        fetch = patch.object(line_api, "fetch_content", side_effect=lambda mid, limit: PNG)
        fetch.start()
        self.addCleanup(fetch.stop)

    def page(self, case, user=None):
        client = Client()
        client.force_login(user or self.kcw)
        return client, client.get(f"/cases/{case.number}/").content.decode()


class ContactStaffTests(WorkTestCase):
    def test_contact_staff_from_my_cases_and_what_is_typed_reaches_the_card(self):
        case = self.withdraw("4500080001")
        reply = self.type(ALICE, "สอบถามเรื่องใบเบิกครับ")
        self.assertIn("กด เรื่องของฉัน เลือกเรื่อง แล้วกด ติดต่อเจ้าหน้าที่", texts(reply))
        self.assertFalse(CaseMessage.objects.exists())  # nothing joins a case unasked
        self.say(self.postback(ALICE, "m=mine"))
        card = self.press(ALICE, case.number)
        self.assertIn("เลขที่ใบเบิก: 4500080001", texts(card))  # the kind the requester picked
        reply = self.press(ALICE, "ติดต่อเจ้าหน้าที่")
        self.assertIn(f"พิมพ์ข้อความถึงเจ้าหน้าที่เรื่อง {case.number} ได้เลย", texts(reply))
        sent = len(self.sent)
        self.type(ALICE, "ขอด่วนนะครับ ลูกค้ารอติดตั้ง")
        self.assertEqual(len(self.sent), sent)  # the bot keeps quiet so staff can answer free
        self.assertEqual(
            CaseMessage.objects.get(direction="requester").text, "ขอด่วนนะครับ ลูกค้ารอติดตั้ง"
        )
        client = Client()
        client.force_login(self.kcw)
        thread = client.get(f"/cases/{case.number}/chat/thread/")
        self.assertContains(thread, "ขอด่วนนะครับ ลูกค้ารอติดตั้ง")
        Case.objects.filter(pk=case.pk).update(status=Case.Status.CLOSED)
        self.assertNotIn("ติดต่อเจ้าหน้าที่", buttons(self.type(ALICE, case.number)))


class FileTests(PhotoMixin, WorkTestCase):
    def test_photos_sent_while_filing_and_while_chatting_reach_the_case(self):
        self.track("มิเตอร์", "ติดตามใบเบิก", number="4500080003")
        self.say(self.image(ALICE))
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = newest_case()
        self.assertEqual(case.attachments.count(), 1)
        case_chat.send(self.kcw, case, "ขอรูปใบเบิกอีกใบ", "สมหญิง กคว. 1234")
        self.type(ALICE, "ได้ครับ")  # takes the held message; the chat opens
        reply = self.say(self.image(ALICE))
        self.assertIn(f"ได้รับไฟล์แล้ว แนบกับเรื่อง {case.number}", texts(reply))
        self.assertEqual(case.attachments.count(), 2)
        photos = [
            (i["meta"], i["faint"]) for i in case_chat.thread(case) if "ผู้แจ้งส่งรูป" in i["text"]
        ]
        self.assertEqual(photos, [("ผู้แจ้ง · ตอนสร้างเรื่อง", True), ("ผู้แจ้ง", False)])


class PageTests(PhotoMixin, WorkTestCase):
    def test_the_card_shows_the_picks_the_number_by_its_kind_the_details_and_photos(self):
        self.track("มิเตอร์", "ติดตามใบเบิก", kind="หมายเลขเครื่องวัดฯ", number="12345678")
        self.say(self.image(ALICE))
        self.press(ALICE, "เพิ่มรายละเอียด")
        self.type(ALICE, "ใช้ติดตั้งงานลูกค้ารายใหม่")
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = newest_case()
        client, page = self.page(case)
        for text in (
            "ข้อมูลที่ผู้แจ้งส่งผ่าน LINE",
            "รายการที่ผู้แจ้งเลือก",
            '<span class="pick pick--tile">Meter, CT, VT</span>',
            '<span class="pick">มิเตอร์</span>',
            '<span class="pick">ติดตามใบเบิก</span>',
            '<span class="pick">หมายเลขเครื่องวัดฯ</span>',
            '<p class="tracking__label">หมายเลขเครื่องวัดฯ</p>',
            'data-copy="12345678"',
            "รายละเอียดที่ผู้แจ้งระบุ",
            "ใช้ติดตั้งงานลูกค้ารายใหม่",
            "รูปที่ผู้แจ้งแนบ",
        ):
            self.assertIn(text, page)
        for text in ("เลขใบเบิก SAP", "· เมนู", "ข้อความจากผู้แจ้ง", "ไม่ได้ระบุรายการของ"):
            self.assertNotIn(text, page)
        photo = case.attachments.get()
        self.assertIn(f'src="/attachments/{photo.pk}/"', page)
        response = client.get(f"/attachments/{photo.pk}/")
        self.assertTrue(response["Content-Disposition"].startswith("inline"))  # opens, no download
        photo.content_type = "application/pdf"
        photo.save(update_fields=["content_type"])
        response = client.get(f"/attachments/{photo.pk}/")
        self.assertTrue(response["Content-Disposition"].startswith("attachment"))

    def test_without_details_the_card_says_so(self):
        self.track("CT", "ติดตามใบเบิก", kind="โครงการ", number="ปรับปรุงสถานีย่อยบางกะปิ")
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        _, page = self.page(newest_case())
        self.assertIn('<span class="pick">โครงการ</span>', page)
        self.assertIn("ผู้แจ้งไม่ได้ระบุรายละเอียดเพิ่มเติม", page)
        self.assertNotIn("รูปที่ผู้แจ้งแนบ", page)


class KojPageTests(PhotoMixin, KojCaseTestCase):
    def test_a_typed_equipment_without_a_number(self):
        self.open_koj()
        self.press(ALICE, "อุปกรณ์อื่น")
        self.press(ALICE, "พิมพ์ชื่ออุปกรณ์")
        self.type(ALICE, "ล่อฟ้าแรงสูง")
        self.press(ALICE, "ติดตามใบเบิก")
        self.press(ALICE, "ไม่มีหมายเลข")
        self.type(ALICE, "10 ตัว ส่งใบเบิก 28 ต.ค.")
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = newest_case()
        division = Division.objects.get(pk=case.owner_division_id)
        _, page = self.page(case, make_staff("koj-viewer", (Role.STAFF, division.code)))
        for text in (
            '<span class="pick">ล่อฟ้าแรงสูง</span>',
            '<span class="pick">ไม่มีหมายเลข</span>',
            "ไม่มีเลขติดตาม เจ้าหน้าที่ค้นหาเอกสารจากรายละเอียดที่ผู้แจ้งระบุ",
            "10 ตัว ส่งใบเบิก 28 ต.ค.",
        ):
            self.assertIn(text, page)
