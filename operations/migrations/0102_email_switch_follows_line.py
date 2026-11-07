"""ADR 0050 Q11: the email switch of a Push Message kind no longer waits for its LINE switch.

Before this, a kind whose LINE switch was off sent no email either. So that nothing starts
sending on the day of the release, a kind whose LINE switch is off gets its email switch turned
off too; an admin turns email back on kind by kind.
"""

from django.db import migrations

REASON = "ADR 0050 Q11: อีเมลแยกจาก LINE แล้ว ชนิดที่ปิด LINE ไว้จึงปิดอีเมลด้วย"
KINDS = (
    "staff_new_case",
    "staff_assigned",
    "staff_assignment_returned",
    "staff_hazard",
    "staff_routing",
    "staff_escalated",
    "staff_handoff",
    "staff_child_work",
    "staff_dispute",
    "staff_reopen",
    "staff_admin_waiting",
    "staff_registration",
    "staff_order_new",
    "staff_order_ack",
    "staff_order_done",
    "staff_order_back",
    "staff_order_soon",
    "staff_order_late",
)


def forward(apps, schema_editor):
    SystemOption = apps.get_model("operations", "SystemOption")
    off = set(
        SystemOption.objects.filter(key__in=[f"push:{k}" for k in KINDS], value="off").values_list(
            "key", flat=True
        )
    )
    for key in off:
        SystemOption.objects.update_or_create(
            key=f"{key}:email", defaults={"value": "off", "reason": REASON}
        )


class Migration(migrations.Migration):
    dependencies = [("operations", "0101_case_acceptance")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
