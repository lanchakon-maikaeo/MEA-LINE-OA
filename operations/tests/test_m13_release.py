"""M13 release candidate: turning one AI task off (RB02; 12 Oct 2569, the triage AI is not part
of the candidate) and the LINE push report the pilot uses to choose a package (C.2.3).

Acceptance → tests: a developer admin stops one AI task from the web or a command, always with
a reason in the audit; the version retires, triage still waiting is closed without calling the
model, new cases keep the rules, and switching back needs evidence for the current code
(StopTriageTests). With the answer AI stopped the bank still answers by keyword
(StopAnswerTests). The push report counts pushes per case, forecasts the month and all 18
districts, proposes a package by the C.2.3 rule, and carries no names or LINE IDs
(UsageReportTests). Load-test subjects are synthetic, reused, and refused in production; the
inside report gives turn timings by percentile (LoadTestTests). The release record never turns a
row with an external need or an unsigned gate into a pass, every test the matrix names exists,
and the record refuses to carry secrets or LINE IDs (CandidateTests).
"""

import importlib.util
import json
import re
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from operations import ai, ai_triage, line_usage, orchestration, services
from operations.contracts import Actor
from operations.models import (
    AIConfigVersion,
    AIRun,
    AITriageRun,
    AuditEvent,
    Case,
    ChannelIdentity,
    LineQuotaSnapshot,
    NotificationDelivery,
    OutboxEvent,
    Role,
)
from operations.tests.test_m02_cases import key, make_staff, problem, setup_world
from operations.tests.test_m03_line import ALICE, LineTestCase, texts
from operations.tests.test_m06_policy import FakeTriageLLM, PolicyTestCase, passing_triage_report
from operations.tests.test_m13_chat_answers import AIAnswerCase

TRIAGE = AIConfigVersion.Task.TRIAGE


@override_settings(AI_ENABLED=True)
class StopTriageTests(PolicyTestCase):
    def setUp(self):
        keys = override_settings(AI_SECRET_KEY=Fernet.generate_key().decode())
        keys.enable()
        self.addCleanup(keys.disable)
        self.llm = FakeTriageLLM()
        post = patch.object(ai, "_post", side_effect=self.llm)
        post.start()
        self.addCleanup(post.stop)
        self.dev = make_staff("dev", (Role.ADMIN, None), (Role.DEVELOPER_ADMIN, None))
        credential = ai.save_credential(
            self.dev, "opentyphoon", "https://api.opentyphoon.ai/v1", "k" * 40, "ตั้งค่า"
        )
        self.profile = ai.create_profile(self.dev, credential, "triage-model")

    def activate_triage(self):
        version = ai.create_version(self.dev, [self.profile], task=TRIAGE)
        ai.test_version(self.dev, version)
        ai.record_eval(version, passing_triage_report(version))
        ai.activate(self.dev, version, "เปิดใช้ AI คัดกรอง")
        return version

    def waiting_events(self):
        return OutboxEvent.objects.filter(event_type=orchestration.EVENT_TRIAGE_AI).exclude(
            status=OutboxEvent.Status.DONE
        )

    def test_stopping_closes_waiting_triage_and_new_cases_keep_the_rules(self):
        version = self.activate_triage()
        first = self.submit(problem(description="หม้อแปลงเสียงดังผิดปกติ"))
        self.submit(problem(description="อุปกรณ์ข้างวัดเสียงดัง"))
        self.assertEqual(self.waiting_events().count(), 2)
        waiting = AITriageRun.objects.get(case=first)
        calls = len(self.llm.calls)  # the connection test asked the model once

        stopped, closed = ai.stop(self.dev, TRIAGE, "ยังไม่อนุมัติให้ใช้ใน release นี้")
        self.assertEqual((stopped, closed), (version, 2))
        version.refresh_from_db()
        self.assertEqual(version.status, AIConfigVersion.Status.RETIRED)
        self.assertEqual(
            set(AITriageRun.objects.values_list("status", flat=True)),
            {AITriageRun.Status.CANCELLED},
        )
        self.assertFalse(self.waiting_events().exists())
        audit = AuditEvent.objects.get(action="ai.task_stopped")
        self.assertEqual(audit.reason, "ยังไม่อนุมัติให้ใช้ใน release นี้")
        self.assertEqual((audit.data["task"], audit.data["closed"]), (TRIAGE, 2))
        # A run already claimed by n8n settles without calling the model.
        self.assertEqual(ai_triage.process(waiting.pk), AITriageRun.Status.CANCELLED)
        self.assertEqual(len(self.llm.calls), calls)

        later = self.submit(problem(description="หม้อแปลงเสียงดังอีกจุด"))
        self.assertFalse(AITriageRun.objects.filter(case=later).exists())
        self.assertTrue(self.proposal(later).rules)  # the rule-based proposal still comes

    def test_switching_back_needs_evidence_for_the_current_code(self):
        version = self.activate_triage()
        ai.stop(self.dev, TRIAGE, "หยุดชั่วคราว")
        version.refresh_from_db()
        self.assertIsNone(ai.activation_blocker(version))
        report = passing_triage_report(version)
        ai.record_eval(
            version, {**report, "security": {**report["security"], "source_digest": "old"}}
        )
        self.assertIsNotNone(ai.activation_blocker(version))
        with self.assertRaises(ValidationError):
            ai.activate(self.dev, version, "เปิดกลับ")

    def test_only_a_developer_admin_with_a_reason_stops_a_task(self):
        self.activate_triage()
        with self.assertRaises(PermissionDenied):
            ai.stop(self.coordinator, TRIAGE, "หยุด")
        with self.assertRaises(ValidationError):
            ai.stop(self.dev, TRIAGE, "  ")
        ai.stop(self.dev, TRIAGE, "หยุด")
        with self.assertRaises(ValidationError):
            ai.stop(self.dev, TRIAGE, "หยุดซ้ำ")  # already off
        self.assertEqual(AuditEvent.objects.filter(action="ai.task_stopped").count(), 1)

    def test_the_admin_page_and_the_command_stop_a_task(self):
        version = self.activate_triage()
        client = Client()
        client.force_login(self.dev)
        self.assertContains(client.get(reverse("admin-ai")), "หยุดใช้ AI ของงาน")
        url = reverse("admin-ai-action", args=["stop"])
        client.post(url, {"task": TRIAGE, "reason": ""})
        version.refresh_from_db()
        self.assertEqual(version.status, AIConfigVersion.Status.ACTIVE)  # no reason, no change
        page = client.post(url, {"task": TRIAGE, "reason": "ทดสอบหยุดจากหน้าเว็บ"}, follow=True)
        self.assertContains(page, "หยุดใช้ AI แล้ว")
        self.assertNotContains(page, "หยุดใช้ AI ของงาน")  # nothing left to stop
        version.refresh_from_db()
        self.assertEqual(version.status, AIConfigVersion.Status.RETIRED)
        staff = Client()
        staff.force_login(self.coordinator)
        ai.activate(self.dev, version, "เปิดกลับเพื่อทดสอบ")
        self.assertEqual(staff.post(url, {"task": TRIAGE, "reason": "x"}).status_code, 403)
        out = StringIO()
        call_command("stop_ai", task=TRIAGE, username="dev", reason="หยุดจากคำสั่ง", stdout=out)
        self.assertIn(f"Stopped triage v{version.number}", out.getvalue())
        with self.assertRaises(CommandError):
            call_command("stop_ai", task=TRIAGE, username="dev", reason="ซ้ำ", stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command("stop_ai", task=TRIAGE, username="coord", reason="x", stdout=StringIO())


class StopAnswerTests(AIAnswerCase):
    def test_with_the_answer_ai_stopped_the_bank_still_answers_by_keyword(self):
        self.mode("write")
        self.approve(ALICE)
        self.type(ALICE, "ใช้ระบบนี้ต้องเสียค่าใช้จ่ายไหม")
        self.assertEqual(AIRun.objects.count(), 1)  # the AI was asked while it was on
        dev = make_staff("dev", (Role.ADMIN, None), (Role.DEVELOPER_ADMIN, None))
        ai.stop(dev, AIConfigVersion.Task.ANSWER, "ผู้ให้บริการ AI ล่ม")
        self.type(ALICE, "ใช้ระบบนี้ต้องเสียค่าใช้จ่ายไหม")
        self.assertEqual(AIRun.objects.count(), 1)  # not asked again
        self.assertIn("02-000-0001", texts(self.type(ALICE, "ขอเบอร์ติดต่อหน่อย")))


class UsageReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setup_world(cls)
        cls.identity = ChannelIdentity.objects.create(
            channel="line", external_id="U" + "a1" * 16, requester=cls.req1, status="active"
        )

    def case(self, requester, when):
        result = services.submit_case(
            Actor.for_requester(requester), idempotency_key=key(), payload=problem()
        )
        Case.objects.filter(pk=result.case_id).update(submitted_at=when)
        return Case.objects.get(pk=result.case_id)

    def push(self, kind, when, case=None, status=NotificationDelivery.Status.SENT, counted=1):
        return NotificationDelivery.objects.create(
            kind=kind,
            level="normal",
            identity=self.identity,
            case=case,
            text="ข้อความจำลอง",
            status=status,
            send_after=when,
            sent_at=when if status in line_usage.COUNTED else None,
            counted=counted,
        )

    def test_pushes_per_case_forecasts_and_the_package(self):
        now = timezone.make_aware(datetime(2026, 10, 15, 12, 0))
        september = timezone.make_aware(datetime(2026, 9, 10, 9, 0))
        for requester in (self.req1, self.req1, self.req2, self.req2):
            case = self.case(requester, september)
            self.push("awaiting_info", september, case)
            self.push("case_closed", september + timedelta(days=3), case)
        self.push("staff_alert", september)
        self.push("staff_alert", september)
        self.push("registration_approved", september)
        self.push("case_closed", september, status=NotificationDelivery.Status.DEFERRED)
        october = timezone.make_aware(datetime(2026, 10, 2, 9, 0))
        for _ in range(3):
            case = self.case(self.req1, october)
            self.push("awaiting_info", october, case)
            self.push("case_closed", october, case, status=NotificationDelivery.Status.UNKNOWN)
        self.push("awaiting_info", october, status=NotificationDelivery.Status.FAILED)
        LineQuotaSnapshot.objects.create(month="2026-10", quota=300, usage=6)

        report = line_usage.report(now=now, months=2)
        sep, oct_ = report["months"]
        self.assertEqual(
            (sep["month"], sep["complete"], oct_["complete"]), ("2026-09", True, False)
        )
        self.assertEqual(
            sep["pushes"],
            {
                "total": 11,
                "requester": 9,
                "staff": 2,
                "on_cases": 8,
                "by_kind": {
                    "awaiting_info": 4,
                    "case_closed": 4,
                    "registration_approved": 1,
                    "staff_alert": 2,
                },
            },
        )
        self.assertEqual(
            (sep["deferred"], sep["cases"]["submitted"], sep["cases"]["districts"]), (1, 4, 2)
        )
        self.assertEqual((sep["pushes_per_case"], sep["forecast"]), (2.0, 11))
        self.assertEqual(sep["forecast_all_districts"], 99)  # 11 × 18 / 2 districts
        self.assertEqual((oct_["pushes"]["total"], oct_["failed"]), (6, 1))  # unknown counts
        self.assertEqual(oct_["forecast"], int(6 / 14.5 * 31))  # 14.5 days of 31 gone
        self.assertEqual(oct_["forecast_all_districts"], oct_["forecast"] * 18)
        self.assertEqual(report["recommendation"]["plan"], "free")
        self.assertEqual(report["quota_snapshot"], {"month": "2026-10", "quota": 300, "usage": 6})
        text = json.dumps(report, ensure_ascii=False)
        for private in (self.identity.external_id, "ผู้แจ้ง 1", "ข้อความจำลอง"):
            self.assertNotIn(private, text)

    def test_the_c23_rule_proposes_a_package(self):
        def months(*pairs):
            return [{"forecast": f, "forecast_all_districts": a} for f, a in pairs]

        self.assertEqual(line_usage.recommend(months((250, 250), (100, 100)))["plan"], "free")
        two_hot = line_usage.recommend(months((250, 250), (260, 260)))
        self.assertEqual((two_hot["plan"], two_hot["budget"]), ("basic", 15_000))
        self.assertIn("สองเดือนติดกัน", two_hot["reasons"][0])
        self.assertEqual(line_usage.recommend(months((40, 720)))["plan"], "basic")
        self.assertEqual(line_usage.recommend(months((40, None), (40, 20_000)))["plan"], "pro")
        over = line_usage.recommend(months((40, 50_000)))
        self.assertEqual(over["plan"], "pro")
        self.assertIn("งบรายเดือน", over["reasons"][-1])

    def test_the_command_writes_the_report(self):
        out = StringIO()
        path = f"{self.id()}.json"
        with patch("pathlib.Path.write_text") as write, patch("pathlib.Path.mkdir"):
            call_command("line_usage_report", months=1, out=path, stdout=out)
        self.assertEqual(json.loads(out.getvalue())["recommendation"]["plan"], "free")
        self.assertEqual(json.loads(write.call_args.args[0])["months"][0]["cases"]["submitted"], 0)


class LoadTestTests(LineTestCase):
    def test_subjects_are_synthetic_reused_and_refused_in_production(self):
        with patch.dict("os.environ", {"LOAD_PASSWORD": "p" * 20}):
            first, again = StringIO(), StringIO()
            call_command("prepare_load_test", requesters=3, stdout=first)
            call_command("prepare_load_test", requesters=3, stdout=again)
            with override_settings(APP_ENV="production"), self.assertRaises(CommandError):
                call_command("prepare_load_test", stdout=StringIO())
        subjects = json.loads(first.getvalue())
        self.assertEqual(subjects, json.loads(again.getvalue()))
        self.assertNotIn("p" * 20, first.getvalue())
        self.assertEqual(len(subjects["registered"]), 3)
        self.assertTrue(all(re.fullmatch(r"U[0-9a-f]{32}", i) for i in subjects["registered"]))
        self.assertEqual(
            ChannelIdentity.objects.filter(
                external_id__in=subjects["registered"], status="active", requester__isnull=False
            ).count(),
            3,
        )
        self.assertTrue(
            get_user_model().objects.get(username="load-staff").check_password("p" * 20)
        )
        too_short = "x" * 5
        with (
            patch.dict("os.environ", {"LOAD_PASSWORD": too_short}),
            self.assertRaises(CommandError),
        ):
            call_command("prepare_load_test", stdout=StringIO())

    def test_the_inside_report_times_each_turn(self):
        since = timezone.now() - timedelta(seconds=1)
        self.type(ALICE, "สวัสดีครับ")
        self.type(ALICE, "สมชาย ทดสอบ")
        out = StringIO()
        call_command("load_report", since=since.isoformat(), stdout=out)
        report = json.loads(out.getvalue())
        self.assertEqual((report["stored"], report["by_status"]), (2, {"done": 2}))
        self.assertEqual(set(report["turn_seconds"]), {"p50", "p95", "p99", "max"})
        self.assertIn("oldest_pending_seconds", report["outbox"])
        with self.assertRaises(CommandError):
            call_command("load_report", since="yesterday", stdout=StringIO())


def release_candidate():
    path = Path(settings.BASE_DIR) / "deploy/release_candidate.py"
    spec = importlib.util.spec_from_file_location("release_candidate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CandidateTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.rc = release_candidate()
        cls.matrix = json.loads(cls.rc.MATRIX.read_text(encoding="utf-8"))

    def test_external_rows_and_unsigned_gates_never_pass(self):
        rc = self.rc
        everything = {test: "pass" for test in rc.named_tests(self.matrix)}
        evidence = {key: {"status": "pass"} for key in self.matrix["evidence"]}
        result = rc.evaluate(self.matrix, everything, evidence, signoff={})
        self.assertEqual(result["verdict"], "not-ready")
        external = [row for row in result["m13"] if row.get("external")]
        self.assertTrue(external)
        self.assertEqual({row["status"] for row in external}, {"external"})
        # Even with every external need dropped, a gate waits for its signer.
        local = {
            **self.matrix,
            "m13": [r for r in self.matrix["m13"] if not r.get("external")],
            "gates": [
                {**g, "rows": [r for r in g["rows"] if not r.get("external")]}
                for g in self.matrix["gates"]
            ],
        }
        unsigned = rc.evaluate(local, everything, evidence, signoff={})
        self.assertEqual({g["status"] for g in unsigned["gates"]}, {"signoff"})
        signed = {g["id"]: {"by": "ผู้รับรอง", "on": "2569-10-12"} for g in local["gates"]}
        self.assertEqual(rc.evaluate(local, everything, evidence, signed)["verdict"], "ready")
        # One failed or missing piece of local evidence decides the row and its gate.
        failed = rc.evaluate(local, everything, {**evidence, "ci": {"status": "fail"}}, signed)
        self.assertEqual(next(g for g in failed["gates"] if g["id"] == "G01")["status"], "fail")
        gone = rc.evaluate(local, {}, evidence, signed)
        self.assertEqual(gone["verdict"], "not-ready")
        self.assertIn("missing", {r["status"] for r in gone["m13"]})

    def test_every_named_test_exists(self):
        from django.test.utils import get_runner

        runner = get_runner(settings)(verbosity=0)
        suite = runner.build_suite(["operations.tests"])
        known = {test.id().removeprefix("operations.tests.") for test in suite}
        self.assertEqual(sorted(set(self.rc.named_tests(self.matrix)) - known), [])

    def test_test_log_outcomes_and_secret_guard(self):
        log = (
            "test_a (operations.tests.test_m01_identity.T.test_a) ... ok\n"
            "test_b (operations.tests.test_m01_identity.T.test_b)\n"
            "Docstring of b. ... FAIL\n"
            "test_c (operations.tests.test_m01_identity.T.test_c) ... skipped 'no db'\n"
        )
        self.assertEqual(
            self.rc.test_results(log),
            {
                "test_m01_identity.T.test_a": "pass",
                "test_m01_identity.T.test_b": "fail",
                "test_m01_identity.T.test_c": "missing",
            },
        )
        value = "-".join(["made", "up", "value", "123"])
        text = "commit abc user U" + "0f" * 16 + " key " + value
        self.assertEqual(
            self.rc.leaks(text, {"LINE_CHANNEL_SECRET": value, "EMPTY": ""}),
            ["LINE user ID", "LINE_CHANNEL_SECRET"],
        )
