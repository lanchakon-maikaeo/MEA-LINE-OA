"""ADR 0059: with OTP off, an employee ID matching the roster is how the employee proved it."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0109_case_first_accepted_at"),
    ]

    operations = [
        migrations.AlterField(
            model_name="registrationrequest",
            name="verified_by",
            field=models.CharField(
                blank=True,
                choices=[
                    ("otp", "รหัส OTP ทางอีเมล"),
                    ("email", "พิมพ์อีเมลตรงทะเบียน"),
                    ("proof", "ข้อมูลยืนยัน"),
                    ("employee_id", "รหัสพนักงานตรงทะเบียน"),
                ],
                max_length=16,
            ),
        ),
    ]
