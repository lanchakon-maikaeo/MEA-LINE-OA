"""Synthetic subjects for `faoj load-test` (E.9 load baseline, M13): approved LINE requesters with
made-up LINE IDs and one staff login whose password comes from the environment. Refused in
production: load runs on staging or a rehearsal, never on live accounts. Re-running reuses them.
Prints the LINE IDs and the staff username as JSON for k6; never the password.
"""

import hashlib
import json
import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count

from operations.models import (
    Case,
    ChannelIdentity,
    District,
    Division,
    Requester,
    Role,
    RoleGrant,
    StaffProfile,
)

USERNAME = "load-staff"
NAME = "ผู้ทดสอบโหลด {n:03d}"


def line_id(n):
    return "U" + hashlib.sha256(f"faoj-load-{n}".encode()).hexdigest()[:32]


class Command(BaseCommand):
    help = "Synthetic LINE requesters and a staff login for the load test (refused in production)."

    def add_arguments(self, parser):
        parser.add_argument("--requesters", type=int, default=50)
        parser.add_argument("--password-env", default="LOAD_PASSWORD")
        parser.add_argument(
            "--division", help="Division code for the staff login (default: busiest)"
        )

    @transaction.atomic
    def handle(self, *args, requesters, password_env, division=None, **options):
        if settings.APP_ENV not in {"staging", "local", "ci"}:
            raise CommandError("Load-test subjects are refused in production.")
        password = os.environ.get(password_env, "")
        if len(password) < 16:
            raise CommandError(f"Set {password_env} to a random password of 16+ characters.")
        owner = (
            Division.objects.filter(code=division).first()
            if division
            else Division.objects.filter(
                pk__in=Case.objects.values("owner_division")
                .annotate(n=Count("id"))
                .order_by("-n")
                .values("owner_division")[:1]
            ).first()
            or Division.objects.filter(is_active=True).order_by("code").first()
        )
        if owner is None:
            raise CommandError("No division to give the staff login (load the org first).")
        user, _ = get_user_model().objects.get_or_create(username=USERNAME)
        user.is_active = True
        user.set_password(password)
        user.save()
        StaffProfile.objects.update_or_create(
            user=user,
            defaults={"display_name": "ผู้ทดสอบโหลด", "must_change_password": False},
        )
        RoleGrant.objects.get_or_create(
            user=user, role=Role.STAFF, division=owner, revoked_at__isnull=True
        )
        districts = list(District.objects.order_by("code"))
        ids = []
        for n in range(1, max(requesters, 0) + 1):
            identity = ChannelIdentity.objects.filter(
                channel="line", external_id=line_id(n)
            ).first()
            if identity is None:
                requester = Requester.objects.create(
                    display_name=NAME.format(n=n),
                    district=districts[(n - 1) % len(districts)],
                    approved_by=user,
                )
                ChannelIdentity.objects.create(
                    channel="line",
                    external_id=line_id(n),
                    requester=requester,
                    status=ChannelIdentity.Status.ACTIVE,
                )
            ids.append(line_id(n))
        self.stdout.write(
            json.dumps({"registered": ids, "staff": USERNAME, "division": owner.code})
        )
