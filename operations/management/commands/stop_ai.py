from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from operations import ai
from operations.models import AIConfigVersion


class Command(BaseCommand):
    help = (
        "Turn one AI task off (RB02) with the same developer-admin check, reason and audit as "
        "the AI admin page. Turning it on again is a switch to an evaluated version."
    )

    def add_arguments(self, parser):
        parser.add_argument("--task", required=True, choices=AIConfigVersion.Task.values)
        parser.add_argument("--username", required=True, help="Developer-admin acting")
        parser.add_argument("--reason", required=True)

    def handle(self, *args, task, username, reason, **options):
        actor = get_user_model().objects.filter(username=username, is_active=True).first()
        if actor is None:
            raise CommandError("Unknown or inactive user.")
        try:
            version, closed = ai.stop(actor, task, reason)
        except (PermissionDenied, ValidationError) as exc:
            raise CommandError("; ".join(getattr(exc, "messages", [str(exc)]))) from exc
        self.stdout.write(f"Stopped {task} v{version.number}; closed {closed} waiting triage runs.")
