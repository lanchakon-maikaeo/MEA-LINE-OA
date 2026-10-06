import os

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.utils import timezone

from operations.management.demo import DemoCommand
from operations.models import Division, Role, RoleGrant, StaffProfile

USERNAME = "e2e-meter"


class Command(DemoCommand):
    help = "Reset the browser-test account (SIM-MTR staff only) in the local/CI demo DB."

    def add_arguments(self, parser):
        parser.add_argument("--password-env", default="E2E_PASSWORD")

    @transaction.atomic
    def handle_demo(self, *args, password_env, **options):
        password = os.environ.get(password_env, "")
        if len(password) < 16:
            raise CommandError(f"Set {password_env} to a random password of 16+ characters.")
        call_command("load_districts", stdout=self.stdout)
        call_command("seed_demo_units", stdout=self.stdout)
        user, _ = get_user_model().objects.get_or_create(username=USERNAME)
        user.is_active = True
        user.set_password(password)
        user.save()
        StaffProfile.objects.update_or_create(
            user=user,
            defaults={
                "display_name": "ผู้ทดสอบกองมิเตอร์",
                "must_change_password": False,
                "sessions_valid_after": None,
            },
        )
        RoleGrant.objects.filter(user=user, revoked_at__isnull=True).update(
            revoked_at=timezone.now(), reason="e2e reset"
        )
        RoleGrant.objects.create(
            user=user, role=Role.STAFF, division=Division.objects.get(code="SIM-MTR")
        )
        self.stdout.write(f"E2E account {USERNAME} ready.")
