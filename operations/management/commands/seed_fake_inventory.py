import json

from django.conf import settings
from django.db import transaction

from operations import inventory
from operations.management.demo import DemoCommand

DEFAULT = settings.BASE_DIR / "operations" / "fixtures" / "inventory_fake_v1.json"


class Command(DemoCommand):
    help = "Reset the fake inventory (คลังจำลอง) from its synthetic fixture (demo DB only, M07)."

    def add_arguments(self, parser):
        parser.add_argument("--source", default=str(DEFAULT))

    @transaction.atomic
    def handle_demo(self, *args, source, **options):
        with open(source, encoding="utf-8") as stream:
            data = json.load(stream)
        rows = [{**row, "source": data["source"]} for row in data["stock"]]
        inventory.seed_fake(rows, actor_label="seed:" + data["name"])
        self.stdout.write(f"Fake inventory: {len(rows)} items reset from {data['name']}.")
