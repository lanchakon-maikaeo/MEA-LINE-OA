import json

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from operations.models import District


class Command(BaseCommand):
    help = "Load the 18 real MEA district names (prototype codes D01-D18); safe to re-run."

    def add_arguments(self, parser):
        parser.add_argument("--source", default=str(settings.BASE_DIR / "synthetic-data.json"))

    @transaction.atomic
    def handle(self, *args, source, **options):
        with open(source, encoding="utf-8") as stream:
            data = json.load(stream)
        if data["metadata"].get("district_ids_are_official"):
            raise ValueError("Official codes must be loaded into official_code, not code.")
        created = 0
        for row in data["districts"]:
            _, was_created = District.objects.update_or_create(
                code=row["id"], defaults={"name": row["name"]}
            )
            created += was_created
        self.stdout.write(f"Districts: {len(data['districts'])} loaded, {created} new.")
