"""ADR 0054 Q13: the "ไม่อยู่ ถึงวันที่" status is gone; the dates people set go with it. The
duty roster's own exceptions (ลา, สลับเวร) live in DutyShift and stay.

The column itself stays for now: dropping it is a one-way step the release before could not run
against (ADR 0015 Q6), so a later release drops it once this one is in production.
"""

from django.db import migrations


def forward(apps, schema_editor):
    StaffProfile = apps.get_model("operations", "StaffProfile")
    StaffProfile.objects.exclude(away_until=None).update(away_until=None)


class Migration(migrations.Migration):
    dependencies = [("operations", "0104_staffprofile_roster_row")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
