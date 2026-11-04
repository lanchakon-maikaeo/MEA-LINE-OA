"""เลขติดตามหลายเลขและวันจ่ายของแต่ละเลข (ADR 0047, grill 4 พ.ย. 2569)."""

from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from operations import case_numbers, typed_lines, warehouse
from operations.models import Case, CaseMessage, CaseMilestone, DivisionSchedule, Role
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m16_kcw_work import WorkTestCase

S = CaseMilestone.Stage


class CaseNumberTests(WorkTestCase):
    def setUp(self):
        super().setUp()
        DivisionSchedule.objects.update_or_create(
            division=self.division,
            defaults={"sla": [{"group": "meter", "size": "", "days": 3}], "issue_weekdays": [1, 3]},
        )
        self.web = Client()
        self.web.force_login(self.kcw)

    def edit(self, case, **data):
        return self.web.post(reverse("case-numbers", args=[case.number]), data, follow=True)

    def test_staff_add_numbers_from_the_details_and_remove_one_with_a_reason(self):
        case = self.withdraw("142019186")
        Case.objects.filter(pk=case.pk).update(
            purpose="เลขที่ใบเบิก 142019186, 142019188, 142019190 วันที่ 04/11/2569"
        )
        case.refresh_from_db()
        self.assertEqual(case_numbers.found_in_text(case), ["142019188", "142019190"])
        page = self.edit(
            case, kind="slip", found=["142019188", "142019190"], typed="142019191 142019192"
        )
        self.assertContains(page, "เพิ่มเลข 142019191, 142019192, 142019188, 142019190 แล้ว")
        self.assertContains(page, "เลขที่เจ้าหน้าที่เพิ่ม")
        self.assertEqual(case.extra_numbers.count(), 4)
        search = self.web.get(reverse("case-search"), {"q": "142019190"})
        self.assertRedirects(search, reverse("case-detail", args=[case.number]))
        self.assertEqual(warehouse.find_cases(self.kcw, self.division, ["142019192"])[0], [case])
        extra = case.extra_numbers.get(number="142019192")
        self.edit(case, remove=extra.pk, reason="")
        self.assertTrue(case.extra_numbers.filter(number="142019192").exists())
        page = self.edit(case, remove=extra.pk, reason="ผู้แจ้งพิมพ์ผิด")
        self.assertFalse(case.extra_numbers.filter(number="142019192").exists())
        self.assertContains(page, "ลบเลข 142019192")
        self.assertContains(page, "ผู้แจ้งพิมพ์ผิด")
        with self.assertRaisesMessage(ValidationError, "อยู่ในเรื่องนี้แล้ว"):
            case_numbers.add(self.kcw, case, "slip", ["142019186"])  # the first number
        with self.assertRaises(PermissionDenied):
            case_numbers.add(make_staff("kko-clerk", (Role.STAFF, "กคอ.")), case, "slip", ["1234"])

    def test_each_number_keys_its_own_pay_day_and_the_message_names_them(self):
        case = self.withdraw("4500070001")
        case_numbers.add(self.kcw, case, "slip", ["4500070002"])
        case_numbers.add(self.kcw, case, "meter", ["99887766"])  # a meter has no pay day
        self.assertEqual(
            warehouse.payable(case), [("", "4500070001"), ("4500070002", "4500070002")]
        )
        monday = timezone.localdate() - timedelta(days=timezone.localdate().weekday() + 7)
        warehouse.record(self.kcw, [case], S.RECEIVED, monday)
        page = self.web.get(reverse("case-detail", args=[case.number]))
        self.assertContains(page, 'name="number" value="4500070002" checked')
        thursday = monday + timedelta(days=3)
        warehouse.record(self.kcw, [case], S.KEYED, monday, thursday, numbers=["4500070002"])
        self.assertEqual(warehouse.unkeyed(case), [("", "4500070001")])
        done, _ = warehouse.record(self.kcw, [case], S.KEYED, monday, numbers=["4500070002"])
        self.assertEqual((done, warehouse.unkeyed(case)), ([], [("", "4500070001")]))
        held = CaseMessage.objects.filter(case=case, status="held").order_by("pk")
        self.assertIn("\nเลขที่ 4500070002\nกำหนดจ่าย", held.last().text)
        page = self.web.get(reverse("case-detail", args=[case.number]))
        self.assertContains(page, "บันทึกคีย์จ่ายแล้ว")  # the first number is left to key
        warehouse.record(self.kcw, [case], S.KEYED, monday, thursday + timedelta(days=5))
        self.assertIn("เลขที่ 4500070001\n", held.last().text)
        done, skipped = warehouse.record(self.kcw, [case], S.KEYED, monday, thursday)
        self.assertEqual([why for _, why in skipped], ["บันทึกคีย์จ่ายแล้วไว้แล้ว"])
        lines = warehouse.status_lines(case)
        self.assertIn(f"กำหนดจ่าย 4500070002: {warehouse.thai_date(thursday)}", lines)
        self.assertEqual(sum(line.startswith("กำหนดจ่าย") for line in lines), 2)
        extra = case.extra_numbers.get(number="4500070002")
        with self.assertRaisesMessage(ValidationError, "บันทึกคีย์จ่ายแล้ว จึงลบไม่ได้"):
            case_numbers.remove(self.kcw, case, extra.pk, "ไม่ใช้แล้ว")

    def test_a_case_of_one_number_keys_as_before(self):
        case = self.withdraw("4500070005")
        monday = timezone.localdate() - timedelta(days=timezone.localdate().weekday() + 7)
        warehouse.record(self.kcw, [case], S.RECEIVED, monday)
        warehouse.record(self.kcw, [case], S.KEYED, monday)
        self.assertEqual(CaseMilestone.objects.get(case=case, stage=S.KEYED).number, "")
        held = CaseMessage.objects.filter(case=case, status="held").last()
        self.assertNotIn("เลขที่", held.text)

    def test_a_number_already_open_elsewhere_is_taken_with_a_note(self):
        first = self.withdraw("4500070001")
        second = self.withdraw("4500070003", meter="12345679")
        ((_, others),) = case_numbers.add(self.kcw, second, "slip", ["4500070001"])
        self.assertEqual(others, [first.number])
        page = self.web.get(reverse("case-detail", args=[second.number]))
        self.assertContains(page, "เลขซ้ำกับเรื่อง")
        case_numbers.add(self.kcw, second, "slip", ["4500070009"])
        self.assertTrue(typed_lines.sap_in_use("4500070009", None))  # a new filing hears of it
