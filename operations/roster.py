"""ทะเบียนพนักงานจากไฟล์ขององค์กร (15 ต.ค. 2569 Q10, Q13, Q16, Q27–Q29).

The owner imports the organisation's roster (.csv or .xlsx) on the admin page; no AI ever reads
it. The file is read once, sealed with the server's PII key while it waits for its preview, and
removed by the import (or after an hour). The preview shows which file column is which field
(guessed from the headers, chosen by the admin, remembered for next time), the rows that cannot
be used, and every ฝ่าย that is no เขต/หน่วยงาน yet: the admin maps it to one, adds it as a new
unit, or leaves it empty. Columns that are not mapped are never read, so the two phone columns
stay in the file. E-mails are kept as a keyed hash and sealed, never in plain text; the audit
trail holds counts only.
"""

import csv
import hashlib
import hmac
import io
import json
import re
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from operations import employees, identity, sealing
from operations.models import District, Employee, RosterSetting, RosterUpload

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 50_000
STAGED_FOR = timedelta(hours=1)
# field: (label, header words that name it). Order is the preview's order.
FIELDS = {
    "employee_id": ("รหัสพนักงาน", ("รหัสพนักงาน", "รหัส", "employeeid", "empid", "id")),
    "name": ("ชื่อ-สกุล", ("ชื่อสกุล", "ชื่อนามสกุล", "ชื่อ", "name", "fullname")),
    "position": ("ตำแหน่ง", ("ตำแหน่ง", "position")),
    "line_of_work": ("สายงาน", ("สายงาน",)),
    "department": ("ฝ่าย (ใช้เป็นเขต/หน่วยงาน)", ("ฝ่าย", "department")),
    "division_name": ("กอง", ("กอง", "division")),
    "section_name": ("แผนก", ("แผนก", "section")),
    "work_group": ("กลุ่มงาน", ("กลุ่มงาน",)),
    "email": ("อีเมล", ("อีเมล", "อีเมล์", "email", "e-mail", "mail")),
}
REQUIRED = ("employee_id", "name")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
NEW_UNIT = "__new__"


def _header_key(text):
    return re.sub(r"[\s\-_.()/]", "", str(text or "")).lower()


def unit_key(text):
    """One ฝ่าย written with or without ฝ่าย, การไฟฟ้านครหลวง, กฟน. or spaces is one key."""
    text = re.sub(r"\s", "", str(text or ""))
    text = re.sub(r"^ฝ่าย", "", text)
    return text.replace("การไฟฟ้านครหลวง", "").replace("กฟน.", "").replace("กฟน", "")


def email_digest(value):
    key = hashlib.sha256(f"employee-email:{settings.SECRET_KEY}".encode()).digest()
    return hmac.new(key, normalise_email(value).encode(), hashlib.sha256).hexdigest()


def normalise_email(value):
    return str(value or "").strip().lower()


# --- Reading ------------------------------------------------------------------------------------


def read_table(filename, data):
    """(headers, rows, first) of the first sheet: every cell as text, the header row found by
    its words; `first` is the file's line number of the row after the headers."""
    if len(data) > MAX_BYTES:
        raise ValidationError("ไฟล์ใหญ่เกิน 10 MB")
    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        table = _xlsx(data)
    elif name.endswith(".csv"):
        table = _csv(data)
    else:
        raise ValidationError("รับไฟล์ .xlsx หรือ .csv")
    for index, row in enumerate(table[:20]):
        if sum(bool(_guess(cell)) for cell in row) >= 2:
            headers = [str(cell or "").strip() for cell in row]
            rows = table[index + 1 :]
            while rows and not any(str(c or "").strip() for c in rows[-1]):
                rows.pop()
            if len(rows) > MAX_ROWS:
                raise ValidationError(f"ไฟล์มีเกิน {MAX_ROWS:,} แถว")
            width = len(headers)
            cells = [[str(c or "").strip() for c in (list(r) + [""] * width)[:width]] for r in rows]
            return headers, cells, index + 2
    raise ValidationError("ไม่พบแถวหัวคอลัมน์ เช่น รหัสพนักงาน ชื่อ-สกุล ใน 20 แถวแรก")


def _csv(data):
    for encoding in ("utf-8-sig", "cp874"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValidationError("อ่านไฟล์ CSV ไม่ได้ ให้บันทึกเป็น UTF-8 หรือใช้ .xlsx")
    return [list(row) for row in csv.reader(io.StringIO(text))]


def _xlsx(data):
    from openpyxl import load_workbook

    try:
        book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises many kinds for a bad file
        raise ValidationError("เปิดไฟล์ .xlsx ไม่ได้") from exc
    try:
        sheet = book.worksheets[0]
        return [
            ["" if cell is None else _cell(cell) for cell in row]
            for row in sheet.iter_rows(values_only=True)
        ]
    finally:
        book.close()


def _cell(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))  # an employee ID typed as a number
    return str(value)


def _guess(header):
    key = _header_key(header)
    if not key:
        return None
    for field, (_, words) in FIELDS.items():
        if key in {_header_key(w) for w in words}:
            return field
    for field, (_, words) in FIELDS.items():  # e.g. "ชื่อ - สกุล (ภาษาไทย)"
        if any(_header_key(w) and key.startswith(_header_key(w)) for w in words if len(w) > 2):
            return field
    return None


def guess_columns(headers):
    """field → header: what was chosen last time when the file still has it, else a guess."""
    saved = _setting().columns
    out = {}
    for field in FIELDS:
        if saved.get(field) in headers:
            out[field] = saved[field]
            continue
        out[field] = next((h for h in headers if _guess(h) == field), "")
    return out


# --- Staging ------------------------------------------------------------------------------------


@transaction.atomic
def stage(actor, filename, data):
    identity.require_admin(actor)
    if not sealing.available():
        raise ValidationError("เซิร์ฟเวอร์ยังไม่มีกุญแจเข้ารหัสข้อมูลส่วนบุคคล (PII_SECRET_KEY)")
    headers, rows, first = read_table(filename, data)
    count = sum(1 for row in rows if any(row))
    if not count:
        raise ValidationError("ไฟล์ไม่มีรายชื่อ")
    RosterUpload.objects.filter(created_at__lt=timezone.now() - STAGED_FOR).delete()
    table = {"headers": headers, "rows": rows, "first": first}
    sealed = sealing.seal(json.dumps(table, ensure_ascii=False))
    return RosterUpload.objects.create(
        uploaded_by=actor, filename=(filename or "")[:200], rows=count, sealed=sealed
    )


def opened(upload):
    """(headers, rows) of a staged file; ValidationError when it expired or cannot be opened."""
    if upload.created_at < timezone.now() - STAGED_FOR:
        raise ValidationError("ไฟล์ที่ตรวจไว้หมดเวลาแล้ว อัปโหลดใหม่")
    text = sealing.unseal(upload.sealed)
    if not text:
        raise ValidationError("เปิดไฟล์ที่ตรวจไว้ไม่ได้ อัปโหลดใหม่")
    table = json.loads(text)
    return table["headers"], Rows(table["rows"], table.get("first", 2))


class Rows(list):
    """A staged file's rows that remember the file line of the first one."""

    def __init__(self, rows, first=2):
        super().__init__(rows)
        self.first = first


def _setting():
    return RosterSetting.objects.order_by("pk").first() or RosterSetting()


# --- Preview ------------------------------------------------------------------------------------


def _records(headers, rows, columns):
    """Rows as {field: value} for the mapped fields only; unmapped columns are never read."""
    index = {field: headers.index(h) for field, h in columns.items() if h and h in headers}
    for number, row in enumerate(rows, start=getattr(rows, "first", 2)):
        if any(row):
            yield number, {field: " ".join(row[i].split()) for field, i in index.items()}


def districts_by_key():
    return {unit_key(d.name): d for d in District.objects.filter(is_active=True)}


def preview(headers, rows, columns):
    """What the import would do: usable rows, problems, and each ฝ่าย with its เขต/หน่วยงาน."""
    missing = [FIELDS[f][0] for f in REQUIRED if not columns.get(f)]
    known, saved = districts_by_key(), _setting().units
    seen, duplicates, incomplete, bad_email, units = set(), [], [], 0, {}
    for number, record in _records(headers, rows, columns):
        employee_id, name = record.get("employee_id", ""), record.get("name", "")
        if not employee_id or not name:
            incomplete.append(number)
            continue
        if employee_id in seen:
            duplicates.append(employee_id)
            continue
        seen.add(employee_id)
        email = record.get("email", "")
        if email and not EMAIL.match(normalise_email(email)):
            bad_email += 1
        department = record.get("department", "")
        if department:
            key = unit_key(department)
            unit = units.setdefault(key, {"text": department, "count": 0})
            unit["count"] += 1
    for key, unit in units.items():
        match = known.get(key)
        unit["key"] = key
        unit["match"] = match
        unit["choice"] = (match.code if match else saved.get(key, NEW_UNIT)) or ""
    return {
        "missing": missing,
        "usable": len(seen),
        "incomplete": incomplete,
        "duplicates": duplicates,
        "bad_email": bad_email,
        "units": sorted(units.values(), key=lambda u: (u["match"] is not None, -u["count"])),
        "existing": Employee.objects.filter(is_active=True, is_synthetic=False).count(),
    }


# --- Import -------------------------------------------------------------------------------------


@transaction.atomic
def import_upload(actor, upload, columns, choices, deactivate_missing, reason):
    """Import a staged roster with the admin's column and ฝ่าย choices; returns a summary."""
    identity.require_admin(actor)
    reason = identity._require_reason(reason)
    upload = RosterUpload.objects.select_for_update().get(pk=upload.pk)
    headers, rows = opened(upload)
    columns = {f: h for f, h in columns.items() if f in FIELDS and h in headers}
    if any(not columns.get(f) for f in REQUIRED):
        raise ValidationError("เลือกคอลัมน์รหัสพนักงานและชื่อ-สกุลก่อนนำเข้า")
    units = _units(actor, headers, rows, columns, choices, reason)
    seen, created, updated, emails = set(), 0, 0, 0
    sealable = sealing.available()
    for _, record in _records(headers, rows, columns):
        employee_id, name = record.get("employee_id", ""), record.get("name", "")
        if not employee_id or not name or employee_id in seen:
            continue
        seen.add(employee_id)
        email = normalise_email(record.get("email", ""))
        email = email if EMAIL.match(email) else ""
        emails += bool(email)
        values = {
            "name": name[:200],
            "position": record.get("position", "")[:200],
            "line_of_work": record.get("line_of_work", "")[:200],
            "department": record.get("department", "")[:200],
            "division_name": record.get("division_name", "")[:200],
            "section_name": record.get("section_name", "")[:200],
            "work_group": record.get("work_group", "")[:200],
            "district": units.get(unit_key(record.get("department", ""))),
            "email_hash": email_digest(email) if email else "",
            "email_sealed": sealing.seal(email) if email and sealable else "",
            "is_active": True,
            "is_synthetic": False,
        }
        _, made = Employee.objects.update_or_create(employee_id=employee_id, defaults=values)
        created, updated = created + made, updated + (not made)
    if not seen:
        raise ValidationError("ไฟล์ไม่มีแถวที่มีทั้งรหัสพนักงานและชื่อ")
    closed = 0
    if deactivate_missing:
        closed = (
            Employee.objects.filter(is_active=True)
            .exclude(employee_id__in=seen)
            .update(is_active=False)
        )
    setting = _setting()
    setting.columns, setting.updated_by = columns, actor
    setting.save()
    upload.delete()
    matched, unmatched, review = employees.match_requesters()
    summary = {
        "created": created,
        "updated": updated,
        "closed": closed,
        "emails": emails,
        "matched": matched,
        "unmatched": unmatched,
        "review": review,
    }
    # Counts only: names and e-mails never go to the audit trail.
    identity.audit(actor, "employees.imported", setting, reason, **summary)
    return summary


def _units(actor, headers, rows, columns, choices, reason):
    """ฝ่าย key → District, as the admin chose: an existing unit, a new one, or none. The choices
    are remembered for the next file."""
    known, by_code = districts_by_key(), {d.code: d for d in District.objects.all()}
    texts = {}
    for _, record in _records(headers, rows, columns):
        if record.get("department"):
            texts.setdefault(unit_key(record["department"]), record["department"])
    setting, out = _setting(), {}
    remembered = dict(setting.units)
    for key, text in texts.items():
        if key in known:
            out[key] = known[key]
            continue
        choice = choices.get(key, remembered.get(key, ""))
        if choice == NEW_UNIT:
            district = _new_unit(actor, text, reason)
            out[key], remembered[key] = district, district.code
        elif choice in by_code:
            out[key], remembered[key] = by_code[choice], choice
        else:
            remembered[key] = ""
    setting.units = remembered
    setting.save()
    return out


def _new_unit(actor, text, reason):
    name = text[:200]
    existing = District.objects.filter(name=name).first()
    if existing is not None:
        return existing
    number = District.objects.filter(code__startswith="U").count() + 1
    while District.objects.filter(code=f"U{number:03d}").exists():
        number += 1
    district = District.objects.create(code=f"U{number:03d}", name=name)
    identity.audit(actor, "district.created", district, reason, code=district.code, source="roster")
    return district


def employee_unit(employee):
    """What the LINE OA user pages show for an employee: ตำแหน่ง · ฝ่าย."""
    parts = [employee.position, employee.department or getattr(employee.district, "name", "")]
    return " · ".join(p for p in parts if p)
