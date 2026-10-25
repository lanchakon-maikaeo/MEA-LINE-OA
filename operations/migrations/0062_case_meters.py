from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("operations", "0061_equipment_number_field")]
    operations = [
        migrations.AddField(
            model_name="case",
            name="meters",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
