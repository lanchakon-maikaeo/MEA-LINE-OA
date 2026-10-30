"""ADR 0036 (grill กคว. นัดโน้ตบุ๊ก 30 ต.ค. 2569 Q8, Q22): a calendar may have times that are
free on every working day; โน้ตบุ๊กและหัวอ่าน gets 09:00 to 15:00 on the hour, lunch left out."""

from django.db import migrations, models

NOTEBOOK_TIMES = ["09:00", "10:00", "11:00", "13:00", "14:00", "15:00"]


def notebook_times(apps, schema_editor):
    Calendar = apps.get_model("operations", "Calendar")
    Calendar.objects.filter(key="notebook").update(default_times=NOTEBOOK_TIMES)


class Migration(migrations.Migration):
    dependencies = [("operations", "0082_sheet_editors")]

    operations = [
        migrations.AddField(
            model_name="calendar",
            name="default_times",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="calendar",
            name="default_minutes",
            field=models.PositiveSmallIntegerField(default=60),
        ),
        migrations.AddField(
            model_name="calendar",
            name="default_capacity",
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.RunPython(notebook_times, migrations.RunPython.noop),
    ]
