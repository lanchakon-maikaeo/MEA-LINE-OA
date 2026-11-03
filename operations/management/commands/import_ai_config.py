import os

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from operations import ai, ai_eval, ai_release
from operations.models import AIConfigVersion, AICredential, AIProfile

CREDENTIAL = "thaillm"


class Command(BaseCommand):
    help = (
        "Import THAILLM_* settings from the environment as a candidate AI config version, "
        "with the same developer-admin checks, test, evaluation and activation as the web page. "
        "--provider gemini saves GOOGLE_GEMINI_API_KEY and can test and switch to an imported "
        "release (ADR 0038: releases are made and evaluated on the Mac only)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True, help="Developer-admin acting")
        parser.add_argument("--reason", required=True)
        parser.add_argument("--provider", choices=("thaillm", "gemini"), default="thaillm")
        parser.add_argument("--release", help="With --provider gemini: release label to test/use")
        parser.add_argument("--test", action="store_true")
        parser.add_argument("--evaluate", action="store_true", help="Run the held-out evaluation")
        parser.add_argument("--activate", action="store_true")
        parser.add_argument("--few-shot", type=int, default=0, help="Retrieved examples (0-5)")
        parser.add_argument("--pipeline", choices=AIConfigVersion.Pipeline.values, default="single")
        parser.add_argument(
            "--typed-fields",
            action="store_true",
            help="Also read SAP slip numbers and the typed rows of มิเตอร์/CT/VT",
        )

    def handle(
        self,
        *args,
        username,
        reason,
        test,
        evaluate,
        activate,
        few_shot,
        pipeline,
        typed_fields=False,
        provider="thaillm",
        release=None,
        **options,
    ):
        actor = get_user_model().objects.filter(username=username, is_active=True).first()
        if actor is None:
            raise CommandError("Unknown or inactive user.")
        if provider == "gemini":
            if evaluate:
                raise CommandError("Evaluate a release on the Mac (admin page), not here.")
            try:
                return self._gemini(actor, reason, release, test, activate)
            except (PermissionDenied, ValidationError) as exc:
                raise CommandError(str(exc)) from exc
        env = os.environ
        models = [env.get("THAILLM_MODEL", "").strip()]
        models += [
            m.strip() for m in env.get("THAILLM_FALLBACK_MODELS", "").split(",") if m.strip()
        ]
        if not models[0] or not env.get("THAILLM_API_KEY") or not env.get("THAILLM_BASE_URL"):
            raise CommandError("Set THAILLM_BASE_URL, THAILLM_API_KEY and THAILLM_MODEL.")
        try:
            credential = AICredential.objects.filter(name=CREDENTIAL).first()
            same_key = (
                credential is not None
                and ai.decrypt_key(credential) == env["THAILLM_API_KEY"].strip()
            )
            if (
                credential is None
                or not same_key
                or credential.base_url != env["THAILLM_BASE_URL"].rstrip("/")
            ):
                credential = ai.save_credential(
                    actor, CREDENTIAL, env["THAILLM_BASE_URL"], env["THAILLM_API_KEY"], reason
                )
            profiles = []
            for model_id in models[: ai.MAX_CHAIN]:
                existing = (
                    AIProfile.objects.filter(credential=credential, model_id=model_id)
                    .order_by("-pk")
                    .first()
                )
                profiles.append(existing or ai.create_profile(actor, credential, model_id))
            version = self._matching(
                profiles, few_shot, pipeline, typed_fields
            ) or ai.create_version(
                actor, profiles, f"import: {reason}", few_shot, pipeline, typed_fields=typed_fields
            )
            self.stdout.write(
                f"Version v{version.number} ({version.status}): {' -> '.join(models)}"
            )
            if test or activate:
                version = ai.test_version(actor, version)
                self.stdout.write(
                    f"Connection test: {'passed' if version.test_passed else 'FAILED'}"
                )
            if evaluate:
                version = ai.record_eval(version, ai_eval.run_gate(version))
                self.stdout.write(f"Evaluation: {'passed' if version.eval_passed else 'FAILED'}")
            if activate and version.status != AIConfigVersion.Status.ACTIVE:
                ai.activate(actor, version, reason)
                self.stdout.write(f"Activated v{version.number}.")
            elif activate:
                self.stdout.write(f"v{version.number} is already active.")
        except (PermissionDenied, ValidationError) as exc:
            raise CommandError(str(exc)) from exc

    def _gemini(self, actor, reason, label, test, activate):
        key = os.environ.get("GOOGLE_GEMINI_API_KEY", "").strip()
        if not key:
            raise CommandError("Set GOOGLE_GEMINI_API_KEY.")
        credential = AICredential.objects.filter(name="gemini").first()
        if credential is None:
            raise CommandError("No gemini provider; run migrate first.")
        if not credential.has_key or ai.decrypt_key(credential) != key:
            ai.save_credential(actor, "gemini", credential.base_url, key, reason)
            self.stdout.write("Gemini key saved.")
        else:
            self.stdout.write("Gemini key unchanged.")
        if not (test or activate):
            return
        if not label or not ai_release.members(label):
            raise CommandError("Give --release with a label imported by import_ai_evidence.")
        if not ai_release.test(actor, label):
            raise CommandError(f"Connection test of {label} FAILED.")
        self.stdout.write(f"Connection test of {label}: passed")
        if activate:
            tasks = [t for t, v in ai_release.members(label).items() if not ai_release.not_ready(v)]
            ai_release.activate(actor, label, tasks, reason)
            self.stdout.write(f"Using release {label} for: {', '.join(sorted(tasks))}")

    def _matching(self, profiles, few_shot, pipeline, typed_fields=False):
        """Reuse the newest non-rejected version with exactly this chain instead of duplicating."""
        wanted = [p.pk for p in profiles]
        for version in AIConfigVersion.objects.exclude(status=AIConfigVersion.Status.REJECTED):
            if (
                version.few_shot == few_shot
                and version.pipeline == pipeline
                and version.typed_fields == bool(typed_fields)
                and [p.pk for p in version.chain()] == wanted
            ):
                return version
        return None
