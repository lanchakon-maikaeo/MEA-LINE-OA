from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from operations import line_menu_release


class Command(BaseCommand):
    help = "Synchronize the release's role-based LINE menus, resuming any partial publication."

    def handle(self, *args, **options):
        try:
            line_menu_release.sync()
        except (ValidationError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write("LINE menu synchronization completed (or outbound LINE is disabled).")
