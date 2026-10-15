"""Give a division its official abbreviation (real structure Q7: some codes are provisional).

Policies name divisions by code and are immutable, so the rename and a new policy version with
the new code are installed in one transaction: routing never sees a code that does not exist.
The rule owner's approval is recorded as for any policy activation.
"""

import copy

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from operations import identity, policy
from operations.models import Division, PolicyVersion


def renamed(document, old, new):
    """The same policy with the division code replaced wherever a unit is named."""
    doc = copy.deepcopy(document)
    doc["units"] = {k: (new if v == old else v) for k, v in doc["units"].items()}
    for entry in doc["responsibility"]:
        if entry.get("owner") == old:
            entry["owner"] = new
    for entry in doc.get("sections", []):
        if entry.get("division") == old:
            entry["division"] = new
    return doc


def next_code(code):
    base, _, number = code.partition("+R")
    count = int(number) + 1 if number.isdigit() else 1
    while PolicyVersion.objects.filter(code=f"{base}+R{count}").exists():
        count += 1
    return f"{base}+R{count}"


class Command(BaseCommand):
    help = "Rename a division code and activate the active policy again with the new code."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="old", required=True)
        parser.add_argument("--to", dest="new", required=True)
        parser.add_argument("--username", required=True, help="Admin acting")
        parser.add_argument("--approved-by", default="", help="Rule owner who approved it")
        parser.add_argument("--reference", default="", help="Approval document or reference")
        parser.add_argument("--reason", required=True)

    def handle(self, *args, old, new, username, approved_by, reference, reason, **options):
        actor = get_user_model().objects.filter(username=username, is_active=True).first()
        if actor is None:
            raise CommandError("Unknown or inactive user.")
        new = new.strip()
        try:
            with transaction.atomic():
                identity.require_admin(actor)
                division = Division.objects.select_for_update().filter(code=old).first()
                if division is None:
                    raise ValidationError(f"No division {old}.")
                if not new or Division.objects.filter(code=new).exists():
                    raise ValidationError(f"{new or 'An empty code'} cannot be used.")
                division.code = new
                division.save(update_fields=["code"])
                identity.audit(actor, "division.renamed", division, reason, old=old, new=new)
                current = policy.active_policy()
                if current is not None:
                    document = renamed(current.document, old, new)
                    if document != current.document:
                        document["code"] = next_code(current.code)
                        version = policy.import_policy(document, actor)
                        policy.activate_policy(actor, version, approved_by, reference, reason)
                        self.stdout.write(f"Policy {version.code} active with {new}.")
        except (ValidationError, PermissionDenied) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"{old} → {new}")
