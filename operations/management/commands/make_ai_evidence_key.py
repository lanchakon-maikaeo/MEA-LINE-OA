import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from operations import ai_evidence


class Command(BaseCommand):
    help = (
        "Make the Ed25519 key that signs AI evaluation evidence on the evaluating machine. The "
        "private key stays in --out (mode 600); put the printed public key in "
        "AI_EVIDENCE_PUBLIC_KEYS on the machines that import evidence."
    )

    def add_arguments(self, parser):
        parser.add_argument("--out", required=True, help="New file for the private key")

    def handle(self, *args, out, **options):
        path = Path(out).expanduser()
        if path.exists():
            raise CommandError(f"{path} exists; a key is never overwritten.")
        path.parent.mkdir(parents=True, exist_ok=True)
        pem, public = ai_evidence.new_private_key()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(pem)
        self.stdout.write(f"Private key: {path}")
        self.stdout.write(f"AI_EVIDENCE_PUBLIC_KEYS={public}")
        self.stdout.write(f"key id {ai_evidence.key_id(public)}")
