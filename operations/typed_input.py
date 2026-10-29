"""กฎตายตัวของสิ่งที่ผู้ใช้ LINE พิมพ์ (grill กคว. 27 ต.ค. 2569 Q24, Q33, Q47).

One line is one row and "," splits its fields. The program tidies what people type before it
checks it, without guessing: spaces around commas and repeated spaces, other commas (， 、 ;),
Thai digits, other dashes, the case of units (5(100)a is 5(100)A), number prefixes such as
"หมายเลข" or "เลขที่", and spaces inside numbers. It never turns O into 0 or l into 1. Each rule
returns (value, what it read, problem); the chat says what it read so people can check it.
"""

import re

from operations import typed_lines

THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
DASHES = str.maketrans({"–": "-", "—": "-", "‐": "-", "−": "-", "‑": "-"})
COMMAS = re.compile(r"\s*[,，、;；]\s*")
PREFIX = re.compile(r"^(?:หมายเลขเครื่องวัดฯ?|หมายเลขเครื่อง|หมายเลข|เลขที่|เลข)\s*")
UNITS = {"a": "A", "v": "V", "kv": "kV", "kva": "kVA", "w": "W", "kw": "kW", "mva": "MVA"}
UNIT = re.compile(r"(?<=[\d)])\s*(mva|kva|kv|kw|a|v|w)\b", re.I)
CODE = re.compile(r"^[A-Za-z0-9-]{2,30}$")
MAX_TEXT = 2000
MAX_ROWS = 50


def tidy(text):
    """The text with digits, dashes, commas and spaces made regular (line breaks kept)."""
    text = str(text or "").translate(THAI_DIGITS).translate(DASHES)
    lines = []
    for line in text.splitlines():
        line = COMMAS.sub(", ", " ".join(line.split())).strip(" ,")
        if line:
            lines.append(line)
    return "\n".join(lines)


def code(value):
    """A number as typed: prefix words and inner spaces removed."""
    return PREFIX.sub("", str(value or "").strip()).replace(" ", "")


def size(value):
    value = " ".join(str(value or "").split())
    return UNIT.sub(
        lambda m: (
            (" " if m.group(1).lower() in ("kva", "kv", "kw", "mva") else "")
            + UNITS[m.group(1).lower()]
        ),
        value,
    )


def rows_of(text):
    """[[field, ...] per line] of tidy text."""
    return [line.split(", ") for line in tidy(text).splitlines()]


def _fail(problem):
    return None, "", problem


def text_rule(text):
    value = " ".join(str(text or "").split()) if "\n" not in str(text or "") else str(text).strip()
    if not value:
        return _fail("กรุณาระบุรายละเอียด")
    if len(value) > MAX_TEXT:
        return _fail(f"พิมพ์ไม่เกิน {MAX_TEXT} ตัวอักษร")
    return value, value, ""


def number_rule(text):
    value = code(tidy(text)).replace(",", "")
    if not re.fullmatch(r"\d{1,9}(?:\.\d{1,4})?", value):
        return _fail("พิมพ์เป็นตัวเลข เช่น 2000")
    return value, value, ""


def sap_rule(text):
    value = code(tidy(text))
    number, problem = typed_lines.clean_sap(value)
    if problem:
        return _fail(problem)
    return number, number, ""


PROJECT = re.compile(r"^[\w\u0E00-\u0E7F ./()-]{2,60}$")  # Thai marks are not \w
MEMO = PROJECT  # เลขที่ M/N may carry the writer's abbreviation, e.g. ฝอจ.คม. 123/2569
# A slip of กอจ. may carry dots (I/69.01.01.001.69.01.001, ADR 0034); kept as typed.
SLIP = re.compile(r"^[A-Za-z0-9./-]{4,30}$")
SLIP_KINDS = {"slip", "return_slip", "work_order", "return_transfer"}
CHECK_AGAIN = "กรุณาตรวจสอบอีกครั้ง"
# หมายเลขเครื่องวัดฯ is eight digits (ADR 0035), here and in the กตฟ. report.
METER_8 = re.compile(r"^\d{8}$")
METER_8_PROBLEM = "กรุณาตรวจสอบเลขเครื่องวัดฯอีกครั้ง เป็นตัวเลข 8 หลัก"


def tracking_rule(text, kind):
    """เลขติดตาม by its kind (รอบใหม่ Q1, Q2): SAP documents keep "/", a meter number is a
    code, a project is a name that may hold Thai and spaces."""
    if kind == "meter":
        value = code(tidy(text))
        if not METER_8.match(value):
            return _fail(METER_8_PROBLEM)
        return value, value, ""
    if kind == "project":
        value = " ".join(tidy(text).split())
        if not PROJECT.match(value):
            return _fail(CHECK_AGAIN)
        return value, value, ""
    if kind in ("memo", "other_number"):  # M/N, the memo for an item with no SAP code
        value = " ".join(tidy(text).split()).upper()
        if not MEMO.match(value):
            return _fail(
                "พิมพ์เลขที่ M/N 2 ถึง 60 ตัว เช่น คม. 123/2569" if kind == "memo" else CHECK_AGAIN
            )
        return value, value, ""
    if kind in SLIP_KINDS:
        value = code(tidy(text)).replace(" ", "")
        if not SLIP.match(value):
            return _fail(CHECK_AGAIN)
        return value, value, ""
    return sap_rule(text)


SIZE_PROBLEMS = {"ct": "พิมพ์ขนาด CT เช่น 100/5A", "vt": "พิมพ์ขนาด VT เช่น 11000/110V"}


def size_rule(text, kind="ct"):
    """One size such as 100/5A or 11000/110V (a CT or VT of a set); the problem names it."""
    value = size(tidy(text))
    if not value or len(value) > typed_lines.LIMITS["size"] or ratio(value) is None:
        return _fail(SIZE_PROBLEMS.get(kind, SIZE_PROBLEMS["ct"]))
    return value, value, ""


def multiplier_from(ct_size, vt_size=""):
    """CT ratio × VT ratio (VT only when the set has one), or None when unreadable."""
    ct = ratio(ct_size)
    if ct is None:
        return None
    if not vt_size:
        return ct
    vt = ratio(vt_size)
    return None if vt is None else ct * vt


def meter_rows(text, group="meter"):
    """หมายเลขเครื่องวัดฯ, ขนาด: one meter per line."""
    rows, shown = [], []
    for parts in rows_of(text):
        if len(parts) != 2:
            return _fail("พิมพ์หมายเลขเครื่องวัดฯ, ขนาด เช่น 12345678, 5(100)A")
        number, value = code(parts[0]), size(parts[1])
        if not typed_lines.METER_NUMBER.match(number):
            return _fail(f"หมายเลขเครื่องวัดฯ '{parts[0]}' ใช้ตัวอักษร ตัวเลข หรือขีด 4 ถึง 20 ตัว")
        if not value or len(value) > typed_lines.LIMITS["size"]:
            return _fail("ใส่ขนาด เช่น 5(100)A")
        rows.append({"group": group, "numbers": [number], "size": value, "quantity": "1"})
        shown.append(f"{number}, {value}")
    numbers = [r["numbers"][0] for r in rows]
    if len(set(numbers)) != len(numbers):
        return _fail("หมายเลขเครื่องวัดฯ ซ้ำกัน")
    if not rows or len(rows) > MAX_ROWS:
        return _fail(f"พิมพ์ 1 ถึง {MAX_ROWS} เครื่อง")
    return rows, "\n".join(shown), ""


def size_quantity_rows(text, group):
    """ขนาด, จำนวน: one size per line."""
    rows, shown = [], []
    for parts in rows_of(text):
        if len(parts) != 2:
            return _fail("พิมพ์บรรทัดละขนาด แบบ ขนาด, จำนวน เช่น 100/5A, 3")
        value, count = size(parts[0]), code(parts[1])
        if not count.isdigit() or int(count) < 1:
            return _fail("จำนวนต้องเป็นจำนวนเต็มมากกว่า 0")
        rows.append({"group": group, "size": value, "quantity": str(int(count))})
        shown.append(f"{value}, {int(count)}")
    if not rows or len(rows) > MAX_ROWS:
        return _fail(f"พิมพ์ 1 ถึง {MAX_ROWS} บรรทัด")
    return rows, "\n".join(shown), ""


def name_rows(text, group="other"):
    """ชื่ออุปกรณ์, ขนาด, จำนวน (ขนาดเว้นได้: ชื่อ, จำนวน)."""
    rows, shown = [], []
    for parts in rows_of(text):
        if len(parts) == 2:
            parts = [parts[0], "", parts[1]]
        if len(parts) != 3 or not parts[0]:
            return _fail("พิมพ์ ชื่ออุปกรณ์, ขนาด, จำนวน เช่น คีมตัดสาย, 2 นิ้ว, 2")
        count = code(parts[2])
        if not count.isdigit() or int(count) < 1:
            return _fail("จำนวนต้องเป็นจำนวนเต็มมากกว่า 0")
        row = {"group": group, "name": parts[0][:100], "quantity": str(int(count))}
        if parts[1]:
            row["size"] = size(parts[1])
        rows.append(row)
        shown.append(", ".join(p for p in (row["name"], row.get("size", ""), row["quantity"]) if p))
    return rows, "\n".join(shown), ""


def numbers_rule(text, pattern=CODE, label="เลข"):
    values = [code(p) for p in tidy(text).replace("\n", ", ").split(", ") if p.strip()]
    if not values:
        return _fail(f"พิมพ์{label} หลายเลขคั่นด้วย ,")
    bad = next((v for v in values if not pattern.match(v)), None)
    if bad is not None:
        return _fail(f"{label} '{bad}' ใช้ตัวอักษร ตัวเลข หรือขีด")
    if len(set(values)) != len(values):
        return _fail(f"{label}ซ้ำกัน")
    return values, ", ".join(values), ""


def ratio(value):
    """The ratio of a CT or VT size: 100/5A → 20, 11000/110V → 100; None when unreadable."""
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", str(value or ""))
    if not m or float(m.group(2)) == 0:
        return None
    return float(m.group(1)) / float(m.group(2))


def multiplier_of(rows):
    """CT ratio × VT ratio of a set's rows, or None when a size cannot be read."""
    found = None
    for group in ("ct", "vt"):
        sizes = {r.get("size") for r in rows if r.get("group") == group}
        if not sizes:
            continue
        if len(sizes) != 1 or ratio(next(iter(sizes))) is None:
            return None
        found = (found or 1) * ratio(next(iter(sizes)))
    return found


def meters_8_rule(text):
    """หมายเลขเครื่องวัดฯ of the กตฟ. report: eight digits each, split by "," or spaces."""
    values = [v for v in re.split(r"[,\s]+", tidy(text)) if v]
    if not values:
        return _fail("กรุณาพิมพ์หมายเลขเครื่องวัดฯ เช่น 12345678")
    if any(not METER_8.match(v) for v in values):
        return _fail(METER_8_PROBLEM)
    if len(set(values)) != len(values):
        return _fail("หมายเลขเครื่องวัดฯ ซ้ำกัน")
    return values, ", ".join(values), ""


def usage_report(text, district_of):
    """หมายเลขเครื่องวัดฯ, การไฟฟ้าเขต, ลักษณะที่พบ in one message (ADR 0035). Each part between
    commas is read alone: digits only are the meters, a district's name is the district, the
    rest is what was found. Typed without commas, the eight-digit numbers and a word naming a
    district are taken out and the words left are what was found. What is missing is asked
    after; nothing is guessed. district_of(text) is the district a text names, or None."""
    tidied = tidy(text)
    if not tidied:
        return _fail("กรุณาระบุรายละเอียด")
    parts = [p for p in re.split(r", |\n", tidied) if p.strip()]
    meters, district, found = [], None, []
    for part in parts:
        words = part.split()
        if all(w.replace("-", "").isdigit() for w in words):
            numbers = [w.replace("-", "") for w in words]
            if any(not METER_8.match(n) for n in numbers):
                return _fail(METER_8_PROBLEM)
            meters += [n for n in numbers if n not in meters]
            continue
        if district is None and (named := district_of(part)) is not None:
            district = named
            continue
        if len(parts) == 1:  # no commas: pick the numbers and a district out of the words
            kept = []
            for w in words:
                if METER_8.match(w):
                    meters += [w] if w not in meters else []
                elif district is None and (named := district_of(w)) is not None:
                    district = named
                else:
                    kept.append(w)
            words = kept
        if words:
            found.append(" ".join(words))
    value = {}
    if meters:
        value["meter_numbers"] = meters
    if district is not None:
        value["district"] = district
    if found:
        value["description"] = ", ".join(found)[:MAX_TEXT]
    return value, "", ""
