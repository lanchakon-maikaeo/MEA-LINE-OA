from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("operations", "0057_typed_lines")]

    operations = [
        migrations.AddField(
            model_name="aiconfigversion",
            name="typed_fields",
            field=models.BooleanField(db_default=False, default=False),
        ),
    ]
