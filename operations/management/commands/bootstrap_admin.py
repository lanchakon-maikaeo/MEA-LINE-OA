import getpass
import os

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from operations import identity


class Command(BaseCommand):
    help = (
        "Create the first admin when no active admin exists. "
        "Password comes from --password-env or an interactive prompt, never argv."
    )

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument("--display-name", required=True)
        parser.add_argument("--password-env", help="Name of an environment variable to read")

    def handle(self, *args, username, display_name, password_env, **options):
        if password_env:
            password = os.environ.get(password_env, "")
        else:
            password = getpass.getpass("Password: ")
            if password != getpass.getpass("Password (again): "):
                raise CommandError("Passwords do not match.")
        try:
            validate_password(password)
            identity.bootstrap_admin(username, display_name, password)
        except (ValidationError, PermissionDenied) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Admin {username} created.")
