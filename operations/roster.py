"""ทะเบียนพนักงานจากไฟล์ขององค์กร (15 ต.ค. 2569 Q10, Q13, Q16, Q27–Q29).

The owner imports the organisation's roster (.csv or .xlsx) on the admin page; no AI ever reads
it. The file is read once, sealed with the server's PII key while it waits for its preview, and
removed by the import (or after an hour). The preview shows which file column is which field
(guessed from the headers, chosen by the admin, remembered for next time), the rows that cannot
be used, and every ฝ่าย that is no เขต/หน่วยงาน yet: the admin maps it to one, adds it as a new
unit, or leaves it empty. Columns that are not mapped are never read, so the two phone columns
stay in the file. E-mails are kept as a keyed hash and sealed, never in plain text; the audit
trail holds counts only.

User, 1 Nov 2569 (ADR 0041): the name comes back as the first name only; the title and the
surname are cut before anything is saved. Each กอง is matched to the system's กอง, the แผนก and
กลุ่มงาน under it are made when missing, and the people linked to the file follow it: their
สังกัด, their name and a สมาชิกกอง role move with each import.
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
from operations.models import (
    District,
    Division,
    Employee,
    Requester,
    Role,
    RoleGrant,
    RosterSetting,
    RosterUnitName,
    RosterUpload,
    Section,
    StaffProfile,
    WorkGroup,
)

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 50_000
STAGED_FOR = timedelta(hours=1)
# field: (label, header words that name it). Order is the preview's order.
FIELDS = {
    "employee_id": ("รหัสพนักงาน", ("รหัสพนักงาน", "รหัส", "employeeid", "empid", "id")),
    "name": ("ชื่อ (เก็บชื่อต้นอย่างเดียว)", ("ชื่อ-สกุล", "ชื่อ", "name")),
    "position": ("ตำแหน่ง", ("ตำแหน่ง", "position")),
    "line_of_work": ("สายงาน", ("สายงาน",)),
    "department": ("ฝ่าย (ใช้เป็นหน่วยงาน (เขต/ฝ่าย))", ("ฝ่าย", "department")),
    "division_name": ("กอง", ("กอง", "division")),
    "section_name": ("แผนก", ("แผนก", "section")),
    "work_group": ("กลุ่มงาน", ("กลุ่มงาน",)),
    "email": ("อีเมล", ("อีเมล", "อีเมล์", "email", "e-mail", "mail")),
}
REQUIRED = ("employee_id",)
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
NEW_UNIT = "__new__"
TITLE = re.compile(r"^(?:นางสาว|น\.ส\.|นาง|นาย)\s*")


def first_name(text):
    """The first name alone: the title and the surname share the file's cell and never reach
    the database (user, 1 Nov 2569)."""
    words = TITLE.sub("", " ".join(str(text or "").split())).split()
    return words[0][:200] if words else ""


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
    raise ValidationError("ไม่พบแถวหัวคอลัมน์ เช่น รหัสพนักงาน ตำแหน่ง ใน 20 แถวแรก")


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


def same_unit(text, name):
    """A unit name as the file writes it; the organisation's export cuts names at 40 letters."""
    return bool(text) and (text == name or (len(text) >= 20 and name.startswith(text)))


class OrgUnits:
    """The file's กอง › แผนก › กลุ่มงาน as the system's units (user, 1 Nov 2569). A กอง matches
    by name, ชื่อย่อ or code; a แผนก or กลุ่มงาน under it is made when missing (`make`), else
    only listed in `new`. A row of the system's own ฝ่าย with no กอง may name a กอง-level unit
    as its แผนก, as บท. (แผนกบริหารทั่วไป) does."""

    def __init__(self, records, make=False):
        self.make, self.new, self.found = make, [], {}
        self.divisions = list(
            Division.objects.filter(is_active=True, is_synthetic=False, kind=Division.Kind.DIVISION)
        )
        self.home = {
            r.get("department") for r in records if self._division(r.get("division_name", ""))
        }

    def _division(self, text):
        hits = [
            d
            for d in self.divisions
            if text and (text in (d.code, d.short_name) or same_unit(text, d.name))
        ]
        return hits[0] if len(hits) == 1 else None

    def of(self, record):
        """(Division, Section, WorkGroup) of one row, None for a level the system lacks."""
        text = record.get("division_name", "")
        division = self._division(text)
        section_name, group_name = record.get("section_name", ""), record.get("work_group", "")
        if not text and record.get("department") in self.home:
            division, section_name, group_name = self._division(section_name), "", ""
        if division is None:
            return None, None, None
        section = self._child(Section, "division", division, section_name)
        group = None
        if section is not None:
            group = self._child(WorkGroup, "section", section, group_name)
        elif section_name and group_name and f"{section_name} › {group_name}" not in self.new:
            self.new.append(f"{section_name} › {group_name}")  # a preview: the แผนก is new too
        return division, section, group

    def _child(self, model, field, parent, name):
        key = (model, parent.pk, name)
        if not name:
            return None
        if key not in self.found:
            rows = model.objects.filter(**{field: parent})
            hit = next((u for u in rows if same_unit(name, u.name) or name == u.code), None)
            if hit is None:
                from operations import units  # local: units builds on identity

                self.new.append(f"{units.short(parent)} › {name}")
                if self.make:
                    hit = model.objects.create(**{field: parent, "name": name[:200]})
            elif not hit.is_active and self.make:  # the file wins (Q3): a closed unit opens
                hit.is_active = True
                hit.save(update_fields=["is_active"])
            self.found[key] = hit
        return self.found[key]


def preview(headers, rows, columns):
    """What the import would do: usable rows, problems, each ฝ่าย with its เขต/หน่วยงาน, and
    the แผนก and กลุ่มงาน it would add."""
    missing = [FIELDS[f][0] for f in REQUIRED if not columns.get(f)]
    known, saved = districts_by_key(), _setting().units
    seen, duplicates, incomplete, bad_email, units = set(), [], [], 0, {}
    records = list(_records(headers, rows, columns))
    org = OrgUnits([r for _, r in records])
    matched = 0
    for number, record in records:
        employee_id = record.get("employee_id", "")
        if not employee_id:
            incomplete.append(number)
            continue
        if employee_id in seen:
            duplicates.append(employee_id)
            continue
        seen.add(employee_id)
        email = record.get("email", "")
        if email and not EMAIL.match(normalise_email(email)):
            bad_email += 1
        matched += org.of(record)[0] is not None
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
        "in_divisions": matched,
        "new_units": org.new,
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
        raise ValidationError("เลือกคอลัมน์รหัสพนักงานก่อนนำเข้า")
    units = _units(actor, headers, rows, columns, choices, reason)
    records = [r for _, r in _records(headers, rows, columns)]
    org = OrgUnits(records, make=True)
    names = dict(Employee.objects.values_list("employee_id", "name"))
    seen, created, updated, emails = set(), 0, 0, 0
    sealable = sealing.available()
    for record in records:
        employee_id = record.get("employee_id", "")
        if not employee_id or employee_id in seen:
            continue
        seen.add(employee_id)
        email = normalise_email(record.get("email", ""))
        email = email if EMAIL.match(email) else ""
        emails += bool(email)
        values = {
            "position": record.get("position", "")[:200],
            "line_of_work": record.get("line_of_work", "")[:200],
            "department": record.get("department", "")[:200],
            "division_name": record.get("division_name", "")[:200],
            "section_name": record.get("section_name", "")[:200],
            "work_group": record.get("work_group", "")[:200],
            "district": units.get(unit_key(record.get("department", ""))),
            "division": org.of(record)[0],
            "email_hash": email_digest(email) if email else "",
            "email_sealed": sealing.seal(email) if email and sealable else "",
            "is_active": True,
            "is_synthetic": False,
        }
        if "name" in columns:
            values["name"] = first_name(record.get("name", ""))
        _, made = Employee.objects.update_or_create(employee_id=employee_id, defaults=values)
        created, updated = created + made, updated + (not made)
    if not seen:
        raise ValidationError("ไฟล์ไม่มีแถวที่มีรหัสพนักงาน")
    follow(actor, seen, names, reason)
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
    summary = {
        "created": created,
        "updated": updated,
        "closed": closed,
        "emails": emails,
        "new_units": len(org.new),
    }
    # Counts only: e-mails and names never go to the audit trail.
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


def follow(actor, employee_ids, old_names, reason):
    """The people tied to the file follow it (Q3, Q7 of 1 Nov 2569): a staff account's สังกัด
    and its สมาชิกกอง role move to the file's กอง (no กอง of the system: no role, so
    เจ้าหน้าที่), and a name the system copied (the employee label or the old first name)
    becomes the new first name. Roles an admin chose stay."""
    from operations import units  # local: units builds on identity

    rows = {
        e.employee_id: e
        for e in Employee.objects.filter(employee_id__in=employee_ids).select_related("division")
    }

    def renamed(current, employee):
        copied = employees.LABELLED.match(current) or current == old_names.get(employee.employee_id)
        return employee.name if employee.name and copied and current != employee.name else None

    why = reason or "ตามทะเบียนพนักงานที่นำเข้า"
    for profile in StaffProfile.objects.filter(employee_id__in=rows).select_related("user"):
        employee, user = rows[profile.employee_id], profile.user
        units.bind_from_roster(user, employee)
        name = renamed(profile.display_name, employee)
        if name:
            StaffProfile.objects.filter(pk=profile.pk).update(display_name=name)
        held = list(RoleGrant.objects.filter(user=user, revoked_at__isnull=True))
        if not user.is_active or [g.role for g in held] != [Role.STAFF]:
            continue
        target = employee.division
        if target is None:
            identity.clear_roles(actor, user, why)
        elif held[0].division_id != target.pk:
            identity.grant_role(actor, user, Role.STAFF, target, why)
    link = Requester.EmployeeLink
    for requester in Requester.objects.filter(
        employee__employee_id__in=rows, employee_link__in=[link.VERIFIED, link.ADMIN]
    ).select_related("employee"):
        name = renamed(requester.display_name, rows[requester.employee.employee_id])
        if name:
            Requester.objects.filter(pk=requester.pk).update(display_name=name)


LEVELS = ("department", "division_name", "section_name", "work_group")
PLACE = ("line_of_work", *LEVELS, "district_id", "division_id")


def path_of(employee):
    """ฝ่าย › กอง › แผนก › กลุ่มงาน as the file says it (the chart's picker)."""
    return " › ".join(getattr(employee, f) for f in LEVELS if getattr(employee, f))


def unit_paths():
    """Every path the active roster has, each with the fields an employee placed there takes."""
    rows = Employee.objects.filter(is_active=True, is_synthetic=False).values(*PLACE).distinct()
    out = {}
    for row in rows:
        out.setdefault(" › ".join(row[f] for f in LEVELS if row[f]), row)
    out.pop("", None)
    return dict(sorted(out.items()))


def place_of(path, paths=None):
    """The fields a person at this path takes; a path that stops above the file's rows (only a
    ฝ่าย, 3 Nov 2569) takes a row below it with the lower levels empty."""
    paths = unit_paths() if paths is None else paths
    if path in paths:
        return paths[path]
    below = next((row for p, row in paths.items() if p.startswith(f"{path} › ")), None)
    if not path or below is None:
        return None
    depth = len(path.split(" › "))
    place, seen = dict(below), 0
    for field in LEVELS:
        seen += bool(below[field])
        if seen > depth:
            place[field] = ""
    if not place["division_name"]:
        place["division_id"] = None
    return place


@transaction.atomic
def move_employee(actor, employee, path, reason=""):
    """An admin moves one person on the chart (1 Nov 2569 Q3): the roster row takes another
    path of the file, and the person's สังกัด and สมาชิกกอง role follow, until the next import."""
    identity.require_admin(actor)
    place = place_of(" ".join((path or "").split()))
    if place is None:
        raise ValidationError("โปรดเลือกหน่วยจากรายการ")
    employee = Employee.objects.select_for_update().get(pk=employee.pk)
    before = path_of(employee)
    for field, value in place.items():
        setattr(employee, field, value)
    employee.save(update_fields=[*PLACE[:5], "district", "division"])
    identity.audit(actor, "employee.unit_changed", employee, reason, unit=path, previous=before)
    follow(actor, [employee.employee_id], {}, reason)


def employee_path(employee, names=None):
    """Q9: ฝ่าย › กอง › แผนก › กลุ่มงาน, N/A where a level is missing; a กอง of the system by its
    ชื่อย่อ, and a unit outside it by the ชื่อย่อ in names (unit_names()) when given."""
    from operations import units  # local: units builds on identity

    if employee is None:
        return []
    division = units.short(employee.division) if employee.division_id else employee.division_name
    levels = [employee.department, division, employee.section_name, employee.work_group]
    if names and not employee.division_id:
        path = []
        for i, field in enumerate(LEVELS):
            value = getattr(employee, field)
            if value:
                path.append(value)
                levels[i] = names.get(" › ".join(path), levels[i])
    return [level or units.NA for level in levels]


def unit_names():
    """ชื่อย่อ of units outside the system, by roster path (ADR 0044)."""
    return dict(RosterUnitName.objects.values_list("path", "short_name"))


def set_unit_name(actor, path, value, reason=""):
    """An admin names a roster unit outside the system; empty removes the name."""
    identity.require_admin(actor)
    path, value = " ".join((path or "").split()), (value or "").strip()[:32]
    if not any(p == path or p.startswith(f"{path} › ") for p in unit_paths()):
        raise ValidationError("ไม่พบหน่วยนี้ในทะเบียน")
    row, _ = RosterUnitName.objects.get_or_create(path=path, defaults={"short_name": value})
    identity.audit(actor, "roster.unit_named", row, reason, unit=path, value=value)
    if value:
        row.short_name = value
        row.save(update_fields=["short_name"])
    else:
        row.delete()


@transaction.atomic
def set_position(actor, employee, position, reason=""):
    """An admin changes a person's ตำแหน่ง on the chart; like a move, until the next import."""
    identity.require_admin(actor)
    position = " ".join((position or "").split())[:200]
    employee = Employee.objects.select_for_update().get(pk=employee.pk)
    previous = employee.position
    employee.position = position
    employee.save(update_fields=["position"])
    identity.audit(
        actor, "employee.position_changed", employee, reason, value=position, previous=previous
    )
