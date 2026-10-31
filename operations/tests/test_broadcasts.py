from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from operations import broadcast_delivery, broadcasts, notifications
from operations.models import (
    BroadcastBatch,
    BroadcastCampaign,
    ChannelIdentity,
    Division,
    LineQuotaSnapshot,
    OutboxEvent,
    Role,
)
from operations.tests.test_m02_cases import make_staff


class BroadcastDraftTests(TestCase):
    def setUp(self):
        self.division = Division.objects.create(code="BC1", name="กองทดสอบประกาศ")
        self.head = make_staff("bc-head", (Role.SUPERVISOR, self.division.code))
        self.director = make_staff("bc-director", (Role.DEPARTMENT_SUPERVISOR, None))
        self.other = make_staff("bc-other", (Role.SUPERVISOR, self.division.code))
        self.member = make_staff("bc-member", (Role.STAFF, self.division.code))
        self.sender = ChannelIdentity.objects.create(
            channel="line", external_id="U" + "a" * 32, staff_user=self.head, is_following=True
        )
        self.friend = ChannelIdentity.objects.create(
            channel="line", external_id="U" + "b" * 32, is_following=True
        )

    def draft(self, **extra):
        return broadcasts.save_draft(
            self.head,
            title="ประกาศทดสอบ",
            content={"kind": "text", "text": "ขอแจ้งกำหนดการ"},
            **extra,
        )

    def test_only_supervisors_create_and_only_owner_or_department_edits(self):
        with self.assertRaises(PermissionDenied):
            broadcasts.save_draft(self.member, title="ทดสอบ", content={})
        row = self.draft()
        with self.assertRaises(PermissionDenied):
            broadcasts.begin_edit(self.other, row.pk, row.revision)
        self.assertEqual(
            broadcasts.begin_edit(self.director, row.pk, row.revision).status, "paused"
        )

    def test_segment_includes_sender_exactly_once_and_resolves_current_members(self):
        segment = broadcasts.save_segment(self.head, "ผู้รับทดสอบ", {"people": [self.friend.pk]})
        row = self.draft(audience="segment", segment_id=segment.pk)
        self.assertEqual(
            set(broadcasts.recipients(row)), {self.sender.external_id, self.friend.external_id}
        )
        segment = broadcasts.save_segment(
            self.head,
            segment.name,
            {"people": [self.friend.pk, self.sender.pk]},
            segment_id=segment.pk,
            revision=segment.revision,
        )
        row.segment = segment
        self.assertEqual(len(broadcasts.recipients(row)), 2)
        self.friend.is_following = False
        self.friend.save()
        self.assertEqual(broadcasts.recipients(row), [self.sender.external_id])

    def test_scheduled_edit_pauses_and_requires_confirmation_again(self):
        row = self.draft()
        row = broadcasts.confirm(
            self.head, row.pk, row.revision, timezone.now() + timedelta(hours=2)
        )
        self.assertEqual(row.status, "scheduled")
        row = broadcasts.begin_edit(self.head, row.pk, row.revision)
        self.assertEqual(row.status, "paused")
        row = broadcasts.save_draft(
            self.head,
            campaign_id=row.pk,
            revision=row.revision,
            title=row.title,
            content={"kind": "text", "text": "กำหนดการใหม่"},
        )
        self.assertEqual(row.status, "paused")
        row.refresh_from_db()
        self.assertEqual(row.messages, [])
        self.assertEqual(broadcasts.confirm(self.head, row.pk, row.revision).status, "queued")

    def test_confirmation_is_idempotent_across_web_and_mini_app(self):
        row = self.draft()
        first = broadcasts.confirm(self.head, row.pk, row.revision)
        again = broadcasts.confirm(self.head, row.pk, row.revision)
        self.assertEqual(first.revision, again.revision)
        self.assertEqual(BroadcastCampaign.objects.count(), 1)
        self.assertEqual(first.messages, [{"type": "text", "text": "ขอแจ้งกำหนดการ"}])

    def test_stale_editor_cannot_overwrite_newer_draft(self):
        row = self.draft()
        broadcasts.begin_edit(self.head, row.pk, row.revision)
        with self.assertRaises(ValidationError):
            broadcasts.save_draft(
                self.head, campaign_id=row.pk, revision=row.revision, title="ฉบับเก่า", content={}
            )

    def test_all_friends_is_not_inferred_from_local_identity_rows(self):
        self.assertIsNone(broadcasts.recipients(self.draft()))

    def test_sender_must_be_following_before_confirmation(self):
        row = self.draft()
        self.sender.is_following = False
        self.sender.save()
        with self.assertRaises(ValidationError):
            broadcasts.confirm(self.head, row.pk, row.revision)
        row.refresh_from_db()
        self.assertEqual(row.status, "draft")

    def test_content_formats_and_unsafe_links(self):
        for content in [
            {"kind": "image", "url": "https://example.com/picture.jpg"},
            {
                "kind": "video",
                "url": "https://example.com/video.mp4",
                "preview": "https://example.com/picture.jpg",
            },
            {
                "kind": "click_image",
                "title": "กำหนดการ",
                "url": "https://example.com/picture.jpg",
                "link": "https://example.com/",
            },
            {
                "kind": "card",
                "title": "กำหนดการ",
                "buttons": [{"label": "ดูรายละเอียด", "url": "https://example.com/"}],
            },
        ]:
            with self.subTest(content=content):
                self.assertEqual(len(broadcasts.build_messages(content)), 1)
        for content in [
            {"kind": "image", "url": "javascript:alert(1)"},
            {"kind": "video", "url": "https://example.com/video.mp4"},
            {"kind": "text", "text": ""},
        ]:
            with self.subTest(content=content), self.assertRaises(ValidationError):
                broadcasts.build_messages(content)


@override_settings(
    APP_ENV="production",
    OUTBOUND_ENABLED=True,
    LINE_PUSH_ENABLED=True,
    LINE_CHANNEL_ACCESS_TOKEN="test-only",
    LINE_PUSH_MONTHLY_BUDGET=2000,
)
class BroadcastDeliveryTests(TestCase):
    setUp = BroadcastDraftTests.setUp
    draft = BroadcastDraftTests.draft

    def quota(self, now):
        return LineQuotaSnapshot.objects.update_or_create(
            month=notifications.month_key(now),
            defaults={"quota": getattr(self, "quota_limit", 2000), "usage": 0, "checked_at": now},
        )[0]

    def prepare(self, campaign=None):
        if campaign is None:
            segment = broadcasts.save_segment(self.head, "กลุ่มทดสอบ", {"people": [self.friend.pk]})
            campaign = self.draft(audience="segment", segment_id=segment.pk)
        campaign = broadcasts.confirm(self.head, campaign.pk, campaign.revision)
        event = OutboxEvent.objects.get(
            event_type=broadcast_delivery.PREPARE, subject_id=str(campaign.pk)
        )
        with patch("operations.notifications.reconcile", side_effect=self.quota):
            broadcast_delivery.prepare(event)
        campaign.refresh_from_db()
        return campaign, event

    def send_events(self):
        return list(OutboxEvent.objects.filter(event_type=broadcast_delivery.SEND).order_by("pk"))

    def test_test_send_is_separate_idempotent_and_only_targets_current_sender(self):
        import uuid

        source = self.draft()
        key = uuid.uuid4()
        test = broadcasts.test_to_self(self.head, source.pk, source.revision, key)
        again = broadcasts.test_to_self(self.head, source.pk, source.revision, key)
        self.assertEqual(test.pk, again.pk)
        source.refresh_from_db()
        self.assertEqual(source.status, "draft")
        self.assertEqual(source.revision, 1)
        event = OutboxEvent.objects.get(
            event_type=broadcast_delivery.PREPARE, subject_id=str(test.pk)
        )
        with patch("operations.notifications.reconcile", side_effect=self.quota):
            broadcast_delivery.prepare(event)
        batch = test.batches.get()
        self.assertEqual(batch.payload["to"], [self.sender.external_id])
        self.assertEqual(batch.recipient_count, 1)
        with patch("operations.line_api.campaign_send", return_value="test-request") as send:
            send_event = self.send_events()[0]
            broadcast_delivery.send_batch(send_event)
            broadcast_delivery.send_batch(send_event)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(notifications.budget_state().used, 1)
        with self.assertRaises(ValidationError):
            broadcasts.test_to_self(self.head, source.pk, source.revision + 1, key)
        with self.assertRaises(PermissionDenied):
            broadcasts.test_to_self(self.other, source.pk, source.revision, uuid.uuid4())

    def test_copy_draft_does_not_send_or_change_source(self):
        source = self.draft()
        copied = broadcasts.copy_draft(self.other, source.pk)
        self.assertEqual(copied.created_by, self.other)
        self.assertEqual(copied.status, "draft")
        self.assertEqual(copied.content, source.content)
        self.assertNotEqual(copied.pk, source.pk)
        self.assertFalse(OutboxEvent.objects.filter(event_type=broadcast_delivery.PREPARE).exists())

    def test_full_group_is_reserved_before_any_send_and_insufficient_quota_holds_all(self):
        self.quota_limit = 1
        with patch("operations.line_api.campaign_send") as send:
            campaign, _ = self.prepare()
        self.assertEqual(campaign.status, "held")
        self.assertFalse(campaign.batches.exists())
        send.assert_not_called()
        self.assertTrue(campaign.detail)

    def test_segment_sender_and_budget_are_counted_once(self):
        campaign, _ = self.prepare()
        self.assertEqual(notifications.budget_state().used, 2)
        with patch("operations.line_api.campaign_send", return_value="request-id") as send:
            event = self.send_events()[0]
            broadcast_delivery.send_batch(event)
            broadcast_delivery.send_batch(event)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(
            set(send.call_args.args[1]["to"]), {self.sender.external_id, self.friend.external_id}
        )
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, "accepted")
        self.assertEqual(notifications.budget_state().used, 2)

    def test_timeout_reuses_identical_payload_and_retry_key(self):
        campaign, _ = self.prepare()
        event = self.send_events()[0]
        with patch(
            "operations.line_api.campaign_send", side_effect=[OSError("timeout"), "original-id"]
        ) as send:
            with self.assertRaises(broadcast_delivery.RetryBroadcast):
                broadcast_delivery.send_batch(event)
            self.assertEqual(notifications.budget_state().used, 2)
            broadcast_delivery.send_batch(event)
            self.assertEqual(send.call_args_list[0], send.call_args_list[1])
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, "accepted")

    def test_unknown_request_is_not_retried_after_safe_retry_window(self):
        campaign, _ = self.prepare()
        batch = campaign.batches.get()
        BroadcastBatch.objects.filter(pk=batch.pk).update(
            status="unknown", first_attempt_at=timezone.now() - timedelta(hours=24)
        )
        with patch("operations.line_api.campaign_send") as send:
            broadcast_delivery.send_batch(self.send_events()[0])
        send.assert_not_called()
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, "partial")
        self.assertEqual(notifications.budget_state().used, 2)

    def test_all_friends_use_complete_delivery_guard_and_poll_original_request(self):
        campaign, _ = self.prepare(self.draft())
        batch = campaign.batches.get()
        self.assertEqual(batch.endpoint, "narrowcast")
        self.assertNotIn("recipient", batch.payload)
        self.assertEqual(
            batch.payload["limit"],
            {"max": 2000, "upToRemainingQuota": True, "forbidPartialDelivery": True},
        )
        event = self.send_events()[0]
        with patch("operations.line_api.campaign_send", return_value="original-request") as send:
            with self.assertRaises(broadcast_delivery.RetryBroadcast):
                broadcast_delivery.send_batch(event)
            with patch(
                "operations.line_api.call",
                return_value={
                    "phase": "succeeded",
                    "successCount": 123,
                    "failureCount": 0,
                    "targetCount": 123,
                },
            ) as poll:
                broadcast_delivery.send_batch(event)
        self.assertEqual(send.call_count, 1)
        self.assertIn("original-request", poll.call_args.args[0])
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, "accepted")
        self.assertEqual(notifications.budget_state().used, 123)

    def test_line_cancels_all_friends_on_quota_shortage_without_counting_delivery(self):
        campaign, _ = self.prepare(self.draft())
        event = self.send_events()[0]
        with patch("operations.line_api.campaign_send", return_value="request-id"):
            with self.assertRaises(broadcast_delivery.RetryBroadcast):
                broadcast_delivery.send_batch(event)
        with patch(
            "operations.line_api.call",
            return_value={"phase": "failed", "errorCode": 5, "successCount": 0, "failureCount": 0},
        ):
            broadcast_delivery.send_batch(event)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, "held")
        self.assertEqual(notifications.budget_state().used, 0)

    def test_paused_schedule_event_does_not_send(self):
        campaign = self.draft()
        campaign = broadcasts.confirm(self.head, campaign.pk, campaign.revision)
        event = OutboxEvent.objects.get(event_type=broadcast_delivery.PREPARE)
        broadcasts.begin_edit(self.head, campaign.pk, campaign.revision)
        with patch("operations.notifications.reconcile") as quota:
            broadcast_delivery.prepare(event)
        quota.assert_not_called()
        self.assertFalse(campaign.batches.exists())

    def test_new_campaign_cannot_spend_another_campaigns_reservation(self):
        self.quota_limit = 3
        first, _ = self.prepare()
        second, _ = self.prepare()
        self.assertEqual(first.status, "sending")
        self.assertEqual(second.status, "held")
        self.assertEqual(notifications.budget_state().used, 2)

    @override_settings(APP_ENV="local", LINE_ALLOWED_RECIPIENTS=frozenset())
    def test_nonproduction_does_not_send_to_unlisted_users_or_all_friends(self):
        for campaign in (None, self.draft()):
            row, _ = self.prepare(campaign)
            self.assertEqual(row.status, "held")
            self.assertFalse(row.batches.exists())

    def test_two_batches_retry_only_the_unfinished_batch(self):
        others = ChannelIdentity.objects.bulk_create(
            [
                ChannelIdentity(channel="line", external_id="U" + f"{i:032x}", is_following=True)
                for i in range(501)
            ]
        )
        segment = broadcasts.save_segment(self.head, "กลุ่มใหญ่", {"people": [p.pk for p in others]})
        campaign, _ = self.prepare(self.draft(audience="segment", segment_id=segment.pk))
        events = self.send_events()
        self.assertEqual(len(events), 2)
        with patch(
            "operations.line_api.campaign_send",
            side_effect=["first-id", OSError("timeout"), "second-id"],
        ) as send:
            broadcast_delivery.send_batch(events[0])
            with self.assertRaises(broadcast_delivery.RetryBroadcast):
                broadcast_delivery.send_batch(events[1])
            broadcast_delivery.send_batch(events[0])
            broadcast_delivery.send_batch(events[1])
        self.assertEqual(send.call_count, 3)
        self.assertEqual(send.call_args_list[1], send.call_args_list[2])
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, "accepted")
        self.assertEqual(notifications.budget_state().used, 502)

    def test_local_acceptance_adds_to_manual_oa_usage_after_snapshot(self):
        campaign, _ = self.prepare()
        LineQuotaSnapshot.objects.filter(month=notifications.month_key(timezone.now())).update(
            usage=100
        )
        with patch("operations.line_api.campaign_send", return_value="request-id"):
            broadcast_delivery.send_batch(self.send_events()[0])
        self.assertEqual(notifications.budget_state().used, 102)

    def test_all_friends_partial_result_is_reported_and_never_resent(self):
        campaign, _ = self.prepare(self.draft())
        event = self.send_events()[0]
        with patch("operations.line_api.campaign_send", return_value="request-id") as send:
            with self.assertRaises(broadcast_delivery.RetryBroadcast):
                broadcast_delivery.send_batch(event)
            with patch(
                "operations.line_api.call",
                return_value={
                    "phase": "succeeded",
                    "successCount": 10,
                    "failureCount": 2,
                    "targetCount": 12,
                },
            ):
                broadcast_delivery.send_batch(event)
            broadcast_delivery.send_batch(event)
        self.assertEqual(send.call_count, 1)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, "partial")
        self.assertEqual(notifications.budget_state().used, 10)

    def test_monthly_limit_failure_stops_batches_that_have_not_started(self):
        from operations import line_api

        others = ChannelIdentity.objects.bulk_create(
            [
                ChannelIdentity(channel="line", external_id="U" + f"{i:032x}", is_following=True)
                for i in range(501)
            ]
        )
        segment = broadcasts.save_segment(self.head, "กลุ่มใหญ่", {"people": [p.pk for p in others]})
        campaign, _ = self.prepare(self.draft(audience="segment", segment_id=segment.pk))
        events = self.send_events()
        with patch(
            "operations.line_api.campaign_send",
            side_effect=line_api.LineApiError(429, "You have reached your monthly limit."),
        ) as send:
            for event in events:
                broadcast_delivery.send_batch(event)
        self.assertEqual(send.call_count, 1)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, "held")
        self.assertEqual(notifications.budget_state().used, 0)


@override_settings(
    APP_ENV="local",
    OUTBOUND_ENABLED=True,
    LINE_PUSH_ENABLED=True,
    LINE_CHANNEL_ACCESS_TOKEN="test-only",
    LINE_PUSH_MONTHLY_BUDGET=3,
)
class BroadcastConcurrencyTests(TransactionTestCase):
    setUp = BroadcastDraftTests.setUp
    draft = BroadcastDraftTests.draft

    @override_settings(APP_ENV="production")
    def test_parallel_campaigns_cannot_reserve_the_same_quota(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        from django.db import close_old_connections

        segment = broadcasts.save_segment(self.head, "กลุ่มทดสอบ", {"people": [self.friend.pk]})
        for _ in range(2):
            row = self.draft(audience="segment", segment_id=segment.pk)
            broadcasts.confirm(self.head, row.pk, row.revision)
        LineQuotaSnapshot.objects.create(
            month=notifications.month_key(timezone.now()),
            quota=3,
            usage=0,
            checked_at=timezone.now(),
        )
        events = list(OutboxEvent.objects.filter(event_type=broadcast_delivery.PREPARE))
        barrier = Barrier(2)

        def prepare(event):
            close_old_connections()
            try:
                broadcast_delivery.prepare(event)
            finally:
                close_old_connections()

        with patch(
            "operations.notifications.reconcile", side_effect=lambda now: barrier.wait(timeout=15)
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(prepare, event) for event in events]
                for future in futures:
                    future.result(timeout=30)
        self.assertEqual(
            sorted(BroadcastCampaign.objects.values_list("status", flat=True)), ["held", "sending"]
        )
        self.assertEqual(notifications.budget_state().used, 2)

    @override_settings(APP_ENV="production")
    def test_parallel_confirmations_create_one_outbox_event(self):
        from concurrent.futures import ThreadPoolExecutor

        from django.db import close_old_connections

        row = self.draft()

        def confirm():
            close_old_connections()
            try:
                return broadcasts.confirm(self.head, row.pk, row.revision).revision
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(confirm) for _ in range(2)]
            self.assertEqual([f.result(timeout=30) for f in futures], [2, 2])
        self.assertEqual(
            OutboxEvent.objects.filter(event_type=broadcast_delivery.PREPARE).count(), 1
        )


class BroadcastApiTests(TestCase):
    def test_duplicate_key_recovers_the_original_request_id(self):
        import io
        import urllib.error
        import uuid

        from operations import line_api

        key = uuid.uuid4()
        error = urllib.error.HTTPError(
            "https://api.line.me",
            409,
            "accepted",
            {"x-line-accepted-request-id": "original-id"},
            io.BytesIO(b"{}"),
        )
        with patch("operations.line_api._request", side_effect=error) as request:
            result = line_api.campaign_send("narrowcast", {"messages": []}, key)
        self.assertEqual(result, "original-id")
        self.assertEqual(request.call_args.kwargs["extra_headers"], {"X-Line-Retry-Key": str(key)})

    def test_quota_refresh_releases_old_limit_after_capacity_is_increased(self):
        LineQuotaSnapshot.objects.create(
            month=notifications.month_key(timezone.now()), quota=10, usage=10, limit_reached=True
        )
        with patch(
            "operations.line_api.call",
            side_effect=[{"type": "limited", "value": 100}, {"totalUsage": 10}],
        ):
            snapshot = notifications.reconcile()
        self.assertFalse(snapshot.limit_reached)
        self.assertEqual(snapshot.quota, 100)
