import json

from django.conf import settings
from django.db import transaction

from operations.management.demo import DemoCommand
from operations.models import AIExample


class Command(DemoCommand):
    help = "Load the synthetic curated AI examples (demo DB only; real ones come from review)."

    def add_arguments(self, parser):
        fixtures = settings.BASE_DIR / "operations/fixtures"
        parser.add_argument(
            "--source",
            action="append",
            help="Example file (repeatable); default: examples-v1 and examples-v2",
        )
        self.defaults = [
            str(fixtures / "ai_examples_v1.json"),
            str(fixtures / "ai_examples_v2.json"),
        ]

    @transaction.atomic
    def handle_demo(self, *args, source=None, **options):
        total = 0
        for path in source or self.defaults:
            with open(path, encoding="utf-8") as stream:
                data = json.load(stream)
            for row in data["examples"]:
                AIExample.objects.update_or_create(
                    text=row["text"],
                    defaults={
                        "draft": row["draft"],
                        "requests": row["requests"],
                        "source": f"curated:{data['version']}",
                        "is_active": True,
                    },
                )
            total += len(data["examples"])
        self.stdout.write(f"AI examples: {total} ensured.")
