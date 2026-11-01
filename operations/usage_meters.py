"""เครื่องวัดในเรื่องการใช้ไฟฟ้า (user, 25 Oct 2569 Q3, Q5, Q11, Q23): each กตฟ. report names one or
more meters, each with its meter number and เขตของเครื่องวัด (the district where it is installed,
which may not be the requester's). The requester types the district's name; it must be one of
the active districts. A case keeps them as [{"number": "12345678", "district": "D14"}]."""

import re

from operations.models import District

METER_NUMBER = re.compile(r"^[A-Za-z0-9-]{4,20}$")
THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
MAX_METERS = 20
# Words around a district's own name: "การไฟฟ้านครหลวงเขตบางเขน", "กฟน. เขตวัดเลียบ", "เขต บางนา".
NOISE = re.compile(r"การไฟฟ้านครหลวง|กฟน\.?|ฝ่าย|เขต|\s+")


def clean_number(text):
    """The meter number as typed (spaces and Thai digits normalised), or "" when malformed."""
    number = str(text or "").translate(THAI_DIGITS).replace(" ", "")
    return number if METER_NUMBER.match(number) else ""


def name_key(name):
    return NOISE.sub("", str(name or "")).casefold()


def match_district(text):
    """The active district whose own name the text is ("บางเขน", "เขตบางเขน"), or None."""
    wanted = name_key(text)
    if not wanted:
        return None
    for district in District.objects.filter(is_active=True).order_by("code"):
        if name_key(district.name) == wanted:
            return district
    return None


def district_in(text):
    """The district a requester's words name (1 Nov 2569: กตฟ. types it among the details): a
    part between commas or lines, else one word of it; None when none is a district's name."""
    districts = {}
    for district in District.objects.filter(is_active=True).order_by("code"):
        districts.setdefault(name_key(district.name), district)
    parts = [p for p in re.split(r"[,\n]", str(text or "")) if p.strip()]
    for piece in [*parts, *(w for p in parts for w in p.split())]:
        found = districts.get(name_key(piece))
        if found is not None:
            return found
    return None


# Words that show a meter is a customer's (Q10): with the policy's usage words (ลักใช้ไฟ,
# ไฟแพง, …, admin-editable) and a district named in the message.
CUSTOMER_WORDS = (
    "ลูกค้า",
    "ผู้ใช้ไฟ",
    "บ้าน",
    "หน่วยไฟ",
    "ทบทวนหน่วย",
    "ร้องเรียน",
    "อ้อมมิเตอร์",
    "ไม่ผ่านมิเตอร์",
)


def customer_meter(text):
    """Whether the message shows its meter is a customer's: the line between a กตฟ. report and
    a meter of กคว. (owner, 25 Oct 2569 Q10). Without it the chat asks instead of guessing."""
    from operations import policy  # local: policy reads the database

    text = str(text or "")
    if any(word in text for word in CUSTOMER_WORDS) or policy.usage_text(text):
        return True
    key = name_key(text)
    return any(
        name_key(name) and name_key(name) in key
        for name in District.objects.filter(is_active=True).values_list("name", flat=True)
    )


def examples(count=3):
    return ", ".join(short(d.name) for d in District.objects.filter(is_active=True)[:count])


def short(name):
    """The district's short name: "การไฟฟ้านครหลวงเขตบางเขน" → "เขตบางเขน"."""
    return str(name).replace("การไฟฟ้านครหลวง", "").strip() or str(name)


def clean(raw):
    """A submission's meters, or ValueError: shape only, the districts are checked in services."""
    if raw in (None, ""):
        return ()
    if not isinstance(raw, list) or len(raw) > MAX_METERS:
        raise ValueError(f"ระบุเครื่องวัดได้ไม่เกิน {MAX_METERS} เครื่อง")
    meters = []
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) - {"number", "district"}:
            raise ValueError("เครื่องวัดแต่ละเครื่องต้องมีหมายเลขและเขต")
        number = clean_number(entry.get("number"))
        district = entry.get("district")
        if not number:
            raise ValueError("หมายเลขเครื่องวัดใช้ตัวอักษร ตัวเลข หรือขีด 4 ถึง 20 ตัว")
        if not isinstance(district, str) or len(district) > 16:
            raise ValueError("เขตของเครื่องวัดไม่ถูกต้อง")  # "" since 1 Nov 2569: not typed
        meters.append({"number": number, "district": district})
    if len({m["number"] for m in meters}) != len(meters):
        raise ValueError("หมายเลขเครื่องวัดซ้ำกัน")
    return tuple(meters)


def describe(meters):
    """For the summary and the case page: "12345678 (เขตบางเขน), 87654321" (no district typed)."""
    names = dict(District.objects.values_list("code", "name"))
    return ", ".join(
        f"{m['number']} ({short(names.get(m['district'], m['district']))})"
        if m["district"]
        else m["number"]
        for m in meters
    )
