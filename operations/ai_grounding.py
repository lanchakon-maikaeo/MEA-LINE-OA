"""Local source checks, not an instruction detector or an authorization mechanism.

Ambiguous values stay blank for the existing guided flow. No additional provider call.
Only an unchanged structured draft value may be inherited; draft prose cannot supply items.
"""

import re
from decimal import Decimal

from operations.conversation import CATEGORY_LABELS, THAI_DIGITS, _parse_date
from operations.typed_lines import NUMBERED, SAP_NUMBER

NUMBER = r"\d+(?:,\d{3})*(?:\.\d+)?"
AUTHORITY = re.compile(
    r"อนุมัติ|ยืนยันส่งเรื่อง|ส่งเรื่องสำเร็จ|ปิดเรื่องแล้ว|ไม่ต้องตรวจสอบ|ไม่ต้องยืนยัน"
    r"|\b(?:approved?|authorized|submitted|administrator|developer|system)\b",
    re.I,
)


def normalise(value):
    return re.sub(r"\s+", " ", value.translate(THAI_DIGITS)).strip().casefold()


def source_excerpt(source, quote):
    """Resolve one quote to original character offsets; never store the model's spelling.

    Whitespace, letter case and Thai digits may differ. Repeated/partial Unicode matches
    are ambiguous: ask for the field instead. Offsets index Python Unicode characters.
    """
    chars, offsets = [], []
    for match in re.finditer(r"\s+|\S", source):
        value = " " if match[0].isspace() else match[0].translate(THAI_DIGITS).casefold()
        chars.extend(value)
        offsets.extend([match.span()] * len(value))
    needle = normalise(quote)
    if not needle:
        return None
    matches = list(re.finditer(rf"(?={re.escape(needle)})", "".join(chars)))
    if len(matches) != 1:
        return None
    position = matches[0].start()
    start, end = offsets[position][0], offsets[position + len(needle) - 1][1]
    excerpt = source[start:end]
    if normalise(excerpt) != needle or len(excerpt) > 2000:
        return None
    return excerpt, {"start": start, "end": end}


def safe_text(value):
    from operations.ai_harness import CUT, minimize

    return isinstance(value, str) and not (
        AUTHORITY.search(value) or CUT in value or minimize(value)[1]
    )


# The plain words of a typed group (typed_lines) name its shared item; a longer catalog name
# still matches first. The other-equipment group has no word: its name is the requester's own.
GROUP_WORDS = {
    "meter": ("มิเตอร์", "เครื่องวัด", "meter"),
    "ct": ("ct", "ซีที"),
    "vt": ("vt", "วีที"),
    "transformer": ("หม้อแปลง", "transformer"),
    "lbs": ("lbs",),
    "rmu": ("rmu",),
}
# A group word right after these is part of another word (a label or another thing).
NOT_GROUP_AFTER = ("เลข", "กล่อง", "ฝา", "ซีล", "ตู้")
# Words a typed row counts in, whatever its group's unit ("2 อัน", "3 ลูก").
COUNT_WORDS = "อัน|ชิ้น|ตัว|เครื่อง|ลูก|ชุด|หน่วย|ม้วน|เส้น|กล่อง|คู่"
# A size or rating ("5a", "220v", "11kv"), never a meter number.
SIZE_LIKE = re.compile(r"\d+(?:\.\d+)?(?:a|v|kv|va|kva|w|kw)")


def _alias_pattern(name, group_word):
    pattern = re.escape(name)
    if re.fullmatch(r"[a-z]{1,3}", name):
        # A short Latin word ("ct") is a whole word, never part of another ("connect").
        pattern = rf"(?<![a-z0-9]){pattern}(?![a-z0-9])"
    if group_word:
        # "เลขมิเตอร์" labels numbers and "กล่องมิเตอร์" is other equipment, not a meter.
        pattern = "".join(f"(?<!{w})" for w in NOT_GROUP_AFTER) + pattern
    return pattern


def mentions(text, catalog):
    """Catalog-derived names and unique category labels; ambiguous names require a choice."""
    aliases = {}
    for item in catalog:
        names = {item.code, item.name, re.split("รุ่น|จำลอง", item.name)[0].strip()}
        if item.category in ("meter", "transformer") and not getattr(item, "entry_group", ""):
            names.add(CATEGORY_LABELS[item.category])
        for name in names:
            if name:
                aliases.setdefault(normalise(name), set()).add(item.code)
    words = set()
    for item in catalog:
        for word in GROUP_WORDS.get(getattr(item, "entry_group", ""), ()):
            aliases[normalise(word)] = {item.code}
            words.add(normalise(word))
    unique = {name: next(iter(codes)) for name, codes in aliases.items() if len(codes) == 1}
    if not unique:
        return []
    pattern = "|".join(_alias_pattern(n, n in words) for n in sorted(unique, key=len, reverse=True))
    return [(m.start(), m.end(), unique[m.group()]) for m in re.finditer(pattern, text)]


def item_spans(text, catalog, draft):
    found = mentions(text, catalog)
    spans = {}
    for position, (_, end, code) in enumerate(found):
        stop = found[position + 1][0] if position + 1 < len(found) else len(text)
        spans.setdefault(code, []).append(text[end:stop])
    # An unnamed correction is unambiguous only for a one-item draft.
    old = (draft or {}).get("items", [])
    if not found and len(old) == 1:
        spans[old[0].get("item_code")] = [text]
    return spans


def quantities(spans, unit, previous=None, correcting=False, typed=False):
    values, corrections = set(), set()
    for span in spans:
        # Never take a date, serial, rating, reference or another item's number.
        pattern = rf"(?<![a-z0-9_./-])({NUMBER})\s*{re.escape(normalise(unit))}(?![a-z0-9_])"
        numbers = re.findall(pattern, span)
        if typed:
            # A typed row says its count after its size ("ขนาด 16 sq.mm. 2 อัน", "จำนวน 5"); a
            # number before a size word ("1 เฟส") is not a count.
            numbers += re.findall(rf"จำนวน\s*({NUMBER})(?![\d.,/])", span)
            numbers += re.findall(
                rf"(?<![a-z0-9_./(-])({NUMBER})\s*(?:{COUNT_WORDS})(?![a-z0-9_])", span
            )
        direct = re.match(rf"\s*(?:จำนวน\s*|อย่างละ\s*)?({NUMBER})(?=\s|$)", span)
        # A typed row's bare number may be a meter number or a size: never its count.
        if (
            direct
            and not typed
            and not re.match(r"\s*(?:โวลต์|แอมป์|kv|kva|volt|amp)\b", span[direct.end() :], re.I)
        ):
            numbers.append(direct[1])
        values.update(Decimal(n.replace(",", "")) for n in numbers)
        corrections.update(
            Decimal(n.replace(",", ""))
            for n in re.findall(rf"เป็น\s*({NUMBER})\s*{re.escape(normalise(unit))}", span)
        )
    if correcting and previous not in (None, "") and len(corrections) == 1:
        if values <= corrections | {Decimal(str(previous))}:
            return corrections
    # "ขอแก้จำนวนเป็น 5 ชิ้นแทน 2 ชิ้น" in the same message: the stated change wins.
    changed = {
        Decimal(n.replace(",", ""))
        for span in spans
        for n in re.findall(
            rf"{QUANTITY_CHANGE}[^\n]{{0,30}}?(?:เป็น|เหลือ)\s*({NUMBER})"
            rf"(?:\s*{re.escape(normalise(unit))})?(?![\d.,/])(?!\s*{MONTH_NAMES})",
            span,
        )
    }
    if len(changed) == 1:
        return changed
    return values


MONTHS = [
    ("มกราคม", "ม.ค."),
    ("กุมภาพันธ์", "ก.พ."),
    ("มีนาคม", "มี.ค."),
    ("เมษายน", "เม.ย."),
    ("พฤษภาคม", "พ.ค."),
    ("มิถุนายน", "มิ.ย."),
    ("กรกฎาคม", "ก.ค."),
    ("สิงหาคม", "ส.ค."),
    ("กันยายน", "ก.ย."),
    ("ตุลาคม", "ต.ค."),
    ("พฤศจิกายน", "พ.ย."),
    ("ธันวาคม", "ธ.ค."),
]


MONTH_NAMES = "(?:" + "|".join(re.escape(n) for pair in MONTHS for n in pair) + ")"


def explicit_dates(text):
    # Full stated dates only; never invent a missing year or interpret a relative deadline.
    text = text.translate(THAI_DIGITS)
    written = re.findall(r"(?<!\d)(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})(?!\d)", text)
    for month, names in enumerate(MONTHS, 1):
        pattern = "|".join(re.escape(name) for name in names)
        for day, year in re.findall(
            rf"(?<!\d)(\d{{1,2}})\s*(?:{pattern})\s*(?:พ\.?ศ\.?\s*)?(\d{{4}})(?!\d)", text
        ):
            written.append(f"{day}/{month}/{year}")
    return {parsed.isoformat() for value in written if (parsed := _parse_date(value))}


# Fields each intent may carry, and the form's own labels for them (ai_screen.FIELD_LABELS).
INTENT_FIELDS = {
    "withdrawal": ("purpose",),
    "return": ("reason", "condition"),
    "problem": ("description", "impact"),
}
CUT_MARK = "[ตัดข้อความที่สั่งระบบ]"
CHANGE = r"(?:แก้|เปลี่ยน|ปรับ|เพิ่ม|ลด|เลื่อน)\S*"
# A quantity change never "moves" (เลื่อน) and is never followed by a month: those are dates.
QUANTITY_CHANGE = r"(?:แก้|เปลี่ยน|ปรับ|เพิ่ม|ลด)\S*"
# Short form fields (purpose, reason, condition) state one thing. A later clause carrying a
# reference number (WO-4512), a clock time (14.30 น.) or an electrical/measurement spec
# (220 โวลต์, 63 แอมป์) is other information; the full message stays with staff.
SHORT_FIELDS = ("purpose", "reason", "condition")
OTHER_INFO = re.compile(
    r"[a-z]+[-_]?\d|\d{1,2}[.:]\d{2}\s*(?:น\.|นาฬิกา)|\d\s*(?:โวลต์|โวลท์|แอมป์|วัตต์|kva|kv|v|a|w|หน้า)"
    r"(?![a-z\u0e00-\u0e7f])",
    re.I,
)
LEADING_CLAUSE = re.compile(r"\s*(?:(?:ใช้เพื่อ|เพื่อ|สำหรับ|ใช้)\s+)?\S+")
# A short field must say something beyond the request itself: the live model once returned
# "เพิ่มอีก" from "ขอเบิกมิเตอร์เพิ่มอีก 2 เครื่อง" as the purpose (11 Oct 2569). Request verbs,
# change words, quantities, units, item names and polite endings carry no purpose.
FILLER = re.compile(
    r"\d+(?:[.,]\d+)?|ขอ|เบิก|ส่งคืน|คืน|แจ้ง|เพิ่ม|ลด|อีก|ด้วย|ทั้งหมด|หน่อย|ครับ|ค่ะ|คะ|นะ|จ้ะ|จ้า|ฮะ"
    r"|แล้ว|และ|กับ|ก็|ตัว|ชิ้น|เครื่อง|อัน|ชุด|ม้วน|เมตร|ลูก|ตู้|หน่วย"
)


def says_nothing(value, catalog):
    """True when a short field holds only request words, quantities and item names."""
    rest = normalise(value)
    for item in catalog:
        for name in (item.name, item.code):
            if name:
                rest = rest.replace(normalise(name), " ")
    rest = FILLER.sub(" ", rest)
    return len(re.findall(r"[\u0e01-\u0e4ea-z]", rest)) < 3


def without_other_info(value):
    """Keep a short field's first clause when a later clause carries other information."""
    lead = LEADING_CLAUSE.match(value)
    if lead and OTHER_INFO.search(value, lead.end()) and not OTHER_INFO.search(lead.group()):
        return value[: lead.end()].strip()
    return value


def _labels():
    from operations.ai_screen import FIELD_LABELS

    return FIELD_LABELS


def boundary_pattern(newline=True):
    names = "|".join(_labels())
    line = r"\n|" if newline else ""
    return re.compile(rf"{line}(?:{names}|หมายเหตุ\S*|รายการ\S*)\s*[:：]|{re.escape(CUT_MARK)}")


VERB = {
    # "ขอ" asks for something only when no other action follows (ขอแก้/ขอคืน/ขอสอบถาม...).
    "withdrawal": re.compile(
        r"ขอ(?!บคุณ|โทษ|แก้|เปลี่ยน|ปรับ|ลด|เพิ่มเป็น|เลื่อน|คืน|ส่ง|แจ้ง|สอบถาม|ถาม|ยกเลิก)"
        r"|เบิก|ต้องการ|อยากได้"
    ),
    "return": re.compile(r"คืน|ส่ง\S{0,20}กลับ"),
}
# A new request clause ("และขอเบิก...", "ส่วน...", "แจ้ง...") ends a labelled or corrected value.
REQUEST_START = r"(?:และ|แล้ว|ส่วน)?(?:ขอ|เบิก|คืน|ส่งคืน|แจ้ง)"
KEEP = re.compile(r"(?:เหมือนเดิม|ตามเดิม|คงเดิม|ตามร่างเดิม)")
DEADLINE = re.compile(r"(?:ต้องการ|ภายใน|ก่อน|ใช้วันที่|รับของ)\S{0,12}\s*(?:วันที่\s*)?$")
PURPOSE_CHUNK = re.compile(r"(?:^|\s)((?:ใช้เพื่อ|เพื่อ|สำหรับ)\S+)")


def _value_end(text, start):
    labels = "|".join(_labels())
    stop = re.compile(
        rf"\n|(?:{labels}|หมายเหตุ\S*|รายการ\S*)\s*[:：]|{re.escape(CUT_MARK)}"
        rf"|\s(?=(?:{labels}|จำนวน)\S*(?:\s|$))|\s(?={REQUEST_START})"
    ).search(text, start)
    return stop.start() if stop else len(text)


def labeled_fields(text):
    """{field: value} for the form's own labels ("อาการ: ..."), each up to the next label,
    line break, cut mark or new request clause. A field labelled twice with different values
    is ambiguous."""
    labels = _labels()
    found, conflicts = {}, set()
    for match in re.finditer(rf"({'|'.join(labels)})\s*[:：]\s*", text):
        value = text[match.end() : _value_end(text, match.end())].strip(" \t,;.")
        name = labels[match.group(1)]
        if not value:
            continue
        if name in found and found[name] != value:
            conflicts.add(name)
        found[name] = value
    return {name: value for name, value in found.items() if name not in conflicts}


def field_corrections(text):
    """{field: value} from "ขอแก้ผลกระทบเป็น X" / "เปลี่ยนเหตุผลการคืน...เป็น X"."""
    labels = _labels()
    found = {}
    pattern = rf"{CHANGE}\s*({'|'.join(labels)})\S*\s*(?:\S+\s+)?เป็น\s*"
    for match in re.finditer(pattern, text):
        value = text[match.end() : _value_end(text, match.end())].strip(" \t,;.")
        if value:
            found.setdefault(labels[match.group(1)], value)
    return found


def kept_fields(text):
    """Fields the requester says stay as they were, named in the same chunk as the keep word
    ("สภาพเหมือนเดิม", "วัตถุประสงค์และวันที่ใช้ตามเดิม", "อาการที่แจ้งยังเหมือนเดิม")."""
    names = set()
    for match in KEEP.finditer(text):
        start = text.rfind(" ", 0, match.start()) + 1
        chunk = text[start : match.start()]
        names |= {name for word, name in _labels().items() if word in chunk}
        if "วัน" in chunk:
            names.add("needed_by")
    return names


def deadline_dates(text):
    """Full dates written right after a deadline word ("ต้องการภายใน 5 พฤษภาคม 2570")."""
    found = set()
    for match in re.finditer(r"\d", text):
        if DEADLINE.search(text[max(0, match.start() - 30) : match.start()]):
            found |= explicit_dates(text[match.start() : match.start() + 25])
    return found


def trim_to_boundary(value):
    """A selected field never runs over another label or a cut mark (screened lines are cut
    marks already; a requester's own multi-line wording is kept)."""
    stop = boundary_pattern(newline=False).search(value, 1)
    return value[: stop.start()].strip(" \t,;.") if stop else value


def snap_end(source, end):
    """Finish an excerpt at the end of its whitespace-separated chunk: Thai writes words without
    spaces, so a selection ending inside a chunk ends inside a phrase ("เปลี่ยน|มิเตอร์ชำรุด")."""
    while end < len(source) and not source[end].isspace():
        end += 1
    return end


def corrected_dates(text):
    """Full dates the requester moved to ("ขอเลื่อนวันที่ต้องการเป็น 16 มีนาคม 2570")."""
    found = set()
    for match in re.finditer(rf"{CHANGE}[^\n]{{0,40}}?(?:เป็น|ไป)\s*", text):
        found |= explicit_dates(text[match.end() : match.end() + 30])
    return found


def negated_codes(text, catalog):
    """Items the requester says they do not want ("ไม่ต้องการฟิวส์", "ไม่ได้ขอเบรกเกอร์")."""
    negation = re.compile(r"ไม่(?:ได้)?(?:ต้องการ|ขอ|เบิก|คืน|เอา)\S{0,4}$")
    return {
        code
        for start, _, code in mentions(text, catalog)
        if negation.search(text[max(0, start - 24) : start].rstrip())
    }


SERIAL_LABEL = r"(?:หมายเลข|เลขเครื่อง|serials?|s/n)"
SLIP_LABEL = r"(?:ใบเบิก|ใบส่งคืน|ใบคืน|เอกสาร|sap)"
# A typed meter row also labels its numbers as the meter's ("เลขมิเตอร์", "หมายเลขเครื่องวัด").
METER_LABEL = r"(?:หมายเลข(?:เครื่องวัด|มิเตอร์|เครื่อง)?|เลข(?:เครื่องวัด|มิเตอร์|เครื่อง)|serials?|s/n)"


def labelled_serials(source, label=SERIAL_LABEL):
    """Identifiers the requester labelled as serial numbers ("หมายเลขเครื่อง X Y")."""
    labelled = []
    for group in re.findall(
        rf"{label}\s*[:：]?\s*([a-z0-9_-]+(?:[ ,]+[a-z0-9_-]+)*)",
        " ".join(source),
    ):
        labelled += [t for t in re.findall(r"[a-z0-9_-]+", group) if t not in labelled]
    return labelled


def serial_tokens(source, label=SERIAL_LABEL):
    text = " ".join(source)
    # Numeric-only identifiers need a serial label, otherwise counts/dates could become serials.
    labelled = set()
    for group in re.findall(
        rf"{label}\s*[:：]?\s*([a-z0-9_-]+(?:[ ,]+[a-z0-9_-]+)*)",
        text,
    ):
        labelled.update(re.findall(r"[a-z0-9_-]+", group))
    mixed = {
        token
        for token in re.findall(r"(?<![a-z0-9_-])[a-z0-9][a-z0-9_-]*", text)
        if any(c.isalpha() for c in token) and any(c.isdigit() for c in token)
    }
    return labelled | mixed


def _named(text, name, catalog):
    """Where the requester names other equipment in their own words (the other-equipment
    group): the text after the name up to the next catalog mention, or nothing."""
    name = normalise(name or "")
    at = text.find(name) if len(name) >= 2 else -1
    if at < 0:
        return []
    end = at + len(name)
    stop = min((start for start, _, _ in mentions(text, catalog) if start >= end), default=None)
    return [text[end:stop]]


def _copied(original, value):
    """The requester's own characters for a value the model copied (case and Thai digits may
    differ), or None when it is not in the message."""
    probe = original.translate(THAI_DIGITS).casefold()
    at = probe.find(value.translate(THAI_DIGITS).casefold())
    return original[at : at + len(value)] if at >= 0 and value else None


def _ground_size(item, text, original_text):
    """A typed row's size must be written in the (screened) message; the original spelling is
    kept. Otherwise it is asked."""
    if not item.size:
        return
    copied = _copied(original_text, item.size)
    if normalise(item.size) not in text or copied is None or not safe_text(copied):
        item.size = ""
        item.rejected.append("size")
    else:
        item.size = copied


SLIP_NUMBER = re.compile(
    rf"{SLIP_LABEL}\s*(?:เลขที่|หมายเลข|เลข|no\.?)?\s*[:：]?\s*([a-z0-9-]+)", re.IGNORECASE
)


def labelled_slip(text):
    """The one SAP slip number the requester labelled ("ใบเบิก 4500012345", "เลขใบเบิก
    WD-2568-0107"), as written, or "" when there is none or more than one. The model sometimes
    leaves a plainly labelled number out (typed-v1, 24 Oct 2569); two labelled numbers, as when
    a message slips in a second one, are left for the chat to ask."""
    found = {}
    for match in SLIP_NUMBER.finditer(text.translate(THAI_DIGITS)):
        number = match[1].strip("-")
        if SAP_NUMBER.match(number) and any(c.isdigit() for c in number):
            found.setdefault(number.casefold(), number)
    return next(iter(found.values())) if len(found) == 1 else ""


def _ground_sap(proposal, text):
    """The SAP slip number must stand alone in the (screened) message and not be a number the
    requester labelled as a meter's or a serial; otherwise the chat asks for it."""
    number = normalise(proposal.sap_number)
    if not number:
        return
    # "ใบเบิกหมายเลข 4500012345" labels the slip, not a meter.
    slips = set(
        re.findall(rf"{SLIP_LABEL}\s*(?:เลขที่|หมายเลข|เลข|no\.?)?\s*[:：]?\s*([a-z0-9-]+)", text)
    )
    labelled = set(labelled_serials([text], METER_LABEL)) - slips
    serials = {normalise(s) for i in proposal.items for s in i.serials}
    alone = re.search(rf"(?<![a-z0-9-]){re.escape(number)}(?![a-z0-9-])", text)
    if alone is None or number in labelled | serials:
        proposal.sap_number = ""
        proposal.rejected_fields.append("sap_number")


def ground(extraction, text, catalog, draft=None, source_context=None):
    original_text, original_draft = source_context or (text, draft)
    labeled = labeled_fields(text)
    corrections = field_corrections(text)
    text = normalise(text)
    kept = kept_fields(text) if draft else set()
    deadlines = deadline_dates(text)
    purposes = PURPOSE_CHUNK.findall(text)
    by_code = {i.code: i for i in catalog}
    spans = item_spans(text, catalog, draft)
    old_items = {i.get("item_code"): i for i in (draft or {}).get("items", [])}
    old_fields = (draft or {}).get("fields", {})
    dates = explicit_dates(text)
    moved = corrected_dates(text)
    if len(dates) > 1 and len(moved) == 1:
        dates = moved
    negated = negated_codes(text, catalog)
    for proposal in extraction.requests:
        _ground_sap(proposal, text)
        retained = []
        for item in proposal.items:
            equipment = by_code.get(item.code)
            group = getattr(equipment, "entry_group", "") if equipment is not None else ""
            old = (
                old_items.get(item.code, {})
                if proposal.intent == (draft or {}).get("intent")
                else {}
            )
            source = spans.get(item.code, [])
            if group == "other" and not source:
                source = _named(text, item.text, catalog)
            if equipment is not None and not source and not old:
                continue  # A catalog entry is not evidence that the requester asked for it.
            if item.code in negated:
                continue  # The requester said they do not want it.
            if equipment is not None and item.quantity is not None:
                previous = old.get("quantity")
                correcting = bool(old) and bool(re.match(r"^(?:ขอ)?(?:แก้|เปลี่ยน)|^จากเดิม", text))
                values = quantities(source, equipment.unit, previous, correcting, typed=bool(group))
                unchanged = (
                    previous not in (None, "")
                    and Decimal(str(previous)) == item.quantity
                    and not values
                )
                if values != {item.quantity} and not unchanged:
                    item.quantity = None
                    item.rejected.append("quantity")
            if group:
                _ground_size(item, text, original_text)
            if equipment is not None:
                original = list(item.serials)
                inherited = old.get("serials", [])
                label = METER_LABEL if group in NUMBERED else SERIAL_LABEL
                tokens = serial_tokens(source, label)
                if group:
                    # A typed row's size or the request's SAP slip number is not a meter number.
                    tokens = {t for t in tokens if not SIZE_LIKE.fullmatch(t)}
                    tokens -= set(re.findall(r"[a-z0-9_-]+", normalise(item.size)))
                    tokens.discard(normalise(proposal.sap_number))
                item.serials = list(
                    dict.fromkeys(
                        s
                        for s in original
                        if s in inherited
                        or (
                            normalise(s) in tokens
                            and any(
                                re.search(
                                    rf"(?<![a-z0-9_-]){re.escape(normalise(s))}(?![a-z0-9_-])", part
                                )
                                for part in source
                            )
                        )
                    )
                )
                # Multiple written identifiers cannot authorize the model to pick a subset.
                if tokens and set(item.serials) != set(inherited):
                    if {normalise(s) for s in item.serials} != tokens:
                        item.serials = []
                written = labelled_serials(source, label)
                count = item.quantity if item.quantity is not None else old.get("quantity")
                if (
                    not item.serials
                    and not inherited
                    and equipment.is_serialized
                    and (proposal.intent != "withdrawal" or group in NUMBERED)
                    and written
                    and count not in (None, "")
                    and len(written) == Decimal(str(count))
                ):
                    # The requester labelled exactly as many serials as the quantity.
                    copies = [source_excerpt(original_text, token) for token in written]
                    if all(copies):
                        item.serials = [c[0] for c in copies]
                if item.serials != original and not (not original and item.serials):
                    item.rejected.append("serials")
            retained.append(item)
        proposal.items = retained
        for name, value in list(proposal.fields.items()):
            previous = (
                old_fields.get(name) if proposal.intent == (draft or {}).get("intent") else None
            )
            unchanged_text = isinstance(previous, str) and normalise(value) == normalise(previous)
            supported = (
                (dates == {value} if dates else value == previous)
                if name == "needed_by"
                else safe_text(value) and (normalise(value) in text or unchanged_text)
            )
            if not supported:
                del proposal.fields[name]
                proposal.rejected_fields.append(name)
            elif name != "needed_by":
                # Validation above uses the filtered input; copying uses the original inbox.
                copied = source_excerpt(original_text, value)
                source = "message"
                if copied is None and unchanged_text:
                    original = (original_draft or {}).get("fields", {}).get(name, "")
                    if safe_text(original) and normalise(original) == normalise(value):
                        copied = source_excerpt(original, value)
                        source = "draft"
                if copied is not None and source == "message":
                    copied = _tidy(original_text, copied, name)
                if copied is None or not safe_text(copied[0]):
                    del proposal.fields[name]
                    proposal.rejected_fields.append(name)
                else:
                    proposal.fields[name] = copied[0]
                    proposal.field_sources[name] = {"source": source, **copied[1]}
        _apply_labels(proposal, labeled, original_text)
        _apply_corrections(proposal, corrections, kept, original_text)
        _fill_stated(proposal, dates, deadlines, purposes, original_text)
        for name in SHORT_FIELDS:
            source = proposal.field_sources.get(name, {}).get("source")
            if name in proposal.fields and source == "message":
                if says_nothing(proposal.fields[name], catalog):
                    # No value stated: dropped, not rejected, so a saved answer stays.
                    del proposal.fields[name]
                    del proposal.field_sources[name]
    filled = [p for p in extraction.requests if p.items or p.fields]
    if filled and len(filled) < len(extraction.requests):
        extraction.requests = filled  # an extra intent with nothing in it is not a request
    if len(extraction.requests) > 1:
        # An extra withdrawal or return needs its own verb in the requester's words; a problem
        # report alone never becomes a withdrawal ("แจ้งปัญหาหม้อแปลง 1 เครื่อง ...").
        extraction.requests = [
            p
            for p in extraction.requests
            if not (VERB.get(p.intent) and not VERB[p.intent].search(text))
            or (draft or {}).get("intent") == p.intent
        ] or extraction.requests
    return extraction


def _tidy(source, copied, name=None):
    """Drop a leading label; stop at a label or cut mark; finish the last phrase; a short
    field leaves out a later clause that carries other information."""
    excerpt, offsets = copied
    start = offsets["start"]
    lead = re.match(rf"(?:{'|'.join(_labels())})\s*[:：]\s*", excerpt)
    if lead:
        excerpt, start = excerpt[lead.end() :], start + lead.end()
    trimmed = trim_to_boundary(excerpt)
    if not trimmed:
        return None
    end = snap_end(source, start + len(trimmed))
    stop = boundary_pattern(newline=False).search(source, start + 1, end)
    value = source[start : stop.start() if stop else end].strip(" \t,;.")
    if name in SHORT_FIELDS:
        value = without_other_info(value)
    if not value:
        return None
    start = source.index(value, start)
    return value, {"start": start, "end": start + len(value)}


def _apply_corrections(proposal, corrections, kept, original_text):
    """ "ขอแก้ผลกระทบเป็น X" sets that field (and X is not also another field); fields the
    requester says stay as before are left to the stored draft instead of the model."""
    for name, value in corrections.items():
        if name not in INTENT_FIELDS.get(proposal.intent, ()) or not safe_text(value):
            continue
        copied = source_excerpt(original_text, value)
        if copied is None:
            continue
        proposal.fields[name] = copied[0]
        proposal.field_sources[name] = {"source": "correction", **copied[1]}
        if name in proposal.rejected_fields:
            proposal.rejected_fields.remove(name)
        for other, text in list(proposal.fields.items()):
            if other != name and other != "needed_by" and normalise(value) in normalise(text):
                del proposal.fields[other]
    for name in kept - set(corrections):
        proposal.fields.pop(name, None)
        proposal.field_sources.pop(name, None)


def _fill_stated(proposal, dates, deadlines, purposes, original_text):
    """Fill what the requester stated unambiguously but the model left out: the one deadline
    date, and the one "เพื่อ.../สำหรับ..." phrase of a withdrawal."""
    if proposal.intent != "withdrawal":
        return
    if "needed_by" not in proposal.fields and len(dates) == 1 and dates <= deadlines:
        proposal.fields["needed_by"] = next(iter(dates))
        proposal.field_sources["needed_by"] = {"source": "stated date"}
    if "purpose" not in proposal.fields and len(purposes) == 1:
        copied = source_excerpt(original_text, purposes[0])
        if copied and safe_text(copied[0]):
            proposal.fields["purpose"] = copied[0]
            proposal.field_sources["purpose"] = {"source": "stated purpose", **copied[1]}


def _apply_labels(proposal, labeled, original_text):
    """A value the requester labelled for this field ("อาการ: มีน้ำมันซึม") wins over a model
    selection that is missing, runs past the label, or contains it."""
    for name in INTENT_FIELDS.get(proposal.intent, ()):
        label = labeled.get(name)
        if label and name in SHORT_FIELDS:
            label = without_other_info(label)
        if not label or not safe_text(label):
            continue
        current = proposal.fields.get(name)
        if current and normalise(current) in normalise(label):
            continue  # a tighter selection inside the labelled value
        copied = source_excerpt(original_text, label)
        if copied is None or not safe_text(copied[0]):
            continue
        proposal.fields[name] = copied[0]
        proposal.field_sources[name] = {"source": "label", **copied[1]}
        if name in proposal.rejected_fields:
            proposal.rejected_fields.remove(name)
