import json

from django.conf import settings
from django.db import transaction

from operations.management.demo import DemoCommand
from operations.models import Division


class Command(DemoCommand):
    help = "Create synthetic SIM-* divisions and coordination desks (local/CI demo DB only)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--source", default=str(settings.BASE_DIR / "priority-engine-demo.json")
        )

    @transaction.atomic
    def handle_demo(self, *args, source, **options):
        with open(source, encoding="utf-8") as stream:
            data = json.load(stream)
        units = [(row, Division.Kind.DIVISION) for row in data["divisions"]]
        units += [(row, Division.Kind.COORDINATION) for row in data["coordination_roles"]]
        for row, kind in units:
            Division.objects.update_or_create(
                code=row["id"],
                defaults={
                    "name": row["name"],
                    "kind": kind,
                    "is_active": True,
                    "is_synthetic": True,
                },
            )
        # The synthetic intake desk owns new cases unless an admin already chose another unit.
        if not Division.objects.filter(is_intake=True).exists():
            Division.objects.filter(code="SIM-TRIAGE").update(is_intake=True)
        self.stdout.write(f"Synthetic units: {len(units)} ensured.")
