import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from operations import attachments, conversation
from operations.line import EVENT_CONVERSATION
from operations.models import OutboxEvent, PostbackToken, WebhookInbox


class Command(BaseCommand):
    help = (
        "Local/staging runner for LINE conversation turns and attachment scans until the "
        "n8n workflows of M04 take over (refused in production)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--interval", type=float, default=2.0)

    def handle(self, *args, once, interval, **options):
        if settings.APP_ENV == "production":
            raise CommandError("Production runs conversation turns through n8n (M04).")
        while True:
            turns = run_once()
            if once:
                self.stdout.write(f"Processed {turns} LINE event(s).")
                return
            time.sleep(interval)


def run_once(batch=50):
    """Turns in event-time order, then replies, attachment scans and housekeeping."""
    conversation.expire_drafts()
    pending = list(
        WebhookInbox.objects.filter(status=WebhookInbox.Status.PENDING)
        .order_by("occurred_at", "id")
        .values_list("pk", flat=True)[:batch]
    )
    for inbox_id in pending:
        outbox = OutboxEvent.objects.filter(
            event_type=EVENT_CONVERSATION, subject_type="line_inbox", subject_id=str(inbox_id)
        )
        try:
            replies = conversation.handle_inbox(inbox_id)
        except Exception as exc:  # noqa: BLE001 - one bad event must not stop the queue
            WebhookInbox.objects.filter(pk=inbox_id).update(
                status=WebhookInbox.Status.FAILED, error=type(exc).__name__[:200]
            )
            outbox.update(status=OutboxEvent.Status.FAILED)
            continue
        outbox.update(status=OutboxEvent.Status.DONE)
        conversation.send_reply(inbox_id, replies)
    attachments.process_pending()
    attachments.purge_rejected()
    PostbackToken.objects.filter(
        expires_at__lt=timezone.now() - timezone.timedelta(days=1)
    ).delete()
    return len(pending)
