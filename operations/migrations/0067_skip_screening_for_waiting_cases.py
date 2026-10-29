"""ADR 0025: cases waiting for screening go to the owner division's approval.

The move calls the current code, which reads columns added up to 0080, so it runs in 0081 after
the schema is complete. On 29 Oct 2569 running it here failed on production (unit_work_id)."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("operations", "0066_kcw_warehouse_calendars_chat")]

    operations = [migrations.RunPython(migrations.RunPython.noop, migrations.RunPython.noop)]
