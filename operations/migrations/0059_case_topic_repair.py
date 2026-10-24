from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("operations", "0058_ai_typed_fields")]

    operations = [
        migrations.AlterField(
            model_name="case",
            name="topic",
            field=models.CharField(
                blank=True,
                choices=[
                    ("meter", "มิเตอร์"),
                    ("transformer", "หม้อแปลง"),
                    ("other", "อุปกรณ์อื่น"),
                    ("usage", "การใช้ไฟฟ้า"),
                    ("suggestion", "ข้อเสนอแนะ"),
                    ("inquiry", "สอบถาม/ติดตาม"),
                    ("system", "ระบบงาน"),
                    ("repair", "ติดตามสถานะการซ่อม"),
                ],
                max_length=16,
            ),
        ),
    ]
