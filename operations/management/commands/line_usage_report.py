import json
from pathlib import Path

from django.core.management.base import BaseCommand

from operations import line_usage


class Command(BaseCommand):
    help = (
        "LINE pushes per case, this month's forecast and the 18-district forecast with the "
        "package C.2.3 proposes (M13 pilot report). Counts only, no names or LINE IDs."
    )

    def add_arguments(self, parser):
        parser.add_argument("--months", type=int, default=2, help="Months back, this one included")
        parser.add_argument("--out", help="Also write the JSON report to this path")

    def handle(self, *args, months, out=None, **options):
        text = json.dumps(line_usage.report(months=months), ensure_ascii=False, indent=2)
        if out:
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_text(text + "\n", encoding="utf-8")
        self.stdout.write(text)
