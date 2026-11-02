"""Proof that this host holds the keys its data was encrypted with (Docs/release/gcp-move).

After a move the database arrives with AI provider keys sealed by AI_SECRET_KEY and roster
e-mails and files sealed by PII_SECRET_KEY. With the wrong keys the rows are there but cannot
be read, which is data lost all the same. This opens every stored value and prints only counts;
it exits 1 when any value does not open. Nothing decrypted is printed or kept.
"""

import sys

from cryptography.fernet import InvalidToken
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand

from operations import ai, sealing
from operations.models import AICredential, Employee, RosterUpload


def check():
    report = {}
    keys = AICredential.objects.exclude(encrypted_key="")
    opened = 0
    for credential in keys.iterator():
        try:
            ai.decrypt_key(credential)
            opened += 1
        except (ImproperlyConfigured, InvalidToken, ValueError):
            pass
    report["ai_keys"] = (opened, keys.count())
    for label, rows in (
        ("roster_emails", Employee.objects.exclude(email_sealed="").values_list("email_sealed")),
        ("roster_files", RosterUpload.objects.exclude(sealed="").values_list("sealed")),
    ):
        tokens = [token for (token,) in rows.iterator()]
        report[label] = (sum(1 for token in tokens if sealing.unseal(token)), len(tokens))
    return report


class Command(BaseCommand):
    help = "Open every stored AI key and sealed roster value with this host's keys (counts only)."

    def handle(self, *args, **options):
        report = check()
        failed = False
        for label, (opened, total) in report.items():
            self.stdout.write(f"{label}: {opened}/{total} open")
            failed |= opened != total
        if failed:
            self.stderr.write(
                "DECRYPT FAILED: this host's AI_SECRET_KEY or PII_SECRET_KEY is wrong"
            )
            sys.exit(1)
        self.stdout.write("decrypt check passed")
