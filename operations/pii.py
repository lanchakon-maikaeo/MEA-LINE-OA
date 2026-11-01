"""Personal data stays out of AI calls (ADR 0014; routing grill 14 Oct 2569, Q26-Q35).

Every AI task that carries text people typed sends its request through ai.post(), which masks
that text here first. Names, phone numbers, national ID numbers, e-mail addresses, house numbers,
account numbers, LINE IDs, links, long numbers (serials and meter numbers too) and the words
staff asked to hide (คำที่ต้องปิดเสมอ) become labels such as [เบอร์โทร-1]. A title stays in front
of a name (นาย[ชื่อ-1]) so the model still reads a person. Labels count afresh in every call, so
nothing links two calls, and the table from label to value lives in this object only while the
call runs: it is never stored or sent. The values go back into the model's answer before
anything reads it. Only what people typed is masked (user and assistant turns); system prompts
are fixed text.

Limits: a name with no title that none of the system's lists know (employees, requesters, staff,
คำที่ต้องปิดเสมอ) can get through. Road, soi and district names stay, because they say where the
work is, not who someone is.
"""

import json
import re
import time
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Max
from django.utils import timezone

from operations import identity
from operations.models import (
    District,
    Division,
    Employee,
    EquipmentType,
    MaskCount,
    MaskedWord,
    Requester,
    StaffProfile,
)

# kind: (the word in its label, what the staff page calls it). An earlier kind wins an overlap.
KINDS = {
    "word": ("ข้อมูลปิด", "คำที่ต้องปิดเสมอ"),
    "email": ("อีเมล", "อีเมล"),
    "link": ("ลิงก์", "ลิงก์"),
    "line": ("ไลน์", "LINE ID"),
    "national_id": ("เลขบัตร", "เลขบัตรประชาชน"),
    "phone": ("เบอร์โทร", "เบอร์โทร"),
    "account": ("เลขบัญชี", "เลขบัญชีและเลขผู้ใช้ไฟ"),
    "address": ("ที่อยู่", "บ้านเลขที่และรหัสไปรษณีย์"),
    "name": ("ชื่อ", "ชื่อคน"),
    "number": ("เลข", "ตัวเลขยาว เช่น เลขเครื่อง เลขมิเตอร์"),
}
RANK = {kind: n for n, kind in enumerate(KINDS)}
LABEL = re.compile(r"\[(" + "|".join(w for w, _ in KINDS.values()) + r")\s*-\s*(\d{1,4})\]")
# The AI tasks as the staff page names them; "eval" is the evaluations' synthetic text.
TASKS = {
    "extract": "สกัดร่างจากแชต",
    "answer": "ตอบแชต",
    "intake": "คุยและกรอกร่างในแชต",
    "screen": "กรองคำสั่งแฝง",
    "triage": "คัดกรองเรื่อง",
    "routing": "ส่งกองตามหน้าที่",
    "sheet_status": "เรียบเรียงสถานะจากชีท",
    "eval": "การประเมิน (ข้อความจำลอง)",
}
NUMBER_DIGITS = 6  # a run of at least this many digits is masked (Q35)

THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
TH = "ก-๎"  # Thai letters, vowels and marks; no digits
TH_START = "ก-ฮเ-ไ"  # a consonant or a leading vowel
LETTER = re.compile(rf"[{TH}A-Za-z]")
THAI_CHAR = re.compile(rf"[{TH}]")

SEP = r"[ .-]?"
PHONE = re.compile(
    rf"(?<![\d+])(?:\+?66{SEP}|0)(?:[689](?:{SEP}\d){{8}}|[2-7](?:{SEP}\d){{7}})(?!\d)"
    r"(?:\s*(?:ต่อ|ext\.?)\s*\d{1,5}(?!\d))?",
    re.I,
)
NATIONAL_ID = re.compile(r"(?<!\d)\d(?:[ -]?\d){12}(?!\d)")
EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
)
LINK = re.compile(r"(?:https?://|www\.|line\.me/|lin\.ee/)[^\s<>\"'\[\]{}]+", re.I)
LINE_ID = re.compile(
    r"(?:line\s*id|lineid|id\s*line|ไลน์\s*ไอดี|ไอดี\s*ไลน์|ไลน์)\s*[:：]?\s*"
    r"(@?(?=[A-Za-z0-9._-]*[A-Za-z])[A-Za-z0-9][A-Za-z0-9._-]{2,29})",
    re.I,
)
HANDLE = re.compile(r"(?<![A-Za-z0-9._%+-])@[A-Za-z0-9][A-Za-z0-9._-]{2,29}")
ACCOUNT = re.compile(
    r"(?:เลขที่\s*บัญชี(?:แสดงสัญญา)?|บัญชี(?:แสดงสัญญา|ธนาคาร|เงินฝาก)?|บช\.?"
    r"|(?<![A-Za-z])c\.?a\.?(?![A-Za-z])|เลขที่\s*สัญญา|สัญญาเลขที่"
    r"|เลข(?:ที่)?\s*ผู้ใช้ไฟ(?:ฟ้า)?|รหัสผู้ใช้ไฟ(?:ฟ้า)?)"
    r"\s*(?:เลขที่|no\.?|#|:|：)?\s*(\d(?:[ -]?\d){5,19})(?!\d)",
    re.I,
)
BANK = re.compile(r"(?<!\d)\d{3}-\d-\d{5}-\d(?!\d)")
ADDRESS = re.compile(
    r"(?:บ้านเลขที่|เลขที่บ้าน)\s*(\d{1,5}(?:\s*/\s*\d{1,4})?)"
    r"|(?:เลขที่|บ้าน)\s*(\d{1,5}\s*/\s*\d{1,4})"
    r"|(?<![\d/])(\d{1,5}/\d{1,4})(?=\s*(?:ซอย|ซ\.|ถนน|ถ\.|หมู่|ม\.|แขวง|ตำบล|ต\.|อำเภอ|อ\.|อาคาร"
    r"|คอนโด))"
    r"|(?:รหัสไปรษณีย์|ไปรษณีย์)\s*(\d{5})(?!\d)"
)
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
CASE_NUMBER = re.compile(r"[A-Za-z]{1,3}\d{2}-\d{4,8}")
DATE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}-\d{1,2}-\d{2,4}")
UNIT_AFTER = re.compile(r"\s*(?:บาท|kva|kwh|kw|หน่วย|วัตต์|โวลต์|แอมป์)", re.I)

# A title keeps its place; the name after it is masked. "คุณ" after ขอบ/บุญ/พระ is a word.
TITLES = r"นางสาว|เด็กชาย|เด็กหญิง|นาย|นาง|น\.ส\.?|ด\.ช\.|ด\.ญ\.|ดร\.|(?<!ขอบ)(?<!บุญ)(?<!พระ)คุณ"
THAI_TITLE = re.compile(rf"({TITLES}) ?(?=[{TH_START}])")
ENGLISH_TITLE = re.compile(
    r"(?<![A-Za-z])(?:Mr|Mrs|Ms|Miss|Dr)\.?\s+([A-Z][A-Za-z'-]+(?:\s+[A-Z][A-Za-z'-]+)?)"
)
NAME_TITLE = re.compile(
    r"^(?:นางสาว|เด็กชาย|เด็กหญิง|นาย|นาง|น\.ส\.?|ด\.ช\.|ด\.ญ\.|ดร\.|คุณ|(?:mrs|mr|ms|miss|dr)(?:\.|\s)+)\s*"
)
SURNAME = re.compile(rf" {{1,2}}([{TH_START}][{TH}]*)")
# Words after a title that make it a word, not a name (นายช่าง, คุณภาพ, คุณแม่, ...).
NOT_NAMES = {
    "นาย": (
        "จ้าง", "ช่าง", "ทุน", "หน้า", "ตรวจ", "ประตู", "ด่าน", "อำเภอ", "ทหาร", "ตำรวจ", "แพทย์",
        "เวร", "ห้าง", "งาน", "ประกัน", "ทะเบียน", "สถานี", "กเทศมนตรี", "กรัฐมนตรี",
    ),
    "นาง": ("พยาบาล", "แบบ"),
    "คุณ": (
        "ภาพ", "สมบัติ", "ค่า", "ประโยชน์", "ลักษณะ", "ธรรม", "วุฒิ", "ความ", "แม่", "พ่อ", "ป้า",
        "ลุง", "ยาย", "ปู่", "ย่า", "น้า", "พี่", "น้อง", "หมอ", "ครู", "ลูกค้า", "ผู้", "ท่าน",
    ),
}  # fmt: skip
# Where a name typed without spaces ends: a strong word first, a short weak one only if none.
STRONG = (
    "แจ้ง", "บอก", "โทร", "ติดต่อ", "ร้องเรียน", "ครับ", "คับ", "ค่ะ", "คะ", "จ้า", "ขอ", "ช่วย",
    "เบอร์", "จาก", "เขต", "กอง", "ฝ่าย", "แผนก", "ฝาก", "ส่ง", "รับ", "ทำ", "อยู่", "เป็น", "ไม่",
    "มี", "ต้อง", "เคย", "กำลัง", "ยัง", "แล้ว", "ซึ่ง", "และ", "หรือ", "กับ", "ของ", "ที่", "ว่า",
    "เมื่อ", "วันที่", "เวลา", "ตอน", "เรื่อง", "บ้าน", "หมู่", "ซอย", "ถนน", "เข้า", "ออก", "ถาม",
    "ตอบ", "เห็น", "พบ", "เจอ", "สั่ง", "เบิก", "คืน", "ซ่อม", "เปลี่ยน", "ตรวจ", "ดู", "ผู้",
    "เจ้าหน้าที่", "หัวหน้า", "ช่าง", "มาก", "ล่วงหน้า",
)  # fmt: skip
WEAK = ("มา", "ไป", "ได้", "ให้", "จะ", "ก็", "นะ", "ด้วย", "ค่า")
# Words that are never a first name standing alone, even when an account is called that.
COMMON = {
    "เจ้าหน้าที่", "ผู้แจ้ง", "หัวหน้า", "ผู้อำนวยการ", "ผอ.", "พนักงาน", "ช่าง", "แอดมิน", "admin",
    "staff", "test", "ทดสอบ", "เขต", "กอง", "ฝ่าย", "แผนก", "การไฟฟ้า", "ไฟฟ้า", "ลูกค้า",
    "ผู้ใช้ไฟ", "ระบบ", "ศูนย์", "สำนักงาน", "intake", "support",
}  # fmt: skip
THINK = re.compile(r"<think>.*?</think>", re.S)
FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def _lengths(values):
    """{length: set} longest first, so a scan tries the longest value at each place first."""
    by_length = {}
    for value in values:
        by_length.setdefault(len(value), set()).add(value)
    return dict(sorted(by_length.items(), reverse=True))


def _thai_id(digits):
    if len(digits) != 13:
        return False
    total = sum(int(d) * (13 - i) for i, d in enumerate(digits[:12]))
    return (11 - total % 11) % 10 == int(digits[12])


def _lower(text):
    """Lower case with the same length, so places in it are places in the text."""
    return "".join(c.lower() if len(c.lower()) == 1 else c for c in text)


@dataclass(frozen=True)
class Names:
    """Names the system knows: full names anywhere, first names only standing alone."""

    full: dict = field(default_factory=dict)
    first: dict = field(default_factory=dict)

    @classmethod
    def of(cls, full=(), first=()):
        return cls(_lengths(full), _lengths(first))


def clean_name(value):
    text = " ".join(str(value or "").replace("​", "").split()).lower()
    return NAME_TITLE.sub("", text).strip()


class Masker:
    """One AI call's masking: labels count afresh and the values stay in this object."""

    def __init__(self, names=None, words=(), codes=(), stops=()):
        self.names = names or Names()
        self.words = _lengths({" ".join(w.lower().split()) for w in words if len(w.strip()) >= 2})
        self.codes = {c.upper() for c in codes}
        stops = {s for s in stops if s}
        self.strong = tuple(sorted(set(STRONG) | stops, key=len, reverse=True))
        self.values = {}  # (kind, value key) -> label
        self.labels = {}  # (label word, n) -> the original text
        self.next = Counter()
        self.reserved = set()  # label-like text already in what people typed
        self.counts = Counter()  # kind -> distinct values masked in this call

    @classmethod
    def for_ai(cls):
        stops = domain_words()
        return cls(known_names(stops), masked_words(), item_codes(), stops)

    # --- Masking --------------------------------------------------------------------------

    def mask(self, text):
        if not isinstance(text, str) or not text.strip():
            return text
        self._reserve(text)
        pieces, last = [], 0
        for start, end, kind in self._spans(text.translate(THAI_DIGITS)):
            pieces += [text[last:start], self._label(kind, text[start:end])]
            last = end
        pieces.append(text[last:])
        return "".join(pieces)

    def mask_body(self, body):
        """A chat-completions body with the user and assistant turns masked."""
        turns = [
            m
            for m in body.get("messages", [])
            if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)
        ]
        for message in turns:  # label-like text anywhere keeps its number out of use
            self._reserve(message["content"])
        return {
            **body,
            "messages": [
                {**m, "content": self._mask_content(m["content"])} if m in turns else m
                for m in body.get("messages", [])
            ],
        }

    def _reserve(self, text):
        for found in LABEL.finditer(text):
            self.reserved.add((found[1], int(found[2])))

    def _mask_content(self, content):
        # Turns are JSON built by the tasks: mask the strings inside, keep the JSON valid.
        try:
            data = json.loads(content)
        except ValueError:
            return self.mask(content)
        if not isinstance(data, (dict, list)):
            return self.mask(content)
        masked = _tree(data, self.mask)
        return content if masked == data else json.dumps(masked, ensure_ascii=False)

    def _label(self, kind, value):
        key = (kind, _value_key(kind, value))
        label = self.values.get(key)
        if label is None:
            word = KINDS[kind][0]
            number = self.next[word] + 1
            while (word, number) in self.reserved:
                number += 1
            self.next[word] = number
            label = f"[{word}-{number}]"
            self.values[key] = label
            self.labels[(word, number)] = value
            self.counts[kind] += 1
        return label

    def _spans(self, cased):
        """(start, end, kind) of what to mask; detectors read Thai digits as Arabic."""
        probe = _lower(cased)
        found = [
            *self._known(probe, self.words, "word"),
            *((m.start(), m.end(), "email") for m in EMAIL.finditer(probe)),
            *self._links(probe),
            *((m.start(1), m.end(1), "line") for m in LINE_ID.finditer(probe)),
            *((m.start(), m.end(), "line") for m in HANDLE.finditer(probe)),
            *(
                (m.start(), m.end(), "national_id")
                for m in NATIONAL_ID.finditer(probe)
                if _thai_id(re.sub(r"\D", "", m.group()))
            ),
            *((m.start(), m.end(), "phone") for m in PHONE.finditer(probe)),
            *((m.start(1), m.end(1), "account") for m in ACCOUNT.finditer(probe)),
            *((m.start(), m.end(), "account") for m in BANK.finditer(probe)),
            *self._addresses(probe),
            *self._known(probe, self.names.full, "name"),
            *self._known(probe, self.names.first, "name", alone=True),
            *self._titled(probe),
            *((m.start(1), m.end(1), "name") for m in ENGLISH_TITLE.finditer(cased)),
            *self._numbers(probe),
        ]
        chosen = []
        for start, end, kind in sorted(found, key=lambda s: (RANK[s[2]], s[0] - s[1], s[0])):
            if end > start and all(end <= s or start >= e for s, e, _ in chosen):
                chosen.append((start, end, kind))
        return sorted(chosen)

    @staticmethod
    def _links(probe):
        for m in LINK.finditer(probe):
            end = m.start() + len(m.group().rstrip(".,;:!?)"))
            yield m.start(), end, "link"

    @staticmethod
    def _addresses(probe):
        for m in ADDRESS.finditer(probe):
            group = next(g for g in range(1, 5) if m.group(g) is not None)
            yield m.start(group), m.end(group), "address"

    def _numbers(self, probe):
        for m in TOKEN.finditer(probe):
            token = m.group()
            if sum(c.isdigit() for c in token) < NUMBER_DIGITS:
                continue
            if token.upper() in self.codes or CASE_NUMBER.fullmatch(token):
                continue
            if DATE.fullmatch(token) or UNIT_AFTER.match(probe, m.end()):
                continue
            yield m.start(), m.end(), "number"

    def _known(self, probe, table, kind, alone=False):
        """Values from a {length: set} table found in the text (case-insensitive)."""
        if not table:
            return
        size, i = len(probe), 0
        while i < size:
            for length, values in table.items():
                if probe[i : i + length] in values and (not alone or self._alone(probe, i, length)):
                    yield i, i + length, kind
                    i += length - 1
                    break
            i += 1

    def _alone(self, probe, start, length):
        """A first name with no letter right before it and none right after, or a word that
        usually follows a name (สมชายแจ้งว่า...)."""
        end = start + length
        if start and LETTER.match(probe[start - 1]):
            return False
        return end >= len(probe) or not LETTER.match(probe[end]) or self._boundary_at(probe, end)

    def _boundary_at(self, probe, at):
        return any(probe.startswith(w, at) for w in self.strong) or any(
            probe.startswith(w, at) for w in WEAK
        )

    def _titled(self, probe):
        for m in THAI_TITLE.finditer(probe):
            start = m.end()
            run_end = start
            while run_end < len(probe) and THAI_CHAR.match(probe[run_end]):
                run_end += 1
            run = probe[start:run_end]
            title = m.group(1)
            if run.startswith(NOT_NAMES.get(title, ())) or self._boundary_at(run, 0):
                continue
            end = start + self._cut(run)
            if end - start < 2:
                continue
            if end == run_end:
                end = self._surname(probe, end)
            yield start, end, "name"

    def _cut(self, run):
        """Where a name typed without a space ends inside a run of Thai letters."""
        for words in (self.strong, WEAK):
            found = [i for i in (run.find(w, 2) for w in words) if i != -1]
            if found:
                return min(found)
        return len(run)

    def _surname(self, probe, end):
        m = SURNAME.match(probe, end)
        if m is None:
            return end
        run = m.group(1)
        if self._boundary_at(run, 0):
            return end
        cut = self._cut(run)
        if cut < 2 or any(stop in run[:cut] for stop in self.strong if len(stop) > 2):
            return end
        return m.start(1) + cut

    # --- Putting the values back ----------------------------------------------------------

    def restore(self, text):
        if not self.labels or not isinstance(text, str):
            return text
        return LABEL.sub(lambda m: self.labels.get((m[1], int(m[2])), m[0]), text)

    def restore_response(self, data):
        """The provider's answer with the values back in the message content."""
        try:
            message = data["choices"][0]["message"]
            content = message["content"]
        except (KeyError, IndexError, TypeError):
            return data
        if not self.labels or not isinstance(content, str) or not LABEL.search(content):
            return data
        stripped = FENCE.sub("", THINK.sub("", content).strip()).strip()
        try:
            parsed = json.loads(stripped)
        except ValueError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            restored = json.dumps(_tree(parsed, self.restore), ensure_ascii=False)
        else:
            restored = self.restore(content)
        choice = {**data["choices"][0], "message": {**message, "content": restored}}
        return {**data, "choices": [choice, *data["choices"][1:]]}


def _tree(value, change):
    if isinstance(value, str):
        return change(value)
    if isinstance(value, list):
        return [_tree(v, change) for v in value]
    if isinstance(value, dict):
        return {k: _tree(v, change) for k, v in value.items()}
    return value


def _value_key(kind, value):
    """The same value typed twice gets one label (081-234-5678 and 0812345678)."""
    if kind in ("phone", "national_id", "account", "number"):
        return re.sub(r"\D", "", value.translate(THAI_DIGITS)) or value
    return " ".join(value.lower().split())


# --- What the masking knows -------------------------------------------------------------------

_NAMES = {"key": None, "at": 0.0, "names": Names()}
NAMES_SECONDS = 300  # rebuilt sooner whenever someone is added


def known_names(stops=()):
    """Requesters with a name, staff and the roster's first names (ADR 0041), rebuilt when a
    list grows or a roster is imported. An employee label (พนักงาน <รหัส>) is no name."""
    key = (
        tuple(
            Requester.objects.filter(is_anonymous=False, is_internal=False)
            .aggregate(n=Count("pk"), last=Max("pk"))
            .values()
        ),
        tuple(StaffProfile.objects.aggregate(n=Count("pk"), last=Max("pk")).values()),
        tuple(Employee.objects.aggregate(n=Count("pk"), last=Max("imported_at")).values()),
    )
    now = time.monotonic()
    if _NAMES["key"] != key or now - _NAMES["at"] > NAMES_SECONDS:
        _NAMES.update(key=key, at=now, names=_build_names(set(stops)))
    return _NAMES["names"]


def _build_names(stops):
    from operations import employees

    values = [
        *Requester.objects.filter(is_anonymous=False, is_internal=False).values_list(
            "display_name", flat=True
        ),
        *StaffProfile.objects.values_list("display_name", flat=True),
        *Employee.objects.filter(is_active=True).exclude(name="").values_list("name", flat=True),
    ]
    full, first = set(), set()
    for value in values:
        if employees.LABELLED.match(" ".join((value or "").split())):
            continue
        tokens = clean_name(value).split()
        if not tokens:
            continue
        if len(tokens) >= 2:
            full.update(j for j in (" ".join(tokens), "".join(tokens)) if len(j) >= 4)
        head = tokens[0]
        if len(head) >= 3 and not head.isdigit() and head not in COMMON and head not in stops:
            first.add(head)
    return Names.of(full, first)


def domain_words():
    """Words of the work itself (equipment, hazards, districts, divisions): never a name, and
    where a name typed without a space ends."""
    from operations import policy  # local: policy builds on far more than this module

    words = set(COMMON)
    current = policy.current_policy()
    if current is not None:
        words.update(e["term"].lower() for e in current.doc.get("equipment_terms", []))
        words.update(t.lower() for t in current.doc.get("hazard_terms", []))
    words.update(n.lower() for n in District.objects.values_list("name", flat=True))
    for code, name in Division.objects.values_list("code", "name"):
        words.update((code.lower(), name.lower()))
    return words


def masked_words():
    return list(MaskedWord.objects.filter(removed_at__isnull=True).values_list("word", flat=True))


def item_codes():
    return list(EquipmentType.objects.values_list("code", flat=True))


# --- Counting (Q30: how much was hidden, never what) ------------------------------------------

_COUNTING_AS = ContextVar("pii_counting_as", default="")


@contextmanager
def counting_as(task):
    """Count the masking under `task`; the evaluations count as "eval", not as real use."""
    token = _COUNTING_AS.set(task)
    try:
        yield
    finally:
        _COUNTING_AS.reset(token)


def counted_as(task):
    """counting_as for a whole function (the evaluation gates)."""

    def decorate(function):
        @wraps(function)
        def run(*args, **kwargs):
            with counting_as(task):
                return function(*args, **kwargs)

        return run

    return decorate


def record(task, counts):
    task = _COUNTING_AS.get() or task
    day = timezone.localdate()
    for kind, number in counts.items():
        rows = MaskCount.objects.filter(day=day, task=task, kind=kind)
        if rows.update(count=F("count") + number):
            continue
        try:
            with transaction.atomic():
                MaskCount.objects.create(day=day, task=task, kind=kind, count=number)
        except IntegrityError:
            rows.update(count=F("count") + number)


def preview(text):
    """What the AI would get for this text (the staff page's "ลองปิดข้อมูล"): no AI call."""
    masker = Masker.for_ai()
    return masker.mask(text), masker.counts


# --- คำที่ต้องปิดเสมอ (Q30, Q34) --------------------------------------------------------------


def hint(word):
    """A word as staff other than admins see it: the first letter of each part (ส**** ม***)."""
    return " ".join(part[:1] + "*" * (len(part) - 1) for part in word.split())


def add_word(actor, word, reason):
    """Any staff member hides a word from the AI from now on."""
    if not identity.staff_scope(actor).has_any_access:
        raise ValidationError("เฉพาะเจ้าหน้าที่")
    word, reason = " ".join(str(word or "").split()), str(reason or "").strip()
    if len(word) < 2 or len(word) > 100:
        raise ValidationError("คำต้องยาว 2 ถึง 100 ตัวอักษร")
    with transaction.atomic():
        if MaskedWord.objects.filter(removed_at__isnull=True, word__iexact=word).exists():
            raise ValidationError("มีคำนี้ในรายการแล้ว")
        row = MaskedWord.objects.create(word=word, reason=reason, added_by=actor)
        identity.audit(actor, "pii.word_added", row, reason, hint=hint(word))
    return row


def remove_word(actor, row, reason):
    """Only an admin takes a word off the list: from then on the AI can see it again."""
    identity.require_admin(actor)
    reason = str(reason or "").strip()
    with transaction.atomic():
        row = MaskedWord.objects.select_for_update().get(pk=row.pk)
        if row.removed_at is not None:
            raise ValidationError("คำนี้ถูกเอาออกไปแล้ว")
        row.removed_by, row.removed_at, row.removal_reason = actor, timezone.now(), reason
        row.save(update_fields=["removed_by", "removed_at", "removal_reason"])
        identity.audit(actor, "pii.word_removed", row, reason, hint=hint(row.word))
    return row
