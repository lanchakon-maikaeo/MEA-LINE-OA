"""An AI version for chat answers (ADR 0012): create from existing profiles, then the same
connection test, evaluation gate and activation as the web page (developer-admin, reason)."""

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from operations import ai, ai_answer
from operations.models import AIConfigVersion, AIProfile


class Command(BaseCommand):
    help = "Create, test, evaluate and activate an AI version that answers chat questions."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True, help="Developer-admin acting")
        parser.add_argument("--reason", required=True)
        parser.add_argument(
            "--profile",
            type=int,
            action="append",
            required=True,
            help="Primary first, then fallbacks",
        )
        parser.add_argument("--test", action="store_true")
        parser.add_argument(
            "--evaluate", action="store_true", help="Calls the provider (~110 calls)"
        )
        parser.add_argument("--activate", action="store_true")

    def handle(self, *args, username, reason, profile, test, evaluate, activate, **options):
        actor = get_user_model().objects.filter(username=username, is_active=True).first()
        if actor is None:
            raise CommandError("Unknown or inactive user.")
        profiles = [AIProfile.objects.filter(pk=pk).first() for pk in profile]
        if None in profiles:
            raise CommandError("Unknown AI profile.")
        try:
            version = self._matching(profiles) or ai.create_version(
                actor, profiles, f"answer: {reason}", task=AIConfigVersion.Task.ANSWER
            )
            self.stdout.write(f"Version v{version.number} ({version.status})")
            if test or activate:
                version = ai.test_version(actor, version)
                self.stdout.write(
                    f"Connection test: {'passed' if version.test_passed else 'FAILED'}"
                )
            if evaluate:
                version = ai.record_eval(version, ai_answer.run_gate(version))
                for run in version.eval_result.get("runs", []):
                    self.stdout.write(f"  {run['metrics']}")
                self.stdout.write(f"Evaluation: {'passed' if version.eval_passed else 'FAILED'}")
            if activate and version.status != AIConfigVersion.Status.ACTIVE:
                ai.activate(actor, version, reason)
                self.stdout.write(f"Activated v{version.number} for chat answers.")
        except (PermissionDenied, ValidationError) as exc:
            raise CommandError("; ".join(getattr(exc, "messages", [str(exc)]))) from exc

    def _matching(self, profiles):
        wanted = [p.pk for p in profiles]
        for version in AIConfigVersion.objects.filter(task=AIConfigVersion.Task.ANSWER).exclude(
            status=AIConfigVersion.Status.REJECTED
        ):
            if [p.pk for p in version.chain()] == wanted:
                return version
        return None
