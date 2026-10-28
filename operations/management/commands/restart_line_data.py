"""ล้างผู้แจ้งและเรื่องทั้งหมด (29 Oct 2569) at the move to LINE OA @705ttrga; see
operations/purge.py `plan_restart`.

Without --apply it only prints what would go. Run it as the migration (owner) role: on the host,
`deploy/e5-move.sh restart-data …` does that after a fresh backup.
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from operations import purge


class Command(BaseCommand):
    help = "Remove every case, requester and LINE account (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Change the database")
        parser.add_argument("--username", default="", help="Admin acting (with --apply)")
        parser.add_argument("--reason", default="", help="Why (with --apply)")
        parser.add_argument(
            "--reset-numbers",
            action="store_true",
            help="Case numbers start again from 000001 (old numbers may still be in sheets)",
        )

    def handle(self, *args, apply, username, reason, reset_numbers, **options):
        try:
            plan = purge.plan_restart()
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc
        out = self.stdout.write
        out(f"Cases to remove: {len(plan.cases)}")
        out("Rows removed:")
        for name, count in plan.summary().items():
            out(f"    {name:<24} {count}")
        out("Case numbers: " + ("start again from 000001" if reset_numbers else "go on counting"))
        if not apply:
            out("\nDry run: nothing changed. Add --apply --username … --reason ….")
            return
        actor = get_user_model().objects.filter(username=username, is_active=True).first()
        if actor is None:
            raise CommandError("--username must be an active admin.")
        try:
            removed = purge.restart(actor, plan, reason, reset_numbers)
        except (ValidationError, PermissionDenied) as exc:
            raise CommandError(" ".join(getattr(exc, "messages", [str(exc)]))) from exc
        self.stdout.write(self.style.SUCCESS(f"\nDone: {sum(removed.values())} rows removed."))
