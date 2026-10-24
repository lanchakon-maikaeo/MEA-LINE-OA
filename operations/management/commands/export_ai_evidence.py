import json
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import CommandError

from operations import ai_evidence
from operations.management.demo import DemoCommand
from operations.models import AIConfigVersion


class Command(DemoCommand):
    help = (
        "Sign the current passing evaluation of an extraction version so a machine that cannot "
        "evaluate (production) can import it. Run on the code that machine runs."
    )

    def add_arguments(self, parser):
        parser.add_argument("--number", type=int, required=True, help="Version number, e.g. 16")
        parser.add_argument("--key", required=True, help="Private key from make_ai_evidence_key")
        parser.add_argument("--out", required=True, help="Evidence file to write")

    def handle_demo(self, *args, number, key, out, **options):
        found = AIConfigVersion.objects.filter(number=number).first()
        if found is None:
            raise CommandError(f"No version v{number}.")
        try:
            data = ai_evidence.export(found, Path(key).expanduser().read_bytes())
        except (OSError, ValueError, ValidationError) as exc:
            raise CommandError("; ".join(getattr(exc, "messages", [str(exc)]))) from exc
        Path(out).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        bundle = data["bundle"]
        self.stdout.write(
            f"v{number} evidence signed ({data['key_id']}): code {bundle['source_digest'][:12]}, "
            f"commit {bundle['commit'] or 'unknown'}, "
            f"config {bundle['portable_digest'][:12]} → {out}"
        )
