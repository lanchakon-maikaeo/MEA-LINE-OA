"""ค้นในชีท (ADR 0027): the chat answers a status from someone else's sheet by a typed number
(for ศอค. the เลข UI, which is what its sheet calls เบอร์เครื่องวัดฯ).

The worker reads two columns of the sheet (numbers and status) every five minutes and keeps
the last good read; nothing is ever written to the sheet. In the chat the program pulls the
numbers out of what was typed and matches them exactly: the lowest row of each number is its
latest report. An empty status is "กำลังดำเนินการ"; a status that is set goes to the AI to be
worded (ai_sheet_status), and the program's own reply goes out whenever the AI cannot.
"""

import logging
import re
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from operations.sheet_models import SheetEntry, SheetSource

logger = logging.getLogger(__name__)
EVERY = timedelta(minutes=5)  # grill Q9
IDLE = timedelta(minutes=30)  # grill Q16: after this a typed text is no longer a number to look up
MAX_NUMBERS = 10  # grill Q7
MAX_ROWS = 10  # reports shown per UI
MAX_OWN = 20  # one employee's own reports shown
PENDING = "กำลังดำเนินการ"
COLUMN = re.compile(r"[A-Z]{1,3}")
THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
# Digits, possibly with dashes typed inside one number ("1234-5678"); a number has 6 to 12
# digits. Dash-joined parts too long for one number are several numbers ("12345678-87654321").
NUMBER = re.compile(r"\d[\d-]*\d|\d")
MIN_DIGITS, MAX_DIGITS = 6, 12


def numbers_in(text):
    """The numbers in what was typed (or in one sheet cell), in order, without repeats."""
    found = []
    for match in NUMBER.finditer(str(text or "").translate(THAI_DIGITS)):
        joined = match.group().replace("-", "")
        parts = [joined] if len(joined) <= MAX_DIGITS else match.group().split("-")
        for digits in parts:
            if MIN_DIGITS <= len(digits) <= MAX_DIGITS and digits not in found:
                found.append(digits)
    return found


def column_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


# --- Reading the sheet -----------------------------------------------------------------------


def owner_columns(source):
    return [c.strip().upper() for c in (source.owner_columns or "").split(",") if c.strip()]


def owner_key(value):
    """An employee ID as compared: its digits without leading zeros ("0012345" = "12345")."""
    return re.sub(r"\D", "", str(value or "").translate(THAI_DIGITS)).lstrip("0")[:32]


def check_source(source):
    for name, value in (("คอลัมน์เลข", source.key_column), ("คอลัมน์สถานะ", source.status_column)):
        if not COLUMN.fullmatch(value or ""):
            raise ValidationError(f"{name} ต้องเป็นตัวอักษรคอลัมน์ เช่น J")
    owners = owner_columns(source)
    if len(owners) > 3 or not all(COLUMN.fullmatch(c) for c in owners):
        raise ValidationError("คอลัมน์รหัสพนักงานใส่ได้ไม่เกิน 3 คอลัมน์ คั่นด้วย , เช่น B,M")
    columns = [source.key_column, source.status_column, *owners]
    if len(set(columns)) != len(columns):
        raise ValidationError("คอลัมน์เลข สถานะ และรหัสพนักงานต้องไม่ซ้ำกัน")
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", source.spreadsheet_id or ""):
        raise ValidationError("รหัสชีทไม่ถูกต้อง คัดลอกจากลิงก์ระหว่าง /d/ กับ /edit")
    if not (source.sheet_name or "").strip():
        raise ValidationError("ใส่ชื่อแท็บของชีท")


def _cell(column, offset):
    return column[offset] if offset < len(column) else ""


def apply_columns(source, keys, statuses, owners=()):
    """Replace the source's entries with this read: one entry per number per row, each with
    the row's status and the first employee ID found in the owner columns."""
    entries = []
    size = max([len(keys), *(len(c) for c in owners)])
    for offset in range(size):
        status = " ".join(str(_cell(statuses, offset)).split())[:500]
        owner = next((k for k in (owner_key(_cell(c, offset)) for c in owners) if k), "")
        row = source.first_row + offset
        numbers = numbers_in(_cell(keys, offset)) or ([""] if owner else [])
        entries += [
            SheetEntry(source=source, number=n, row=row, status=status, owner=owner)
            for n in numbers
        ]
    with transaction.atomic():
        SheetEntry.objects.filter(source=source).delete()
        SheetEntry.objects.bulk_create(entries, batch_size=1000)
        SheetSource.objects.filter(pk=source.pk).update(
            rows_read=size, last_synced_at=timezone.now(), last_error=""
        )
    return len(entries)


def sync(source):
    from operations.koj_sheets import read_columns

    check_source(source)
    keys, statuses, *owners = read_columns(
        source.spreadsheet_id,
        source.sheet_name,
        [source.key_column, source.status_column, *owner_columns(source)],
        source.first_row,
    )
    return apply_columns(source, keys, statuses, owners)


def sync_due(now=None):
    """Read one due source; a failed read keeps the last good entries."""
    now = now or timezone.now()
    with transaction.atomic():
        source = (
            SheetSource.objects.select_for_update(skip_locked=True)
            .filter(Q(enabled=True, next_sync_at__lte=now) | Q(refresh_requested=True))
            .filter(Q(syncing_until__isnull=True) | Q(syncing_until__lt=now))
            .order_by("next_sync_at")
            .first()
        )
        if source is None:
            return False
        source.syncing_until = now + timedelta(minutes=2)
        source.refresh_requested = False
        source.next_sync_at = now + EVERY
        source.save(update_fields=["syncing_until", "refresh_requested", "next_sync_at"])
    try:
        sync(source)
    except Exception as exc:  # one bad source must not stop the others; no sheet text in logs
        error = (
            "; ".join(exc.messages)[:200]
            if isinstance(exc, ValidationError)
            else "ดึงข้อมูลไม่สำเร็จ ตรวจการตั้งค่าและสิทธิ์อ่านชีท"
        )
        SheetSource.objects.filter(pk=source.pk).update(last_error=error)
    finally:
        SheetSource.objects.filter(pk=source.pk).update(syncing_until=None)
    return True


def sync_loop():
    import time

    from django.db import close_old_connections

    while True:
        try:
            close_old_connections()
            handled = sync_due()
        except Exception:
            logger.warning("ค้นในชีท: source maintenance failed")
            handled = False
        finally:
            close_old_connections()
        time.sleep(1 if handled else 10)


# --- Answering -------------------------------------------------------------------------------


def source_for(key):
    return SheetSource.objects.filter(key=key, enabled=True).first()


def lookup(source, numbers):
    """[{"number", "found", "count", "statuses"}] in the order typed. A UI reported more than
    once shows every report (เจษ: "หากเจอมากกว่า 1 Row ให้แสดงหมด"), oldest first, up to
    MAX_ROWS of the latest."""
    rows = {}
    entries = SheetEntry.objects.filter(source=source, number__in=[n for n in numbers if n])
    for entry in entries.order_by("row"):
        rows.setdefault(entry.number, []).append(entry.status)
    return [
        {
            "number": n,
            "found": n in rows,
            "count": len(rows.get(n, [])),
            "statuses": rows.get(n, [])[-MAX_ROWS:],
        }
        for n in numbers
    ]


def reports(item):
    """(order, status) of each report shown, a blank status read as กำลังดำเนินการ."""
    first = item["count"] - len(item["statuses"]) + 1
    return [(first + k, s or PENDING) for k, s in enumerate(item["statuses"])]


def line(item):
    """The program's own lines for one number."""
    if not item["found"]:
        return f"ไม่พบเลข UI {item['number']} ในรายการแจ้งปัญหา"
    if item["count"] == 1:
        status = item["statuses"][0]
        if not status:
            return f"เลข UI {item['number']} {PENDING}"
        return f"เลข UI {item['number']} สถานะจาก ฝอจ.: {status}"
    head = f"เลข UI {item['number']} แจ้งไว้ {item['count']} ครั้ง"
    if item["count"] > len(item["statuses"]):
        head += f" (แสดง {len(item['statuses'])} ครั้งล่าสุด)"
    return "\n".join([head, *(f"ครั้งที่ {n}: {status}" for n, status in reports(item))])


def own_reports(source, employee_id):
    """[(numbers, status)] of the rows this employee reported, oldest first, up to MAX_OWN of
    the latest (เจษ: ข้อมูลรายการจาก Google Sheets แมพด้วย Employee_ID)."""
    key = owner_key(employee_id)
    if not key:
        return []
    rows = {}
    for entry in SheetEntry.objects.filter(source=source, owner=key).order_by("row", "pk"):
        numbers, _ = rows.setdefault(entry.row, ([], entry.status))
        if entry.number:
            numbers.append(entry.number)
    return list(rows.values())


def own_answer(reports, employee_id, note=""):
    if not reports:
        lines = [
            f"ยังไม่พบรายการที่แจ้งด้วยรหัสพนักงาน {employee_id}",
            "รายการที่แจ้งโดยไม่ได้กรอกรหัสพนักงานจะไม่ขึ้นที่นี่ ตรวจด้วยเลข UI ได้ที่ FAQ",
        ]
    else:
        shown = reports[-MAX_OWN:]
        lines = [f"รายการที่แจ้งด้วยรหัสพนักงาน {employee_id} ทั้งหมด {len(reports)} รายการ"]
        if len(reports) > len(shown):
            lines[0] += f" (แสดง {len(shown)} รายการล่าสุด)"
        first = len(reports) - len(shown) + 1
        for n, (numbers, status) in enumerate(shown, first):
            ui = ", ".join(numbers) if numbers else "ไม่ได้กรอก"
            lines.append(f"{n}. เลข UI {ui}: {status or PENDING}")
    if note:
        lines.append(note)
    return "\n".join(lines)


FOLLOW_UP = "อยากทราบเรื่องเลข UI อื่นอีกไหม พิมพ์มาได้เลย"


def worded(item):
    """Whether the AI words this number's lines: found, with at least one status set."""
    return item["found"] and any(item["statuses"])


def answer(items, extra=False, note="", ai_text="", follow_up=FOLLOW_UP):
    """The reply: the AI's wording of the set statuses when there is one, the program's own
    lines for everything else."""
    lines = [ai_text] if ai_text else []
    lines += [line(i) for i in items if not (ai_text and worded(i))]
    if any(not i["found"] for i in items):
        lines.append("ตรวจเลขอีกครั้ง หรือกดแจ้งปัญหาเพื่อแจ้งเรื่องใหม่")
    if extra:
        lines.append(f"ตอบให้ {MAX_NUMBERS} เลขแรกก่อน เลขที่เหลือพิมพ์มาใหม่ได้")
    if note:
        lines.append(note)
    lines.append(follow_up or FOLLOW_UP)
    return "\n".join(lines)


def stale_note(source, now):
    """Said when the last good read is old, so nobody takes an old status for today's."""
    if source.last_synced_at is None:
        return "ยังดึงข้อมูลจากชีทไม่ได้ ลองใหม่อีกครั้งภายหลัง"
    if now - source.last_synced_at > timedelta(hours=1):
        return f"ข้อมูลล่าสุดเมื่อ {timezone.localtime(source.last_synced_at):%d/%m %H:%M} น."
    return ""


# --- Admin ----------------------------------------------------------------------------------

FIELDS = (
    "name",
    "spreadsheet_id",
    "sheet_name",
    "key_column",
    "status_column",
    "owner_columns",
    "first_row",
)


@transaction.atomic
def save_source(actor, source, data, reason):
    """Create or change a source with a reason; a changed sheet is read again at once."""
    from operations import identity

    identity.require_admin(actor)
    reason = identity._require_reason(reason)
    if not reason:
        raise ValidationError("ใส่เหตุผลก่อนบันทึก")
    created = source is None
    if created:
        key = (data.get("key") or "").strip()
        if not re.fullmatch(r"[a-z0-9-]{2,40}", key):
            raise ValidationError("รหัสแหล่งข้อมูลใช้ a-z 0-9 และ - ยาว 2 ถึง 40 ตัว")
        if SheetSource.objects.filter(key=key).exists():
            raise ValidationError("รหัสแหล่งข้อมูลนี้มีอยู่แล้ว")
        source = SheetSource(key=key)
    for name in FIELDS:
        value = str(data.get(name, getattr(source, name)) or "").strip()
        if name in ("key_column", "status_column", "owner_columns"):
            value = value.upper().replace(" ", "")
        setattr(source, name, value)
    try:
        source.first_row = max(1, min(int(source.first_row), 1000))
    except (TypeError, ValueError) as exc:
        raise ValidationError("แถวแรกของข้อมูลต้องเป็นตัวเลข") from exc
    source.name = source.name[:120]
    if not source.name:
        raise ValidationError("ตั้งชื่อแหล่งข้อมูล")
    check_source(source)
    source.enabled = bool(data.get("enabled"))
    source.refresh_requested = True
    source.updated_by = actor
    source.save()
    identity.audit(
        actor,
        "sheet_source.created" if created else "sheet_source.changed",
        source,
        reason,
        enabled=source.enabled,
        columns=f"{source.key_column},{source.status_column}",
    )
    return source


def request_refresh(actor, source):
    from operations import identity

    identity.require_admin(actor)
    SheetSource.objects.filter(pk=source.pk).update(refresh_requested=True)
    identity.audit(actor, "sheet_source.refresh_requested", source)
