"""Monitoring (15 ต.ค. 2569 Q40–Q47): daily numbers of the AI tasks, the chat answers and the
system; Evidently's drift check on numbers from masked text; alerts; who may look and change.
"""

import hashlib
import uuid
from datetime import datetime, time, timedelta

from django.core.exceptions import PermissionDenied
from django.test import Client, TestCase
from django.utils import timezone

from operations import admin_nav, monitoring, options
from operations.models import (
    AIConfigVersion,
    AIRun,
    ChannelIdentity,
    ConversationMessage,
    MonitoringDay,
    Role,
    UnansweredQuestion,
    WebhookInbox,
)
from operations.tests.test_m02_cases import make_staff

SECRET_TEXT = "ขอเบิกมิเตอร์ด่วน คุณสมชาย โทร 0812345678 (ข้อมูลจำลอง)"


class MonitoringTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = make_staff("monadmin", (Role.ADMIN, None))
        cls.dev = make_staff("mondev", (Role.ADMIN, None), (Role.DEVELOPER_ADMIN, None))
        cls.staff = make_staff("monstaff")
        cls.line = ChannelIdentity.objects.create(channel="line", external_id="U" + "9" * 32)
        cls.version = AIConfigVersion.objects.create(number=901, created_by=cls.dev)

    def day_at(self, day, hour=10):
        return timezone.make_aware(datetime.combine(day, time(hour)))

    def inbox(self, at):
        event = uuid.uuid4().hex
        return WebhookInbox.objects.create(
            channel="line",
            webhook_event_id=event,
            event_type="message",
            source_type="user",
            source_user_id=self.line.external_id,
            payload={},
            payload_hash=hashlib.sha256(event.encode()).hexdigest(),
            occurred_at=at,
            received_at=at,
            processed_at=at + timedelta(seconds=2),
            status="done",
        )

    def ai_run(self, at, attempts, answer=None):
        row = AIRun.objects.create(
            identity=self.line,
            inbox=self.inbox(at),
            config_version=self.version,
            prompt_version="p",
            schema_version="s",
            attempts=attempts,
            result={"answer": answer} if answer else {},
        )
        AIRun.objects.filter(pk=row.pk).update(created_at=at)
        return row

    def message(self, at, text):
        row = ConversationMessage.objects.create(
            identity=self.line, inbox=self.inbox(at), text=text
        )
        ConversationMessage.objects.filter(pk=row.pk).update(created_at=at)
        return row


class NumbersTests(MonitoringTestCase):
    def test_a_day_counts_calls_answers_and_keeps_no_text(self):
        day = timezone.localdate() - timedelta(days=1)
        at = self.day_at(day)
        self.ai_run(
            at,
            [{"model": "a", "outcome": "ok", "latency_ms": 900}],
            {"key": "hours", "by": "model"},
        )
        self.ai_run(
            at,
            [
                {"model": "a", "outcome": "ReadTimeout"},
                {"model": "b", "outcome": "ok", "latency_ms": 2000},
            ],
        )
        self.ai_run(at, [{"model": "a", "outcome": "HTTPError"}], {"key": "none", "refused": "x"})
        UnansweredQuestion.objects.create(key="k" * 64, text="ถามอะไร", first_at=at, last_at=at)
        self.message(at, SECRET_TEXT)
        row = monitoring.compute(day)
        chat = row.metrics["ai"]["chat"]
        self.assertEqual((chat["calls"], chat["ok"], chat["fallback"], chat["error"]), (3, 2, 1, 1))
        self.assertEqual(chat["p95_ms"], 2000)
        rag = row.metrics["rag"]
        self.assertEqual(
            (rag["answered"], rag["written"], rag["refused"], rag["unanswered"]), (1, 1, 1, 1)
        )
        self.assertEqual(rag["keys"], {"hours": 1})
        self.assertEqual(row.metrics["inputs"]["messages"], 1)
        stored = str(row.metrics)
        self.assertNotIn("สมชาย", stored)
        self.assertNotIn("0812345678", stored)
        self.assertEqual(row.metrics["drift"]["status"], "few")

    def test_an_error_rate_over_the_threshold_is_an_alert_and_a_badge(self):
        day = timezone.localdate()
        for _ in range(4):
            self.ai_run(self.day_at(day, 8), [{"model": "a", "outcome": "HTTPError"}])
        self.ai_run(self.day_at(day, 8), [{"model": "a", "outcome": "ok", "latency_ms": 500}])
        row = monitoring.compute(day)
        self.assertTrue(any("ผิดพลาด 80%" in a for a in row.alerts))
        self.assertEqual(admin_nav.badges()["monitoring"], len(row.alerts))

    def test_evidently_compares_the_last_week_with_the_weeks_before(self):
        today = timezone.localdate()
        for back in range(1, 35):
            for n in range(4):  # the current week needs at least 20 messages
                text = "สั้น (จำลอง)" if back > 7 else "ข้อความยาวมาก " * 8 + f"เลข 1234567{n} (จำลอง)"
                self.message(self.day_at(today - timedelta(days=back), 9 + n), text)
        drift = monitoring.drift(today)
        self.assertEqual(drift["status"], "ok")
        self.assertTrue(drift["columns"]["length"]["drifted"])
        self.assertIn("p_value", drift["columns"]["hour"])
        html = monitoring.full_report_html(today)
        self.assertIn("<html", html.lower())

    def test_the_worker_computes_yesterday_once_after_the_hour(self):
        monitoring._last_check = None
        early = timezone.make_aware(datetime.combine(timezone.localdate(), time(1, 0)))
        self.assertIsNone(monitoring.maybe_run(early))
        monitoring._last_check = None
        late = early + timedelta(hours=2)
        row = monitoring.maybe_run(late)
        self.assertEqual(row.day, timezone.localdate() - timedelta(days=1))
        self.assertIsNone(monitoring.maybe_run(late + timedelta(seconds=30)))  # once a minute
        monitoring._last_check = None
        self.assertIsNone(monitoring.maybe_run(late + timedelta(minutes=5)))  # already done
        self.assertEqual(MonitoringDay.objects.count(), 1)


class PageTests(MonitoringTestCase):
    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client

    def test_admins_look_and_only_the_ai_right_computes(self):
        self.assertEqual(
            self.client_for(self.staff).get("/console/admin/monitoring/").status_code, 403
        )
        page = self.client_for(self.admin).get("/console/admin/monitoring/")
        self.assertContains(page, "Monitoring")
        self.assertNotContains(page, "คำนวณตอนนี้")
        self.assertEqual(
            self.client_for(self.admin).post("/console/admin/monitoring/").status_code, 403
        )
        with self.assertRaises(PermissionDenied):
            options.change(self.admin, options.MONITOR_ERROR, "0.05", "เข้มขึ้น")
        page = self.client_for(self.dev).post("/console/admin/monitoring/", follow=True)
        self.assertContains(page, "คำนวณตัวเลขล่าสุดแล้ว")
        self.assertTrue(MonitoringDay.objects.filter(day=timezone.localdate()).exists())
        options.change(self.dev, options.MONITOR_ERROR, "0.05", "เข้มขึ้น")

    def test_the_full_report_is_sandboxed_and_needs_enough_messages(self):
        page = self.client_for(self.admin).get("/console/admin/monitoring/report/", follow=True)
        self.assertContains(page, "ข้อความยังน้อยเกินไป")
        today = timezone.localdate()
        for back in range(1, 35):
            for hour in (8, 10, 13, 15):
                self.message(
                    self.day_at(today - timedelta(days=back), hour), f"ข้อความ {back} (จำลอง)"
                )
        response = self.client_for(self.admin).get("/console/admin/monitoring/report/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Security-Policy"].startswith("sandbox allow-scripts"))
