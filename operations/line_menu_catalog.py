"""The service menu agreed by the owner on 16 October 2026; no browser editing step.

23 Oct 2569: the กคว. tile asks for the equipment first (มิเตอร์ / CT / VT / อุปกรณ์อื่น), then
ติดตามใบเบิก / ติดตามใบส่งคืน / แจ้งปัญหา (conversation._action_card); the other tiles stay."""

from django.core.exceptions import ValidationError
from django.db import transaction

from operations.models import Division, LineMenuItem

A, P, T = LineMenuItem.Action, LineMenuItem.Place, LineMenuItem.Tab
TOPICS = [
    (
        "Meter, CT, VT",
        "กคว.",
        "meter",
        [
            ("มิเตอร์", A.EQUIPMENT, "meter"),
            ("CT", A.EQUIPMENT, "ct"),
            ("VT", A.EQUIPMENT, "vt"),
            ("อุปกรณ์อื่น", A.EQUIPMENT, "other"),
            ("ติดตามเรื่อง", A.TRACK),
        ],
    ),
    ("หม้อแปลง LBS RMU", "กอจ.", "transformer", []),
    ("เครื่องมือและอุปกรณ์", "กคอ.", "other", []),
    (
        "ระบบ SMG, AMR",
        "ศอค.",
        "system",
        [
            ("ปัญหา SMG-AMR", A.PROBLEM),
            ("ปัญหาการจัดทำบิล", A.PROBLEM),
            ("ปัญหาตัดต่อทางไกล", A.PROBLEM),
            ("ติดตามเรื่อง", A.TRACK),
        ],
    ),
    (
        "ตรวจสอบการใช้ไฟฟ้า",
        "กตฟ.",
        "usage",
        [
            ("แจ้งละเมิดใช้ไฟฟ้า", A.PROBLEM),
            ("ตรวจสอบค่าไฟแพง", A.PROBLEM),
            ("แจ้งมิเตอร์ชำรุด", A.PROBLEM),
            ("ติดตามเรื่อง", A.TRACK),
        ],
    ),
]
WORK = [
    ("งานใหม่ของกอง", A.WORK_NEW),
    ("งานที่มอบให้ฉัน", A.WORK_MINE),
    ("งานสั่งที่ได้รับ", A.WORK_ORDERS),
    ("ใกล้หรือเกินกำหนด", A.WORK_DUE),
    ("ค้นหาเลขเรื่อง", A.WORK_FIND),
    ("เปิดเว็บ", A.OPEN_WEB),
]
ORDER = [
    ("พิมพ์สั่งงาน", A.ORDER_NEW),
    ("งานที่ฉันสั่ง", A.ORDER_ISSUED),
    ("มอบเรื่องที่มีอยู่", A.ORDER_ASSIGN),
    ("งานเกินกำหนด", A.ORDER_LATE),
    ("สรุปวันนี้", A.ORDER_TODAY),
    ("เปิดเว็บ", A.OPEN_WEB),
]
GUEST = [
    ("ลงทะเบียน", A.REGISTER),
    ("ปัญหา/ข้อเสนอแนะ", A.GENERAL),
    ("เรื่องของฉัน", A.MINE),
    ("ติดตามเลขเรื่อง", A.FIND),
]


@transaction.atomic
def official_division_code():
    """Preserve the division ID and old policy history when adopting the owner's abbreviation."""
    from operations import identity, policy
    from operations.management.commands.rename_division_code import next_code, renamed

    old = Division.objects.select_for_update().filter(code="กองอุปกรณ์ฯ").first()
    if old is None:
        return
    if Division.objects.filter(code="กอจ.").exists():
        raise ValidationError("พบทั้งกองอุปกรณ์ฯ และ กอจ. ต้องจัดหน่วยงานซ้ำก่อนอัปเดตเมนู")
    old.code = "กอจ."
    old.save(update_fields=["code"])
    actor = identity.system_user()
    identity.audit(
        actor,
        "division.renamed",
        old,
        "ผู้ใช้ยืนยันชื่อย่อใน Q7 วันที่ 16 ต.ค. 2569",
        old="กองอุปกรณ์ฯ",
        new="กอจ.",
    )
    current = policy.active_policy()
    if current is not None:
        document = renamed(current.document, "กองอุปกรณ์ฯ", "กอจ.")
        if document != current.document:
            document["code"] = next_code(current.code)
            version = policy.import_policy(document)
            policy.activate_policy(
                None,
                version,
                "ผู้ใช้ยืนยันในแชท",
                "Q7 · 2026-10-16",
                "เปลี่ยนชื่อย่อกองเป็น กอจ. หน่วยงานและกติกาเดิม",
            )


@transaction.atomic
def sync():
    """Reconcile the fixed catalogue, keeping IDs of unchanged positions and retiring old taps."""
    official_division_code()
    kept = []

    def put(tab, place, position, label, action, parent=None, **values):
        # A changed button gets a new ID so an old tap cannot become a different service.
        row = (
            LineMenuItem.objects.filter(
                tab=tab,
                place=place,
                position=position,
                parent=parent,
                label=label,
                action=action,
            )
            .order_by("pk")
            .first()
        )
        fields = {"division": None, "topic": "", "kind": "", "note": "", **values}
        if row is None:
            row = LineMenuItem(
                tab=tab, place=place, position=position, parent=parent, label=label, action=action
            )
        for name, value in fields.items():
            setattr(row, name, value)
        row.is_active = True
        row.save()
        kept.append(row.pk)
        return row

    for index, (label, code, topic, choices) in enumerate(TOPICS, 1):
        codes = [code, "กองอุปกรณ์ฯ"] if code == "กอจ." else [code]
        division = Division.objects.filter(
            code__in=codes, is_active=True, is_synthetic=False
        ).first()
        if division is None:
            raise ValidationError(f"ยังไม่มีหน่วยงาน {code} โหลดโครงสร้างองค์กรก่อน")
        parent = put(T.REPORT, P.TILE, index, label, A.TOPIC, division=division, note=code)
        if not choices:
            choices = [("เบิกอุปกรณ์", A.NEW), ("ส่งคืนอุปกรณ์", A.RETURN), ("ติดตามเรื่อง", A.TRACK)]
        for number, (choice, action, *group) in enumerate(choices, 1):
            put(
                T.REPORT,
                P.CHOICE,
                number,
                choice,
                action,
                parent=parent,
                division=division,
                topic=group[0] if group else topic,  # an equipment choice holds its LINE group
                kind="withdrawal" if action == A.NEW else "",
            )
    put(T.REPORT, P.TILE, 6, "ปัญหา/ข้อเสนอแนะ", A.GENERAL, note="ทุกกอง/ศูนย์")
    for index, (label, action) in enumerate(GUEST[2:], 1):
        put(T.REPORT, P.BAR, index, label, action)
    for tab, rows in ((T.GUEST, GUEST), (T.WORK, WORK), (T.ORDER, ORDER)):
        for index, (label, action) in enumerate(rows, 1):
            put(tab, P.TILE, index, label, action)
    LineMenuItem.objects.exclude(pk__in=kept).update(is_active=False)
