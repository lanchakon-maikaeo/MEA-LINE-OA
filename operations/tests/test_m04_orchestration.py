import json
import secrets
import threading
import uuid
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone

from operations import identity, line_api, notifications, options, orchestration, services
from operations.contracts import Actor
from operations.management.commands.run_worker import run_once
from operations.models import (
    AuditEvent,
    Case,
    ChannelIdentity,
    LineQuotaSnapshot,
    NotificationDelivery,
    OutboxEvent,
    RegistrationRequest,
    Requester,
    Role,
)
from operations.tests.test_m02_cases import make_staff, withdrawal
from operations.tests.test_m03_line import ALICE, BOB, LineTestCase, texts

N = NotificationDelivery
WORKFLOWS = Path(settings.BASE_DIR) / "workflows" / "n8n"


class OrchestrationTestCase(LineTestCase):
    def setUp(self):
        super().setUp()
        self.token = secrets.token_urlsafe(48)
        overrides = override_settings(ORCHESTRATOR_TOKEN=self.token, LINE_PUSH_ENABLED=True)
        overrides.enable()
        self.addCleanup(overrides.disable)
        self.pushes = []
        self.push_errors = []
        push = patch.object(line_api, "push", side_effect=self.fake_push)
        push.start()
        self.addCleanup(push.stop)
        self.alice = self.approve(ALICE)

    def fake_push(self, to, messages, retry_key):
        self.pushes.append((to, [m["text"] for m in messages], str(retry_key)))
        if self.push_errors:
            raise self.push_errors.pop(0)

    def api(self, path, body=None, token=None):
        response = Client().post(
            f"/internal/v1/{path}",
            data=json.dumps(body or {}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token if token is None else token}",
        )
        return response.status_code, response.json()

    def case(self):
        result = services.submit_case(
            Actor.for_requester(self.alice),
            idempotency_key=f"k-{uuid.uuid4().hex}",
            payload=withdrawal(),
        )
        return Case.objects.get(pk=result.case_id)

    def act(self, case, action, user=None, **payload):
        case.refresh_from_db()
        services.perform_action(
            Actor.staff(user or self.intake),
            case.pk,
            action,
            expected_revision=case.revision,
            idempotency_key=f"k-{uuid.uuid4().hex}",
            payload=payload or None,
        )

    def due_now(self):
        OutboxEvent.objects.filter(status="pending").update(next_attempt_at=timezone.now())

    def n8n_round(self):
        """What the case-notification workflow does: claim, then send each event."""
        status, body = self.api(
            "outbox/claim", {"event_types": ["notification.requested"], "limit": 10}
        )
        self.assertEqual(status, 200)
        return [
            self.api(
                "notifications/send", {"event_id": e["event_id"], "lease_token": e["lease_token"]}
            )[1]
            for e in body["events"]
        ]

    def sent_months(self, count, level="normal"):
        for _ in range(count):
            N.objects.create(
                kind="x",
                level=level,
                identity=None,
                text="x",
                status=N.Status.SENT,
                counted=1,
                sent_at=timezone.now(),
                send_after=timezone.now(),
            )


# --- Claim / lease ------------------------------------------------------------------------------


class LeaseTests(OrchestrationTestCase):
    def event(self, event_type=orchestration.EVENT_NOTIFICATION, **fields):
        return OutboxEvent.objects.create(
            event_type=event_type,
            subject_type="test",
            subject_id="1",
            correlation_id=uuid.uuid4(),
            occurred_at=timezone.now(),
            **fields,
        )

    def test_claim_leases_due_events_once_and_reclaims_expired_leases(self):
        due = self.event()
        later = self.event(next_attempt_at=timezone.now() + timedelta(minutes=5))
        self.event(orchestration.EVENT_CONVERSATION)
        first = orchestration.claim(orchestration.N8N_TYPES, limit=10)
        self.assertEqual([e.pk for e in first], [due.pk])
        self.assertEqual(orchestration.claim(orchestration.N8N_TYPES, limit=10), [])
        OutboxEvent.objects.filter(pk=due.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )
        again = orchestration.claim(orchestration.N8N_TYPES)
        self.assertEqual((again[0].event_id, again[0].attempts), (due.event_id, 2))
        self.assertNotEqual(again[0].lease_token, first[0].lease_token)
        later.refresh_from_db()
        self.assertEqual(later.status, "pending")

    def test_stale_or_wrong_tokens_change_nothing(self):
        self.event()
        (leased,) = orchestration.claim(orchestration.N8N_TYPES)
        for token in ("", "wrong", None):
            with self.subTest(token=token), self.assertRaises(orchestration.LeaseError):
                orchestration.complete(leased.event_id, token)
        orchestration.complete(leased.event_id, leased.lease_token)
        with self.assertRaises(orchestration.LeaseError):
            orchestration.complete(leased.event_id, leased.lease_token)  # second completion
        expired = self.event()
        (leased,) = orchestration.claim(orchestration.N8N_TYPES)
        OutboxEvent.objects.filter(pk=expired.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )
        with self.assertRaises(orchestration.LeaseError):
            orchestration.fail(leased.event_id, leased.lease_token, "late")

    def test_failures_back_off_then_dead_letter_and_admins_requeue(self):
        event = self.event(orchestration.EVENT_AI)  # two attempts allowed
        (leased,) = orchestration.claim(orchestration.N8N_TYPES)
        settled = orchestration.fail(
            leased.event_id, leased.lease_token, "timeout", retry_after=120
        )
        self.assertEqual(settled.status, "pending")
        self.assertGreaterEqual(settled.next_attempt_at - timezone.now(), timedelta(seconds=110))
        OutboxEvent.objects.filter(pk=event.pk).update(next_attempt_at=timezone.now())
        (leased,) = orchestration.claim(orchestration.N8N_TYPES)
        settled = orchestration.fail(leased.event_id, leased.lease_token, "timeout")
        self.assertEqual((settled.status, settled.attempts), ("failed", 2))
        self.assertTrue(orchestration.requeue(settled))
        settled.refresh_from_db()
        self.assertEqual((settled.status, settled.attempts), ("pending", 0))

    def test_renewal_is_bounded(self):
        self.event()
        (leased,) = orchestration.claim(orchestration.N8N_TYPES, lease_seconds=60)
        orchestration.renew(leased.event_id, leased.lease_token, 300)
        OutboxEvent.objects.filter(pk=leased.pk).update(
            leased_at=timezone.now() - timedelta(minutes=9)
        )
        with self.assertRaises(orchestration.LeaseError):
            orchestration.renew(leased.event_id, leased.lease_token, 300)


class WorkerTests(OrchestrationTestCase):
    def test_a_lease_lost_while_handling_does_not_stop_the_worker(self):
        # The Mac slept mid test run (11 Oct 2569): the wall clock jumped past the lease.
        from operations.management.commands import run_worker

        real = run_worker.handle_event

        def paused(event):
            real(event)
            OutboxEvent.objects.filter(pk=event.pk).update(
                lease_expires_at=timezone.now() - timedelta(minutes=8)
            )

        before = len(self.sent)
        self.post(self.text(ALICE, "สวัสดีครับ"))
        with patch.object(run_worker, "handle_event", side_effect=paused):
            with self.assertLogs("operations.management.commands.run_worker", "WARNING"):
                run_once()
        self.assertEqual(len(self.sent), before + 1)  # answered once
        run_once()  # claimed again: the handled message is not answered twice
        self.assertEqual(len(self.sent), before + 1)
        self.assertFalse(OutboxEvent.objects.filter(status="leased").exists())


class ParallelClaimTests(TransactionTestCase):
    def test_parallel_claims_never_hand_out_the_same_event(self):
        for _ in range(20):
            OutboxEvent.objects.create(
                event_type=orchestration.EVENT_NOTIFICATION,
                subject_type="t",
                subject_id="1",
                correlation_id=uuid.uuid4(),
                occurred_at=timezone.now(),
            )
        barrier = threading.Barrier(4)
        claimed = []

        def worker():
            try:
                barrier.wait()
                claimed.extend(e.pk for e in orchestration.claim(orchestration.N8N_TYPES, limit=10))
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(claimed), 20)
        self.assertEqual(len(set(claimed)), 20)


# --- Internal API ------------------------------------------------------------------------


class InternalApiTests(OrchestrationTestCase):
    def test_service_token_is_required_and_scoped(self):
        for token in ("", "wrong", self.token[:-1]):
            with self.subTest(token=token):
                self.assertEqual(self.api("outbox/claim", token=token)[0], 401)
        with override_settings(ORCHESTRATOR_TOKEN="short"):
            self.assertEqual(self.api("outbox/claim", token="short")[0], 401)
        self.assertEqual(Client().get("/internal/v1/outbox/claim").status_code, 405)
        # Django-owned work (chat turns) is never handed to n8n, even when asked for.
        self.post(self.text(ALICE, "สวัสดี"))
        status, body = self.api("outbox/claim", {"event_types": ["conversation.message_received"]})
        self.assertEqual((status, body["events"]), (200, []))
        # The token reaches no staff or admin page.
        response = Client().get("/console/admin/line/", HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(response.status_code, 302)

    def test_bad_input_and_stale_leases(self):
        self.assertEqual(
            self.api("outbox/complete", {"event_id": "nope", "lease_token": "x"})[0], 400
        )
        response = Client().post(
            "/internal/v1/outbox/claim",
            data=b"[1]",
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )
        self.assertEqual(response.status_code, 400)
        status, body = self.api(
            "outbox/complete", {"event_id": str(uuid.uuid4()), "lease_token": "x"}
        )
        self.assertEqual((status, body["error"]), (409, "LEASE_INVALID"))

    def test_request_info_reaches_the_requester_through_worker_and_n8n_calls(self):
        case = self.case()
        self.act(case, "start_triage")
        self.act(case, "request_info", reason="ขอรูปมิเตอร์ที่ชำรุด")
        run_once()  # worker plans the notification from decision.recorded
        delivery = N.objects.get()
        self.assertEqual(
            (delivery.kind, delivery.level, delivery.identity.external_id),
            ("request_info", "critical", ALICE),
        )
        results = self.n8n_round()
        self.assertEqual(results, [{"outcome": "sent", "event": "done"}])
        (to, messages, key) = self.pushes[0]
        self.assertEqual(to, ALICE)
        self.assertIn("ขอรูปมิเตอร์ที่ชำรุด", messages[0])
        delivery.refresh_from_db()
        self.assertEqual(
            (delivery.status, delivery.counted, str(delivery.retry_key)), ("sent", 1, key)
        )
        self.assertEqual(self.n8n_round(), [])  # nothing left, nothing sent twice


# --- Planning --------------------------------------------------------------------------------


class PlanningTests(OrchestrationTestCase):
    def test_only_the_agreed_events_are_notified(self):
        case = self.case()
        self.act(case, "start_triage")
        self.act(case, "claim", user=make_staff("intake3", (Role.STAFF, "SIM-TRIAGE")))
        self.act(case, "hold", reason="รอของ")
        self.act(case, "resume")
        self.act(case, "decline", reason="ซ้ำ")
        own = self.case()
        services.perform_action(
            Actor.for_requester(self.alice),
            own.pk,
            "cancel",
            expected_revision=1,
            idempotency_key=f"k-{uuid.uuid4().hex}",
        )
        staff_cancel = self.case()
        self.act(staff_cancel, "cancel", reason="เขตแจ้งทางโทรศัพท์")
        run_once()
        kinds = sorted(N.objects.values_list("kind", "level"))
        self.assertEqual(kinds, [("case_cancelled", "normal"), ("case_declined", "normal")])
        normal = N.objects.get(kind="case_declined")
        self.assertGreaterEqual(normal.send_after - timezone.now(), timedelta(minutes=9))

    def test_registration_results_go_to_that_identity(self):
        for user, decide in ((BOB, "approve"), ("U" + "e" * 32, "reject")):
            identity.submit_registration("line", user, "ผู้ขอ", "D02")
            registration = RegistrationRequest.objects.get(identity__external_id=user)
            if decide == "approve":
                identity.approve_registration(self.admin, registration)
            else:
                identity.reject_registration(self.admin, registration, "ไม่ใช่พนักงาน")
        identity.submit_registration("line", "U" + "f" * 32, "ผู้ขอ", "D02")
        identity.cancel_registration("line", "U" + "f" * 32)
        run_once()
        texts_by_user = dict(N.objects.values_list("identity__external_id", "text"))
        self.assertIn("ลงทะเบียนเรียบร้อย", texts_by_user[BOB])
        self.assertIn("ไม่ใช่พนักงาน", texts_by_user["U" + "e" * 32])
        self.assertNotIn("U" + "f" * 32, texts_by_user)

    def test_requesters_without_line_and_replanned_events_add_nothing(self):
        direct = identity.create_requester(self.admin, "ไม่มีไลน์", "D03", "โทรมา")
        result = services.submit_case(
            Actor.staff(self.intake),
            idempotency_key=f"k-{uuid.uuid4().hex}",
            payload=withdrawal(requester_id=direct.pk, channel="phone", intake_note="โทร"),
        )
        case = Case.objects.get(pk=result.case_id)
        self.act(case, "start_triage")
        self.act(case, "request_info", reason="ขอเลข")
        run_once()
        self.assertFalse(N.objects.exists())
        mine = self.case()
        self.act(mine, "start_triage")
        self.act(mine, "request_info", reason="ขอเลข")
        run_once()
        event = OutboxEvent.objects.filter(event_type="decision.recorded").latest("id")
        notifications.plan_for_event(event)  # a crashed worker plans the same event again
        self.assertEqual(N.objects.count(), 1)


# --- Sending and the message budget -----------------------------------------------------------


class SendingTests(OrchestrationTestCase):
    def planned(self, level="normal", identity_row=None, text="ข้อความ"):
        identity_row = identity_row or ChannelIdentity.objects.get(external_id=ALICE)
        return N.objects.create(
            kind="test", level=level, identity=identity_row, text=text, send_after=timezone.now()
        )

    def test_waiting_notifications_for_one_person_go_in_one_push(self):
        first = self.planned(text="หนึ่ง")
        self.planned(text="สอง")
        third = self.planned(level="critical", text="สาม")
        self.assertEqual(notifications.send(third.pk).outcome, "sent")
        self.assertEqual(self.pushes[0][1], ["สาม", "หนึ่ง", "สอง"])
        self.assertEqual(notifications.send(first.pk).outcome, "already")
        self.assertEqual(set(N.objects.values_list("status", flat=True)), {"sent"})
        self.assertEqual(notifications.budget_state().used, 1)  # one request to one person

    def test_budget_tiers_keep_a_reserve_for_critical_and_stop_at_the_limit(self):
        self.assertEqual(notifications.budget_state().budget, 10)  # non-production cap
        self.sent_months(8)  # 80%: tier 2
        self.assertEqual(notifications.send(self.planned().pk).outcome, "deferred")
        self.assertEqual(notifications.send(self.planned("critical").pk).outcome, "sent")
        self.sent_months(1)  # 10/10: tier 3
        self.assertEqual(notifications.send(self.planned("critical").pk).outcome, "deferred")
        self.assertEqual(len(self.pushes), 1)
        with override_settings(APP_ENV="production"):
            self.assertEqual(notifications.budget_state().budget, 300)
            LineQuotaSnapshot.objects.create(
                month=notifications.month_key(timezone.now()), quota=200
            )
            self.assertEqual(notifications.budget_state().budget, 200)

    def test_recipient_is_rechecked_at_send_time(self):
        cases = [
            ({"LINE_PUSH_ENABLED": False}, None, "push disabled"),
            ({"LINE_ALLOWED_RECIPIENTS": frozenset()}, None, "not in allowlist"),
            ({}, {"is_following": False}, "unfollowed or blocked"),
        ]
        for overrides, identity_change, detail in cases:
            with self.subTest(detail), override_settings(**overrides):
                ChannelIdentity.objects.filter(external_id=ALICE).update(is_following=True)
                if identity_change:
                    ChannelIdentity.objects.filter(external_id=ALICE).update(**identity_change)
                delivery = self.planned()
                self.assertEqual(notifications.send(delivery.pk).outcome, "skipped")
                delivery.refresh_from_db()
                self.assertEqual(delivery.detail, detail)
        ChannelIdentity.objects.filter(external_id=ALICE).update(is_following=True)
        case = self.case()
        delivery = N.objects.create(
            kind="t",
            level="critical",
            identity=ChannelIdentity.objects.get(external_id=ALICE),
            case=case,
            text="x",
            send_after=timezone.now(),
        )
        Requester.objects.filter(pk=self.alice.pk).update(status="suspended")
        self.assertEqual(notifications.send(delivery.pk).outcome, "skipped")
        self.assertEqual(self.pushes, [])
        with override_settings(APP_ENV="production", LINE_ALLOWED_RECIPIENTS=frozenset()):
            Requester.objects.filter(pk=self.alice.pk).update(status="active")
            self.assertEqual(notifications.send(self.planned().pk).outcome, "sent")
        # ตัวเลือกระบบ line_audience (14 Oct 2569): a test host may open the bot to everyone.
        options.change(None, options.LINE_AUDIENCE, "everyone", "เปิดให้ทุกคนลงทะเบียน")
        with override_settings(LINE_ALLOWED_RECIPIENTS=frozenset()):
            self.assertEqual(notifications.send(self.planned().pk).outcome, "sent")

    def test_line_outcomes_are_recorded_as_they_are(self):
        unknown = self.planned("critical")
        self.push_errors = [line_api.LineApiError(500, "oops", retry_after=30)]
        result = notifications.send(unknown.pk)
        self.assertEqual((result.outcome, result.final, result.retry_after), ("retry", False, 30))
        unknown.refresh_from_db()
        self.assertEqual((unknown.status, unknown.counted), ("unknown", 1))  # counted while unknown
        self.push_errors = [line_api.LineApiError(409, "accepted")]
        self.assertEqual(notifications.send(unknown.pk).outcome, "sent")
        self.assertEqual(self.pushes[0][2], self.pushes[1][2])  # same X-Line-Retry-Key

        self.push_errors = [TimeoutError()]
        timeout = self.planned("critical")
        self.assertEqual(notifications.send(timeout.pk).outcome, "retry")
        timeout.refresh_from_db()
        self.assertEqual(timeout.status, "unknown")

        self.push_errors = [line_api.LineApiError(400, "bad request")]
        bad = self.planned("critical")
        self.assertEqual(notifications.send(bad.pk).outcome, "failed")
        bad.refresh_from_db()
        self.assertEqual((bad.status, bad.counted), ("failed", 0))

        self.push_errors = [
            line_api.LineApiError(429, '{"message":"You have reached your monthly limit."}')
        ]
        limited = self.planned("critical")
        self.assertEqual(notifications.send(limited.pk).outcome, "deferred")
        self.assertEqual(notifications.budget_state().tier, 3)
        # LINE's own limit stops even critical notifications while our count is still low.
        pushes = len(self.pushes)
        self.assertEqual(notifications.send(self.planned("critical").pk).outcome, "deferred")
        self.assertEqual(len(self.pushes), pushes)

    def test_send_endpoint_retries_unknown_pushes_through_the_outbox(self):
        case = self.case()
        self.act(case, "start_triage")
        self.act(case, "request_info", reason="ขอรูป")
        run_once()
        self.push_errors = [line_api.LineApiError(503, "busy", retry_after=60)]
        self.assertEqual(self.n8n_round(), [{"outcome": "retry", "event": "pending"}])
        self.due_now()
        self.assertEqual(self.n8n_round(), [{"outcome": "sent", "event": "done"}])
        self.assertEqual(len({key for _, _, key in self.pushes}), 1)


# --- Maintenance, admin view and chat updates ----------------------------------------------


class MaintenanceTests(OrchestrationTestCase):
    def test_reconcile_counts_lines_own_usage_and_alerts_once_per_level(self):
        answers = {
            "/v2/bot/message/quota": {"type": "limited", "value": 300},
            "/v2/bot/message/quota/consumption": {"totalUsage": 9},
        }
        with patch.object(line_api, "call", side_effect=lambda path, *a, **k: answers[path]):
            status, report = self.api("maintenance")
            self.assertEqual(status, 200)
            self.assertTrue(report["reconciled"])
            self.assertEqual(report["budget"]["used"], 9)  # 9 of the non-production cap of 10
            self.assertEqual(report["alerts"], ["50", "80", "forecast"])
            status, report = self.api("maintenance")
        self.assertEqual(report["alerts"], [])
        self.assertEqual(AuditEvent.objects.filter(action="line.budget_alert").count(), 1)

    def test_admin_sees_budget_held_deliveries_and_requeues_dead_letters(self):
        dead = OutboxEvent.objects.create(
            event_type="notification.requested",
            subject_type="t",
            subject_id="1",
            correlation_id=uuid.uuid4(),
            occurred_at=timezone.now(),
            status="failed",
            attempts=8,
            last_error="LINE 500",
        )
        admin = Client()
        admin.force_login(self.admin)
        page = admin.get("/console/admin/line/")
        self.assertContains(page, "LINE 500")
        self.assertContains(page, "ใช้แล้ว")
        admin.post(f"/console/admin/line/requeue/{dead.pk}/", {"reason": ""})
        dead.refresh_from_db()
        self.assertEqual(dead.status, "pending")
        self.assertTrue(AuditEvent.objects.filter(action="outbox.requeued").exists())
        staff = Client()
        staff.force_login(self.meter)
        self.assertEqual(staff.get("/console/admin/line/").status_code, 403)

    def test_changes_not_pushed_show_once_as_new_updates(self):
        self.say(self.postback(ALICE, "m=mine"))  # first visit sets the baseline
        case = self.case()
        self.act(case, "start_triage")
        self.act(case, "request_info", reason="ขอรูปหน้าปัด")
        reply = self.say(self.postback(ALICE, "m=mine"))
        everything = "\n".join(m.get("text", "") for m in self.sent[-1])
        self.assertIn(f"มีอัปเดตใหม่\n{case.number} · รอข้อมูลเพิ่ม", everything)
        self.assertIn("ขอรูปหน้าปัด", everything)
        self.say(self.postback(ALICE, "m=mine"))
        self.assertNotIn("มีอัปเดตใหม่", "\n".join(m.get("text", "") for m in self.sent[-1]))
        self.assertTrue(texts(reply))


# --- n8n workflow files --------------------------------------------------------------------------


class WorkflowFileTests(OrchestrationTestCase):
    def test_workflows_call_only_internal_django_apis_without_secrets(self):
        files = sorted(WORKFLOWS.glob("*.json"))
        self.assertEqual(
            [f.name for f in files],
            [
                "case-notification.json",
                "case-triage-ai.json",
                "conversation-ai.json",
                "reminder-recovery.json",
            ],
        )
        ids = set()
        for path in files:
            raw = path.read_text()
            workflow = json.loads(raw)
            with self.subTest(path.name):
                self.assertRegex(workflow["id"], r"^[A-Za-z0-9]{16}$")
                ids.add(workflow["id"])
                self.assertNotIn("Bearer", raw)
                self.assertNotRegex(raw, r"https?://(?!web:8000/internal/v1/)[^\s\"]+")
                for node in workflow["nodes"]:
                    if node["type"] == "n8n-nodes-base.httpRequest":
                        self.assertTrue(
                            node["parameters"]["url"].startswith("http://web:8000/internal/v1/")
                        )
                        self.assertEqual(
                            node["credentials"]["httpHeaderAuth"]["id"], "faojDjangoToken1"
                        )
        self.assertEqual(len(ids), 4)
        notify = json.loads((WORKFLOWS / "case-notification.json").read_text())
        claim = next(
            n for n in notify["nodes"] if n["parameters"].get("url", "").endswith("/outbox/claim")
        )
        self.assertIn("notification.requested", claim["parameters"]["jsonBody"])
        ai = json.loads((WORKFLOWS / "conversation-ai.json").read_text())
        urls = [n["parameters"].get("url", "") for n in ai["nodes"]]
        self.assertTrue(ai["active"])  # M05 turned the AI step on
        self.assertIn("http://web:8000/internal/v1/ai/extract", urls)
        triage = json.loads((WORKFLOWS / "case-triage-ai.json").read_text())
        nodes = {n["parameters"].get("url", ""): n for n in triage["nodes"]}
        self.assertIn("http://web:8000/internal/v1/ai/triage", nodes)  # M06
        self.assertIn(
            "case.triage_ai_requested",
            nodes["http://web:8000/internal/v1/outbox/claim"]["parameters"]["jsonBody"],
        )

    def test_worker_command_runs_once(self):
        out = StringIO()
        call_command("run_worker", "--once", stdout=out)
        self.assertIn("Handled", out.getvalue())
