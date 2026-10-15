"""What a load run did inside the system (E.9, M13), next to what k6 saw at the edge: webhook
events stored, turns finished, seconds from receiving an event to finishing its turn and to the
reply decision, by percentile, and what still waits in the inbox and outbox. Counts only.
"""

import json
import math
from collections import Counter
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from operations import orchestration
from operations.models import WebhookInbox


def percentiles(values):
    values = sorted(values)
    if not values:
        return None

    def at(share):
        return round(values[min(len(values) - 1, math.ceil(share * len(values)) - 1)], 3)

    return {"p50": at(0.5), "p95": at(0.95), "p99": at(0.99), "max": round(values[-1], 3)}


class Command(BaseCommand):
    help = "Inbox and turn timings since a time (load test report, counts only)."

    def add_arguments(self, parser):
        parser.add_argument("--since", required=True, help="ISO time the load started")

    def handle(self, *args, since, **options):
        try:
            start = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CommandError("--since must be an ISO time with a zone") from exc
        rows = list(
            WebhookInbox.objects.filter(received_at__gte=start).values_list(
                "status", "reply_status", "received_at", "processed_at", "reply_used_at"
            )
        )
        turns = [(done - got).total_seconds() for _, _, got, done, _ in rows if done]
        replies = [(used - got).total_seconds() for _, _, got, _, used in rows if used]
        report = {
            "since": start.isoformat(),
            "stored": len(rows),
            "by_status": dict(Counter(status for status, *_ in rows)),
            "replies": dict(Counter(reply or "none" for _, reply, *_ in rows)),
            "turn_seconds": percentiles(turns),
            "reply_decision_seconds": percentiles(replies),
            "outbox": orchestration.health(),
        }
        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
