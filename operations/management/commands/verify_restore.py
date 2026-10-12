"""Checks after a restore drill (M12, RB05/E.9): the schema is complete, every kept file the
database points to is there with the same checksum, and how much recent data the backup held.

RPO here is the gap between the drill's start and the newest row restored. While the system is
idle that overstates the loss (it is an upper bound); a drill should follow some activity.
"""

import hashlib
import json
import time
from datetime import UTC, datetime

from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import Max

from operations import attachments
from operations.models import (
    Attachment,
    AuditEvent,
    Case,
    CaseLine,
    ConversationMessage,
    OutboxEvent,
    Requester,
    WebhookInbox,
)

COUNTED = (Case, CaseLine, AuditEvent, Attachment, Requester, ConversationMessage, OutboxEvent)


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect(started):
    executor = MigrationExecutor(connection)
    unapplied = len(executor.migration_plan(executor.loader.graph.leaf_nodes()))
    newest = [
        AuditEvent.objects.aggregate(m=Max("created_at"))["m"],
        Case.objects.aggregate(m=Max("updated_at"))["m"],
        WebhookInbox.objects.aggregate(m=Max("received_at"))["m"],
        OutboxEvent.objects.aggregate(m=Max("occurred_at"))["m"],
    ]
    newest = max((m for m in newest if m), default=None)
    missing = mismatched = checked = 0
    root = attachments.storage_dir()
    kept = Attachment.objects.exclude(scan_status=Attachment.ScanStatus.REJECTED).exclude(
        storage_name=""
    )
    for attachment in kept.only("storage_name", "sha256").iterator():
        checked += 1
        path = root / attachment.storage_name
        if not path.is_file():
            missing += 1
        elif attachment.sha256 and _sha256(path) != attachment.sha256:
            mismatched += 1
    report = {
        "started_at": datetime.fromtimestamp(started, UTC).isoformat(),
        "unapplied_migrations": unapplied,
        "counts": {m._meta.label: m.objects.count() for m in COUNTED},
        "newest_row_at": newest.isoformat() if newest else None,
        "rpo_seconds": int(started - newest.timestamp()) if newest else None,
        "attachments_checked": checked,
        "attachments_missing": missing,
        "attachments_mismatched": mismatched,
    }
    report["ok"] = unapplied == 0 and missing == 0 and mismatched == 0
    return report


class Command(BaseCommand):
    help = "Report schema, row counts, file checks and RPO of a restored database (JSON)."

    def add_arguments(self, parser):
        parser.add_argument("--started", type=int, default=None, help="Drill start (epoch s)")

    def handle(self, *args, started, **options):
        self.stdout.write(json.dumps(collect(started or int(time.time())), indent=2))
