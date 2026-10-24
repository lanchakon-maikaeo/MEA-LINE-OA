import sys
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from operations import ai_evidence


class Command(BaseCommand):
    help = (
        "Import signed AI evaluation evidence (export_ai_evidence) with the same developer-admin "
        "check, reason and audit as the AI admin page. The version still needs a connection test "
        "here, and someone must switch to it."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Evidence file, or - for stdin")
        parser.add_argument("--username", required=True, help="Developer-admin acting")
        parser.add_argument("--reason", required=True)

    def handle(self, *args, file, username, reason, **options):
        actor = get_user_model().objects.filter(username=username, is_active=True).first()
        if actor is None:
            raise CommandError("Unknown or inactive user.")
        raw = sys.stdin.buffer.read() if file == "-" else Path(file).read_bytes()
        try:
            version, warning = ai_evidence.import_evidence(actor, raw, reason)
        except (PermissionDenied, ValidationError) as exc:
            raise CommandError("; ".join(getattr(exc, "messages", [str(exc)]))) from exc
        self.stdout.write(
            f"Evidence attached to v{version.number} ({version.get_status_display()}); "
            "next: connection test, then a switch by the owner."
        )
        if warning:
            self.stdout.write(self.style.WARNING(warning))
