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
        return _fail("กรุณาพิมพ์ข้อความ")
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


def meter_rows(text, group="meter"):
    """หมายเลขเครื่องวัดฯ, ขนาด: one meter per line."""
    rows, shown = [], []
    for parts in rows_of(text):
        if len(parts) != 2:
            return _fail("พิมพ์บรรทัดละเครื่อง แบบ หมายเลขเครื่องวัดฯ, ขนาด เช่น 12345678, 5(100)A")
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
