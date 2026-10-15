"""Reference data for staging (M12 Q10; E.3: staging runs on synthetic data).

Staging runs on the real ฝอจ. structure and its FAJO-2 policy (real structure Q8 a, ADR 0008)
with synthetic items, requesters, example cases and inventory. The demo commands guard their
writes to a local/CI demo database; staging has its own database name and host, so this runs
their imports directly. Production refuses it, and nothing is reset or deleted: every import is
an upsert.
"""

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from operations import policy
from operations.management.commands import (
    load_ai_examples,
    seed_demo_cases,
    seed_fake_inventory,
    seed_org_demo,
)
from operations.models import PolicyVersion

DEMO_IMPORTS = (seed_demo_cases, seed_fake_inventory)


class Command(BaseCommand):
    help = "Load the real units and FAJO-2 policy with synthetic items, cases and inventory."

    def add_arguments(self, parser):
        parser.add_argument("--with-ai-examples", action="store_true")

    def handle(self, *args, with_ai_examples, **options):
        if settings.APP_ENV not in {"staging", "local", "ci"}:
            raise CommandError("Synthetic seed data is refused in production.")
        call_command("load_districts", stdout=self.stdout)
        call_command("load_org", stdout=self.stdout)
        version = policy.import_policy(policy.load_document(seed_org_demo.REAL_POLICY))
        if version.status != PolicyVersion.Status.ACTIVE:
            policy.activate_policy(
                None,
                version,
                "ข้อมูลทดสอบ staging (รอเจ้าของกติการับรองค่าจริง)",
                "STAGING-SEED / ADR 0008",
                "seed staging",
            )
        modules = DEMO_IMPORTS + ((load_ai_examples,) if with_ai_examples else ())
        for module in modules:
            command = module.Command(stdout=self.stdout, stderr=self.stderr)
            parsed = command.create_parser("manage.py", module.__name__.rsplit(".", 1)[-1])
            command.handle_demo(**vars(parsed.parse_args([])))
