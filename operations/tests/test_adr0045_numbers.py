"""เลขเรื่องตามกองเจ้าภาพ (ADR 0045, grill 4 พ.ย. 2569).

Acceptance → tests: a typed number reads with or without its dot, a space or leading zeros, an
old number still reads, and a number is found in text even with Thai words stuck to it
(ReadTests); a case's number starts with its owner division's code and counts on that
division's sequence, which its orders share, and the web search reads typed numbers the same
way (IssueTests); in LINE a number typed without its division code opens the requester's only
match or offers the matches (RequesterLineTests), and opens a staff member's order
(StaffLineTests).
"""

import uuid

from django.test import Client, SimpleTestCase
from django.urls import reverse
from django.utils import timezone

from operations import numbering, services, work_orders
from operations.contracts import Actor
from operations.models import Case, Division, LineMenuItem
from operations.tests.test_m02_cases import withdrawal as plain_withdrawal
from operations.tests.test_m03_line import ALICE, LineTestCase, texts
from operations.tests.test_m13_org import OrgTestCase, withdrawal
from operations.tests.test_m13_staff_chat import StaffChatTestCase

KNOWN = sorted(["กคว.", "กคอ.", "บท.", "ฝอจ.", "กองเครื่องมือฯ"], key=len, reverse=True)


def year():
    return f"{(timezone.localdate().year + 543) % 100:02d}"


class ReadTests(SimpleTestCase):
    def test_typed_numbers_read_as_issued(self):
        for typed in ("กคว.69-000142", "กคว69-142", "กคว. 69-000142", " กคว 69-0142 "):
            self.assertEqual(numbering.read(typed, KNOWN), "กคว.69-000142", typed)
        self.assertEqual(numbering.read("บท.69-5", KNOWN), "บท.69-000005")
        self.assertEqual(numbering.read("w69-000142", KNOWN), "W69-000142")  # issued before
        self.assertEqual(numbering.read("T69-000001", KNOWN), "T69-000001")
        for typed in ("69-000142", "W69-142", "xกคว.69-1", "สวัสดี", ""):
            self.assertEqual(numbering.read(typed, KNOWN), "", typed)

    def test_numbers_in_text(self):
        text = "เรื่องกคว.69-000142 กับ W69-000001 วันที่ 2569-10-08 โทร 02-123-4567 กองเครื่องมือฯ69-7"
        self.assertEqual(
            numbering.find(text, KNOWN),
            ["กคว.69-000142", "W69-000001", "กองเครื่องมือฯ69-000007"],
        )

    def test_a_number_typed_without_its_code(self):
        self.assertEqual(numbering.tail("69-142"), "69-000142")
        self.assertEqual(numbering.tail("กคว69-142"), "")


class IssueTests(OrgTestCase):
    def test_cases_and_orders_count_on_their_owner_divisions_sequence(self):
        director = self.admin
        self.submit(withdrawal(("MTR_SYN_A", "1"), ("FUSE_SYN_A", "2")))
        self.assertEqual(
            set(Case.objects.values_list("number", flat=True)),
            {f"กคว.{year()}-000001", f"กองอุปกรณ์ฯ{year()}-000001"},
        )
        kcw = Division.objects.get(code="กคว.")
        order = work_orders.create(director, "งานทดสอบ", kcw)
        self.assertEqual(order.number, f"กคว.{year()}-000002")
        _, later = self.submit(withdrawal(("MTR_SYN_A", "1")))
        self.assertEqual(later.number, f"กคว.{year()}-000003")

        client = Client()
        client.force_login(self.kcw)
        found = client.get("/cases/", {"q": f"กคว{year()}-3"})
        self.assertRedirects(
            found, reverse("case-detail", args=[later.number]), fetch_redirect_response=False
        )
        listed = client.get("/cases/", {"q": f"{year()}-1"})
        self.assertContains(listed, f"กคว.{year()}-000001")


class RequesterLineTests(LineTestCase):
    def setUp(self):
        super().setUp()
        self.requester = self.approve(ALICE)

    def submitted(self):
        result = services.submit_case(
            Actor.for_requester(self.requester),
            idempotency_key=f"adr0045-{uuid.uuid4().hex}",
            payload=plain_withdrawal(),
        )
        return Case.objects.get(pk=result.case_id)

    def test_a_number_without_its_code_opens_the_only_match_or_offers_them(self):
        case = self.submitted()
        reply = self.type(ALICE, case.number.replace(".", ""))
        self.assertIn(f"เรื่อง {case.number}", texts(reply))
        tail = case.number[-9:]
        reply = self.type(ALICE, tail)
        self.assertIn(f"เรื่อง {case.number}", texts(reply))
        other = self.submitted()
        Case.objects.filter(pk=other.pk).update(number=f"กคอ.{tail}")
        reply = self.type(ALICE, tail)
        self.assertIn(f"เลข {tail} มี 2 เรื่อง", texts(reply))


class StaffLineTests(StaffChatTestCase):
    def test_find_an_order_by_its_number_without_the_code(self):
        self.link(self.head, ALICE)
        order = work_orders.create(self.director, "งานทดสอบ", self.kae)
        self.tile(LineMenuItem.Action.WORK_FIND)
        reply = self.type(ALICE, order.number[-9:])
        self.assertIn(f"งานสั่ง {order.number}", texts(reply))
