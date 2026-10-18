"""สมาชิกกองกับ Supervisor (16 Oct 2569 Q3–Q5).

The roles: สมาชิกกอง, Supervisor ของกอง and Supervisor ระดับฝ่าย, and the link from an authority
row to the membership that brings it (member_of). 0042 merges the old duties.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0040_ai_keywords"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="deadlinepause",
            name="kind",
            field=models.CharField(
                choices=[
                    ("awaiting_info", "รอข้อมูลจากเขต"),
                    ("supervisor", "Supervisor อนุมัติพัก"),
                ],
                max_length=16,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="rolegrant",
            name="rolegrant_scope_matches_role",
        ),
        migrations.AddField(
            model_name="authoritygrant",
            name="member_of",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="operations.division",
            ),
        ),
        migrations.AlterField(
            model_name="rolegrant",
            name="role",
            field=models.CharField(
                choices=[
                    ("admin", "แอดมินระบบ"),
                    ("developer_admin", "แอดมินผู้พัฒนา"),
                    ("system_support", "ผู้ช่วยเหลือระบบ"),
                    ("department_supervisor", "Supervisor ระดับฝ่าย"),
                    ("staff", "สมาชิกกอง"),
                    ("approver", "ผู้อนุมัติ"),
                    ("issuer", "ผู้จ่าย"),
                    ("receiver", "ผู้รับคืน"),
                    ("inspector", "ผู้ตรวจสภาพ"),
                    ("resolver", "ผู้แก้ปัญหา"),
                    ("supervisor", "Supervisor ของกอง"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="rolegrant",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("division__isnull", True),
                        (
                            "role__in",
                            [
                                "admin",
                                "department_supervisor",
                                "developer_admin",
                                "system_support",
                            ],
                        ),
                    ),
                    models.Q(
                        models.Q(
                            (
                                "role__in",
                                [
                                    "admin",
                                    "department_supervisor",
                                    "developer_admin",
                                    "system_support",
                                ],
                            ),
                            _negated=True,
                        ),
                        ("division__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="rolegrant_scope_matches_role",
            ),
        ),
    ]
