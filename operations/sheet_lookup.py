"""ค้นในชีท (ADR 0027): the chat answers a status from someone else's sheet by a typed number
(for ศอค. the เลข UI, which is what its sheet calls เบอร์เครื่องวัดฯ).

The worker reads two columns of the sheet (numbers and status) every five minutes and keeps
the last good read; nothing is ever written to the sheet. In the chat the program pulls the
numbers out of what was typed and matches them exactly: the lowest row of each number is its
latest report. An empty status is "กำลังดำเนินการ"; a status that is set goes to the AI to be
worded (ai_sheet_status), and the program's own reply goes out whenever the AI cannot.

ADR 0029 (grill กคอ. 28 ต.ค. 2569): a source may match หมายเลขอุปกรณ์ instead (letters and
digits, compared without spaces, "-", "/" or "."; typed as "ชื่อเครื่องมือ, หมายเลข" a line
each), answer only rows of the asker's ฟข. (district_column), show a status cell as its word
(status_words, so a repair code never reaches ฟข.) and show a few more cells such as dates.

ADR 0046: when such a piece is not found, the five rows of the asker's ฟข. closest to what was
typed (difflib, by number and by tool name) come back as quick replies; the answer still comes
only from an exact match.
"""

import logging
import re
import unicodedata
from datetime import date, timedelta
from difflib import SequenceMatcher

from django.core.exceptions import PermissionDenied, ValidationError
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
MAX_EXTRA = 4
CODE = re.compile(r"(?=[A-Z0-9ก-ฮ]*\d)[A-Z0-9ก-ฮ]{2,30}")  # one digit at least
CODE_DROP = re.compile(r"[\s./\\_-]")
INVISIBLE = re.compile("[\u00ad\u200b-\u200f\u2060\ufeff]")
NEAR_K, NEAR_CUTOFF = 5, 0.6  # ADR 0046: top five that are this alike at least
TOKEN = re.compile(r"[A-Za-z0-9ก-ฮ][A-Za-z0-9ก-ฮ./-]*")


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


def clean(value):
    """ADR 0046: one form for what ฟข. typed and what the sheet says: NFC, Thai digits, no
    invisible characters, and ำ typed as ํ + า made one."""
    text = unicodedata.normalize("NFC", str(value or "")).translate(THAI_DIGITS)
    return INVISIBLE.sub("", text).replace("\u0e4d\u0e32", "\u0e33")


def code_key(value):
    """หมายเลขอุปกรณ์ as compared: "nc-801", "NC 801", "NC_801" and "NC801" are one piece."""
    return CODE_DROP.sub("", clean(value)).upper()


def codes_in(text):
    """[(name, number)] of "ชื่อเครื่องมือ, หมายเลข" lines (a comma or tab between them), in
    order, without repeats; a line without one takes the words that look like numbers."""
    found, seen = [], set()
    for raw in re.split(r"[\n;]+", str(text or "").translate(THAI_DIGITS)):
        parts = [p.strip() for p in re.split(r"[,\t，]", raw) if p.strip()]
        if not parts:
            continue
        if len(parts) > 1:
            pairs = [(" ".join(", ".join(parts[:-1]).split()), parts[-1])]
        else:
            pairs = [("", t) for t in TOKEN.findall(parts[0])]
        for name, number in pairs:
            key = code_key(number)
            if CODE.fullmatch(key) and key not in seen:
                seen.add(key)
                found.append((name.upper() if name.isascii() else name, key))
    return found


DATE = re.compile(
    r"^\s*(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}\s*[ก-๙]{1,4}\.?[ก-๙]*\.?\s*\d{2,4})\s*$"
)


NAME_DROP = re.compile(r"[^0-9a-zก-๙]+")


def name_key(value):
    return NAME_DROP.sub("", clean(value).lower())


def name_matches(typed, written):
    """Whether the name ฟข. typed fits a tool's name in the sheet: one inside the other, or a
    word of three letters or more in common ("clip on" fits "CLIP ON VOLT AMMETER 600A")."""
    a, b = name_key(typed), name_key(written)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    return bool(_words(typed) & _words(written))


def _words(value):
    return {name_key(w) for w in re.split(r"[\s,./()-]+", str(value)) if len(name_key(w)) >= 3}


def iso_date(value):
    """A dd/mm/yyyy cell (Buddhist years too) as YYYY-MM-DD, or "" when it is not one."""
    found = re.match(
        r"^\s*(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\s*$", str(value or "").translate(THAI_DIGITS)
    )
    if not found:
        return ""
    day, month, year = (int(x) for x in found.groups())
    if year > 2400:
        year -= 543
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}"


SERIAL = re.compile(r"^\s*(\d{5,6})(?:\.0+)?\s*$")
SERIAL_EPOCH = date(1899, 12, 30)  # day 0 of a Google Sheets date number


def serial_date(value):
    """A date the sheet keeps as a bare day count as dd/mm/yyyy in Buddhist years; any other
    value as it is. ซจ.'s date cells are formula results with no date format, so the sheet
    gives 46027 for 05/01/2569 (30 ต.ค. 2569); a date typed with its Buddhist year as if it
    were AD gives 244389, which is that year already."""
    found = SERIAL.match(str(value or ""))
    if not found:
        return value
    day = SERIAL_EPOCH + timedelta(days=int(found[1]))
    if 1990 <= day.year <= 2100:
        year = day.year + 543
    elif 2533 <= day.year <= 2643:
        year = day.year
    else:
        return value
    return f"{day:%d/%m}/{year}"


def is_date(value):
    """Whether a cell is a date and nothing else (what the AI may see of a repair row)."""
    return bool(DATE.match(str(value or "").translate(THAI_DIGITS)))


def keys_in(source, text):
    """The numbers a lookup in this source looks for."""
    if source.match == source.Match.CODE:
        return [number for _, number in codes_in(text)]
    return numbers_in(text)


def _plain(value):
    return re.sub(r"[\s.]", "", str(value or ""))


def district_matches(cell, district):
    """Whether a sheet's ฟข. cell names this district: its ชื่อย่อ ฟข. or its name."""
    cell_key = _plain(cell)
    if not cell_key or district is None:
        return False
    if district.short_name and cell_key == _plain(district.short_name):
        return True
    name = district.name.replace("การไฟฟ้านครหลวงเขต", "").replace("เขต", "").strip()
    return bool(name) and _plain(name) in cell_key


SEGMENT_EDGE = " \t\n,;:-/|"


def segment(number, note):
    """The part of a note about this meter (28 ต.ค. 2569: ศอค.'s notes often list several
    meters, "57158638 รอตามงาน, 57119935 online AMR 2025"): the text after the number up to the
    next number, or after the run of numbers it starts. A note without the number stays whole."""
    text = str(note or "")
    found = [
        m
        for m in NUMBER.finditer(text.translate(THAI_DIGITS))
        if MIN_DIGITS <= len(m.group().replace("-", "")) <= MAX_DIGITS  # a meter, not "2025"
    ]
    at = next((k for k, m in enumerate(found) if m.group().replace("-", "") == number), None)
    if at is None:
        return " ".join(text.split())
    for k in range(at, len(found)):
        end = found[k + 1].start() if k + 1 < len(found) else len(text)
        piece = text[found[k].end() : end].strip(SEGMENT_EDGE)
        if piece:
            return " ".join(piece.split())
    return " ".join(text.split())


def column_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


# --- Reading the sheet -----------------------------------------------------------------------


def owner_columns(source):
    return [c.strip().upper() for c in (source.owner_columns or "").split(",") if c.strip()]


def key_columns(source):
    """The columns a typed number is looked for in (ADR 0034: ซจ. uses E and K)."""
    return [c.strip().upper() for c in (source.key_column or "").split(",") if c.strip()]


def owner_key(value):
    """An employee ID as compared: its digits without leading zeros ("0012345" = "12345")."""
    return re.sub(r"\D", "", str(value or "").translate(THAI_DIGITS)).lstrip("0")[:32]


def extra_columns(source):
    return [
        (str(c.get("column", "")).upper(), str(c.get("label", ""))[:40])
        for c in (source.extra_columns or [])[:MAX_EXTRA]
    ]


def check_source(source):
    keys = key_columns(source)
    if not keys or len(keys) > 3 or not all(COLUMN.fullmatch(c) for c in keys):
        raise ValidationError("คอลัมน์เลขต้องเป็นตัวอักษรคอลัมน์ เช่น J หรือหลายคอลัมน์คั่นด้วย , เช่น E,K")
    if not COLUMN.fullmatch(source.status_column or ""):
        raise ValidationError("คอลัมน์สถานะ ต้องเป็นตัวอักษรคอลัมน์ เช่น J")
    owners = owner_columns(source)
    if len(owners) > 3 or not all(COLUMN.fullmatch(c) for c in owners):
        raise ValidationError("คอลัมน์รหัสพนักงานใส่ได้ไม่เกิน 3 คอลัมน์ คั่นด้วย , เช่น B,M")
    if source.district_column and not COLUMN.fullmatch(source.district_column):
        raise ValidationError("คอลัมน์ ฟข. ต้องเป็นตัวอักษรคอลัมน์ เช่น C")
    for name, value in (
        ("คอลัมน์ชื่อเครื่องมือ", source.name_column),
        ("คอลัมน์วันที่", source.order_column),
        ("คอลัมน์ส่งคืนแล้ว", source.done_column),
    ):
        if value and not COLUMN.fullmatch(value):
            raise ValidationError(f"{name} ต้องเป็นตัวอักษรคอลัมน์ เช่น D")
    # ADR 0034: a ฟข. column is the sheet owner's choice (คม. keeps one, ซจ. shows every area).
    extras = extra_columns(source)
    if not all(COLUMN.fullmatch(c) and label for c, label in extras):
        raise ValidationError("คอลัมน์ที่แสดงเพิ่มต้องเป็น ตัวอักษรคอลัมน์=ชื่อ เช่น F=วันที่รับ")
    columns = [*keys, source.status_column, *owners]
    columns += [c for c in [source.district_column, source.name_column] if c]
    if len(set(columns)) != len(columns):
        raise ValidationError("คอลัมน์ที่อ่านต้องไม่ซ้ำกัน")
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", source.spreadsheet_id or ""):
        raise ValidationError("รหัสชีทไม่ถูกต้อง คัดลอกจากลิงก์ระหว่าง /d/ กับ /edit")


def _cell(column, offset):
    return column[offset] if offset < len(column) else ""


def _date_cell(label, value):
    return serial_date(value) if "วัน" in label else value


def _row_numbers(source, cell):
    if source.match == source.Match.CODE:
        key = code_key(cell)
        return [key] if CODE.fullmatch(key) else []
    return numbers_in(cell)


def apply_columns(
    source,
    keys,
    statuses,
    owners=(),
    districts=(),
    extras=(),
    names=(),
    orders=(),
    dones=(),
    more_keys=(),
):
    """Replace the source's entries with this read: one entry per number per row, each with
    the row's status, the first employee ID found in the owner columns, its ฟข. and the extra
    cells to show. more_keys are the other number columns of a row."""
    entries = []
    labels = [label for _, label in extra_columns(source)]
    size = max([len(keys), *(len(c) for c in owners), *(len(c) for c in more_keys)])
    for offset in range(size):
        status = " ".join(str(_cell(statuses, offset)).split())[:500]
        owner = next((k for k in (owner_key(_cell(c, offset)) for c in owners) if k), "")
        row = source.first_row + offset
        district = " ".join(str(_cell(districts, offset)).split())[:120]
        tool = " ".join(str(_cell(names, offset)).split())[:200]
        order = iso_date(serial_date(_cell(orders, offset)))
        done = bool(str(_cell(dones, offset)).strip())
        # A column named for a day (วันที่…, วันเข้า) may hold Google's bare date numbers.
        shown = [
            [label, " ".join(str(_date_cell(label, _cell(column, offset))).split())[:120]]
            for label, column in zip(labels, extras, strict=False)
        ]
        numbers = []
        for column in (keys, *more_keys):
            numbers += [n for n in _row_numbers(source, _cell(column, offset)) if n not in numbers]
        numbers = numbers or ([""] if owner else [])
        entries += [
            SheetEntry(
                source=source,
                number=n,
                row=row,
                status=status,
                owner=owner,
                district=district,
                name=tool,
                order=order,
                done=done,
                extra=[s for s in shown if s[1]],
            )
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
    owners, extras = owner_columns(source), [c for c, _ in extra_columns(source)]
    optional = {
        "districts": source.district_column,
        "names": source.name_column,
        "orders": source.order_column,
        "dones": source.done_column,
    }
    wanted = [c for c in optional.values() if c]
    numbers = key_columns(source)
    keys, *rest = read_columns(
        source.spreadsheet_id,
        source.sheet_name,
        [*numbers, source.status_column, *owners, *wanted, *extras],
        source.first_row,
    )
    more_keys, rest = rest[: len(numbers) - 1], rest[len(numbers) - 1 :]
    statuses, *rest = rest
    read_owners, rest = rest[: len(owners)], rest[len(owners) :]
    named = {}
    for field, column in optional.items():
        named[field] = rest.pop(0) if column else ()
    return apply_columns(
        source, keys, statuses, read_owners, extras=rest, more_keys=more_keys, **named
    )


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


def word(source, status, done=False):
    """A status cell as ฟข. may read it (ADR 0029): its word once the piece is back with ฟข.,
    else its word, the blank word, or as written."""
    status = (status or "").strip()
    done_words = source.done_words or {}
    if done and (status in done_words or "*" in done_words):  # "*": whatever the status says
        return done_words.get(status) or done_words["*"]
    words = source.status_words or {}
    if not status:
        return source.blank_word or ""
    return words.get(status) or status


def _tools(source, numbers, district, names):
    """ADR 0029 with คม.'s sheet: one item per tool that carries a typed number in the asker's
    ฟข., its latest row by date, narrowed to the name ฟข. typed when one fits."""
    names = names or {}
    groups, elsewhere = {}, set()
    entries = SheetEntry.objects.filter(source=source, number__in=[n for n in numbers if n])
    for entry in entries.order_by("order", "row"):
        if source.district_column and not district_matches(entry.district, district):
            elsewhere.add(entry.number)
            continue
        groups.setdefault(entry.number, {})[name_key(entry.name)] = entry  # latest wins
    common = {
        "kind": source.match,
        "label": source.number_label,
        "scoped": bool(source.district_column),
        "hidden": sorted(k for k in (source.status_words or {}) if k),
    }
    items = []
    for n in numbers:
        tools = list(groups.get(n, {}).values())
        typed = names.get(n, "")
        fitting = [t for t in tools if name_matches(typed, t.name)] if typed else tools
        if not tools:
            items.append(
                {
                    **common,
                    "number": n,
                    "name": typed,
                    "found": False,
                    "count": 0,
                    "statuses": [],
                    "extras": [],
                    "elsewhere": n in elsewhere,
                }
            )
            continue
        shown = sorted(fitting or tools, key=lambda t: (t.order, t.row))[-MAX_ROWS:]
        for t in shown:
            items.append(
                {
                    **common,
                    "number": n,
                    "name": t.name or typed,
                    "found": True,
                    "count": 1,
                    "statuses": [word(source, t.status, t.done)],
                    "extras": t.extra,
                    "elsewhere": False,
                    "unmatched": typed if typed and not fitting else "",
                }
            )
    return items


def lookup(source, numbers, district=None, names=None):
    """[{"number", "found", "count", "statuses", "elsewhere", "extras"}] in the order typed. A
    UI reported more than once shows every report (เจษ: "หากเจอมากกว่า 1 Row ให้แสดงหมด"),
    oldest first, up to MAX_ROWS of the latest. With a ฟข. column only the rows of the asker's
    district count; "elsewhere" says another district has the number. A source with a tool
    name column answers per tool (names: {number: the name typed})."""
    if source.name_column:
        return _tools(source, numbers, district, names)
    rows, extras, elsewhere = {}, {}, set()
    entries = SheetEntry.objects.filter(source=source, number__in=[n for n in numbers if n])
    for entry in entries.order_by("row"):
        if source.district_column and not district_matches(entry.district, district):
            elsewhere.add(entry.number)
            continue
        status = entry.status
        if source.match == source.Match.DIGITS:
            status = segment(entry.number, status)  # only this meter's part of the note
        rows.setdefault(entry.number, []).append(word(source, status, entry.done))
        extras[entry.number] = entry.extra
    return [
        {
            "number": n,
            "found": n in rows,
            "count": len(rows.get(n, [])),
            "statuses": rows.get(n, [])[-MAX_ROWS:],
            "elsewhere": n in elsewhere and n not in rows,
            "extras": extras.get(n, []),
            "kind": source.match,
            "label": source.number_label,
            "scoped": bool(source.district_column),
            # The raw values a word stands for (repair codes): never in a reply (ADR 0029).
            "hidden": sorted(k for k in (source.status_words or {}) if k),
        }
        for n in numbers
    ]


def near(source, items, district=None):
    """ADR 0046: [(name, number)] of up to five rows of the asker's ฟข. most alike a piece not
    found, by its number or by the tool name typed with it, best first."""
    missed = [(i["number"], name_key(i.get("name"))) for i in items if not i["found"]]
    if source.match != source.Match.CODE or not missed:
        return []
    scores = {}
    rows = SheetEntry.objects.filter(source=source).values_list("number", "name", "district")
    for number, name, cell in rows.iterator():
        if source.district_column and not district_matches(cell, district):
            continue
        written = name_key(name)
        for typed, typed_name in missed:
            score = SequenceMatcher(None, typed, number).ratio()
            if typed_name and written:
                score = max(score, SequenceMatcher(None, typed_name, written).ratio())
            if score >= NEAR_CUTOFF and score > scores.get((number, name), 0):
                scores[(number, name)] = score
    best = sorted(scores, key=lambda k: (-scores[k], k))[:NEAR_K]
    return [(name, number) for number, name in best]


def near_label(name, number):
    """A button of 20 letters at most: the number, then whole words of the name that fit."""
    label = number[:20]
    for word in str(name or "").split():
        if len(label) + 1 + len(word) > 20:
            break
        label += " " + word
    return label


def reports(item):
    """(order, status) of each report shown, a blank status read as กำลังดำเนินการ."""
    first = item["count"] - len(item["statuses"]) + 1
    return [(first + k, s or PENDING) for k, s in enumerate(item["statuses"])]


def line(item):
    """The program's own lines for one number."""
    if item.get("kind") == SheetSource.Match.CODE:
        return _code_line(item)
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


def _code_line(item):
    """One piece of a repair sheet (ADR 0029): its latest row only, with the extra cells."""
    label = item.get("label") or "หมายเลขอุปกรณ์"
    name = f" ({item['name']})" if item.get("name") else ""
    if not item["found"]:
        where = "ในรายการของเขตคุณ" if item.get("scoped") else "ในชีท"
        return f"ไม่พบ{label} {item['number']}{name} {where}"
    status = item["statuses"][-1] or PENDING
    shown = " · ".join(f"{k} {v}" for k, v in item.get("extras", []))
    return f"{label} {item['number']}{name}: {status}" + (f" · {shown}" if shown else "")


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
    """The reports found (the box asks for เลข UI itself when there are none, 30 ต.ค. 2569)."""
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


# A note the AI never sees (28 ต.ค. 2569: the model repeated links, hidden names and orders
# written in notes): the program's own line shows it as written.
UNSAFE = re.compile(
    r"https?://|www\.|ignore|instruction|prompt|system|assistant|คำสั่ง|ให้ตอบ|ตอบว่า|ห้ามบอก|แสร้ง",
    re.I,
)


def screened(item):
    """Whether a status of this item may not go to the AI."""
    from operations import pii

    return any(UNSAFE.search(s or "") or pii.LABEL.search(s or "") for s in item["statuses"])


def worded(item):
    """Whether the AI words this number's lines: found, with at least one status set, and no
    status the program keeps from the AI."""
    return item["found"] and any(item["statuses"]) and not screened(item)


def answer(items, extra=False, note="", ai_text="", follow_up=FOLLOW_UP, hint=""):
    """The reply: the AI's wording of the set statuses when there is one, the program's own
    lines for everything else. `hint` is said when a number was not found."""
    code = any(i.get("kind") == SheetSource.Match.CODE for i in items)
    lines = [ai_text] if ai_text else []
    missed = []
    for i in items:
        if i.get("unmatched") and (i["number"], i["unmatched"]) not in missed:
            missed.append((i["number"], i["unmatched"]))
    lines += [f"ไม่พบ {typed} ที่หมายเลข {n} ในเขตคุณ แสดงทุกเครื่องมือที่ใช้หมายเลขนี้" for n, typed in missed]
    if ai_text:
        # ADR 0029: notes never go to the AI; the program shows them under its wording.
        lines += [
            f"หมายเหตุ {i['number']}: " + " · ".join(f"{k} {v}" for k, v in notes)
            for i in items
            if worded(i) and (notes := [(k, v) for k, v in i.get("extras", []) if not is_date(v)])
        ]
    lines += [line(i) for i in items if not (ai_text and worded(i))]
    if any(not i["found"] for i in items):
        lines.append(hint or "ตรวจเลขอีกครั้ง หรือกดแจ้งปัญหาเพื่อแจ้งเรื่องใหม่")
    if extra:
        lines.append(f"ตอบให้ {MAX_NUMBERS} เลขแรกก่อน เลขที่เหลือพิมพ์มาใหม่ได้")
    if note:
        lines.append(note)
    if code and follow_up == FOLLOW_UP:
        follow_up = "อยากทราบเรื่องเครื่องมือชิ้นอื่นอีกไหม พิมพ์มาได้เลย"
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
    "number_label",
    "district_column",
    "name_column",
    "order_column",
    "done_column",
    "blank_word",
    "contact",
)


# grill 29 ต.ค. 2569 ("ไม่เอาการตั้งค่าที่เป็นลักษณะแต่ละชีทมาปนกับชีทอื่น"): each แบบชีท has
# columns of its own. Saving a sheet clears those of the other แบบ, so nothing the form no longer
# shows still changes the answer (a left-over ฟข. column would still hide rows).
OWN_COLUMNS = {
    SheetSource.Match.DIGITS: ("owner_columns",),
    SheetSource.Match.CODE: ("district_column", "name_column", "order_column", "done_column"),
}
NUMBER_LABELS = {SheetSource.Match.DIGITS: "เลข UI", SheetSource.Match.CODE: "หมายเลขอุปกรณ์"}


def read_list(source):
    """[(column, what it holds)] the sheet's แบบ reads, in the order ทดสอบอ่าน shows them."""
    found = [(c, source.number_label) for c in key_columns(source)]
    found += [(source.status_column, "สถานะ")]
    if source.match == source.Match.DIGITS:
        found += [(c, "รหัสพนักงานผู้แจ้ง") for c in owner_columns(source)]
    else:
        found += [
            (column, what)
            for column, what in (
                (source.district_column, "ฟข."),
                (source.name_column, "ชื่อเครื่องมือ"),
                (source.order_column, "วันที่"),
                (source.done_column, "งานจบแล้ว"),
            )
            if column
        ]
    found += [(c, label) for c, label in extra_columns(source)]
    shown = {}
    for column, what in found:
        shown.setdefault(column, what)
    return list(shown.items())


def sample(source, row):
    """The program's line for one sheet row ({column: cell}) as the chat would answer it, before
    any AI wording; "" for a row with no number."""

    def cell(column):
        return " ".join(str(row.get(column, "") if column else "").split())

    numbers = [n for c in key_columns(source) for n in _row_numbers(source, cell(c))]
    if not numbers:
        return ""
    number, status = numbers[0], cell(source.status_column)
    if source.match == source.Match.DIGITS:
        status = segment(number, status)
    return line(
        {
            "number": number,
            "found": True,
            "count": 1,
            "statuses": [word(source, status, bool(cell(source.done_column)))],
            "extras": [
                [label, _date_cell(label, cell(c))] for c, label in extra_columns(source) if cell(c)
            ],
            "kind": source.match,
            "label": source.number_label,
            "name": cell(source.name_column),
        }
    )


def _pairs(text, limit):
    """{"a": "b"} of "a=b" lines (the admin form's status words and extra columns)."""
    found = {}
    for raw in str(text or "").splitlines():
        if "=" in raw:
            key, value = (" ".join(x.split()) for x in raw.split("=", 1))
            if key and value:
                found[key[:60]] = value[:60]
    if len(found) > limit:
        raise ValidationError(f"ใส่ได้ไม่เกิน {limit} บรรทัด")
    return found


def build_source(actor, source, data):
    """The sheet as the form says, checked and not saved; ทดสอบอ่าน and บันทึก both use it."""
    from operations import sheet_access

    created = source is None
    if created:
        if not sheet_access.may_open(actor):
            raise PermissionDenied
    else:
        sheet_access.require(actor, source)  # ADR 0033: the unit's ผู้ดูแลชีท, or an admin
    if created:
        # The flow box picks a sheet by name; the key only has to be unique and stay put.
        key = (data.get("key") or "").strip() or _new_key()
        if not re.fullmatch(r"[a-z0-9-]{2,40}", key):
            raise ValidationError("รหัสแหล่งข้อมูลใช้ a-z 0-9 และ - ยาว 2 ถึง 40 ตัว")
        if SheetSource.objects.filter(key=key).exists():
            raise ValidationError("รหัสแหล่งข้อมูลนี้มีอยู่แล้ว")
        source = SheetSource(key=key)
    for name in FIELDS:
        value = str(data.get(name, getattr(source, name)) or "").strip()
        if name in (
            "key_column",
            "status_column",
            "owner_columns",
            "district_column",
            "name_column",
            "order_column",
            "done_column",
        ):
            value = value.upper().replace(" ", "")
        setattr(source, name, value)
    if "link" in data:
        source.spreadsheet_id = data.get("link") or ""
    source.spreadsheet_id = sheet_access.spreadsheet_id(source.spreadsheet_id)
    if created or "unit" in data:
        sheet_access.place(actor, source, data.get("unit") or "")
    if data.get("match") in SheetSource.Match.values:
        source.match = data["match"]
    source.number_label = source.number_label[:40] or NUMBER_LABELS[source.match]
    if "status_words" in data:
        source.status_words = _pairs(data.get("status_words"), 40)
    if "done_words" in data:
        # "ว่าง=ส่งคืน ฟข. แล้ว" stands for a blank status cell
        pairs = _pairs(data.get("done_words"), 40)
        aliases = {"ว่าง": "", "ทุกสถานะ": "*"}  # ADR 0034: ซจ.'s ยกเลิก whatever the status
        source.done_words = {aliases.get(k, k): v for k, v in pairs.items()}
    if "extra_columns" in data:
        source.extra_columns = [
            {"column": k.upper(), "label": v}
            for k, v in _pairs(data.get("extra_columns"), MAX_EXTRA).items()
        ]
    if "ai_words_shown" in data:
        source.ai_words = bool(data.get("ai_words"))
    for kind, names in OWN_COLUMNS.items():
        if kind != source.match:
            for name in names:
                setattr(source, name, "")
    if source.match != source.Match.CODE:
        source.done_words = {}
    try:
        source.first_row = max(1, min(int(source.first_row), 1000))
    except (TypeError, ValueError) as exc:
        raise ValidationError("แถวแรกของข้อมูลต้องเป็นตัวเลข") from exc
    source.name = source.name[:120]
    source.contact = source.contact[:100]
    if not source.name:
        raise ValidationError("ตั้งชื่อแหล่งข้อมูล")
    check_source(source)
    source.enabled = bool(data.get("enabled"))
    return source


@transaction.atomic
def save_source(actor, source, data, reason):
    """Create or change a source with a reason; a changed sheet is read again at once."""
    from operations import identity

    created = source is None
    reason = identity._require_reason(reason)
    if not reason:
        raise ValidationError("ใส่เหตุผลก่อนบันทึก")
    source = build_source(actor, source, data)
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


def _new_key():
    number = SheetSource.objects.count() + 1
    while SheetSource.objects.filter(key=f"sheet-{number}").exists():
        number += 1
    return f"sheet-{number}"


def request_refresh(actor, source):
    from operations import identity, sheet_access

    sheet_access.require(actor, source)
    SheetSource.objects.filter(pk=source.pk).update(refresh_requested=True)
    identity.audit(actor, "sheet_source.refresh_requested", source)
