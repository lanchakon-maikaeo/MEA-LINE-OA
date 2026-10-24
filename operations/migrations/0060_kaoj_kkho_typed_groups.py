from django.db import migrations, models

# The กอจ. and กคอ. tiles ask for the equipment first like กคว. (user, 24 Oct 2569): three more
# typed groups with their shared items and fields, and both divisions follow SAP slips.
ITEMS = [
    ("TYPED_TRF", "หม้อแปลง ตามที่แจ้ง", "เครื่อง", "transformer"),
    ("TYPED_LBS", "LBS ตามที่แจ้ง", "ตัว", "lbs"),
    ("TYPED_RMU", "RMU ตามที่แจ้ง", "ชุด", "rmu"),
]
ALL, ITEM_KINDS = ["withdrawal", "return", "problem"], ["withdrawal", "return"]
FIELDS = [
    ("numbers", "หมายเลขเครื่อง", ALL),
    ("size", "ขนาด", ALL),
    ("quantity", "จำนวน", ITEM_KINDS),
]


def seed(apps, schema_editor):
    EquipmentType = apps.get_model("operations", "EquipmentType")
    EntryField = apps.get_model("operations", "EntryField")
    for code, name, unit, group in ITEMS:
        EquipmentType.objects.get_or_create(
            code=code,
            defaults={
                "name": name,
                "category": "transformer",
                "unit": unit,
                "is_serialized": True,
                "line_group": group,
                "entry_group": group,
            },
        )
        for position, (key, label, kinds) in enumerate(FIELDS, 1):
            EntryField.objects.get_or_create(
                group=group,
                key=key,
                defaults={
                    "label": label,
                    "kinds": kinds,
                    "required": True,
                    "position": position,
                    "is_base": True,
                },
            )
    Division = apps.get_model("operations", "Division")
    Division.objects.filter(code__in=["กอจ.", "กองอุปกรณ์ฯ", "กคอ."]).update(requires_sap=True)


class Migration(migrations.Migration):
    dependencies = [("operations", "0059_case_topic_repair")]

    operations = [
        migrations.AlterField(
            model_name="equipmenttype",
            name="line_group",
            field=models.CharField(
                blank=True,
                choices=[
                    ("meter", "มิเตอร์"),
                    ("ct", "CT"),
                    ("vt", "VT"),
                    ("other", "อุปกรณ์อื่น"),
                    ("transformer", "หม้อแปลง"),
                    ("lbs", "LBS"),
                    ("rmu", "RMU"),
                ],
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="equipmenttype",
            name="entry_group",
            field=models.CharField(
                blank=True,
                choices=[
                    ("meter", "มิเตอร์"),
                    ("ct", "CT"),
                    ("vt", "VT"),
                    ("other", "อุปกรณ์อื่น"),
                    ("transformer", "หม้อแปลง"),
                    ("lbs", "LBS"),
                    ("rmu", "RMU"),
                ],
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="entryfield",
            name="group",
            field=models.CharField(
                choices=[
                    ("meter", "มิเตอร์"),
                    ("ct", "CT"),
                    ("vt", "VT"),
                    ("other", "อุปกรณ์อื่น"),
                    ("transformer", "หม้อแปลง"),
                    ("lbs", "LBS"),
                    ("rmu", "RMU"),
                ],
                max_length=16,
            ),
        ),
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
