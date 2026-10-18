"""ปิดข้อมูลก่อนส่ง AI (ADR 0014; routing grill 14 Oct 2569, Q22, Q26-Q35).

The user approved real case text going to the AI in production on one condition: personal data
is masked first, in every AI task and every environment. Acceptance → tests:
- every kind is masked and a title stays; roads, sois, districts, item codes, case numbers,
  dates and amounts stay (DetectorTests: Q27, Q32, Q35);
- labels count afresh in every call, one value keeps one label, label-like text people typed is
  never replaced, and the values come back into the answer (LabelTests: Q28, Q33);
- extraction, chat answers, triage and screening mask before the provider sees anything, the
  draft still gets the real values, and the counts record how many of each kind with the
  evaluations apart (TaskTests: Q26, Q30);
- every staff member tries the masking and adds words, only an admin removes one, and others see
  only hints (PageTests: Q30, Q34).
"""

import json
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from operations import ai, ai_answer, ai_screen, ai_triage, pii
from operations.management.commands.run_worker import run_once
from operations.models import (
    AuditEvent,
    ConversationDraft,
    Division,
    MaskCount,
    MaskedWord,
    Role,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import ALICE
from operations.tests.test_m05_ai import AITestCase, answer, request

PAGE = "/console/pii/"


def thai_id(first12):
    total = sum(int(d) * (13 - i) for i, d in enumerate(first12))
    return first12 + str((11 - total % 11) % 10)


VALID_ID = thai_id("310170020717")  # synthetic, with a correct check digit


def masker():
    return pii.Masker(
        names=pii.Names.of(full={"สมศรี มีสุข", "สมศรีมีสุข"}, first={"สมศรี"}),
        words=["โครงการลับ"],
        codes=["MTR_SYN_A", "1010020001"],
        stops={"มิเตอร์", "หม้อแปลง", "สายไฟ", "ไหม้"},
    )


class DetectorTests(SimpleTestCase):
    def check(self, text, expected):
        self.assertEqual(masker().mask(text), expected)

    def test_phone_numbers(self):
        for text in ("โทร 081-234-5678", "โทร 0812345678", "โทร +66 81 234 5678", "โทร ๐๘๑๒๓๔๕๖๗๘"):
            self.check(text, "โทร [เบอร์โทร-1]")
        self.check("สำนักงาน 02-123-4567 ต่อ 12 ค่ะ", "สำนักงาน [เบอร์โทร-1] ค่ะ")

    def test_ids_mail_links_and_line(self):
        spaced = f"{VALID_ID[0]}-{VALID_ID[1:5]}-{VALID_ID[5:10]}-{VALID_ID[10:12]}-{VALID_ID[12]}"
        self.check(f"บัตร {spaced}", "บัตร [เลขบัตร-1]")
        self.check("ส่ง somchai.j@example.co.th นะ", "ส่ง [อีเมล-1] นะ")
        self.check("ดูที่ https://example.com/a?b=1. ครับ", "ดูที่ [ลิงก์-1]. ครับ")
        self.check("ไลน์ไอดี somchai99 หรือ @somchai_line", "ไลน์ไอดี [ไลน์-1] หรือ [ไลน์-2]")

    def test_accounts_and_house_numbers_but_not_where_the_work_is(self):
        self.check("CA 012345678 บัญชี 123-4-56789-0", "CA [เลขบัญชี-1] บัญชี [เลขบัญชี-2]")
        self.check(
            "บ้านเลขที่ 12/3 ซอยลาดพร้าว 101 เขตบางกะปิ รหัสไปรษณีย์ 10240",
            "บ้านเลขที่ [ที่อยู่-1] ซอยลาดพร้าว 101 เขตบางกะปิ รหัสไปรษณีย์ [ที่อยู่-2]",
        )

    def test_names_keep_their_title(self):
        self.check("นายสมชาย ใจดี แจ้งว่ามิเตอร์เสีย", "นาย[ชื่อ-1] แจ้งว่ามิเตอร์เสีย")
        self.check("คุณสมหมายโทรมาบอก", "คุณ[ชื่อ-1]โทรมาบอก")
        self.check("นางสาวกมลวรรณ ศรีสุข เขตบางกะปิ", "นางสาว[ชื่อ-1] เขตบางกะปิ")
        self.check("Mr. John Smith called", "Mr. [ชื่อ-1] called")
        for text in (
            "ขอบคุณมากครับ",
            "คุณภาพไฟไม่ดี",
            "นายช่างมาดูแล้ว",
            "คุณช่วยดูหน่อย",
            "นางพยาบาลมาด้วย",
        ):
            self.check(text, text)

    def test_names_the_system_knows_and_words_staff_listed(self):
        self.check("สมศรีแจ้งว่าไฟดับ", "[ชื่อ-1]แจ้งว่าไฟดับ")
        self.check("ยืนยันโดย สมศรี มีสุข แล้ว", "ยืนยันโดย [ชื่อ-1] แล้ว")
        self.check("สมศรีสุดา ไม่เกี่ยว", "สมศรีสุดา ไม่เกี่ยว")  # inside another name
        self.check("เรื่องโครงการลับ", "เรื่อง[ข้อมูลปิด-1]")

    def test_long_numbers_but_not_what_the_work_needs(self):
        self.check("เลขเครื่อง SN12345678 มิเตอร์เลข 4402135589", "เลขเครื่อง [เลข-1] มิเตอร์เลข [เลข-2]")
        kept = (
            "เรื่อง W69-000001 รหัส MTR_SYN_A และ 1010020001 วันที่ 2026-10-14 หรือ 14/10/2569 "
            "ราคา 120000 บาท จำนวน 25 ตัว"
        )
        self.check(kept, kept)


class LabelTests(SimpleTestCase):
    def test_one_value_one_label_and_a_fresh_count_each_call(self):
        first = masker()
        self.assertEqual(
            first.mask("081-234-5678 หรือ 0812345678 หรือ 0898765432"),
            "[เบอร์โทร-1] หรือ [เบอร์โทร-1] หรือ [เบอร์โทร-2]",
        )
        self.assertEqual(first.counts, {"phone": 2})
        self.assertEqual(masker().mask("0898765432"), "[เบอร์โทร-1]")

    def test_label_like_text_people_typed_stays(self):
        m = masker()
        body = {
            "messages": [
                {"role": "user", "content": "SN99887766"},
                {"role": "user", "content": "ป้าย [เลข-1] ที่พิมพ์มาเอง"},
            ]
        }
        self.assertEqual(m.mask_body(body)["messages"][0]["content"], "[เลข-2]")
        self.assertEqual(m.restore("[เลข-1] / [เลข-2]"), "[เลข-1] / SN99887766")

    def test_only_what_people_typed_and_the_json_stays_valid(self):
        m = masker()
        payload = {
            "message": "โทร 0812345678",
            "catalog": [{"code": "MTR_SYN_A", "name": "มิเตอร์"}],
            "quantity": 1234567,
        }
        body = {
            "model": "x",
            "messages": [
                {"role": "system", "content": "อย่าโทร 0812345678"},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        }
        sent = m.mask_body(body)
        self.assertEqual(sent["messages"][0], body["messages"][0])
        self.assertEqual(
            json.loads(sent["messages"][1]["content"]), {**payload, "message": "โทร [เบอร์โทร-1]"}
        )
        self.assertIn("0812345678", body["messages"][1]["content"])  # the caller's body stays

    def test_values_come_back_into_the_answer(self):
        m = masker()
        m.mask("นายสมชาย ใจดี โทร 0812345678")
        content = json.dumps({"description": 'นาย[ชื่อ-1] โทร [เบอร์โทร-1] "ด่วน"'}, ensure_ascii=False)
        for raw in (content, f"```json\n{content}\n```"):
            data = m.restore_response({"choices": [{"message": {"content": raw}}], "usage": {}})
            self.assertEqual(
                json.loads(data["choices"][0]["message"]["content"])["description"],
                'นายสมชาย ใจดี โทร 0812345678 "ด่วน"',
            )
        plain = m.restore_response({"choices": [{"message": {"content": "ติดต่อ [ชื่อ-1]"}}]})
        self.assertEqual(plain["choices"][0]["message"]["content"], "ติดต่อ สมชาย ใจดี")
        unknown = {"choices": [{"message": {"content": "[ชื่อ-9]"}}]}
        self.assertEqual(m.restore_response(unknown), unknown)


class TaskTests(AITestCase):
    def test_a_report_reaches_the_ai_masked_and_the_draft_gets_the_values(self):
        seen = []

        def echo(body):
            message = json.loads(body["messages"][-1]["content"])["message"]
            seen.append(message)
            return answer(request("problem", ("TRF_SYN_A", None, "หม้อแปลง"), description=message))

        self.llm.default = echo
        typed = "หม้อแปลงหน้าบ้านเสียงดัง ติดต่อคุณสมชายเบอร์ 081-234-5678"
        self.post(self.text(ALICE, typed))
        run_once()
        self.assertEqual(self.claim_and_run_ai(), ["applied"])
        self.assertEqual(seen, ["หม้อแปลงหน้าบ้านเสียงดัง ติดต่อคุณ[ชื่อ-1]เบอร์ [เบอร์โทร-1]"])
        self.assertEqual(ConversationDraft.objects.get().data["fields"]["description"], typed)
        counts = dict(MaskCount.objects.filter(task="extract").values_list("kind", "count"))
        self.assertEqual(counts, {"name": 1, "phone": 1})

    def test_every_task_masks_through_one_door_and_evaluations_count_apart(self):
        tasks, record = [], pii.record

        def spy(task, counts):
            tasks.append(task)
            record(task, counts)

        profile = self.profiles[0]
        typed = json.dumps({"message": "โทร 0812345678"}, ensure_ascii=False)
        turns = [{"role": "system", "content": "s"}, {"role": "user", "content": typed}]
        self.llm.default = {"choices": [{"message": {"content": "{}"}}]}
        parts = [ai_screen.Segment("message", 0, 14, "โทร 0812345678")]
        before = len(self.llm.calls)
        with patch.object(pii, "record", side_effect=spy):
            ai_answer.call(profile, turns, ["office_hours"], 5)
            ai_triage.call(profile, turns, [], 5)
            self.assertEqual(ai_screen.llm_labels(profile, parts, 5), ["request"])
            with pii.counting_as("eval"):
                ai.call_model(profile, turns, [], 5)
        self.assertEqual(tasks, ["answer", "triage", "screen", "extract"])
        bodies = [c["body"] for c in self.llm.calls[before:]] + [self.llm.screen_calls[-1]["body"]]
        self.assertEqual(len(bodies), 4)
        for body in bodies:
            self.assertNotIn("0812345678", json.dumps(body, ensure_ascii=False))
        counts = dict(MaskCount.objects.filter(kind="phone").values_list("task", "count"))
        self.assertEqual(counts, {"answer": 1, "triage": 1, "screen": 1, "eval": 1})


class PageTests(TestCase):
    def setUp(self):
        Division.objects.create(code="D-T", name="กองทดสอบ")
        self.staff = make_staff("staff1", (Role.STAFF, "D-T"))
        self.admin = make_staff("admin1", (Role.ADMIN, None))
        self.client.force_login(self.staff)

    def test_any_staff_member_tries_the_masking_and_nothing_is_kept(self):
        page = self.client.post(PAGE, {"action": "preview", "text": "คุณสมชายโทร 0812345678"})
        self.assertContains(page, "คุณ[ชื่อ-1]โทร [เบอร์โทร-1]")
        self.assertContains(page, "ปิดไป: ชื่อคน 1 · เบอร์โทร 1")
        self.assertFalse(MaskCount.objects.exists())  # trying is not use
        self.assertFalse(AuditEvent.objects.filter(action__startswith="pii.").exists())
        # 15 ต.ค. 2569 Q5: the links are for admins (overview, account menu, admin menu); the
        # page itself still works for any staff member.
        self.assertNotContains(self.client.get("/account/"), f'href="{PAGE}"')
        self.assertNotContains(self.client.get("/console/"), f'href="{PAGE}"')
        self.client.force_login(self.admin)
        self.assertContains(
            self.client.get("/console/"), f'class="btn btn--secondary" href="{PAGE}"'
        )
        self.assertContains(self.client.get("/console/admin/users/"), f'<a href="{PAGE}">')

    def test_staff_add_words_and_only_an_admin_removes_one(self):
        self.client.post(PAGE, {"action": "add", "word": "สมปอง  ทองดี", "reason": "ชื่อลูกค้า"})
        row = MaskedWord.objects.get()
        self.assertEqual((row.word, row.added_by), ("สมปอง ทองดี", self.staff))
        event = AuditEvent.objects.get(action="pii.word_added")
        self.assertEqual(event.data, {"hint": "ส**** ท****"})  # never the word itself
        self.assertEqual(pii.preview("ลูกค้าชื่อสมปอง ทองดี")[0], "ลูกค้าชื่อ[ข้อมูลปิด-1]")
        page = self.client.get(PAGE)
        self.assertContains(page, "ส**** ท****")
        self.assertNotContains(page, "สมปอง")
        self.client.post(PAGE, {"action": "add", "word": "สมปอง ทองดี", "reason": "ซ้ำ"})
        self.assertEqual(MaskedWord.objects.count(), 1)
        remove = {"action": "remove", "word_id": row.pk, "reason": "อยู่ในทะเบียนแล้ว"}
        self.assertEqual(self.client.post(PAGE, remove).status_code, 403)

        self.client.force_login(self.admin)
        self.assertContains(self.client.get(PAGE), "สมปอง ทองดี")
        self.client.post(PAGE, remove)
        row.refresh_from_db()
        self.assertEqual((row.removed_by, row.removal_reason), (self.admin, "อยู่ในทะเบียนแล้ว"))
        self.assertTrue(AuditEvent.objects.filter(action="pii.word_removed").exists())
        self.assertEqual(pii.preview("สมปอง ทองดี")[0], "สมปอง ทองดี")

    def test_counts_show_real_use_and_only_staff_open_the_page(self):
        today = timezone.localdate()
        MaskCount.objects.create(day=today, task="extract", kind="phone", count=3)
        MaskCount.objects.create(day=today, task="eval", kind="phone", count=50)
        page = self.client.get(PAGE)
        self.assertContains(page, "สกัดร่างจากแชต 3")
        self.assertContains(page, '<td class="num">3</td>')
        self.assertNotContains(page, pii.TASKS["eval"])
        self.client.force_login(make_staff("nobody"))
        self.assertEqual(self.client.get(PAGE).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(PAGE).status_code, 302)
