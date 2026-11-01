import logging
import threading
import time
from pathlib import Path

from django.core.management.base import BaseCommand
from django.utils import timezone

from operations import (
    ai_chat,
    ai_eval,
    ai_keywords,
    ai_routing,
    attachments,
    broadcast_media,
    conversation,
    monitor_judge,
    monitoring,
    notifications,
    orchestration,
    policy,
    staff_email,
    work_orders,
)
from operations.models import FormSubmission, OutboxEvent, PostbackToken, WebhookInbox

HEARTBEAT = Path("/tmp/worker-heartbeat")  # noqa: S108 - container-local liveness marker
logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Django worker (M04): LINE chat turns and notification planning from the outbox, plus "
        "attachment scans and housekeeping, and AI evaluations requested from the web (in a "
        "separate thread, so chat turns never wait). Run exactly one per environment; n8n owns "
        "sending."
    )

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--interval", type=float, default=1.0)

    def handle(self, *args, once, interval, **options):
        if not once:
            from operations import line_menu_release

            threading.Thread(
                target=line_menu_release.run_loop, name="line-menu", daemon=True
            ).start()
            threading.Thread(target=ai_eval.job_loop, name="ai-eval", daemon=True).start()
            threading.Thread(
                target=scan_broadcast_media, name="broadcast-media", daemon=True
            ).start()
            from operations import koj_tracking

            threading.Thread(target=koj_tracking.sync_loop, name="koj-sources", daemon=True).start()
            from operations import sheet_lookup

            threading.Thread(
                target=sheet_lookup.sync_loop, name="sheet-sources", daemon=True
            ).start()
            from operations import chat_guides

            # ADR 0037 Q31, Q32: the AI tunes the free chat guides and checks them at night.
            threading.Thread(
                target=chat_guides.nightly_loop, name="chat-guides", daemon=True
            ).start()
        next_approval_check = 0
        while True:
            if time.monotonic() >= next_approval_check:
                from operations import identity

                identity.approve_verified_registrations()
                next_approval_check = time.monotonic() + 300
            handled = run_once()
            HEARTBEAT.touch()
            if once:
                broadcast_media.process_pending()
                self.stdout.write(f"Handled {handled} event(s).")
                return
            if not handled:
                time.sleep(interval)


def handle_event(event):
    if event.event_type in {"broadcast.prepare", "broadcast.send_batch"}:
        from operations import broadcast_delivery

        if event.event_type == broadcast_delivery.PREPARE:
            broadcast_delivery.prepare(event)
        else:
            broadcast_delivery.send_batch(event)
    elif event.event_type == orchestration.EVENT_CONVERSATION:
        inbox_id = event.payload["inbox_id"]
        replies = conversation.handle_inbox(inbox_id)
        conversation.send_reply(inbox_id, replies)
        ai_chat.start_loading(inbox_id)  # only when the message went to AI
    elif event.event_type == orchestration.EVENT_ROUTING_AI:
        ai_routing.process(event.payload["ai_routing_run_id"])  # ADR 0013
    elif event.event_type == orchestration.EVENT_KEYWORDS_AI:
        ai_keywords.process(event.payload["ai_keyword_run_id"])  # 15 ต.ค. 2569 Q9
    elif event.event_type == orchestration.EVENT_SHEET_STATUS_AI:
        from operations import ai_sheet_status

        ai_sheet_status.process(event.payload["sheet_status_run_id"])  # ADR 0027
    else:
        notifications.plan_for_event(event)
        staff_email.plan_for_event(event)  # M10: การแจ้งเตือนเจ้าหน้าที่


def _lease_lost(event, error):
    """The lease ran out while the event was handled: the machine paused (a laptop slept mid
    test run on 11 Oct 2569; a VM can pause too). The worker goes on, and the event is claimed
    again later; its handlers are idempotent (a handled inbox is skipped, a planned notification
    is not planned twice)."""
    logger.warning("Outbox event %s: %s Claimed again later.", event.event_id, error)


def run_once(limit=20):
    """Claim due Django-owned events in event-time order; failures retry, then dead-letter."""
    events = orchestration.claim(orchestration.DJANGO_TYPES, limit, lease_seconds=120)
    for event in events:
        try:
            handle_event(event)
        except Exception as exc:  # noqa: BLE001 - one bad event must not stop the queue
            try:
                settled = orchestration.fail(
                    event.event_id,
                    event.lease_token,
                    type(exc).__name__,
                    retry_after=getattr(exc, "retry_after", None),
                )
            except orchestration.LeaseError as lost:
                _lease_lost(event, lost)
                continue
            if settled.status == OutboxEvent.Status.FAILED and event.event_type.startswith(
                "broadcast."
            ):
                from operations import broadcast_delivery

                broadcast_delivery.exhausted(event)
            if (
                settled.status == OutboxEvent.Status.FAILED
                and event.event_type == orchestration.EVENT_CONVERSATION
            ):
                WebhookInbox.objects.filter(pk=event.payload["inbox_id"]).update(
                    status=WebhookInbox.Status.FAILED, error=type(exc).__name__[:200]
                )
            continue
        try:
            orchestration.complete(event.event_id, event.lease_token)
        except orchestration.LeaseError as lost:
            _lease_lost(event, lost)
    ai_chat.expire_waiting()
    from operations import ai_sheet_status

    ai_sheet_status.expire_waiting()  # ADR 0027: the program's reply when the AI is late
    conversation.expire_drafts()
    policy.sweep_clocks()  # M06: acknowledgement reminders and escalation
    work_orders.remind()  # 16 ต.ค. 2569 Q14: งานสั่ง near and past its due date
    monitoring.maybe_run()  # 15 ต.ค. 2569 Q43: yesterday's numbers after the set hour
    monitor_judge.process_queued()  # 16 ต.ค. 2569 Q12: the judge's test on synthetic answers
    attachments.process_pending()
    attachments.purge_rejected()
    PostbackToken.objects.filter(
        expires_at__lt=timezone.now() - timezone.timedelta(days=1)
    ).delete()
    FormSubmission.objects.filter(  # ADR 0015 Q21: one-time form keys last a day
        created_at__lt=timezone.now() - timezone.timedelta(days=1)
    ).delete()
    return len(events)


def scan_broadcast_media():
    # Video scans can take longer than a chat turn; keep them off the chat/heartbeat loop.
    from django.db import close_old_connections

    while True:
        try:
            close_old_connections()
            broadcast_media.process_pending(limit=1)
        except Exception:  # noqa: BLE001 - the lease recovers failed scans on the next pass
            logger.exception("Broadcast media scan failed")
        finally:
            close_old_connections()
        time.sleep(2)
