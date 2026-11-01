"""หนึ่งคนหนึ่งบทบาท (ADR 0041, user 1 Nov 2569).

แอดมินผู้พัฒนา ends (every แอดมินระบบ now changes the AI settings), and a person holding several
roles keeps only the highest one; the rest are revoked with the reason, so the history stays.
ผู้ช่วยเหลือระบบ is no role an admin hands out (ADR 0007) and is left alone.
"""

from django.db import migrations
from django.utils import timezone

REASON = "หนึ่งคนหนึ่งบทบาท (ADR 0041)"
# Highest first; the old split duties count as สมาชิกกอง.
RANK = [
    "admin",
    "department_supervisor",
    "supervisor",
    "staff",
    "approver",
    "issuer",
    "receiver",
    "inspector",
    "resolver",
    "department_viewer",
    "division_viewer",
]


def forward(apps, schema_editor):
    RoleGrant = apps.get_model("operations", "RoleGrant")
    StaffProfile = apps.get_model("operations", "StaffProfile")
    now = timezone.now()
    active = RoleGrant.objects.filter(revoked_at__isnull=True)
    active.filter(role="developer_admin").update(revoked_at=now, reason=REASON)
    homes = dict(StaffProfile.objects.values_list("user_id", "division_id"))
    held = {}
    for grant in active.exclude(role="system_support").order_by("pk"):
        held.setdefault(grant.user_id, []).append(grant)
    for user_id, grants in held.items():
        if len(grants) < 2:
            continue
        keep = min(
            grants,
            key=lambda g: (
                RANK.index(g.role) if g.role in RANK else len(RANK),
                g.division_id != homes.get(user_id),
                g.pk,
            ),
        )
        RoleGrant.objects.filter(pk__in=[g.pk for g in grants if g.pk != keep.pk]).update(
            revoked_at=now, reason=REASON
        )


class Migration(migrations.Migration):
    dependencies = [("operations", "0092_role_labels_admin")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
