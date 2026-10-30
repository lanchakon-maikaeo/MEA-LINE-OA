"""ADR 0034: a sheet may be searched in several number columns and carry a phone, and ซจ.'s
repair sheet is there from the start (grill กอจ. 29 Oct 2569 Q22 to Q26)."""

from django.db import migrations, models

SJ_SHEET = "1gc79oBgvqq_ZJ4oB5NajkeT9f8294Dg2WtK75kCTLqM"  # pragma: allowlist secret


def add_sj_sheet(apps, schema_editor):
    SheetSource = apps.get_model("operations", "SheetSource")
    if SheetSource.objects.filter(key="koj-switch-repairs").exists():
        return
    SheetSource.objects.create(
        key="koj-switch-repairs",
        name="งานซ่อมสวิตช์ตัดตอน ซจ.",
        enabled=True,
        spreadsheet_id=SJ_SHEET,
        sheet_name="",  # the first tab
        key_column="E,K",  # หนังสือแจ้งเลขที่, WO/WBS/เลขรับ/ใบแจ้งซ่อม
        status_column="AF",  # สถานะงาน
        first_row=2,
        match="code",
        number_label="เลขงานซ่อม",
        done_column="AE",  # ยกเลิก
        done_words={"*": "ยกเลิกงานแล้ว"},
        extra_columns=[
            {"column": "R", "label": "สรุปลักษณะงาน"},
            {"column": "G", "label": "วันที่ ซจ. รับงาน"},
            {"column": "M", "label": "วันที่นัดหมาย"},
            {"column": "Z", "label": "วันที่ปฏิบัติงาน"},
        ],
        ai_words=False,  # Q23: the program's own lines
    )


class Migration(migrations.Migration):
    dependencies = [("operations", "0081_move_waiting_cases")]

    operations = [
        migrations.AlterField(
            model_name="sheetsource",
            name="key_column",
            field=models.CharField(default="J", max_length=20),
        ),
        migrations.AddField(
            model_name="sheetsource",
            name="contact",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.RunPython(add_sj_sheet, migrations.RunPython.noop),
    ]
