from django.db import migrations

OLD = "ลงทะเบียนครั้งแรก: พิมพ์ชื่อ-นามสกุล เลือกเขต แล้วยืนยันด้วยรหัสพนักงาน ไม่มีรหัสกด 'ใช้วิธีอื่น' ให้เจ้าหน้าที่ตรวจแทน"
NEW = (
    "ลงทะเบียนครั้งแรก: เลือก พนักงาน / อื่น ๆ / ไม่ระบุตัวตน "
    "พนักงานยืนยันตัวตนตามทะเบียนแล้วระบบให้สมาชิกกองเมื่อข้อมูลกองชัดเจน "
    "หากต้องรอแอดมินตรวจยังแจ้งปัญหาได้ ผู้เลือกอื่น ๆ ให้ชื่อแล้วแจ้งปัญหาหรือข้อเสนอแนะได้ทันที "
    "ผู้ไม่ระบุตัวตนซ่อนชื่อจากเจ้าหน้าที่และติดตามเรื่องผ่าน LINE เดิมได้"
)


def update_default(apps, schema_editor):
    # Update only the old bundled answer; preserve administrator-written content.
    apps.get_model("operations", "ChatAnswer").objects.filter(key="register", text=OLD).update(
        text=NEW
    )


class Migration(migrations.Migration):
    dependencies = [("operations", "0051_resumable_menu_release")]
    operations = [migrations.RunPython(update_default, migrations.RunPython.noop)]
