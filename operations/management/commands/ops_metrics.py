"""Operational numbers for alerts (M12, E.9): ages and counts only, never names, LINE user IDs or
message text. The host's metrics job writes them to its journal every minute."""

import json
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Min
from django.utils import timezone

from operations import notifications, orchestration
from operations.models import (
    Attachment,
    ExternalOperation,
    NotificationDelivery,
    OutboxEvent,
    WebhookInbox,
)


def _age(now, moment):
    return int((now - moment).total_seconds()) if moment else 0


def collect(now=None):
    now = now or timezone.now()
    day_ago = now - timedelta(days=1)
    outbox = orchestration.health(now)
    budget = notifications.budget_state(now)
    unknown_ops = ExternalOperation.objects.filter(status=ExternalOperation.Status.UNKNOWN)
    return {
        "inbox_pending_oldest_seconds": _age(
            now,
            WebhookInbox.objects.filter(status=WebhookInbox.Status.PENDING).aggregate(
                m=Min("received_at")
            )["m"],
        ),
        "inbox_failed_24h": WebhookInbox.objects.filter(
            status=WebhookInbox.Status.FAILED, received_at__gte=day_ago
        ).count(),
        "outbox_oldest_pending_seconds": max(outbox["oldest_pending_seconds"].values(), default=0),
        "outbox_dead_letter": outbox["dead_letter"],
        "outbox_expired_leases": OutboxEvent.objects.filter(
            status=OutboxEvent.Status.LEASED, lease_expires_at__lt=now
        ).count(),
        "line_push_used_percent": budget.percent,
        "line_push_tier": budget.tier,
        "line_delivery_unknown": NotificationDelivery.objects.filter(
            status=NotificationDelivery.Status.UNKNOWN
        ).count(),
        "line_delivery_failed_24h": NotificationDelivery.objects.filter(
            status=NotificationDelivery.Status.FAILED, created_at__gte=day_ago
        ).count(),
        "external_unknown": unknown_ops.count(),
        "external_unknown_oldest_seconds": _age(
            now, unknown_ops.aggregate(m=Min("updated_at"))["m"]
        ),
        "scan_pending_oldest_seconds": _age(
            now,
            Attachment.objects.filter(scan_status=Attachment.ScanStatus.PENDING_SCAN).aggregate(
                m=Min("created_at")
            )["m"],
        ),
    }


class Command(BaseCommand):
    help = "Print operational metrics as JSON (no personal data)."

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(collect(), sort_keys=True))
