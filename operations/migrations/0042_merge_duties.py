"""สมาชิกกองกับ Supervisor (16 Oct 2569 Q3, Q4): the old duties merge into สมาชิกกอง.

Every member of a division now does every step, so the old duties (ผู้อนุมัติ ผู้จ่าย ผู้รับคืน
ผู้ตรวจสภาพ ผู้แก้ปัญหา) merge into สมาชิกกอง: each person keeps one membership per division and
the duty rows are revoked with a reason. Supervisor ของกอง stays. Each membership gets the
authority rows it brings (member_of), which count only while the work is not split. A separate
migration from 0041 because PostgreSQL builds 0041's index only with no pending row events.
"""

from django.db import migrations
from django.utils import timezone

DUTIES = ("approver", "issuer", "receiver", "inspector", "resolver")
DIVISION_AUTHORITY = (
    "withdrawal_approval",
    "allocation",
    "condition_certification",
    "priority_exception",
)
MERGED = "รวมหน้าที่ย่อยเป็นสมาชิกกอง (16 ต.ค. 2569)"


def merge_duties(apps, schema_editor):
    RoleGrant = apps.get_model("operations", "RoleGrant")
    AuthorityGrant = apps.get_model("operations", "AuthorityGrant")
    Division = apps.get_model("operations", "Division")
    AuditEvent = apps.get_model("operations", "AuditEvent")
    now = timezone.now()
    active = RoleGrant.objects.filter(revoked_at__isnull=True, division__isnull=False)
    for grant in active.filter(role__in=DUTIES).order_by("pk"):
        if not active.filter(
            user_id=grant.user_id, division_id=grant.division_id, role__in=("staff", "supervisor")
        ).exists():
            RoleGrant.objects.create(
                user_id=grant.user_id, division_id=grant.division_id, role="staff", reason=MERGED
            )
        grant.revoked_at, grant.reason = now, MERGED
        grant.save(update_fields=["revoked_at", "reason"])
        AuditEvent.objects.create(
            action="role.revoked",
            target_type="User",
            target_id=str(grant.user_id),
            reason=MERGED,
            data={"role": grant.role, "division_id": grant.division_id, "merged_into": "staff"},
        )
    pairs = set(active.values_list("user_id", "division_id"))
    for user_id, division_id in sorted(pairs):
        division = Division.objects.get(pk=division_id)
        wanted = (
            [("routing_decision", None)]
            if division.kind == "coordination"
            else [(activity, division_id) for activity in DIVISION_AUTHORITY]
        )
        for activity, scope in wanted:
            if AuthorityGrant.objects.filter(
                user_id=user_id,
                member_of_id=division_id,
                activity=activity,
                division_id=scope,
                revoked_at__isnull=True,
            ).exists():
                continue
            AuthorityGrant.objects.create(
                user_id=user_id,
                activity=activity,
                division_id=scope,
                member_of_id=division_id,
                valid_from=now,
                reason=f"มาพร้อมการเป็นสมาชิก {division.code}",
                approval_reference="สมาชิกกอง",
            )


class Migration(migrations.Migration):
    dependencies = [("operations", "0041_division_members")]

    operations = [migrations.RunPython(merge_duties, migrations.RunPython.noop)]
