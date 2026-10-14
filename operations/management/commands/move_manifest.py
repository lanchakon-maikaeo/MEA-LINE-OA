"""Proof that a host move lost nothing (ADR 0015, Q14).

On the old host, after maintenance is on and writes have stopped:
    manage.py move_manifest > source.json
On the new host, after the restore:
    manage.py move_manifest --compare source.json
The second run exits 1 and lists every difference: row counts of every table, the case-number
counters, the audit sequence, unapplied migrations and the attachment files. Nothing opens the
new host until it passes; on a failure the old host is switched back on.
"""

import json
import sys
import time

from django.core.management.base import BaseCommand
from django.db import connection
from django.db.models import Count, Max

from operations.management.commands.verify_restore import collect
from operations.models import AuditEvent, CaseNumberCounter


def manifest():
    with connection.cursor() as cursor:
        tables = sorted(connection.introspection.table_names(cursor))
        counts = {}
        for table in tables:
            cursor.execute(f"SELECT count(*) FROM {connection.ops.quote_name(table)}")
            counts[table] = cursor.fetchone()[0]
    audit = AuditEvent.objects.aggregate(count=Count("id"), last=Max("id"))
    counters = {
        f"{c.prefix}{c.year_be}": c.last_value
        for c in CaseNumberCounter.objects.order_by("prefix", "year_be")
    }
    files = collect(int(time.time()))
    return {
        "tables": counts,
        "case_number_counters": counters,
        "audit": {"count": audit["count"], "last_id": audit["last"]},
        "unapplied_migrations": files["unapplied_migrations"],
        "attachments": {
            k: files[k]
            for k in ("attachments_checked", "attachments_missing", "attachments_mismatched")
        },
    }


def differences(source, target):
    problems = []
    for table in sorted(set(source["tables"]) | set(target["tables"])):
        a, b = source["tables"].get(table), target["tables"].get(table)
        if a != b:
            problems.append(f"table {table}: {a} -> {b}")
    if source["case_number_counters"] != target["case_number_counters"]:
        problems.append(
            f"case numbers: {source['case_number_counters']} -> {target['case_number_counters']}"
        )
    if source["audit"] != target["audit"]:
        problems.append(f"audit: {source['audit']} -> {target['audit']}")
    if target["unapplied_migrations"]:
        problems.append(f"unapplied migrations: {target['unapplied_migrations']}")
    if source["attachments"]["attachments_checked"] != target["attachments"]["attachments_checked"]:
        problems.append(f"attachments: {source['attachments']} -> {target['attachments']}")
    if (
        target["attachments"]["attachments_missing"]
        or target["attachments"]["attachments_mismatched"]
    ):
        problems.append(f"attachment files: {target['attachments']}")
    return problems


class Command(BaseCommand):
    help = "Write this database's move manifest (JSON), or --compare it with the old host's."

    def add_arguments(self, parser):
        parser.add_argument("--compare", help="Manifest written on the old host")

    def handle(self, *args, compare=None, **options):
        current = manifest()
        if not compare:
            self.stdout.write(json.dumps(current, indent=2, sort_keys=True))
            return
        with open(compare) as stream:
            source = json.load(stream)
        problems = differences(source, current)
        for problem in problems:
            self.stderr.write(f"MISMATCH {problem}")
        if problems:
            sys.exit(1)
        self.stdout.write(
            f"move verified: {len(current['tables'])} tables, "
            f"{current['audit']['count']} audit events, "
            f"{current['attachments']['attachments_checked']} attachments"
        )
