"""Load the real ฝอจ. structure: divisions (กอง), the coordinator and their แผนก (ADR 0008).

Safe in every environment, production included: it only adds or updates the units in the file
(never real people), never deletes, and deactivates a แผนก the file no longer lists. Division
codes are the keys; a code changes with rename_division_code, never by editing the file alone.
"""

import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from operations.models import Division, Section

DEFAULT = settings.BASE_DIR / "operations" / "fixtures" / "org_fajo_v1.json"


def load(document):
    """(divisions changed, sections changed, sections closed) for one structure document."""
    units = [(document["coordinator"], Division.Kind.COORDINATION)]
    units += [(row, Division.Kind.DIVISION) for row in document["divisions"]]
    divisions = sections = closed = 0
    for row, kind in units:
        if row["code"] == "กองอุปกรณ์ฯ" and Division.objects.filter(code="กอจ.").exists():
            row = {**row, "code": "กอจ."}
        division, created = Division.objects.get_or_create(
            code=row["code"], defaults={"name": row["name"], "kind": kind}
        )
        if created or (division.name, division.kind, division.is_synthetic) != (
            row["name"],
            kind,
            False,
        ):
            division.name, division.kind, division.is_synthetic = row["name"], kind, False
            division.is_active = True
            division.save(update_fields=["name", "kind", "is_synthetic", "is_active"])
            divisions += 1
        wanted = []
        for order, entry in enumerate(row.get("sections", []), start=1):
            section, made = Section.objects.get_or_create(division=division, name=entry["name"])
            values = (entry.get("code", ""), order, True, False)
            if (
                made
                or (
                    section.code,
                    section.order,
                    section.is_active,
                    section.is_synthetic,
                )
                != values
            ):
                section.code, section.order, section.is_active, section.is_synthetic = values
                section.save(update_fields=["code", "order", "is_active", "is_synthetic"])
                sections += 1
            wanted.append(section.pk)
        closed += (
            Section.objects.filter(division=division, is_active=True)
            .exclude(pk__in=wanted)
            .update(is_active=False)
        )
    from operations.line_menu import bind_seed_divisions

    bind_seed_divisions()
    return divisions, sections, closed


class Command(BaseCommand):
    help = "Load the real ฝอจ. divisions, coordinator and sections (upsert; never deletes)."

    def add_arguments(self, parser):
        parser.add_argument("--source", default=str(DEFAULT))

    @transaction.atomic
    def handle(self, *args, source, **options):
        try:
            with open(source, encoding="utf-8") as stream:
                document = json.load(stream)
            divisions, sections, closed = load(document)
        except (OSError, ValueError, KeyError) as exc:
            raise CommandError(f"Cannot load {source}: {exc}") from exc
        self.stdout.write(
            f"{document['code']}: divisions {divisions} changed, sections {sections} changed, "
            f"{closed} closed."
        )
