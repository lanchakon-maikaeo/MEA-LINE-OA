"""The staff tabs of the LINE menu (16 Oct 2569 Q9, Q13, Q15): งานของกอง for every staff member
who linked LINE, and สั่งงาน for Supervisors. Admins change them on the เมนู LINE page.
"""

from django.db import migrations

WORK = [
    ("งานใหม่ของกอง", "เรื่องที่รอกองทำ", "work_new"),
    ("งานที่มอบให้ฉัน", "เรื่องที่ฉันรับผิดชอบ", "work_mine"),
    ("งานสั่งที่ได้รับ", "งานจาก Supervisor", "work_orders"),
    ("ใกล้หรือเกินกำหนด", "เรื่องและงานสั่ง", "work_due"),
    ("ค้นหาเลขเรื่อง", "เลขเรื่องหรือเลขงาน", "work_find"),
    ("เปิดเว็บ", "ฟอร์มยาวและรายงาน", "open_web"),
]
ORDER = [
    ("สั่งงานใหม่", "ถึงกองหรือคนในกอง", "order_new"),
    ("งานที่ฉันสั่ง", "ดูความคืบหน้า", "order_issued"),
    ("มอบเรื่องที่มีอยู่", "เลือกคนรับผิดชอบ", "order_assign"),
    ("เกินกำหนดทุกกอง", "กองที่ฉันดูแล", "order_late"),
    ("สรุปวันนี้", "เรื่องเข้าและปิดวันนี้", "order_today"),
    ("เปิดเว็บ", "ฟอร์มยาวและรายงาน", "open_web"),
]


def seed(apps, schema_editor):
    Item = apps.get_model("operations", "LineMenuItem")
    for tab, rows in (("work", WORK), ("order", ORDER)):
        if Item.objects.filter(tab=tab).exists():
            continue
        for position, (label, note, action) in enumerate(rows, start=1):
            Item.objects.create(
                tab=tab, place="tile", position=position, label=label, note=note, action=action
            )


def unseed(apps, schema_editor):
    apps.get_model("operations", "LineMenuItem").objects.filter(tab__in=["work", "order"]).delete()


class Migration(migrations.Migration):
    dependencies = [("operations", "0044_staff_menu_tabs")]

    operations = [migrations.RunPython(seed, unseed)]
