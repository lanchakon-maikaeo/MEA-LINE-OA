import os
import uuid
from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.utils import timezone

from operations import policy, services, workflow
from operations.contracts import Actor, CommandFailed
from operations.management.demo import DemoCommand
from operations.models import (
    AuthorityGrant,
    Case,
    District,
    Division,
    Requester,
    Role,
    RoleGrant,
    StaffProfile,
)

USERNAME = "e2e-meter"
# M10 journeys: one synthetic account per duty, all with the same throwaway password.
ACCOUNTS = {
    USERNAME: ("ผู้ทดสอบกองมิเตอร์", Role.STAFF, "SIM-MTR"),
    "e2e-intake": ("ผู้ทดสอบกองรับเรื่อง", Role.STAFF, "SIM-TRIAGE"),
    "e2e-eqp": ("ผู้ทดสอบกองอุปกรณ์", Role.STAFF, "SIM-EQP"),
    "e2e-approver": ("ผู้ทดสอบผู้อนุมัติ", Role.APPROVER, "SIM-EQP"),
    "e2e-issuer": ("ผู้ทดสอบผู้จ่าย", Role.ISSUER, "SIM-EQP"),
    "e2e-receiver": ("ผู้ทดสอบผู้รับคืน", Role.RECEIVER, "SIM-MTR"),
    "e2e-trf": ("ผู้ทดสอบกองหม้อแปลง", Role.STAFF, "SIM-TRF"),
}
REQUESTER = "ผู้แจ้งทดสอบ E2E"


class Command(DemoCommand):
    help = "Reset the browser-test accounts, synthetic requester and stock (local/CI demo DB)."

    def add_arguments(self, parser):
        parser.add_argument("--password-env", default="E2E_PASSWORD")

    @transaction.atomic
    def handle_demo(self, *args, password_env, **options):
        password = os.environ.get(password_env, "")
        if len(password) < 16:
            raise CommandError(f"Set {password_env} to a random password of 16+ characters.")
        quiet = StringIO()
        call_command("load_districts", stdout=quiet)
        call_command("seed_demo_units", stdout=quiet)
        call_command("seed_demo_cases", stdout=quiet)  # synthetic items (idempotent)
        call_command("seed_demo_policy", stdout=quiet)
        call_command("seed_fake_inventory", stdout=quiet)
        users = {}
        for username, (name, role, code) in ACCOUNTS.items():
            user, _ = get_user_model().objects.get_or_create(username=username)
            user.is_active = True
            user.set_password(password)
            user.save()
            StaffProfile.objects.update_or_create(
                user=user,
                defaults={
                    "display_name": name,
                    "must_change_password": False,
                    "sessions_valid_after": None,
                },
            )
            RoleGrant.objects.filter(user=user, revoked_at__isnull=True).update(
                revoked_at=timezone.now(), reason="e2e reset"
            )
            RoleGrant.objects.create(user=user, role=role, division=Division.objects.get(code=code))
            users[username] = user
        approver = users["e2e-approver"]
        grants = policy.active_grants(approver, AuthorityGrant.Activity.WITHDRAWAL_APPROVAL)
        if not grants.filter(division__code="SIM-EQP").exists():
            AuthorityGrant.objects.create(
                user=approver,
                activity=AuthorityGrant.Activity.WITHDRAWAL_APPROVAL,
                division=Division.objects.get(code="SIM-EQP"),
                valid_from=timezone.now() - timedelta(minutes=1),
                reason="อำนาจจำลองสำหรับ e2e",
                approval_reference="SIM-E2E-M10",
                is_synthetic=True,
            )
        requester, _ = Requester.objects.get_or_create(
            display_name=REQUESTER,
            district=District.objects.get(code="D01"),
            defaults={"approved_by": users["e2e-intake"]},
        )
        stopped = self._stop_leftovers(requester, users)
        self.stdout.write(
            f"E2E accounts ready: {', '.join(ACCOUNTS)}; leftovers stopped: {stopped}."
        )

    def _stop_leftovers(self, requester, users):
        """Earlier runs' cases would pile up in the demo queues (a hazard problem waiting for a
        handoff looks like real work) and compete for the small fake stock (Q8). Staff end them
        as they would: the approver stops outstanding withdrawals, the owner division cancels
        problem cases still open. Returns that arrived wait for inspection and are kept."""
        owners = {
            code: users[name] for name, (_, role, code) in ACCOUNTS.items() if role == Role.STAFF
        }
        stopped = 0
        for case in Case.objects.filter(
            requester=requester, status__in=workflow.OPEN
        ).select_related("owner_division"):
            if case.kind == Case.Kind.WITHDRAWAL:
                numbers = [line.line_no for line in case.lines.all() if line.outstanding > 0]
                if not numbers:
                    continue
                who, action = users["e2e-approver"], "terminate_outstanding"
                payload = {"lines": numbers, "reason": "ล้างข้อมูลทดสอบ e2e รอบก่อน"}
            elif case.kind == Case.Kind.PROBLEM and case.owner_division.code in owners:
                who, action = owners[case.owner_division.code], "cancel"
                payload = {"reason": "ล้างข้อมูลทดสอบ e2e รอบก่อน"}
            else:
                continue
            try:
                services.perform_action(
                    Actor.staff(who),
                    case.pk,
                    action,
                    expected_revision=case.revision,
                    idempotency_key=f"e2e-reset-{uuid.uuid4().hex}",
                    payload=payload,
                )
                stopped += 1
            except CommandFailed:
                continue
        return stopped
