"""ผู้ช่วยเหลือระบบ (ADR 0007), on the host only: one person, the system's designer.

manage.py system_support --grant <username> --reason "…"
manage.py system_support --revoke <username> --reason "…"
manage.py system_support            (who holds it)
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from operations import support


class Command(BaseCommand):
    help = "Grant, revoke or show ผู้ช่วยเหลือระบบ (one holder; acts for every division)."

    def add_arguments(self, parser):
        action = parser.add_mutually_exclusive_group()
        action.add_argument("--grant", metavar="USERNAME")
        action.add_argument("--revoke", metavar="USERNAME")
        parser.add_argument("--reason", default="")

    def handle(self, *args, grant, revoke, reason, **options):
        if not grant and not revoke:
            holder = support.holder()
            self.stdout.write(holder.username if holder else "nobody holds ผู้ช่วยเหลือระบบ")
            return
        username = grant or revoke
        user = get_user_model().objects.filter(username=username).first()
        if user is None:
            raise CommandError(f"No account {username}.")
        try:
            (support.grant if grant else support.revoke)(user, reason)
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc
        self.stdout.write(f"{'granted' if grant else 'revoked'} ผู้ช่วยเหลือระบบ: {username}")
