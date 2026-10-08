from pathlib import Path

from django.core.management.base import CommandError

from operations import line_mockup
from operations.management.demo import DemoCommand


class Command(DemoCommand):
    help = (
        "Record the requester's LINE journeys from the real chat code (each scenario rolled back) "
        "and write a self-contained mockup page. Demo database only; nothing is sent."
    )

    def add_arguments(self, parser):
        parser.add_argument("--output", default="reports/line-mockup.html")
        parser.add_argument("--only", nargs="*", help="Scenario keys to record (default: all).")

    def handle_demo(self, *args, output, only, **options):
        try:
            data = line_mockup.record(only)
        except line_mockup.MockupError as exc:
            said = [s["say"] for s in getattr(exc, "steps", [])]
            raise CommandError(f"{exc} (after: {' / '.join(said[-3:])})") from exc
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(line_mockup.render(data))
        steps = sum(len(s["steps"]) for s in data["scenarios"])
        skipped = f"; skipped {', '.join(data['skipped'])}" if data["skipped"] else ""
        self.stdout.write(
            f"Wrote {path} ({len(data['scenarios'])} scenarios, {steps} steps{skipped})."
        )
