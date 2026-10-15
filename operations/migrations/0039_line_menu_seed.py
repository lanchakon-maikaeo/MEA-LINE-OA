"""The LINE menu as the owner listed it on 15 ต.ค. 2569; admins change it later on its page."""

from django.db import migrations

TOPICS = [
    # (label, note, division codes, choices: (label, action, kind, topic))
    (
        "Meter, CT, VT",
        "กคว. · ติดตามใบเบิก ส่งคืน สถานะการซ่อม",
        ["กคว."],
        [
            ("ติดตามใบเบิก", "track", "withdrawal", ""),
            ("ส่งคืน", "return", "", ""),
            ("สถานะการซ่อม", "track", "return", ""),
        ],
    ),
    (
        "หม้อแปลง, LBS, RMU",
        "กองอุปกรณ์ · ติดตามใบเบิก ส่งคืน สถานะการซ่อม",
        ["กองอุปกรณ์ฯ"],
        [
            ("ติดตามใบเบิก", "track", "withdrawal", ""),
            ("ส่งคืน", "return", "", ""),
            ("สถานะการซ่อม", "track", "return", ""),
        ],
    ),
    (
        "เครื่องมือและอุปกรณ์",
        "กคอ. · เครื่องมือ เครื่องใช้ อุปกรณ์",
        ["กคอ.", "กองเครื่องมือฯ"],
        [
            ("ติดตามใบเบิก", "track", "withdrawal", ""),
            ("ส่งคืน", "return", "", ""),
            ("สถานะการซ่อม", "track", "return", ""),
        ],
    ),
    (
        "ระบบ SMG, AMR",
        "ศอค. · ปัญหาการใช้งาน การจัดทำบิล การตัด-ต่อทางไกล",
        ["ศอค."],
        [
            ("ปัญหาการใช้งาน", "problem", "", "system"),
            ("การจัดทำบิล", "problem", "", "system"),
            ("การตัด-ต่อทางไกล", "problem", "", "system"),
        ],
    ),
    (
        "ตรวจสอบการใช้ไฟฟ้า",
        "กตฟ. · ติดตามสถานะ แจ้งพบเหตุการณ์ละเมิด",
        ["กตฟ."],
        [
            ("ติดตามสถานะ", "track", "", ""),
            ("แจ้งพบการละเมิด", "problem", "", "usage"),
        ],
    ),
    ("แจ้งปัญหา/ข้อเสนอแนะ", "ทุกกอง/ศูนย์", [], []),
]
BAR = [("เรื่องของฉัน", "mine"), ("ติดตามเลขเรื่อง", "find")]


def seed(apps, schema_editor):
    Item = apps.get_model("operations", "LineMenuItem")
    Division = apps.get_model("operations", "Division")
    if Item.objects.exists():
        return
    for position, (label, note, codes, choices) in enumerate(TOPICS, start=1):
        division = Division.objects.filter(code__in=codes, is_synthetic=False).first()
        tile = Item.objects.create(
            place="tile",
            position=position,
            label=label,
            note=note,
            action="topic" if choices else "general",
            division=division,
        )
        for n, (choice, action, kind, topic) in enumerate(choices, start=1):
            Item.objects.create(
                parent=tile,
                place="choice",
                position=n,
                label=choice,
                action=action,
                division=division,
                kind=kind,
                topic=topic,
            )
    for position, (label, action) in enumerate(BAR, start=1):
        Item.objects.create(place="bar", position=position, label=label, action=action)


def unseed(apps, schema_editor):
    apps.get_model("operations", "LineMenuItem").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("operations", "0038_line_menu")]

    operations = [migrations.RunPython(seed, unseed)]
