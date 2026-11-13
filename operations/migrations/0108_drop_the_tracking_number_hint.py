"""13 พ.ย. 2569 (owner): the tracking question of a tile no longer says how to type the number.
The sentence was typed into the flows in the editor, so it is taken out of every version."""

from django.db import migrations

HINT = "พิมพ์เลขหรือชื่อตามที่เลือก เลขที่ใบเบิกพิมพ์ / ได้ เช่น 4500012345/10"


def drop(apps, schema_editor):
    for version in apps.get_model("operations", "LineFlowVersion").objects.all():
        nodes = [n for n in (version.graph or {}).get("nodes", []) if HINT in (n.get("text") or "")]
        for node in nodes:
            node["text"] = node["text"].replace(HINT, "").strip()
        if nodes:
            version.save(update_fields=["graph"])


class Migration(migrations.Migration):
    dependencies = [("operations", "0107_line_steps_private_slots_and_via")]
    operations = [migrations.RunPython(drop, migrations.RunPython.noop)]
