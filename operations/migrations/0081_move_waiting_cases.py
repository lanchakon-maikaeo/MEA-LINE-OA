"""ADR 0025: cases waiting for screening when this release arrives go to the owner division's
approval (moved here from 0067, which ran before the columns the current code reads existed).
A fresh database has none, so nothing runs there."""

from django.db import migrations


def forwards(apps, schema_editor):
    Case = apps.get_model("operations", "Case")
    if not Case.objects.filter(status__in=["submitted", "triage"]).exists():
        return
    from operations import approvals  # the current code: needs the schema of 0080

    approvals.move_waiting()


class Migration(migrations.Migration):
    dependencies = [("operations", "0080_slip_tracking")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
