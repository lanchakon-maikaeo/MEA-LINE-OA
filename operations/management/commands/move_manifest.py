"""Proof that a host move lost nothing (ADR 0015, Q14).

On the old host, after maintenance is on and writes have stopped:
    manage.py move_manifest > source.json
On the new host, after the restore:
    manage.py move_manifest --compare source.json
The second run exits 1 and lists every difference: row counts of every table, the case-number
counters, the audit sequence, unapplied migrations and the attachment files. Nothing opens the
new host until it passes; on a failure the old host is switched back on.

--deep (Docs/release/gcp-move, 2 Nov 2026: no data may be lost) adds what counts cannot see: a
digest of every row of every table (an UPDATE keeps the count), every sequence, the applied
migrations (both hosts must run the same release) and the sha256 of every private file, not
only the attachments the database names. A deep source manifest makes --compare deep too.
"""

import hashlib
import json
import sys
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.db.migrations.recorder import MigrationRecorder
from django.db.models import Count, Max

from operations.management.commands.verify_restore import collect
from operations.models import AuditEvent, CaseNumberCounter


def deep_manifest():
    """Row digests independent of row order, host locale and session time zone."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL TimeZone = 'UTC'")
        cursor.execute("SET LOCAL DateStyle = 'ISO, YMD'")
        cursor.execute("SET LOCAL IntervalStyle = 'postgres'")
        cursor.execute("SET LOCAL extra_float_digits = 3")
        content = {}
        for table in sorted(connection.introspection.table_names(cursor)):
            name = connection.ops.quote_name(table)
            cursor.execute(
                f"SELECT md5(coalesce(string_agg(h, '' ORDER BY h), '')) "
                f'FROM (SELECT md5(t::text) COLLATE "C" AS h FROM {name} t) rows'
            )
            content[table] = cursor.fetchone()[0]
        cursor.execute(
            "SELECT sequencename, last_value FROM pg_sequences "
            "WHERE schemaname = current_schema() ORDER BY sequencename"
        )
        sequences = {name: value for name, value in cursor.fetchall()}
    applied = sorted(
        f"{app}.{name}"
        for app, name in MigrationRecorder.Migration.objects.values_list("app", "name")
    )
    return {
        "content": content,
        "sequences": sequences,
        "applied_migrations": applied,
        "files": file_digests(Path(settings.MEDIA_ROOT)),
    }


def file_digests(root):
    files = {}
    if root.is_dir():
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(block)
            files[str(path.relative_to(root))] = digest.hexdigest()
    return files


def keyed_differences(label, a, b, limit=20):
    keys = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    shown = [f"{label} {k}: {a.get(k, 'missing')} -> {b.get(k, 'missing')}" for k in keys[:limit]]
    if len(keys) > limit:
        shown.append(f"{label}: {len(keys) - limit} more differ")
    return shown


def manifest(deep=False):
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
    result = {
        "tables": counts,
        "case_number_counters": counters,
        "audit": {"count": audit["count"], "last_id": audit["last"]},
        "unapplied_migrations": files["unapplied_migrations"],
        "attachments": {
            k: files[k]
            for k in ("attachments_checked", "attachments_missing", "attachments_mismatched")
        },
    }
    if deep:
        result.update(deep_manifest())
    return result


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
    if "content" in source:
        if "content" not in target:
            problems.append("the source manifest is deep: compare with --deep")
            return problems
        a, b = set(source["applied_migrations"]), set(target["applied_migrations"])
        if a != b:
            problems.append(
                "applied migrations differ (deploy the same release on both hosts first): "
                f"only on the old host {sorted(a - b)[:5]}, only here {sorted(b - a)[:5]}"
            )
        problems += keyed_differences("rows of", source["content"], target["content"])
        problems += keyed_differences("sequence", source["sequences"], target["sequences"])
        problems += keyed_differences("file", source["files"], target["files"])
    return problems


class Command(BaseCommand):
    help = "Write this database's move manifest (JSON), or --compare it with the old host's."

    def add_arguments(self, parser):
        parser.add_argument("--compare", help="Manifest written on the old host")
        parser.add_argument(
            "--deep", action="store_true", help="Also row digests, sequences, migrations, all files"
        )

    def handle(self, *args, compare=None, deep=False, **options):
        if not compare:
            self.stdout.write(json.dumps(manifest(deep), indent=2, sort_keys=True))
            return
        with open(compare) as stream:
            source = json.load(stream)
        current = manifest(deep or "content" in source)
        problems = differences(source, current)
        for problem in problems:
            self.stderr.write(f"MISMATCH {problem}")
        if problems:
            sys.exit(1)
        self.stdout.write(
            f"move verified{' (deep)' if 'content' in current else ''}: "
            f"{len(current['tables'])} tables, "
            f"{current['audit']['count']} audit events, "
            f"{current['attachments']['attachments_checked']} attachments"
            + (f", {len(current['files'])} files" if "files" in current else "")
        )
