"""เลขเรื่องและเลขงานสั่ง (ADR 0045): the owner division's code, the Buddhist-era year and the
division's own gap-free sequence for that year, shared by its cases and its orders, such as
กคว.69-000142. Numbers issued before keep their old shape (W69-000142, T69-000001)."""

import re

from django.utils import timezone

from operations.models import CaseNumberCounter, Division

# The letters before the year are the code, with any Thai words stuck to it (Thai has no
# spaces); dots, a space and leading zeros may be missing when someone types it.
_NUMBER = re.compile(r"([฀-๿A-Za-z.\-]*?)[.\s]?(?<!\d)(\d{2})-(\d{1,6})(?!\d)")
_TAIL = re.compile(r"(\d{2})-(\d{1,6})")
OLD_KINDS = "WRPT"  # before ADR 0045: one sequence a kind for the whole system, T for งานสั่ง


def issue(division, now):
    """Gap-free per division and Buddhist-era year: the counter row is locked and rolls back."""
    prefix, year_be = division.code, timezone.localtime(now).year + 543
    CaseNumberCounter.objects.bulk_create(
        [CaseNumberCounter(prefix=prefix, year_be=year_be)], ignore_conflicts=True
    )
    counter = CaseNumberCounter.objects.select_for_update().get(prefix=prefix, year_be=year_be)
    counter.last_value += 1
    counter.save(update_fields=["last_value"])
    return f"{prefix}{year_be % 100:02d}-{counter.last_value:06d}"


def codes():
    """Every division code, longest first; a closed division's numbers still exist."""
    return sorted(Division.objects.values_list("code", flat=True), key=len, reverse=True)


def _bare(text):
    return "".join(text.split()).replace(".", "").upper()


def _written(prefix, year, sequence, known, whole):
    bare = _bare(prefix)
    for code in known:
        if _bare(code) and (bare == _bare(code) or not whole and bare.endswith(_bare(code))):
            return f"{code}{year}-{int(sequence):06d}"
    if bare[-1:] in tuple(OLD_KINDS) and (len(bare) == 1 or not whole) and len(sequence) == 6:
        return f"{bare[-1]}{year}-{sequence}"
    return ""


def read(text, known=None):
    """The number the whole text is, written as issued (กคว69-142 → กคว.69-000142), else ''."""
    match = _NUMBER.fullmatch((text or "").strip())
    if match is None:
        return ""
    return _written(*match.groups(), codes() if known is None else known, whole=True)


def find(text, known=None):
    """Every number in the text, written as issued, in order and once each."""
    known = codes() if known is None else known
    found = (_written(*m.groups(), known, whole=False) for m in _NUMBER.finditer(text or ""))
    return list(dict.fromkeys(n for n in found if n))


def tail(text):
    """'69-000142' for a number typed without its division code (ADR 0045), else ''."""
    match = _TAIL.fullmatch("".join((text or "").split()))
    return f"{match[1]}-{int(match[2]):06d}" if match else ""
