"""การกระทบยอดทะเบียน (M11, ADR 0005): this system against the registry's (SAP) documents.

SAP cannot be connected, so it stays the owner of the figures (ADR 0003) and talks to this system
through files: staff post in SAP first, then record the fact here with the SAP document number and
an evidence file (manual_reference). Registry staff upload SAP exports (documents, serials,
balances); `reconcile` compares every recorded fact with the latest export of its document and
keeps one open ส่วนต่างทะเบียน per problem, closing it by itself once a later export agrees.
People decide what is wrong; nothing here changes a figure to make numbers match.
"""

import csv
import hashlib
import io
import re
import secrets
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.db.models import Exists, Max, OuterRef, Q
from django.utils import timezone

from operations import attachments, identity, inventory, options, policy
from operations.contracts import ErrorCode, fail
from operations.models import (
    Attachment,
    Case,
    Division,
    EquipmentType,
    RegistryConfig,
    RegistryDiscrepancy,
    RegistryDocumentLine,
    RegistryImport,
    RegistryRecord,
)

D = RegistryDiscrepancy
R = RegistryRecord.Kind
L = RegistryDocumentLine.Kind
MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 50_000
MAX_EVIDENCE = 5
UNRECONCILED_DAYS = 7  # Q11: older than this and in no export yet
CASE_NUMBER = re.compile(r"\b([WRP]\d{2}-\d{6})\b")
# Actions whose fact cites a registry document in manual_reference mode (Q13).
EVIDENCE_ACTIONS = frozenset(
    {
        "issue",
        "receive",
        "inspect",
        "decide_inspection",
        "resolve_discrepancy",
        "resolve_return_discrepancy",
    }
)
RECONCILED_KINDS = [k for k in D.Kind.values if k != D.Kind.EVIDENCE]
RECORD_FOR_DOCUMENT = {R.ISSUE: L.ISSUE, R.RETURN: L.RETURN, R.STATUS: L.STATUS}
OPEN = [D.Status.OPEN, D.Status.WITH_DIVISION, D.Status.WAITING_REGISTRY]

# --- Columns (Q14): canonical names, Thai labels and the SAP/Thai headers read by default ------

FIELD_LABELS = {
    "document_number": "เลขเอกสาร",
    "line_no": "รายการที่",
    "posting_date": "วันที่ผ่านรายการ",
    "movement_type": "ประเภทการเคลื่อนไหว",
    "official_code": "รหัสพัสดุ",
    "quantity": "จำนวน",
    "serial": "เลขเครื่อง",
    "reference": "อ้างอิง/ข้อความ",
    "reversed_document": "เอกสารที่ถูกกลับรายการ",
    "plant": "Plant",
    "storage_location": "ที่เก็บ (SLoc)",
    "unrestricted": "พร้อมจ่าย (Unrestricted)",
    "quality_inspection": "รอตรวจ (Quality inspection)",
    "blocked": "รอซ่อม (Blocked)",
}
DEFAULT_ALIASES = {
    "document_number": ["Material Document", "Mat. Doc.", "MatDoc", "เลขเอกสาร", "เลขที่เอกสาร"],
    "line_no": ["Material Doc.Item", "Mat.Doc.Item", "Item", "รายการที่"],
    "posting_date": ["Posting Date", "Pstng Date", "วันที่ผ่านรายการ", "วันที่"],
    "movement_type": ["Movement Type", "Movement type", "MvT", "ประเภทการเคลื่อนไหว"],
    "official_code": ["Material", "Material Number", "รหัสพัสดุ", "รหัสทางการ"],
    "quantity": ["Quantity", "Qty in UnE", "Qty in Un. of Entry", "จำนวน"],
    "serial": ["Serial Number", "Serial No.", "เลขเครื่อง"],
    "reference": ["Reference", "Document Header Text", "Text", "อ้างอิง", "ข้อความ"],
    "reversed_document": ["Reversed Document", "Reversal of Doc.", "เอกสารที่ถูกกลับรายการ"],
    "plant": ["Plant", "Plnt"],
    "storage_location": ["Storage Location", "SLoc", "ที่เก็บ"],
    "unrestricted": ["Unrestricted", "Unrestricted use", "Unrestr. Use", "พร้อมจ่าย"],
    "quality_inspection": ["In Quality Insp.", "Quality Inspection", "รอตรวจ"],
    "blocked": ["Blocked", "Blocked Stock", "รอซ่อม"],
}
LAYOUTS = {
    RegistryImport.Kind.DOCUMENTS: {
        "required": ["document_number", "movement_type", "official_code", "quantity"],
        "optional": [
            "line_no",
            "posting_date",
            "serial",
            "reference",
            "reversed_document",
            "plant",
            "storage_location",
        ],
    },
    RegistryImport.Kind.SERIALS: {
        "required": ["document_number", "official_code", "serial"],
        "optional": ["line_no"],
    },
    RegistryImport.Kind.BALANCES: {
        "required": ["official_code", "unrestricted"],
        "optional": ["plant", "storage_location", "quality_inspection", "blocked"],
    },
}
MOVEMENT_KINDS = [L.ISSUE, L.RETURN, L.STATUS, L.REVERSAL]


# --- Who may do what ----------------------------------------------------------------------------


def registry_division():
    """The unit the active policy names as registry (SIM-REG in the synthetic policy)."""
    current = policy.current_policy()
    code = (current.units.get("registry") if current else "") or ""
    return Division.objects.filter(code=code, is_active=True).first() if code else None


def per_division():
    """ADR 0008 (real structure Q6, Q15): no registry unit; each division uploads its own SAP
    exports and decides the registry discrepancies of its own facts and cases."""
    return registry_division() is None


def admins_only():
    """ตัวเลือกระบบ registry_access (15 ต.ค. 2569 Q6): admins hold the registry, else divisions."""
    return options.get(options.REGISTRY_ACCESS) == "admin"


def is_registry_staff(user):
    """Decides ส่วนต่างทะเบียน: the admins when the registry is admins only; else the registry
    unit's staff, or (per division) any division's staff for their own (see `may_decide`)."""
    if not getattr(user, "is_authenticated", False):
        return False
    scope = identity.staff_scope(user)
    if admins_only():
        return scope.is_admin
    division = registry_division()
    if division is None:
        return scope.is_support or bool(scope.division_roles)
    return scope.is_support or bool(scope.division_roles.get(division.pk))


def home_divisions(d):
    """The divisions a discrepancy belongs to: whose fact, whose case, and where it was sent."""
    ids = {d.division_id}
    if d.record_id:
        ids.add(d.record.division_id)
    if d.case_id:
        ids.add(d.case.owner_division_id)
    return ids - {None}


def may_decide(user, d):
    if not is_registry_staff(user):
        return False
    scope = identity.staff_scope(user)
    if admins_only():
        return True
    return (
        not per_division()
        or scope.is_support
        or bool(home_divisions(d) & set(scope.division_roles))
    )


def own_divisions(user):
    """None for a full view (registry unit, ผู้ช่วยเหลือระบบ, admin); else the user's divisions."""
    scope = identity.staff_scope(user)
    if not per_division() or scope.is_support or scope.is_admin:
        return None
    return set(scope.division_roles)


def case_condition(divisions):
    """Registry staff read (never act on) cases whose facts cite SAP or that a discrepancy
    names, so they can check them; other units get nothing from this."""
    home = registry_division()
    if home is None or home.pk not in divisions:
        return Q(pk__in=[])
    return Q(Exists(RegistryRecord.objects.filter(case=OuterRef("pk")))) | Q(
        Exists(RegistryDiscrepancy.objects.filter(case=OuterRef("pk")))
    )


def may_open(user):
    """The registry page: registry staff, or an admin (read-only)."""
    return is_registry_staff(user) or identity.staff_scope(user).is_admin


def may_import(user):
    return is_registry_staff(user) or identity.staff_scope(user).is_admin


def _require(allowed, message):
    if not allowed:
        raise PermissionDenied(message)


# --- Configuration (Q14–Q16) --------------------------------------------------------------------


def config():
    row, _ = RegistryConfig.objects.get_or_create(pk=1)
    return row


def aliases(row=None):
    row = row or config()
    merged = {name: list(names) for name, names in DEFAULT_ALIASES.items()}
    for name, names in (row.column_aliases or {}).items():
        if name in merged:
            merged[name] = list(dict.fromkeys([*names, *merged[name]]))
    return merged


def movement_kind(code, row=None):
    row = row or config()
    for kind in MOVEMENT_KINDS:
        if code in (row.movement_types or {}).get(kind, []):
            return kind
    return L.OTHER


def _codes(text):
    return sorted({c.strip() for c in re.split(r"[\s,;]+", text or "") if c.strip()})


def update_config(actor, movement_types, column_aliases, storage_locations, reason):
    """Admin only, audited. Inputs are plain text from the settings form."""
    identity.require_admin(actor)
    types = {kind: _codes(movement_types.get(kind, "")) for kind in MOVEMENT_KINDS}
    seen = {}
    for kind, codes in types.items():
        for code in codes:
            if code in seen:
                raise ValidationError(f"ประเภทการเคลื่อนไหว {code} อยู่ทั้ง {seen[code]} และ {kind}")
            seen[code] = kind
    names = {}
    for name in DEFAULT_ALIASES:
        extra = [a.strip() for a in (column_aliases.get(name, "") or "").splitlines() if a.strip()]
        if extra:
            names[name] = extra[:20]
    places = []
    for line in (storage_locations or "").splitlines():
        parts = [p.strip() for p in re.split(r"[/,\s]+", line) if p.strip()]
        if parts:
            places.append(parts[:2] if len(parts) > 1 else [parts[0], ""])
    row = config()
    row.movement_types, row.column_aliases, row.storage_locations = types, names, places
    row.updated_by = actor
    row.save()
    identity.audit(
        actor,
        "registry.config_updated",
        row,
        reason,
        movement_types=types,
        storage_locations=places,
        aliases=sorted(names),
    )
    return row


def set_line_group(actor, item, group, reason=""):
    identity.require_admin(actor)
    group = (group or "").strip()
    if group and group not in EquipmentType.LineGroup.values:
        raise ValidationError("เลือกหมวดเมนู LINE จากรายการ")
    before = item.line_group
    if before != group:
        item.line_group = group
        item.save(update_fields=["line_group"])
        identity.audit(actor, "registry.line_group_set", item, reason, before=before, after=group)
    return item


def set_official_code(actor, item, code, reason=""):
    """Q5: one registry code per item, unique; empty clears it (admin, audited)."""
    identity.require_admin(actor)
    code = (code or "").strip()
    if len(code) > 40:
        raise ValidationError("รหัสทางการยาวเกิน 40 ตัวอักษร")
    if code and EquipmentType.objects.filter(official_code=code).exclude(pk=item.pk).exists():
        raise ValidationError(f"รหัสทางการ {code} ใช้กับของอื่นแล้ว")
    before = item.official_code
    if before == code:
        return item
    item.official_code = code
    item.save(update_fields=["official_code"])
    identity.audit(
        actor, "registry.official_code_set", item, reason, item=item.code, before=before, after=code
    )
    return item


def import_official_codes(actor, name, data):
    """A two-column file (our code, registry code); all rows or none."""
    identity.require_admin(actor)
    headers, rows = read_table(data, name)
    index = {_normal(h): i for i, h in enumerate(headers)}
    ours = next((index[k] for k in ("code", "รหัสในระบบ", "รหัสของ") if k in index), None)
    theirs = next(
        (index[_normal(a)] for a in DEFAULT_ALIASES["official_code"] if _normal(a) in index), None
    )
    if ours is None or theirs is None:
        raise ValidationError("ไฟล์ต้องมีคอลัมน์ 'code' (รหัสในระบบ) และ 'รหัสพัสดุ' (หรือ Material)")
    items = {i.code: i for i in EquipmentType.objects.all()}
    pairs, problems = [], []
    for number, row in enumerate(rows, start=2):
        code, official = _cell(row, ours), _cell(row, theirs)
        if not code:
            continue
        if code not in items:
            problems.append(f"แถว {number}: ไม่พบของรหัส {code}")
        else:
            pairs.append((items[code], official))
    officials = [o for _, o in pairs if o]
    if len(officials) != len(set(officials)):
        problems.append("รหัสทางการซ้ำกันในไฟล์")
    if problems:
        raise ValidationError(problems[:20])
    with transaction.atomic():
        EquipmentType.objects.filter(pk__in=[i.pk for i, _ in pairs]).update(official_code="")
        for item, official in pairs:
            item.refresh_from_db()
            set_official_code(actor, item, official, f"นำเข้าจากไฟล์ {name}")
    return len(pairs)


# --- Reading SAP exports (Q14) -----------------------------------------------------------------


def _normal(text):
    return re.sub(r"[\s._]+", " ", str(text or "")).strip().casefold()


def _cell(row, index):
    if index is None or index >= len(row) or row[index] is None:
        return ""
    value = row[index]
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _decode(data):
    for encoding in ("utf-8-sig", "cp874"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValidationError("อ่านไฟล์ไม่ได้: รองรับ UTF-8 หรือ Windows-874 (TIS-620)")


def read_table(data, name):
    """(headers, rows) from CSV (comma/semicolon/tab) or .xlsx (first sheet)."""
    if len(data) > MAX_BYTES:
        raise ValidationError("ไฟล์ใหญ่เกิน 5 MB")
    if name.lower().endswith(".xlsx") or data[:2] == b"PK":
        from openpyxl import load_workbook

        try:
            book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as exc:  # noqa: BLE001 - any broken workbook is the same answer
            raise ValidationError("เปิดไฟล์ Excel ไม่ได้") from exc
        rows = [list(r) for r in book.worksheets[0].iter_rows(values_only=True)]
        book.close()
    else:
        text = _decode(data)
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(io.StringIO(text), dialect))
    rows = [r for r in rows if any(c not in (None, "") for c in r)]
    if not rows:
        raise ValidationError("ไฟล์ไม่มีข้อมูล")
    if len(rows) - 1 > MAX_ROWS:
        raise ValidationError(f"ไฟล์มีเกิน {MAX_ROWS:,} แถว แบ่งเป็นหลายไฟล์")
    headers = [str(h or "").strip() for h in rows[0]]
    return headers, rows[1:]


def map_columns(headers, kind, row=None):
    """Field → column index by name or alias; missing required columns are named in Thai."""
    names = aliases(row)
    normal = {_normal(h): i for i, h in enumerate(headers)}
    found = {}
    layout = LAYOUTS[kind]
    for field in [*layout["required"], *layout["optional"]]:
        for candidate in [field, *names.get(field, [])]:
            if _normal(candidate) in normal:
                found[field] = normal[_normal(candidate)]
                break
    missing = [FIELD_LABELS[f] for f in layout["required"] if f not in found]
    if missing:
        raise ValidationError("ไม่พบคอลัมน์: " + ", ".join(missing) + " (ตั้งชื่อเรียกอื่นได้ที่หน้าตั้งค่าทะเบียน)")
    return found


def parse_number(value):
    """SAP numbers: '1,234.000', '3-' (negative), '1.234,5' (comma decimals) or numbers."""
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return Decimal(str(value))
    text = str(value or "").strip().replace(" ", "")
    if not text:
        return None
    negative = text.endswith("-") or text.startswith("-")
    text = text.strip("-")
    if "," in text and "." in text and text.rfind(",") > text.rfind("."):
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", "")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(value) from exc
    return -number if negative else number


def parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in ("%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d", "%Y%m%d", "%d-%m-%Y"):
        try:
            parsed = datetime.strptime(text[:10], pattern).date()
        except ValueError:
            continue
        if parsed.year > 2400:  # Buddhist Era
            parsed = parsed.replace(year=parsed.year - 543)
        return parsed
    raise ValueError(value)


def _serials(text):
    return [s for s in re.split(r"[\s,;]+", str(text or "")) if s]


# --- Storing an upload ---------------------------------------------------------------------------


def storage_dir():
    path = attachments.storage_dir().parent / "registry"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _keep(data):
    name = secrets.token_hex(16)
    path = storage_dir() / name
    path.write_bytes(data)
    path.chmod(0o600)
    return name


def _new_import(actor, kind, name, data, exported_at, period_from=None, period_to=None):
    digest = hashlib.sha256(data).hexdigest()
    earlier = RegistryImport.objects.filter(sha256=digest).first()
    if earlier is not None:
        when = timezone.localtime(earlier.uploaded_at)
        raise ValidationError(
            f"ไฟล์นี้นำเข้าแล้วเมื่อ {when:%d/%m/}{when.year + 543} {when:%H:%M} น. (Q21)"
        )
    if exported_at > timezone.now() + timedelta(minutes=5):
        raise ValidationError("เวลาส่งออกจาก SAP อยู่ในอนาคต")
    if period_from and period_to and period_from > period_to:
        raise ValidationError("ช่วงวันที่ไม่ถูกต้อง")
    if period_to and period_to > timezone.localdate():
        raise ValidationError("ไฟล์ครอบคลุมวันที่ในอนาคตไม่ได้")
    return RegistryImport(
        kind=kind,
        original_name=(name or "export")[:200],
        storage_name=_keep(data),
        sha256=digest,
        size=len(data),
        period_from=period_from,
        period_to=period_to,
        exported_at=exported_at,
        uploaded_by=actor,
    )


def import_documents(actor, name, data, exported_at, period_from, period_to, now=None):
    """รายการเอกสารจาก SAP (Q4/Q11): all rows or none, then reconcile; returns the import."""
    _require(may_import(actor), "เฉพาะกองทะเบียนหรือแอดมินนำเข้าไฟล์ทะเบียน")
    if period_from is None or period_to is None:
        raise ValidationError("ระบุช่วงวันที่ที่ไฟล์ครอบคลุม")
    row = config()
    headers, rows = read_table(data, name)
    columns = map_columns(headers, RegistryImport.Kind.DOCUMENTS, row)
    lines, problems, ignored = [], [], 0
    for number, values in enumerate(rows, start=2):

        def get(field, values=values):
            return _cell(values, columns.get(field))

        document = get("document_number")
        if not document:
            problems.append(f"แถว {number}: ไม่มีเลขเอกสาร")
            continue
        try:
            quantity = parse_number(values[columns["quantity"]])
            posting = (
                parse_date(values[columns["posting_date"]]) if "posting_date" in columns else None
            )
            line_no = int(parse_number(get("line_no")) or 0) if "line_no" in columns else 0
        except (ValueError, IndexError):
            problems.append(f"แถว {number}: จำนวน วันที่ หรือรายการที่อ่านไม่ได้")
            continue
        kind = movement_kind(get("movement_type"), row)
        if kind == L.OTHER:
            ignored += 1
        reference = get("reference")
        match = CASE_NUMBER.search(reference.upper())
        lines.append(
            RegistryDocumentLine(
                document_number=document[:64],
                line_no=line_no,
                movement_type=get("movement_type")[:16],
                kind=kind,
                official_code=get("official_code")[:40],
                quantity=abs(quantity) if quantity is not None else None,
                serials=_serials(get("serial")),
                posting_date=posting,
                reference=reference[:200],
                case_number=match.group(1) if match else "",
                reversed_document=get("reversed_document")[:64],
                plant=get("plant")[:16],
                storage_location=get("storage_location")[:16],
            )
        )
    if problems:
        raise ValidationError(
            problems[:20] + ([f"และอีก {len(problems) - 20} แถว"] if len(problems) > 20 else [])
        )
    if not row.movement_types or not any(row.movement_types.values()):
        raise ValidationError("ยังไม่ได้ตั้งประเภทการเคลื่อนไหวของ SAP (หน้าตั้งค่าทะเบียน) จึงแยกจ่าย/รับคืนไม่ได้")
    with transaction.atomic():
        upload = _new_import(
            actor, RegistryImport.Kind.DOCUMENTS, name, data, exported_at, period_from, period_to
        )
        upload.rows = len(lines)
        upload.save()
        for line in lines:
            line.source = upload
        RegistryDocumentLine.objects.bulk_create(lines, batch_size=1000)
        summary = reconcile(found_by=upload, now=now)
        summary["ignored_rows"] = ignored
        upload.summary = summary
        upload.save(update_fields=["summary"])
        identity.audit(
            actor,
            "registry.documents_imported",
            upload,
            name,
            rows=len(lines),
            period=f"{period_from}..{period_to}",
            new=summary["new"],
        )
        _mail_registry(upload, summary)
    return upload


def import_serials(actor, name, data, exported_at, now=None):
    """เลขเครื่องของเอกสาร (Q15): serials a documents export lacks, merged by document+code."""
    _require(may_import(actor), "เฉพาะกองทะเบียนหรือแอดมินนำเข้าไฟล์ทะเบียน")
    headers, rows = read_table(data, name)
    columns = map_columns(headers, RegistryImport.Kind.SERIALS)
    grouped = defaultdict(list)
    for values in rows:
        document = _cell(values, columns["document_number"])
        code = _cell(values, columns["official_code"])
        serial = _cell(values, columns["serial"])
        if document and code and serial:
            grouped[(document[:64], code[:40])] += _serials(serial)
    if not grouped:
        raise ValidationError("ไฟล์ไม่มีเลขเครื่อง")
    with transaction.atomic():
        upload = _new_import(actor, RegistryImport.Kind.SERIALS, name, data, exported_at)
        upload.rows = sum(len(v) for v in grouped.values())
        upload.save()
        RegistryDocumentLine.objects.bulk_create(
            RegistryDocumentLine(
                source=upload, document_number=doc, official_code=code, serials=serials
            )
            for (doc, code), serials in grouped.items()
        )
        upload.summary = reconcile(found_by=upload, now=now)
        upload.save(update_fields=["summary"])
        identity.audit(actor, "registry.serials_imported", upload, name, rows=upload.rows)
        _mail_registry(upload, upload.summary)
    return upload


def import_balances(actor, name, data, exported_at):
    """ยอดคงเหลือ (Q16): Unrestricted → พร้อมจ่าย, QI → รอตรวจ, Blocked → รอซ่อม, for the
    storage locations of ฝอจ.; stored as an advisory snapshot with its as_of (ADR 0003)."""
    _require(may_import(actor), "เฉพาะกองทะเบียนหรือแอดมินนำเข้าไฟล์ทะเบียน")
    row = config()
    headers, rows = read_table(data, name)
    columns = map_columns(headers, RegistryImport.Kind.BALANCES, row)
    places = {tuple(p) for p in row.storage_locations or []}
    totals = defaultdict(
        lambda: {
            "available": Decimal(0),
            "awaiting_inspection": Decimal(0),
            "awaiting_repair": Decimal(0),
        }
    )
    skipped_places, problems = 0, []
    for number, values in enumerate(rows, start=2):
        code = _cell(values, columns["official_code"])
        if not code:
            continue
        place = (
            _cell(values, columns.get("plant")),
            _cell(values, columns.get("storage_location")),
        )
        if places and place not in places and (place[0], "") not in places:
            skipped_places += 1
            continue
        try:
            for field, target in (
                ("unrestricted", "available"),
                ("quality_inspection", "awaiting_inspection"),
                ("blocked", "awaiting_repair"),
            ):
                if field in columns:
                    totals[code][target] += parse_number(values[columns[field]]) or Decimal(0)
        except (ValueError, IndexError):
            problems.append(f"แถว {number}: ตัวเลขอ่านไม่ได้")
    if problems:
        raise ValidationError(problems[:20])
    items = {i.official_code: i for i in EquipmentType.objects.exclude(official_code="")}
    snapshot_rows = [
        {"item": items[code].code, **{k: str(v) for k, v in figures.items()}}
        for code, figures in totals.items()
        if code in items
    ]
    unmapped = sorted(code for code in totals if code not in items)
    if not snapshot_rows:
        raise ValidationError("ไม่มีรหัสพัสดุในไฟล์ที่ตรงกับรหัสทางการของของในระบบ")
    with transaction.atomic():
        upload = _new_import(actor, RegistryImport.Kind.BALANCES, name, data, exported_at)
        upload.rows = len(rows)
        upload.summary = {
            "items": len(snapshot_rows),
            "unmapped": unmapped[:50],
            "skipped_places": skipped_places,
        }
        upload.save()
        inventory.import_snapshot(
            actor, snapshot_rows, exported_at, f"SAP: {upload.original_name}", authorized=True
        )
    return upload


# --- Facts recorded against the registry (Q3/Q13) ----------------------------------------------


def take_evidence(ctx, case, payload):
    """1–5 files this person just attached to this case, not yet evidence of another fact."""
    ids = payload.get("evidence") or []
    if not isinstance(ids, list) or not ids:
        fail(ErrorCode.VALIDATION_ERROR, "แนบหลักฐานเอกสารจากระบบทะเบียนเดิม (manual_reference)")
    if len(ids) > MAX_EVIDENCE:
        fail(ErrorCode.VALIDATION_ERROR, f"แนบหลักฐานได้ไม่เกิน {MAX_EVIDENCE} ไฟล์")
    files = list(
        Attachment.objects.select_for_update().filter(
            pk__in=[str(i) for i in ids],
            case=case,
            uploaded_by=ctx.user,
            registry_record__isnull=True,
            created_at__gte=timezone.now() - timedelta(hours=1),
        )
    )
    usable = [
        f
        for f in files
        if f.scan_status not in (Attachment.ScanStatus.INFECTED, Attachment.ScanStatus.REJECTED)
    ]
    if len(usable) != len(set(map(str, ids))):
        fail(ErrorCode.VALIDATION_ERROR, "หลักฐานไม่ถูกต้อง แนบไฟล์ใหม่อีกครั้ง")
    return usable


def item_lines(rows):
    """[(item, quantity, serials)] → one entry per item with its registry code."""
    merged = {}
    for item, quantity, serials in rows:
        entry = merged.setdefault(
            item.pk,
            {
                "item": item.code,
                "official_code": item.official_code,
                "quantity": Decimal(0),
                "serials": [],
            },
        )
        entry["quantity"] += Decimal(quantity)
        entry["serials"] += list(serials or [])
    return [
        {**entry, "quantity": format(entry["quantity"].normalize(), "f")}
        for entry in merged.values()
    ]


def note(ctx, case, kind, action, source, document_number, rows, division_id, payload):
    """Manual mode: the registry document behind a fact, with its evidence; else nothing."""
    if inventory.mode() != inventory.MANUAL:
        return None
    evidence = take_evidence(ctx, case, payload)
    record = RegistryRecord.objects.create(
        case=case,
        kind=kind,
        action=action,
        source=source,
        document_number=document_number,
        lines=item_lines(rows),
        division_id=division_id or case.owner_division_id,
        recorded_by=ctx.user,
        recorded_at=timezone.now(),
    )
    Attachment.objects.filter(pk__in=[f.pk for f in evidence]).update(registry_record=record)
    return record


# --- Reconciliation (Q9–Q11) -----------------------------------------------------------------


def _lock():
    digest = hashlib.sha256(b"registry-reconcile").digest()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s)", [int.from_bytes(digest[:8], "big", signed=True)]
        )


def _working_day(calendar, day):
    return day.isoweekday() in calendar.weekdays and day not in calendar.holidays


def last_working_day(day, calendar=None):
    from operations.queues import calendar_now

    calendar = calendar or calendar_now()
    for _ in range(31):
        if _working_day(calendar, day):
            return day
        day -= timedelta(days=1)
    return day


def coverage(calendar=None):
    """Spans [from, last working day) of every documents export (Q11)."""
    return [
        (start, last_working_day(end, calendar))
        for start, end in RegistryImport.objects.filter(
            kind=RegistryImport.Kind.DOCUMENTS
        ).values_list("period_from", "period_to")
    ]


def covered(day, spans):
    return any(start <= day < end for start, end in spans)


def documents(numbers=None):
    """Each document as its latest export says (Q21), with serials files merged in;
    `numbers` limits the work to these documents (and reversals that name them)."""
    base = RegistryDocumentLine.objects.all()
    if numbers is not None:
        base = base.filter(Q(document_number__in=numbers) | Q(reversed_document__in=numbers))
    latest = {}
    for number, source_id, exported in (
        base.filter(source__kind=RegistryImport.Kind.DOCUMENTS)
        .values_list("document_number", "source_id", "source__exported_at")
        .distinct()
    ):
        best = latest.get(number)
        if best is None or (exported, source_id) > best:
            latest[number] = (exported, source_id)
    docs = {}
    reversed_numbers = set()
    lines = base.filter(source__kind=RegistryImport.Kind.DOCUMENTS).values_list(
        "document_number",
        "source_id",
        "kind",
        "official_code",
        "quantity",
        "serials",
        "case_number",
        "reversed_document",
    )
    for number, source_id, kind, code, quantity, serials, case_number, reversed_doc in lines:
        if latest[number][1] != source_id:
            continue
        doc = docs.setdefault(
            number,
            {
                "kinds": set(),
                "codes": defaultdict(Decimal),
                "serials": defaultdict(set),
                "cases": set(),
                "reversed": False,
                "source": source_id,
            },
        )
        doc["kinds"].add(kind)
        if kind == L.REVERSAL and reversed_doc:
            reversed_numbers.add(reversed_doc)
            continue
        if code:
            doc["codes"][code] += quantity or Decimal(0)
            doc["serials"][code].update(serials or [])
        if case_number:
            doc["cases"].add(case_number)
    serial_latest = {}
    for number, code, serials, exported, source_id in base.filter(
        source__kind=RegistryImport.Kind.SERIALS
    ).values_list(
        "document_number", "official_code", "serials", "source__exported_at", "source_id"
    ):
        key = (number, code)
        if key not in serial_latest or (exported, source_id) > serial_latest[key][0]:
            serial_latest[key] = ((exported, source_id), serials)
    for (number, code), (_, serials) in serial_latest.items():
        if number in docs:
            docs[number]["serials"][code] = set(serials)
    for number in reversed_numbers:
        if number in docs:
            docs[number]["reversed"] = True
    return docs


def _number(value):
    return format(Decimal(value).normalize(), "f")


def _wanted(records, docs, spans):
    """{key: fields} of every discrepancy the current data shows."""
    wanted = {}
    groups = defaultdict(list)
    recorded_numbers = set()

    def want(record, kind, expected, found, detail, number="", case=None):
        key = (record.pk, kind) if record else (None, number, kind)
        wanted[key] = {
            "record": record,
            "case": record.case if record else case,
            "document_number": record.document_number if record else number,
            "expected": expected,
            "found": found,
            "detail": detail[:300],
        }

    for record in records:
        recorded_numbers.add(record.document_number)
        missing = [line["item"] for line in record.lines if not line.get("official_code")]
        if missing:
            want(
                record,
                D.Kind.NO_CODE,
                {"items": missing},
                {},
                "ยังไม่ผูกรหัสพัสดุ: " + ", ".join(missing),
            )
        doc = docs.get(record.document_number)
        if doc is None:
            day = timezone.localtime(record.recorded_at).date()
            if covered(day, spans):
                want(
                    record,
                    D.Kind.NOT_FOUND,
                    {"document": record.document_number},
                    {},
                    f"ไม่พบเอกสาร {record.document_number} ในไฟล์ที่ครอบคลุมวันที่ "
                    f"{day:%d/%m/}{day.year + 543}",
                )
            continue
        expected_kind = RECORD_FOR_DOCUMENT.get(record.kind)
        if doc["reversed"]:
            want(
                record,
                D.Kind.TYPE,
                {"kind": record.kind},
                {"reversed": True},
                f"เอกสาร {record.document_number} ถูกกลับรายการใน SAP แล้ว",
            )
            continue
        if expected_kind and expected_kind not in doc["kinds"]:
            found = sorted(k for k in doc["kinds"] if k != L.OTHER) or ["other"]
            want(
                record,
                D.Kind.TYPE,
                {"kind": expected_kind},
                {"kinds": found},
                f"บันทึกเป็น{L(expected_kind).label} แต่ใน SAP เป็น "
                + ", ".join(L(k).label if k in L.values else k for k in found),
            )
            continue
        if expected_kind:
            groups[(record.document_number, record.kind)].append(record)

    for (number, _kind), members in groups.items():
        doc = docs[number]
        ours, serials = defaultdict(Decimal), defaultdict(set)
        for record in members:
            for line in record.lines:
                if line.get("official_code"):
                    ours[line["official_code"]] += Decimal(line["quantity"])
                    serials[line["official_code"]].update(line.get("serials") or [])
        first = members[0]
        others = [m.case.number for m in members[1:] if m.case_id != first.case_id]
        suffix = f" (รวมเรื่อง {', '.join(sorted(set(others)))})" if others else ""
        quantity_gap = {
            c: (q, doc["codes"].get(c, Decimal(0)))
            for c, q in ours.items()
            if doc["codes"].get(c, Decimal(0)) != q
        }
        if quantity_gap:
            want(
                first,
                D.Kind.QUANTITY,
                {c: _number(q) for c, (q, _) in quantity_gap.items()},
                {c: _number(t) for c, (_, t) in quantity_gap.items()},
                "; ".join(
                    f"{c} บันทึก {_number(q)} · SAP {_number(t)}"
                    for c, (q, t) in quantity_gap.items()
                )
                + suffix,
            )
        serial_gap = {
            c: s
            for c, s in serials.items()
            if s and doc["serials"].get(c) and doc["serials"][c] != s
        }
        if serial_gap:
            want(
                first,
                D.Kind.SERIALS,
                {c: sorted(s) for c, s in serial_gap.items()},
                {c: sorted(doc["serials"][c]) for c in serial_gap},
                "; ".join(
                    f"{c} ขาดใน SAP {', '.join(sorted(s - doc['serials'][c])) or '-'}"
                    f" · เกินใน SAP {', '.join(sorted(doc['serials'][c] - s)) or '-'}"
                    for c, s in serial_gap.items()
                )
                + suffix,
            )

    for number, doc in docs.items():
        if number in recorded_numbers or not doc["cases"] or doc["reversed"]:
            continue
        if not doc["kinds"] & {L.ISSUE, L.RETURN, L.STATUS}:
            continue
        cases = sorted(doc["cases"])
        want(
            None,
            D.Kind.UNRECORDED,
            {},
            {"cases": cases, "codes": {c: _number(q) for c, q in doc["codes"].items()}},
            f"เอกสาร {number} ใน SAP อ้างเรื่อง {', '.join(cases)} แต่ไม่มีบันทึกในระบบนี้",
            number,
            Case.objects.filter(number__in=cases).order_by("number").first(),
        )
    return wanted


def _suppressed(key):
    """An accepted discrepancy is not raised again for the same record (or document) and kind."""
    accepted = D.objects.filter(status=D.Status.RESOLVED, resolution=D.Resolution.ACCEPTED)
    if key[0] is not None:
        return accepted.filter(record_id=key[0], kind=key[1]).exists()
    return accepted.filter(record__isnull=True, document_number=key[1], kind=key[2]).exists()


def reconcile(found_by=None, now=None):
    """Compare every recorded fact with the registry; open, refresh or close discrepancies."""
    now = now or timezone.now()
    _lock()
    docs = documents()
    spans = coverage()
    records = list(RegistryRecord.objects.select_related("case").order_by("recorded_at", "id"))
    wanted = _wanted(records, docs, spans)
    existing = {}
    for d in D.objects.select_for_update().filter(status__in=OPEN, kind__in=RECONCILED_KINDS):
        key = (d.record_id, d.kind) if d.record_id else (None, d.document_number, d.kind)
        existing[key] = d
    new = defaultdict(int)
    for key, fields in wanted.items():
        current = existing.pop(key, None)
        if current is not None:
            changed = (current.expected, current.found, current.detail) != (
                fields["expected"],
                fields["found"],
                fields["detail"],
            )
            if changed:
                current.expected, current.found, current.detail = (
                    fields["expected"],
                    fields["found"],
                    fields["detail"],
                )
                current.save(update_fields=["expected", "found", "detail", "updated_at"])
            continue
        if _suppressed(key):
            continue
        kind = key[-1]
        D.objects.create(kind=kind, opened_at=now, found_by=found_by, **fields)
        new[kind] += 1
    for stale in existing.values():
        stale.status, stale.resolution = D.Status.RESOLVED, D.Resolution.MATCHED
        stale.resolution_reason = "ข้อมูลล่าสุดตรงกันแล้ว"
        stale.resolved_at = now
        stale.save()
    return {
        "new": dict(new),
        "new_total": sum(new.values()),
        "resolved": len(existing),
        "open": D.objects.filter(status__in=OPEN).count(),
    }


def unreconciled(now=None, divisions=None):
    """Q11: facts older than 7 days whose document is in no export and whose day no export
    covers yet (a warning for registry staff, not a discrepancy); `divisions` narrows to the
    facts those divisions recorded (ADR 0008)."""
    now = now or timezone.now()
    spans = coverage()
    known = set(
        RegistryDocumentLine.objects.filter(source__kind=RegistryImport.Kind.DOCUMENTS)
        .values_list("document_number", flat=True)
        .distinct()
    )
    old = RegistryRecord.objects.filter(recorded_at__lt=now - timedelta(days=UNRECONCILED_DAYS))
    if divisions is not None:
        old = old.filter(division_id__in=divisions)
    return [
        r
        for r in old.select_related("case").order_by("recorded_at")
        if r.document_number not in known
        and not covered(timezone.localtime(r.recorded_at).date(), spans)
    ]


def record_state(record, docs=None, spans=None):
    """What the case page says about one fact: matched, waiting for an export, or a problem."""
    if record.discrepancies.filter(status__in=OPEN).exists():
        return "discrepancy"
    docs = documents() if docs is None else docs
    if record.document_number in docs:
        return "matched"
    spans = coverage() if spans is None else spans
    if covered(timezone.localtime(record.recorded_at).date(), spans):
        return "discrepancy"
    return "waiting"


# --- Deciding a discrepancy (Q12) --------------------------------------------------------------


def _locked(discrepancy_id):
    d = (
        D.objects.select_for_update(of=("self",))
        .select_related("record", "case")
        .filter(pk=discrepancy_id)
        .first()
    )
    if d is None:
        raise ValidationError("ไม่พบส่วนต่างนี้")
    return d


def _reason(text, message="ระบุเหตุผล"):
    text = (text or "").strip()
    if not text and message != "ระบุเหตุผล":
        raise ValidationError(message)
    return text[:2000]


def _registry_only(actor, d=None):
    _require(
        is_registry_staff(actor) and (d is None or may_decide(actor, d)),
        "เฉพาะเจ้าหน้าที่กองทะเบียนตัดสินส่วนต่างทะเบียน"
        if not per_division()
        else "เฉพาะกองเจ้าของข้อเท็จจริงตัดสินส่วนต่างทะเบียนนี้",
    )


def _close(d, actor, resolution, reason, now):
    d.status, d.resolution, d.resolution_reason = D.Status.RESOLVED, resolution, reason
    d.resolved_by, d.resolved_at = actor, now
    d.save()


@transaction.atomic
def fix_number(actor, discrepancy_id, number, reason):
    """(ก) The document number was typed wrong: the fact keeps its history, the record gets the
    registry's number, and the comparison runs again at once."""
    _registry_only(actor)
    d = _locked(discrepancy_id)
    _registry_only(actor, d)
    if d.status == D.Status.RESOLVED or d.record is None:
        raise ValidationError("แก้เลขเอกสารได้เฉพาะส่วนต่างที่ยังเปิดและผูกกับบันทึก")
    number = (number or "").strip()[:64]
    if not number or number == d.record.document_number:
        raise ValidationError("ระบุเลขเอกสารใหม่ที่ต่างจากเดิม")
    reason = _reason(reason)
    record = RegistryRecord.objects.select_for_update().get(pk=d.record_id)
    before = record.document_number
    record.document_number = number
    record.save(update_fields=["document_number"])
    now = timezone.now()
    _close(d, actor, D.Resolution.NUMBER_FIXED, reason, now)
    identity.audit(
        actor,
        "registry.number_fixed",
        record,
        reason,
        number=record.case.number,
        before=before,
        after=number,
        discrepancy=d.pk,
    )
    return reconcile(now=now)


@transaction.atomic
def send_to_division(actor, discrepancy_id, note_text):
    """(ข) The division that recorded the fact checks it (not a งานย่อย: the case may be closed)."""
    _registry_only(actor)
    d = _locked(discrepancy_id)
    _registry_only(actor, d)
    if d.status not in (D.Status.OPEN, D.Status.WAITING_REGISTRY):
        raise ValidationError("ส่งให้กองตรวจได้เฉพาะส่วนต่างที่รอตรวจ")
    division_id = (
        d.record.division_id if d.record else (d.case.owner_division_id if d.case else None)
    )
    if division_id is None:
        raise ValidationError("ส่วนต่างนี้ไม่ผูกกับกองใด")
    d.status, d.division_id = D.Status.WITH_DIVISION, division_id
    d.division_note = _reason(note_text, "บอกกองว่าต้องตรวจอะไร")
    d.division_answer, d.division_answer_note, d.answered_by, d.answered_at = "", "", None, None
    d.save()
    identity.audit(actor, "registry.sent_to_division", d, d.division_note, division=d.division.code)
    return d


@transaction.atomic
def answer(actor, discrepancy_id, answer_kind, note_text):
    """The division's answer goes back to registry staff, who decide."""
    d = _locked(discrepancy_id)
    if d.status != D.Status.WITH_DIVISION:
        raise ValidationError("ส่วนต่างนี้ไม่ได้รอกองตรวจ")
    _require(
        bool(identity.staff_scope(actor).division_roles.get(d.division_id)),
        "เฉพาะสมาชิกกองที่ถูกขอให้ตรวจ",
    )
    if answer_kind not in D.Answer.values:
        raise ValidationError("เลือกผลการตรวจ")
    d.division_answer, d.division_answer_note = answer_kind, _reason(note_text)
    d.answered_by, d.answered_at = actor, timezone.now()
    d.status = D.Status.OPEN
    d.save()
    identity.audit(
        actor, "registry.division_answered", d, d.division_answer_note, answer=answer_kind
    )
    return d


@transaction.atomic
def wait_for_registry(actor, discrepancy_id, reference, reason):
    """(ค) This system is right; SAP will be corrected. Closes by itself once an export agrees."""
    _registry_only(actor)
    d = _locked(discrepancy_id)
    _registry_only(actor, d)
    if d.status not in (D.Status.OPEN, D.Status.WITH_DIVISION):
        raise ValidationError("ส่วนต่างนี้ไม่ได้รอตรวจ")
    if d.kind == D.Kind.EVIDENCE:
        raise ValidationError("หลักฐานต้องแนบใหม่ในระบบนี้")
    d.registry_reference = (reference or "").strip()[:64]
    if not d.registry_reference:
        raise ValidationError("ระบุเลขเอกสารแก้ไขของ SAP")
    d.resolution_reason = _reason(reason)
    d.status = D.Status.WAITING_REGISTRY
    d.save()
    identity.audit(
        actor,
        "registry.waiting_for_registry",
        d,
        d.resolution_reason,
        reference=d.registry_reference,
    )
    return d


@transaction.atomic
def accept(actor, discrepancy_id, reason):
    """(ง) Accepted with a reason; not raised again for the same fact and kind."""
    _registry_only(actor)
    d = _locked(discrepancy_id)
    _registry_only(actor, d)
    if d.status == D.Status.RESOLVED:
        raise ValidationError("ส่วนต่างนี้ปิดแล้ว")
    _close(d, actor, D.Resolution.ACCEPTED, _reason(reason), timezone.now())
    identity.audit(actor, "registry.accepted", d, d.resolution_reason, kind=d.kind)
    return d


def may_add_evidence(user, record):
    scope = identity.staff_scope(user)
    return is_registry_staff(user) or bool(scope.division_roles.get(record.division_id))


@transaction.atomic
def add_evidence(actor, record_id, files):
    """A new evidence file for a fact (after the old one failed its scan)."""
    from operations import uploads

    record = RegistryRecord.objects.select_for_update().select_related("case").get(pk=record_id)
    _require(may_add_evidence(actor, record), "เฉพาะกองที่บันทึกหรือกองทะเบียนแนบหลักฐาน")
    problems = uploads.check(files)
    if problems or not files:
        raise ValidationError(problems or ["เลือกไฟล์หลักฐาน"])
    stored = uploads.store(actor, record.case, files, note="หลักฐานทะเบียน (แนบใหม่)")
    Attachment.objects.filter(pk__in=[a.pk for a in stored]).update(registry_record=record)
    return stored


def after_scan(attachment):
    """Evidence that failed its scan opens a discrepancy (Q13); a clean replacement closes it."""
    if attachment.registry_record_id is None:
        return
    S = Attachment.ScanStatus
    with transaction.atomic():
        record = (
            RegistryRecord.objects.select_for_update()
            .select_related("case")
            .get(pk=attachment.registry_record_id)
        )
        current = D.objects.filter(record=record, kind=D.Kind.EVIDENCE, status__in=OPEN).first()
        if attachment.scan_status in (S.INFECTED, S.REJECTED) and current is None:
            D.objects.create(
                kind=D.Kind.EVIDENCE,
                record=record,
                case=record.case,
                document_number=record.document_number,
                detail=f"ไฟล์หลักฐาน {attachment.declared_name} ใช้ไม่ได้ "
                f"({attachment.get_scan_status_display()})",
                opened_at=timezone.now(),
            )
        elif attachment.scan_status == S.CLEAN and current is not None:
            if attachment.created_at > current.opened_at:
                _close(
                    current,
                    None,
                    D.Resolution.EVIDENCE_REPLACED,
                    "หลักฐานใหม่ผ่านการสแกน",
                    timezone.now(),
                )


# --- Views of the work --------------------------------------------------------------------------


def visible_discrepancies(user):
    """Registry staff and admins see all; another division sees what was sent to it. Per
    division (ADR 0008) each division sees every discrepancy of its own facts and cases."""
    divisions = list(identity.staff_scope(user).division_roles)
    if per_division() and may_open(user):
        own = own_divisions(user)
        if own is None:
            return D.objects.all()
        return D.objects.filter(
            Q(division_id__in=own)
            | Q(record__division_id__in=own)
            | Q(case__owner_division_id__in=own)
        )
    if may_open(user):
        return D.objects.all()
    return D.objects.filter(status=D.Status.WITH_DIVISION, division_id__in=divisions)


def counts(user):
    seen = visible_discrepancies(user)
    return {
        "open": seen.filter(status=D.Status.OPEN).count(),
        "with_division": seen.filter(status=D.Status.WITH_DIVISION).count(),
        "waiting_registry": seen.filter(status=D.Status.WAITING_REGISTRY).count(),
    }


def report(now=None, user=None):
    """Discrepancy report (acceptance): open by kind, age, and facts not reconciled yet; per
    division (ADR 0008) only the user's own."""
    now = now or timezone.now()
    seen = visible_discrepancies(user) if user is not None else D.objects.all()
    own = own_divisions(user) if user is not None else None
    open_items = seen.filter(status__in=OPEN)
    by_kind = {kind: 0 for kind in D.Kind.values}
    for kind in open_items.values_list("kind", flat=True):
        by_kind[kind] += 1
    last = RegistryImport.objects.filter(kind=RegistryImport.Kind.DOCUMENTS).aggregate(
        exported=Max("exported_at"), until=Max("period_to")
    )
    return {
        "open": open_items.count(),
        "by_kind": [(D.Kind(k).label, n) for k, n in by_kind.items()],
        "older_7": open_items.filter(opened_at__lt=now - timedelta(days=7)).count(),
        "older_30": open_items.filter(opened_at__lt=now - timedelta(days=30)).count(),
        "waiting_registry": open_items.filter(status=D.Status.WAITING_REGISTRY).count(),
        "with_division": open_items.filter(status=D.Status.WITH_DIVISION).count(),
        "unreconciled": len(unreconciled(now, own)),
        "records": (
            RegistryRecord.objects.filter(division_id__in=own)
            if own is not None
            else RegistryRecord.objects
        ).count(),
        "last_export": last["exported"],
        "covered_until": last["until"],
    }


def _mail_registry(upload, summary):
    """Q19: at most one email per import to registry staff, only when something new opened."""
    new = summary.get("new") or {}
    if not sum(new.values()):
        return []
    from operations import staff_email

    division = registry_division()
    if division is None:
        return []
    rows = [f"{D.Kind(k).label}: {n}" for k, n in sorted(new.items())]
    text = "\n".join(
        [
            staff_email.SUBJECTS["staff_registry"],
            f"จากไฟล์ {upload.original_name}"
            f" (ช่วง {upload.period_from or '-'} ถึง {upload.period_to or '-'})",
            *rows,
            f"เปิดดู: {settings.STAFF_WEB_BASE_URL.rstrip('/')}/registry/",
            "อีเมลนี้ส่งอัตโนมัติจากระบบรับเรื่อง ฝอจ.",
        ]
    )
    return staff_email.plan(
        "staff_registry",
        f"registry-import:{upload.pk}",
        None,
        staff_email.members(division.pk),
        text=text,
    )
