from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from operations import policy


class Command(BaseCommand):
    help = (
        "Import a routing/priority policy JSON as an immutable version (M06), optionally "
        "activating it with the rule owner's approval, under the same admin checks as the web."
    )

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True, help="Admin acting")
        parser.add_argument("--source", default=str(policy.DEMO_POLICY))
        parser.add_argument("--activate", action="store_true")
        parser.add_argument("--approved-by", default="", help="Rule owner who approved it")
        parser.add_argument("--reference", default="", help="Approval document or reference")
        parser.add_argument("--reason", default="")

    def handle(self, *args, username, source, activate, approved_by, reference, reason, **options):
        actor = get_user_model().objects.filter(username=username, is_active=True).first()
        if actor is None:
            raise CommandError("Unknown or inactive user.")
        try:
            version = policy.import_policy(policy.load_document(source), actor)
            self.stdout.write(
                f"{version.code}: {version.get_status_display()} ({version.digest[:12]})"
            )
            if activate:
                _, changed = policy.activate_policy(actor, version, approved_by, reference, reason)
                self.stdout.write(f"Activated; {changed} open case proposal(s) changed.")
        except (ValidationError, PermissionDenied, OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
