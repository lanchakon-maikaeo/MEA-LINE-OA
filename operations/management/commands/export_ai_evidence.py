import json
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import CommandError

from operations import ai_evidence
from operations.management.demo import DemoCommand
from operations.models import AIConfigVersion


class Command(DemoCommand):
    help = (
        "Sign the current passing evaluation of a release (--release 2026.10.31.1, ADR 0038) or "
        "of an earlier extraction version (--number) so a machine that cannot evaluate "
        "(production) can import it. Run on the code that machine runs."
    )

    def add_arguments(self, parser):
        which = parser.add_mutually_exclusive_group(required=True)
        which.add_argument("--release", help="Release label, e.g. 2026.10.31.1")
        which.add_argument("--number", type=int, help="Earlier extraction version, e.g. 16")
        parser.add_argument("--key", required=True, help="Private key from make_ai_evidence_key")
        parser.add_argument("--out", required=True, help="Evidence file to write")

    def handle_demo(self, *args, release=None, number=None, key, out, **options):
        try:
            private = Path(key).expanduser().read_bytes()
            if release:
                data = ai_evidence.export_release(release, private)
            else:
                found = AIConfigVersion.objects.filter(number=number).first()
                if found is None:
                    raise CommandError(f"No version v{number}.")
                data = ai_evidence.export(found, private)
        except (OSError, ValueError, ValidationError) as exc:
            raise CommandError("; ".join(getattr(exc, "messages", [str(exc)]))) from exc
        Path(out).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        bundle = data["bundle"]
        what = release or f"v{number}"
        config = "" if release else f", config {bundle['portable_digest'][:12]}"
        self.stdout.write(
            f"{what} evidence signed ({data['key_id']}): code {bundle['source_digest'][:12]}, "
            f"commit {bundle['commit'] or 'unknown'}{config} → {out}"
        )
