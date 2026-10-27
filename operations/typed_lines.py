"""ช่องที่ผู้ใช้พิมพ์ (user, 23 Oct 2569): the LINE chat lets a requester type a line instead of
choosing a model from the catalogue.

Each LINE group has fields (EntryField, admin-editable): meter numbers, size and quantity for
มิเตอร์; size and quantity for CT and VT; name, size and quantity for other equipment. A row is
typed in one message split by "/" or field by field. Every row of a group joins the group's
shared item (EquipmentType.entry_group) as one case line: `details` keeps the rows, the quantity
is their sum and, for returns and problem reports, the meter numbers are the line's serials. A
problem report may skip every field and asks no quantity.

A withdrawal or return to a division with `requires_sap` also carries the SAP slip number
(เลขใบเบิก/ใบส่งคืน SAP); an open case that already cites the number refuses it, without telling
the requester whose case that is.
"""

import re
import secrets

from django.core.exceptions import ValidationError
from django.db import transaction

from operations import identity, workflow
from operations.models import Case, CaseLine, Division, EntryField, EquipmentType

K = Case.Kind
GROUPS = tuple(EquipmentType.LineGroup.values)  # meter, ct, vt, other, transformer, lbs, rmu
# Groups whose rows name each unit by its number (the number is the unit's serial).
NUMBERED = ("meter", "transformer", "lbs", "rmu")
# grill กคว. 27 Oct 2569 Q9: a slip number is text up to 30 characters and may carry "/10".
SAP_NUMBER = re.compile(r"^[A-Za-z0-9/-]{4,30}$")
METER_NUMBER = re.compile(r"^[A-Za-z0-9-]{4,20}$")  # loose until the real formats are known
THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
LIMITS = {"size": 40, "name": 100, "extra": 200}
EQUIPMENT_NUMBER = re.compile(r"^[A-Za-z0-9-]{2,30}$")
MAX_ROWS = 50
MAX_NUMBERS = 500
SAP_LABELS = {K.WITHDRAWAL: "เลขใบเบิก SAP", K.RETURN: "เลขใบส่งคืน SAP"}
# ติดตามสถานะการซ่อม (user, 24 Oct 2569): a problem-kind case that cites its SAP repair number.
REPAIR_SAP_LABEL = "เลข SAP งานซ่อม"


def label_of(group):
    return EquipmentType.LineGroup(group).label


def shared_item(group):
    return EquipmentType.objects.filter(entry_group=group, is_active=True).first()


def is_shared(item):
    return bool(getattr(item, "entry_group", ""))


def fields(group, kind):
    """The fields asked for one row of this group, in order (a problem report: no quantity)."""
    rows = EntryField.objects.filter(group=group, is_active=True).order_by("position", "pk")
    return [f for f in rows if kind in (f.kinds or [])]


def required(field, kind):
    return kind != K.PROBLEM and field.required


# --- SAP slip numbers -------------------------------------------------------------------------


def sap_divisions():
    rows = Division.objects.filter(requires_sap=True, is_active=True)
    return set(rows.values_list("code", flat=True))


def needs_sap(kind, division_codes):
    return kind in SAP_LABELS and bool(set(division_codes) & sap_divisions())


def identified(lines):
    """Whether typed rows name a piece by its number: what ติดตามสถานะการซ่อม may give in place of
    the SAP repair number (25 Oct 2569 Q7)."""
    return any(
        row.get("numbers") or row.get("equipment_number")
        for line in lines
        for row in (line.get("details") if isinstance(line, dict) else line.details) or []
    )


def sap_label(kind, topic=""):
    """What this case's SAP number is called, or "" when this kind of case has none."""
    if kind == K.PROBLEM and topic == Case.Topic.REPAIR:
        return REPAIR_SAP_LABEL
    return SAP_LABELS.get(kind, "")


def clean_sap(text):
    """(number, "") or ("", why not)."""
    value = re.sub(r"\s", "", str(text or "").translate(THAI_DIGITS))
    if not SAP_NUMBER.match(value):
        return "", "เลข SAP ใช้ตัวอักษร ตัวเลข ขีด หรือ / ได้ 4 ถึง 30 ตัว"
    return value, ""


def sap_in_use(number, requester, exclude=None):
    """Why this SAP number cannot start another case, or "". An open case of this requester is
    named; anyone else's is not."""
    found = Case.objects.filter(sap_number=number).exclude(status__in=workflow.TERMINAL)
    if exclude is not None:
        found = found.exclude(pk=exclude.pk)
    own = found.filter(requester=requester).first() if requester is not None else None
    if own is not None:
        return f"เลขนี้อยู่ในเรื่อง {own.number} ที่ยังไม่จบ ติดตามเรื่องนั้นได้"
    if found.exists():
        return "เลขนี้มีเรื่องที่ยังไม่จบอยู่แล้ว ตรวจเลขอีกครั้ง หรือติดต่อกองที่ดูแล"
    return ""


# --- One typed row -----------------------------------------------------------------------------


def numbers_in(text):
    return [n for n in re.split(r"[\s,]+", str(text or "").translate(THAI_DIGITS).strip()) if n]


def take(entry, field, value, kind, unit):
    """Put one typed value into the row; returns why it was refused, or ""."""
    value = " ".join(str(value or "").split())
    if not value or value == "-":
        return "กรุณาพิมพ์ข้อมูลช่องนี้" if required(field, kind) else ""
    if field.key == EntryField.Key.NUMBERS:
        numbers = numbers_in(value)
        if len(numbers) > MAX_NUMBERS or not all(METER_NUMBER.match(n) for n in numbers):
            return "หมายเลขเครื่องวัดใช้ตัวอักษร ตัวเลข หรือขีด 4 ถึง 20 ตัว คั่นด้วยเว้นวรรค"
        if len(set(numbers)) != len(numbers):
            return "หมายเลขเครื่องวัดซ้ำกัน"
        entry["numbers"] = numbers
    elif field.key == EntryField.Key.QUANTITY:
        number = re.fullmatch(
            rf"(\d{{1,4}})\s*(?:{re.escape(unit)})?", value.translate(THAI_DIGITS).replace(",", "")
        )
        if number is None or int(number.group(1)) < 1:
            return "จำนวนต้องเป็นจำนวนเต็มมากกว่า 0"
        entry["quantity"] = str(int(number.group(1)))
    elif field.key == EntryField.Key.EQUIPMENT_NUMBER:
        number = value.translate(THAI_DIGITS).replace(" ", "")
        if not EQUIPMENT_NUMBER.match(number):
            return "หมายเลขอุปกรณ์ใช้ตัวอักษร ตัวเลข หรือขีด 2 ถึง 30 ตัว"
        entry["equipment_number"] = number
    elif field.key in (EntryField.Key.SIZE, EntryField.Key.NAME):
        limit = LIMITS[field.key]
        if len(value) > limit:
            return f"พิมพ์ไม่เกิน {limit} ตัวอักษร"
        entry[field.key] = value
    else:
        if len(value) > LIMITS["extra"]:
            return f"พิมพ์ไม่เกิน {LIMITS['extra']} ตัวอักษร"
        entry.setdefault("extra", {})[field.key] = value
    return ""


def split(text):
    """The values of a row typed in one message ("12345678/5(100)A/2"), or None when the text
    is the answer of one field."""
    if "/" not in str(text or ""):
        return None
    return [part.strip() for part in str(text).split("/")]


def check(entry, kind, group, lines, requester):
    """Why a finished row cannot join the draft, or "": the meter numbers against the
    quantity, the other rows and (for a return) the requester's other open returns."""
    numbers = entry.get("numbers", [])
    if numbers and entry.get("quantity") and len(numbers) != int(entry["quantity"]):
        return f"หมายเลขเครื่องวัดมี {len(numbers)} เลข แต่จำนวน {entry['quantity']}"
    taken = {n for line in lines for row in line.get("details", []) for n in row.get("numbers", [])}
    repeated = sorted(set(numbers) & taken)
    if repeated:
        return f"หมายเลข {', '.join(repeated)} อยู่ในรายการก่อนหน้าแล้ว"
    if kind == K.RETURN and numbers and _in_open_returns(numbers):
        return "หมายเลขนี้อยู่ในเรื่องส่งคืนอื่นที่ยังไม่จบ ตรวจหมายเลขอีกครั้ง หรือติดต่อกองที่ดูแล"
    return ""


def _in_open_returns(numbers):
    """Whether an open return already lists one of these meter numbers (its line serials)."""
    lines = CaseLine.objects.filter(case__kind=K.RETURN).exclude(case__status__in=workflow.TERMINAL)
    # jsonb ?| also matches the strings of an array, which is what serials are.
    return lines.filter(serials__has_any_keys=list(numbers)).exists()


def add(lines, group, entry, kind):
    """The draft lines with this row joined to the group's shared item (one line per group)."""
    item = shared_item(group)
    if item is None:
        raise LookupError(group)
    lines = [dict(line) for line in lines]
    line = next((x for x in lines if x.get("item_code") == item.code), None)
    if line is None:
        line = {"item_code": item.code, "details": []}
        lines.append(line)
    row = {k: v for k, v in entry.items() if v not in ("", [], {}, None)}
    line["details"] = [*line.get("details", []), {"group": group, **row}]
    if kind != K.PROBLEM:
        line["quantity"] = str(sum(int(r.get("quantity", 0) or 0) for r in line["details"]))
    numbers = [n for r in line["details"] for n in r.get("numbers", [])]
    if kind != K.WITHDRAWAL and numbers and item.is_serialized:
        line["serials"] = numbers  # what arrives (returns) or is broken (problems) is counted
    return lines


def describe(row, unit=""):
    """One typed row for the summary and the case page: "5(100)A · 2 เครื่อง (หมายเลข …)"."""
    parts = [row[k] for k in ("name", "size") if row.get(k)]
    if row.get("quantity"):
        parts.append(f"{row['quantity']} {unit}".strip())
    parts += [str(v) for v in (row.get("extra") or {}).values() if v]
    if row.get("equipment_number"):
        parts.append(f"หมายเลขอุปกรณ์ {row['equipment_number']}")
    text = " · ".join(parts)
    if row.get("numbers"):
        text += f" (หมายเลข {', '.join(row['numbers'])})"
    return text.strip()


# --- Shape of `details` in a submission --------------------------------------------------------


def clean_details(raw):
    """A submission line's `details`, or ValueError: the rows a requester typed."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_ROWS:
        raise ValueError("details must be a list of at most 50 rows.")
    rows = []
    for row in raw:
        if not isinstance(row, dict) or set(row) - {
            "group",
            "size",
            "quantity",
            "numbers",
            "name",
            "equipment_number",
            "extra",
        }:
            raise ValueError(
                "Each detail row has group, size, quantity, numbers, name, equipment_number, extra."
            )
        if row.get("group") not in GROUPS:
            raise ValueError("A detail row needs a LINE group.")
        clean = {"group": row["group"]}
        for key in ("size", "name"):
            if row.get(key) not in (None, ""):
                if not isinstance(row[key], str) or len(row[key]) > LIMITS[key]:
                    raise ValueError(f"{key} must be text of at most {LIMITS[key]} characters.")
                clean[key] = row[key].strip()
        if row.get("quantity") not in (None, ""):
            if not re.fullmatch(r"\d{1,4}", str(row["quantity"])) or int(row["quantity"]) < 1:
                raise ValueError("A detail quantity is a whole number above 0.")
            clean["quantity"] = str(int(row["quantity"]))
        if row.get("equipment_number") not in (None, ""):
            if not isinstance(row["equipment_number"], str) or not EQUIPMENT_NUMBER.match(
                row["equipment_number"]
            ):
                raise ValueError("equipment_number is 2-30 letters, digits or dashes.")
            clean["equipment_number"] = row["equipment_number"]
        numbers = row.get("numbers") or []
        if not isinstance(numbers, list) or len(numbers) > MAX_NUMBERS:
            raise ValueError("numbers must be a list.")
        if not all(isinstance(n, str) and METER_NUMBER.match(n) for n in numbers):
            raise ValueError("Each meter number is 4-20 letters, digits or dashes.")
        if numbers:
            clean["numbers"] = list(numbers)
        extra = row.get("extra") or {}
        if not isinstance(extra, dict) or len(extra) > 10:
            raise ValueError("extra must be an object of at most 10 fields.")
        for value in extra.values():
            if not isinstance(value, str) or len(value) > LIMITS["extra"]:
                raise ValueError("Each extra field is text of at most 200 characters.")
        if extra:
            clean["extra"] = {str(k)[:32]: v.strip() for k, v in extra.items()}
        rows.append(clean)
    return rows


# --- Admin changes (รายการอุปกรณ์ and กอง pages; audited, a reason is optional) ----------------

KINDS = (K.WITHDRAWAL, K.RETURN, K.PROBLEM)
# Fields the system counts with: always asked of a withdrawal and a return.
STRUCTURAL = {EntryField.Key.NUMBERS, EntryField.Key.QUANTITY}


@transaction.atomic
def set_requires_sap(actor, division, value, reason=""):
    identity.require_admin(actor)
    division = Division.objects.select_for_update().get(pk=division.pk)
    if division.requires_sap == bool(value):
        return division
    division.requires_sap = bool(value)
    division.save(update_fields=["requires_sap"])
    identity.audit(actor, "division.requires_sap", division, reason, value=division.requires_sap)
    return division


def _label(text):
    label = " ".join(str(text or "").split())
    if not 1 <= len(label) <= 40:
        raise ValidationError("ชื่อช่องต้องมี 1 ถึง 40 ตัวอักษร")
    return label


def _kinds(values):
    kinds = [k for k in KINDS if k in set(values or [])]
    if not kinds:
        raise ValidationError("เลือกอย่างน้อยหนึ่งอย่าง: เบิก คืน หรือแจ้งปัญหา")
    return kinds


@transaction.atomic
def update_field(actor, field, label, required, kinds, position, active, reason=""):
    """A base field keeps its kinds and stays on; numbers and quantity stay required."""
    identity.require_admin(actor)
    field = EntryField.objects.select_for_update().get(pk=field.pk)
    before = (field.label, field.required, list(field.kinds), field.position, field.is_active)
    field.label = _label(label)
    try:
        field.position = max(0, min(int(position), 99))
    except (TypeError, ValueError) as exc:
        raise ValidationError("ลำดับต้องเป็นตัวเลข") from exc
    if field.is_base:
        field.required = True if field.key in STRUCTURAL else bool(required)
    else:
        field.required, field.kinds, field.is_active = bool(required), _kinds(kinds), bool(active)
    after = (field.label, field.required, list(field.kinds), field.position, field.is_active)
    if after == before:
        return field
    field.updated_by = actor
    field.save()
    identity.audit(
        actor, "entry_field.changed", field, reason, group=field.group, key=field.key,
        label=field.label, required=field.required, kinds=field.kinds, active=field.is_active,
    )  # fmt: skip
    return field


@transaction.atomic
def add_field(actor, group, label, required, kinds, reason=""):
    identity.require_admin(actor)
    if group not in GROUPS:
        raise ValidationError("ไม่รู้จักหมวดนี้")
    if EntryField.objects.filter(group=group, is_active=True).count() >= 10:
        raise ValidationError("หมวดหนึ่งมีช่องได้ไม่เกิน 10 ช่อง")
    last = EntryField.objects.filter(group=group).order_by("-position").first()
    field = EntryField.objects.create(
        group=group,
        key=f"new-{secrets.token_hex(4)}",
        label=_label(label),
        kinds=_kinds(kinds),
        required=bool(required),
        position=(last.position + 1) if last else 1,
        updated_by=actor,
    )
    field.key = f"x{field.pk}"
    field.save(update_fields=["key"])
    identity.audit(
        actor, "entry_field.added", field, reason, group=group, key=field.key, label=field.label
    )
    return field


@transaction.atomic
def set_unit(actor, group, unit, reason=""):
    """The unit the chat shows for a typed group (its shared item's unit)."""
    identity.require_admin(actor)
    item = EquipmentType.objects.select_for_update().filter(entry_group=group).first()
    unit = " ".join(str(unit or "").split())
    if item is None or not 1 <= len(unit) <= 32:
        raise ValidationError("หน่วยต้องมี 1 ถึง 32 ตัวอักษร")
    if unit == item.unit:
        return item
    previous, item.unit = item.unit, unit
    item.save(update_fields=["unit"])
    identity.audit(actor, "entry_group.unit", item, reason, previous=previous, unit=unit)
    return item
