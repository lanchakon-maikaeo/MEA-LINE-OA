from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("operations", "0062_case_meters")]
    operations = [
        migrations.AddField(
            model_name="aiconfigversion",
            name="menu_fields",
            field=models.BooleanField(db_default=False, default=False),
        ),
    ]
