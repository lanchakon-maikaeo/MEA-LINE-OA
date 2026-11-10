"""ADR 0054 Q9, Q2: every group of people on the Push Message page gets an email switch of its
own beside its LINE switch.

Before this one switch per group served both channels. So that nothing changes on the day of the
release, each group's new email switch takes the value its old switch holds. Then Q2: a new case
reaches the owner กอง's แอดมินกอง by LINE and by email, and the แอดมินฝ่าย on duty by email too,
even where somebody had switched those off.
"""

from django.db import migrations

REASON = "ADR 0054 Q9: อีเมลของผู้รับแต่ละกลุ่มแยกจาก LINE ค่าเริ่มเท่ากับสวิตช์เดิมของกลุ่มนั้น"
REASON_Q2 = "ADR 0054 Q2: เรื่องใหม่ส่งถึงแอดมินกองของกองเจ้าภาพทั้ง LINE และอีเมล และอีเมลถึงแอดมินเวร"
GROUPS = ("duty", "assignee", "division_admin", "people")
Q2_ON = (
    "push:staff_new_case",
    "push:staff_new_case:email",
    "push:staff_new_case:division_admin",
    "push:staff_new_case:division_admin:email",
    "push:staff_new_case:duty:email",
)


def forward(apps, schema_editor):
    SystemOption = apps.get_model("operations", "SystemOption")
    for row in SystemOption.objects.filter(key__startswith="push:"):
        parts = row.key.split(":")
        if len(parts) == 3 and parts[2] in GROUPS:
            SystemOption.objects.get_or_create(
                key=f"{row.key}:email", defaults={"value": row.value, "reason": REASON}
            )
    for key in Q2_ON:
        SystemOption.objects.update_or_create(
            key=key, defaults={"value": "on", "reason": REASON_Q2, "updated_by": None}
        )


class Migration(migrations.Migration):
    dependencies = [("operations", "0105_staff_away_retired")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
