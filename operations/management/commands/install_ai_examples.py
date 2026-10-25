from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from operations import ai


class Command(BaseCommand):
    help = (
        "Install one bundled set of curated AI examples (v1, v2, v3) in any database, with the "
        "same developer-admin check, reason and audit as the AI admin page (25 Oct 2569 Q17)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--set", required=True, choices=sorted(ai.EXAMPLE_SETS))
        parser.add_argument("--username", required=True, help="Developer-admin acting")
        parser.add_argument("--reason", required=True)

    def handle(self, *args, username, reason, **options):
        actor = get_user_model().objects.filter(username=username, is_active=True).first()
        if actor is None:
            raise CommandError("Unknown or inactive user.")
        try:
            count, created = ai.install_examples(actor, options["set"], reason)
        except (PermissionDenied, ValidationError) as exc:
            raise CommandError("; ".join(getattr(exc, "messages", [str(exc)]))) from exc
        self.stdout.write(f"examples-{options['set']}: {count} ensured, {created} new.")
