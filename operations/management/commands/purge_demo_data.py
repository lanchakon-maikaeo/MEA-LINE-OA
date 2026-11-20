"""ล้างข้อมูลจำลองบน production (15 ต.ค. 2569 Q1–Q4, Q33, Q37); see operations/purge.py.

Without --apply it only prints what would go, the cases to pick the examples from, and the
accounts that would be turned off. Run it as the migration (owner) role: after a fresh
backup, `deploy/gcp-move.sh manage purge_demo_data …` runs it in the migrate service.
"""

from datetime import datetime

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from operations import purge


def _split(value):
    return [part for part in (value or "").replace(" ", ",").split(",") if part]


class Command(BaseCommand):
    help = "Remove the demo data from production (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--keep",
            default="",
            help="Case numbers to keep as labelled examples (at most 2), comma separated; "
            "'none' keeps no example",
        )
        parser.add_argument("--keep-user", default="", help="Demo usernames to leave on")
        parser.add_argument(
            "--before",
            default=purge.GO_LIVE.isoformat(),
            help="Cases submitted before this time are demo data (default: go-live)",
        )
        parser.add_argument("--apply", action="store_true", help="Change the database")
        parser.add_argument("--username", default="", help="Admin acting (with --apply)")
        parser.add_argument("--reason", default="", help="Why (with --apply)")

    def handle(self, *args, keep, keep_user, before, apply, username, reason, **options):
        try:
            cutoff = datetime.fromisoformat(before)
        except ValueError as exc:
            raise CommandError("--before must be an ISO date and time with its offset") from exc
        if cutoff.tzinfo is None:
            raise CommandError("--before needs a time zone offset, e.g. +07:00")
        numbers = [] if keep.strip().lower() == "none" else _split(keep)
        try:
            plan = purge.plan(numbers, cutoff, _split(keep_user))
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc
        self._report(plan, cutoff)
        if not apply:
            self.stdout.write("\nDry run: nothing changed. Add --apply --username … --reason ….")
            return
        if not keep.strip():
            raise CommandError("Pick the examples with --keep <numbers> (or --keep none).")
        actor = get_user_model().objects.filter(username=username, is_active=True).first()
        if actor is None:
            raise CommandError("--username must be an active admin.")
        try:
            removed = purge.apply(actor, plan, reason)
        except (ValidationError, PermissionDenied) as exc:
            raise CommandError(" ".join(getattr(exc, "messages", [str(exc)]))) from exc
        self.stdout.write(self.style.SUCCESS(f"\nDone: {sum(removed.values())} rows removed."))

    def _report(self, plan, cutoff):
        out = self.stdout.write
        out(f"Demo cases (synthetic divisions, or submitted before {cutoff.isoformat()}):")
        out(f"  remove {len(plan.cases)} case(s)")
        for case in plan.cases:
            out(
                f"    {case.number}  {case.submitted_at:%Y-%m-%d}  {case.owner_division.code:<14}"
                f"  {case.kind:<10}  {case.status}"
            )
        out("Examples to keep: " + (", ".join(c.number for c in plan.keep) or "(none chosen)"))
        out("  candidates in real divisions (pick 1 withdrawal and 1 problem):")
        for case in purge.candidates(cutoff):
            out(
                f"    {case.number}  {case.submitted_at:%Y-%m-%d}  {case.owner_division.code:<14}"
                f"  {case.kind:<10}  {case.status}"
            )
        out(f"Accounts to turn off ({len(plan.users)}), roles revoked, never deleted:")
        for user in plan.users:
            out(f"    {user.username}{'' if user.is_active else '  (already off)'}")
        out("Rows removed with them:")
        for name, count in plan.summary().items():
            out(f"    {name:<24} {count}")
        emptied = {}
        for (model, name), pks in plan.rows.empty.items():
            emptied[f"{model.__name__}.{name}"] = len(pks)
        if emptied:
            out("References emptied (rows stay):")
            for key, count in sorted(emptied.items()):
                out(f"    {key:<32} {count}")
        if plan.left:
            out("Synthetic rows that stay because something kept refers to them:")
            for what, why in plan.left:
                out(f"    {what}: {why}")
        if plan.contact is not None:
            out("The bot answer 'contact' still has placeholder phone numbers: it is retired.")
