"""ปุ่มปัญหา/ข้อเสนอแนะ (ADR 0030): one bubble per division and ปัญหาการใช้งานหรืออื่นๆ, then the
details in one message; the division picked owns the case, the admins hold the rest on their own
tab, send many to a division at once or answer them; staff say ปัญหา or ข้อเสนอแนะ at the result;
and on production the flow is walked and published by itself."""

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.test import Client, override_settings

from operations import line_flows, problem_pick, queues, services
from operations.contracts import Actor, CommandFailed
from operations.models import (
    AckClock,
    Case,
    LineFlow,
    LineFlowVersion,
    LineMenuItem,
    NotificationDelivery,
    OutboxEvent,
    Role,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import ALICE, buttons, texts
from operations.tests.test_m14_kcw_menu import KcwTestCase

TILE = "ปัญหา/ข้อเสนอแนะ"
BUBBLES = [
    "กองเครื่องวัดหน่วยไฟฟ้า",
    "กองอุปกรณ์การจำหน่าย",
    "กองเครื่องมือเครื่องใช้และอุปกรณ์",
    "ศูนย์ปฏิบัติการระบบอ่านเครื่องวัดอัตโนมัติ",
    "กองตรวจสอบการใช้ไฟฟ้า",
    "แผนกบริหารทั่วไป",
    "ปัญหาการใช้งานหรืออื่นๆ",
]


def carousel(reply):
    flex = [m for m in reply if m["type"] == "flex"]
    return [b["body"]["contents"][0]["text"] for b in flex[-1]["contents"]["contents"]]


class PickTestCase(KcwTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.boss = make_staff("problem-admin", (Role.ADMIN, None))
        cls.kcw_resolver = make_staff("kcw-resolver", (Role.STAFF, "กคว."))
        cls.coordinator = make_staff("fajo-head", (Role.STAFF, "ฝอจ."))

    def setUp(self):
        super().setUp()
        from operations import options
        from operations.models import SystemOption

        SystemOption.objects.update_or_create(key=options.SCREENING, defaults={"value": "off"})

    def tile(self, label):
        return LineMenuItem.objects.get(place="tile", tab="report", label=label, is_active=True)

    def file(self, bubble, details="จอมิเตอร์ดับหลังฝนตก", user=ALICE):
        self.say(self.postback(user, f"m=item&id={self.tile(TILE).pk}"))
        self.press(user, bubble)
        self.type(user, details)
        reply = self.press(user, "ยืนยันส่งเรื่อง")
        return Case.objects.order_by("-submitted_at").first(), reply


class ChatTests(PickTestCase):
    def test_the_tile_shows_one_bubble_per_division_then_the_admins(self):
        reply = self.say(self.postback(ALICE, f"m=item&id={self.tile(TILE).pk}"))
        self.assertIn(problem_pick.PROMPT, texts(reply))
        self.assertEqual(carousel(reply), BUBBLES)
        reply = self.press(ALICE, "กองเครื่องวัดหน่วยไฟฟ้า")
        self.assertIn("พิมพ์รายละเอียดที่ต้องการแจ้ง ส่งรูปประกอบได้", texts(reply))
        reply = self.type(ALICE, "จอมิเตอร์ดับหลังฝนตก")
        summary = texts(reply)
        for line in (
            "สรุปเรื่อง: ปัญหา/ข้อเสนอแนะ",
            "ส่งถึง: กองเครื่องวัดหน่วยไฟฟ้า",
            "รายละเอียด: จอมิเตอร์ดับหลังฝนตก",
        ):
            self.assertIn(line, summary)
        self.assertNotIn("หัวข้อ:", summary)
        self.assertEqual(list(buttons(reply)), ["ยืนยันส่งเรื่อง", "เปลี่ยนกอง", "แก้ไข", "ยกเลิกร่าง"])
        reply = self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual(
            (case.owner_division.code, case.topic, case.held_by_admin, case.status),
            ("กคว.", "general", False, "awaiting_approval"),  # ADR 0044: รอรับเรื่อง
        )
        self.assertIn(
            f"ส่งเรื่อง {case.number} ถึงกองเครื่องวัดหน่วยไฟฟ้าแล้ว ดูความคืบหน้าได้ที่ เรื่องของฉัน",
            texts(reply),
        )

    def test_the_division_changes_on_the_summary_and_the_details_stay(self):
        self.say(self.postback(ALICE, f"m=item&id={self.tile(TILE).pk}"))
        self.press(ALICE, "กองเครื่องวัดหน่วยไฟฟ้า")
        self.type(ALICE, "ขอเพิ่มช่องค้นหาในเว็บ")
        reply = self.press(ALICE, "เปลี่ยนกอง")
        self.assertEqual(carousel(reply), BUBBLES)
        reply = self.press(ALICE, "กองตรวจสอบการใช้ไฟฟ้า")
        self.assertIn("ส่งถึง: กองตรวจสอบการใช้ไฟฟ้า", texts(reply))
        self.assertIn("รายละเอียด: ขอเพิ่มช่องค้นหาในเว็บ", texts(reply))
        reply = self.press(ALICE, "แก้ไข")
        self.assertIn("พิมพ์รายละเอียด", texts(reply))
        reply = self.type(ALICE, "ขอเพิ่มช่องค้นหาเลขเครื่องวัด")
        self.assertIn("รายละเอียด: ขอเพิ่มช่องค้นหาเลขเครื่องวัด", texts(reply))
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        self.assertEqual(Case.objects.get().owner_division.code, "กตฟ.")

    def test_typing_report_a_problem_opens_the_same_carousel(self):
        reply = self.say(self.postback(ALICE, "m=problems"))
        self.assertEqual(carousel(reply), BUBBLES)
        reply = self.type(ALICE, "แผนกบริหารทั่วไป")  # a name typed instead of tapped
        self.assertIn("พิมพ์รายละเอียด", texts(reply))

    def test_other_waits_for_the_admins_out_of_every_division_queue(self):
        case, reply = self.file("ปัญหาการใช้งานหรืออื่นๆ", "เข้าเว็บไม่ได้ตั้งแต่เช้า")
        self.assertTrue(case.held_by_admin)
        self.assertEqual((case.owner_division.code, case.status), ("ฝอจ.", "submitted"))
        self.assertEqual(case.owner_label, "แอดมิน")
        self.assertIn("แอดมินจะตอบเองหรือส่งให้กองที่ดูแล", texts(reply))
        self.assertFalse(AckClock.objects.filter(case=case, kind="intake").exists())
        self.assertFalse(queues.visible(self.coordinator).filter(pk=case.pk).exists())
        self.assertTrue(queues.visible(self.boss).filter(pk=case.pk).exists())

    def test_the_admins_hear_on_line_unless_they_turned_it_off(self):
        from operations import orchestration, staff_email

        quiet = make_staff("quiet-admin", (Role.ADMIN, None))
        quiet.staff_profile.notify_admin_waiting = False
        quiet.staff_profile.save()
        case, _ = self.file("ปัญหาการใช้งานหรืออื่นๆ", "โทร 0812345678 เข้าเว็บไม่ได้")
        event = OutboxEvent.objects.get(
            event_type=orchestration.EVENT_CASE_SUBMITTED, subject_id=str(case.pk)
        )
        with patch("operations.staff_line.plan") as plan:
            staff_email.plan_for_event(event)
        calls = [c.args for c in plan.call_args_list if c.args[0] == "staff_admin_waiting"]
        kind, key, planned, users, chosen = calls[0]
        self.assertEqual((kind, planned, chosen), ("staff_admin_waiting", case, True))
        self.assertIn(self.boss, users)
        self.assertNotIn(quiet, users)
        mine, _ = self.file("กองเครื่องวัดหน่วยไฟฟ้า")  # a division was picked: no admin alert
        event = OutboxEvent.objects.get(
            event_type=orchestration.EVENT_CASE_SUBMITTED, subject_id=str(mine.pk)
        )
        with patch("operations.staff_line.plan") as plan:
            staff_email.plan_for_event(event)
        kinds = {c.args[0] for c in plan.call_args_list}
        self.assertNotIn("staff_admin_waiting", kinds)  # ADR 0048: staff_new_case instead

    def test_the_admin_alert_shows_the_start_of_the_text_masked(self):
        from operations import staff_line

        typed = f"โทร 0812345678 ผม{self.requester.display_name} เข้าเว็บไม่ได้ " + "ก" * 100
        case, _ = self.file("ปัญหาการใช้งานหรืออื่นๆ", typed)
        text = staff_line._text("staff_admin_waiting", case)
        head, body = text.split("\n", 1)
        self.assertEqual(head, f"มีเรื่องรอแอดมิน {case.number}")
        self.assertIn("เข้าเว็บไม่ได้", body)
        self.assertNotIn("0812345678", body)
        self.assertNotIn(self.requester.display_name, body)
        self.assertLessEqual(len(body), 80)
        # every other staff alert keeps the lock screen rule
        self.assertNotIn("เข้าเว็บไม่ได้", staff_line._text("staff_dispute", case))


class AdminTests(PickTestCase):
    def act(self, user, case, action, **payload):
        case.refresh_from_db()
        return services.perform_action(
            Actor.staff(user),
            case.pk,
            action,
            expected_revision=case.revision,
            idempotency_key=f"t-{action}-{case.pk}-{case.revision}",
            payload=payload,
        )

    def test_an_admin_sends_a_held_case_to_a_division(self):
        case, _ = self.file("ปัญหาการใช้งานหรืออื่นๆ")
        with self.assertRaises(CommandFailed):
            self.act(self.coordinator, case, "admin_send", division="กคว.", reason="ของ กคว.")
        self.act(self.boss, case, "admin_send", division="กคว.", reason="เรื่องมิเตอร์")
        case.refresh_from_db()
        self.assertEqual(
            (case.held_by_admin, case.owner_division.code, case.status),
            (False, "กคว.", "awaiting_approval"),  # ADR 0044: รอรับเรื่อง
        )

    def test_an_admin_answers_a_held_case_and_says_it_is_a_suggestion(self):
        case, _ = self.file("ปัญหาการใช้งานหรืออื่นๆ", "อยากให้มีปุ่มภาษาอังกฤษ")
        with self.assertRaises(CommandFailed):  # ปัญหา or ข้อเสนอแนะ first
            self.act(self.boss, case, "admin_answer", category="considered", message="รับไว้")
        self.act(
            self.boss,
            case,
            "admin_answer",
            category="considered",
            message="รับไว้พิจารณาในรอบหน้า",
            **{"as": "suggestion"},
        )
        case.refresh_from_db()
        self.assertEqual(
            (case.topic, case.status, case.owner_label),
            ("suggestion", "awaiting_confirmation", "แอดมิน"),
        )

    def test_a_division_says_problem_or_suggestion_when_it_resolves(self):
        case, _ = self.file("กองเครื่องวัดหน่วยไฟฟ้า")
        case.status = "in_progress"
        case.save(update_fields=["status"])
        from operations.models import Role as R
        from operations.models import RoleGrant

        RoleGrant.objects.create(
            user=self.kcw_resolver, role=R.RESOLVER, division=case.owner_division
        )
        with self.assertRaises(CommandFailed):
            self.act(self.kcw_resolver, case, "resolve", category="fixed", message="เปลี่ยนจอ")
        with self.assertRaises(CommandFailed):  # a suggestion's result for a problem
            self.act(
                self.kcw_resolver,
                case,
                "resolve",
                category="considered",
                message="x",
                **{"as": "problem"},
            )
        self.act(
            self.kcw_resolver,
            case,
            "resolve",
            category="fixed",
            message="เปลี่ยนจอแล้ว",
            **{"as": "problem"},
        )
        case.refresh_from_db()
        self.assertEqual(case.topic, "problem")


class TabTests(PickTestCase):
    def setUp(self):
        super().setUp()
        self.web = Client()
        self.web.force_login(self.boss)

    def test_the_tab_lists_waiting_cases_and_sends_many_after_confirming(self):
        first, _ = self.file("ปัญหาการใช้งานหรืออื่นๆ", "เรื่องที่หนึ่ง")
        second, _ = self.file("ปัญหาการใช้งานหรืออื่นๆ", "เรื่องที่สอง")
        page = self.web.get("/console/admin/problems/")
        self.assertContains(page, "รอแอดมิน")
        self.assertContains(page, first.number)
        self.assertContains(page, "เลือกทั้งหมดตามตัวกรอง (2 เรื่อง)")
        asked = self.web.post(
            "/console/admin/problems/send/",
            {"all_filtered": "1", "tab": "waiting", "to_division": "กคว.", "reason": "เรื่องมิเตอร์"},
        )
        self.assertContains(asked, "ส่ง 2 เรื่องให้ กคว.")
        self.assertTrue(Case.objects.filter(held_by_admin=True).count() == 2)  # not yet
        sent = self.web.post(
            "/console/admin/problems/send/",
            {
                "confirm": "1",
                "case": [first.number, second.number],
                "tab": "waiting",
                "to_division": "กคว.",
                "reason": "เรื่องมิเตอร์",
            },
            follow=True,
        )
        self.assertContains(sent, "ส่ง 2 เรื่องให้ กคว. แล้ว")
        self.assertFalse(Case.objects.filter(held_by_admin=True).exists())
        self.assertEqual(set(Case.objects.values_list("owner_division__code", flat=True)), {"กคว."})

    def test_only_admins_see_the_tab(self):
        page = self.web.get("/console/admin/problems/")
        self.assertContains(page, 'aria-current="page">ปัญหาและข้อเสนอแนะ')
        staff = Client()
        staff.force_login(self.coordinator)
        self.assertNotEqual(staff.get("/console/admin/problems/").status_code, 200)


class AutopublishTests(PickTestCase):
    def graph(self):
        path = Path(settings.BASE_DIR) / "operations" / "fixtures" / problem_pick.FIXTURE
        return line_flows.clean_graph(json.loads(path.read_text(encoding="utf-8"))["graph"])

    def test_the_fixture_walks_every_bubble_to_the_summary(self):
        self.assertEqual(problem_pick.simulate(self.graph()), [])

    def test_a_broken_flow_is_kept_as_a_draft_with_what_failed(self):
        graph = self.graph()
        graph["edges"] = [e for e in graph["edges"] if e["id"] != "e_d"]  # details lead nowhere
        problems = problem_pick.simulate(graph)
        self.assertTrue(problems)
        graph = self.graph()
        graph["nodes"][1]["buttons"][0]["set"] = "division=กอง-ไม่มี"
        self.assertIn("ไม่พบกอง", " ".join(problem_pick.simulate(graph)))

    def test_production_publishes_once_and_the_chat_follows_it(self):
        self.assertIsNone(problem_pick.autopublish())  # not production: nothing
        out = StringIO()
        with override_settings(APP_ENV="production"):
            self.assertTrue(problem_pick.autopublish(out.write))
            self.assertIsNone(problem_pick.autopublish(out.write))  # once only
        flow = LineFlow.objects.get(tile=TILE)
        published = flow.versions.get(status=LineFlowVersion.Status.PUBLISHED)
        self.assertTrue(published.note.startswith(problem_pick.MARK))
        self.assertIn("เผยแพร่แล้ว", out.getvalue())
        reply = self.say(self.postback(ALICE, f"m=item&id={self.tile(TILE).pk}"))
        self.assertEqual(carousel(reply), BUBBLES)
        self.press(ALICE, "ปัญหาการใช้งานหรืออื่นๆ")
        self.type(ALICE, "แจ้งเตือนไม่ขึ้น")
        reply = self.press(ALICE, "เปลี่ยนกอง")
        reply = self.press(ALICE, "ศูนย์ปฏิบัติการระบบอ่านเครื่องวัดอัตโนมัติ")
        self.assertIn("ส่งถึง: ศูนย์ปฏิบัติการระบบอ่านเครื่องวัดอัตโนมัติ", texts(reply))
        self.assertIn("รายละเอียด: แจ้งเตือนไม่ขึ้น", texts(reply))
        reply = self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual((case.owner_division.code, case.topic), ("ศอค.", "general"))
        self.assertIn(f"ส่งเรื่อง {case.number} ถึง", texts(reply))

    def test_seeding_puts_the_new_flow_in_the_draft(self):
        call_command("seed_line_flows", stdout=StringIO())
        draft = line_flows.draft_of(LineFlow.objects.get(tile=TILE))
        self.assertEqual(draft.graph["nodes"][1]["config"].get("style"), "carousel")
        self.assertFalse(NotificationDelivery.objects.exists())
