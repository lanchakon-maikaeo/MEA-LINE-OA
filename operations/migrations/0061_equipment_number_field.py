"""หมายเลขอุปกรณ์ (user, 25 Oct 2569 Q7): an optional field of the "other" group for returns and
problem reports, right after the name, so ติดตามสถานะการซ่อม can name the piece when there is
no SAP repair number. Sizes and quantities move one place down unless an admin moved them."""

from django.db import migrations


def seed(apps, schema_editor):
    EntryField = apps.get_model("operations", "EntryField")
    if EntryField.objects.filter(group="other", key="equipment_number").exists():
        return
    for key, before, after in (("quantity", 3, 4), ("size", 2, 3)):
        EntryField.objects.filter(group="other", key=key, position=before).update(position=after)
    EntryField.objects.create(
        group="other",
        key="equipment_number",
        label="หมายเลขอุปกรณ์",
        kinds=["return", "problem"],
        required=False,
        position=2,
        is_base=True,
    )


class Migration(migrations.Migration):
    dependencies = [("operations", "0060_kaoj_kkho_typed_groups")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
