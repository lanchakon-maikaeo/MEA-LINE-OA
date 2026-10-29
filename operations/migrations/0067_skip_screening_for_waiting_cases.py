"""ADR 0025: cases waiting for screening when this release arrives go to the owner division's
approval. A fresh database has none, so nothing runs there."""

from django.db import migrations


def forwards(apps, schema_editor):
    Case = apps.get_model("operations", "Case")
    if not Case.objects.filter(status__in=["submitted", "triage"]).exists():
        return
    from operations import approvals  # the current code: this runs only on a live database

    approvals.move_waiting()


class Migration(migrations.Migration):
    dependencies = [("operations", "0066_kcw_warehouse_calendars_chat")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
