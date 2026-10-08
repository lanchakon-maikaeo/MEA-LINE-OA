import json
from datetime import datetime

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from operations import inventory


class Command(BaseCommand):
    help = (
        "Import registry figures (manual_reference mode, M07 Q10). The file is either a list of "
        "rows or synthetic-data.json (its stock_snapshot and metadata.snapshot_at)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", default=str(settings.BASE_DIR / "synthetic-data.json"))
        parser.add_argument("--as-of", help="ISO time of the figures (default: from the file)")
        parser.add_argument("--source", help="Where the figures come from")
        parser.add_argument("--username", help="Admin recorded as importer")
        parser.add_argument("--synthetic", action="store_true")

    def handle(self, *args, file, as_of, source, username, synthetic, **options):
        with open(file, encoding="utf-8") as stream:
            data = json.load(stream)
        if isinstance(data, dict):
            rows = data.get("stock_snapshot") or []
            metadata = data.get("metadata") or {}
            as_of = as_of or metadata.get("snapshot_at")
            synthetic = synthetic or bool(metadata.get("contains_synthetic_data"))
            source = source or f"{file} stock_snapshot"
        else:
            rows = data
        if not as_of or not source:
            raise CommandError("Give --as-of and --source for a plain list of rows.")
        try:
            moment = datetime.fromisoformat(as_of)
        except ValueError as exc:
            raise CommandError("--as-of must be an ISO date-time with a time zone.") from exc
        if moment.tzinfo is None:
            raise CommandError("--as-of needs a time zone, e.g. +07:00.")
        actor = None
        if username:
            actor = get_user_model().objects.filter(username=username, is_active=True).first()
            if actor is None:
                raise CommandError("Unknown or inactive user.")
        try:
            created = inventory.import_snapshot(actor, rows, moment, source, synthetic)
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc
        self.stdout.write(f"Imported {len(created)} rows as of {moment.isoformat()}.")
