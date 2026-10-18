"""ทะเบียนพนักงาน: checking who someone is against the organisation's employee list (ADR 0010).

The user (10 Oct 2569 Q1, Q3): a requester or staff member proves who they are with an
employee ID that matches the list, or by another way, never one fixed way. An admin imports the
list as CSV; the proof value (for example a birth date) is kept only as a keyed hash, so a leak
of this table does not leak the values. Failed checks are counted per LINE account and stop
after MAX_FAILED a day, so the list cannot be guessed through LINE.
"""

import csv
import hashlib
import hmac
import io
import re
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from operations.models import AuditEvent, District, Division, Employee, Requester

MAX_FAILED = 3  # failed checks per LINE account per 24 hours
COLUMNS = ("employee_id", "name", "district_code", "division_code", "position", "check")
DEFAULT_LABEL = "ข้อมูลยืนยันตัวตน"
THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
# A title in front of a name; an English one needs a dot or a space after it.
TITLE = re.compile(
    r"^(?:นางสาว|นาง|นาย|น\.ส\.|ด\.ช\.|ด\.ญ\.|ดร\.|(?:mrs|mr|ms|miss|dr)(?:\.|\s)+)\s*"
)


def normalise(value):
    """The same proof typed differently (spaces, slashes, Thai digits, case) is one value."""
    text = str(value or "").translate(THAI_DIGITS).strip().lower()
    return re.sub(r"[\s\-/.:]", "", text)


def _key():
    return hashlib.sha256(f"employee-check:{settings.SECRET_KEY}".encode()).digest()


def digest(value):
    return hmac.new(_key(), normalise(value).encode(), hashlib.sha256).hexdigest()


def check_label():
    """What to ask for as proof, as the latest import named it."""
    label = (
        Employee.objects.filter(is_active=True)
        .exclude(check_label="")
        .values_list("check_label", flat=True)
        .first()
    )
    return label or DEFAULT_LABEL


def has_directory():
    """A roster someone can prove themselves against: a proof value or a roster e-mail."""
    from django.db.models import Q

    return (
        Employee.objects.filter(is_active=True)
        .filter(~Q(check_hash="") | ~Q(email_hash=""))
        .exists()
    )


@transaction.atomic
def import_csv(actor, data, check_label_text, deactivate_missing=False, synthetic=False):
    """Admin only (actor None = manage.py). Upserts by employee_id; returns a summary."""
    from operations import identity

    if actor is not None:
        identity.require_admin(actor)
    label = " ".join((check_label_text or "").split())[:100]
    if not label:
        raise ValidationError("บอกชื่อข้อมูลยืนยันตัวตน เช่น วันเกิด (วว/ดด/ปปปป)")
    try:
        text = data.decode("utf-8-sig") if isinstance(data, bytes) else data
    except UnicodeDecodeError as exc:
        raise ValidationError("ไฟล์ต้องเป็น CSV แบบ UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not {"employee_id", "name", "check"} <= set(reader.fieldnames):
        raise ValidationError(f"หัวคอลัมน์ต้องมี {', '.join(COLUMNS)}")
    districts = {d.code: d for d in District.objects.all()}
    divisions = {d.code: d for d in Division.objects.all()}
    seen, problems, created, updated = set(), [], 0, 0
    for number, row in enumerate(reader, start=2):
        employee_id = (row.get("employee_id") or "").strip()
        name = " ".join((row.get("name") or "").split())
        proof = row.get("check") or ""
        if not employee_id or not name or not normalise(proof):
            problems.append(f"แถว {number}: ต้องมีรหัสพนักงาน ชื่อ และข้อมูลยืนยัน")
            continue
        if employee_id in seen:
            problems.append(f"แถว {number}: รหัสพนักงาน {employee_id} ซ้ำ")
            continue
        district_code = (row.get("district_code") or "").strip()
        division_code = (row.get("division_code") or "").strip()
        if district_code and district_code not in districts:
            problems.append(f"แถว {number}: ไม่รู้จักหน่วยงาน (เขต/ฝ่าย) {district_code}")
            continue
        if division_code and division_code not in divisions:
            problems.append(f"แถว {number}: ไม่รู้จักกอง {division_code}")
            continue
        seen.add(employee_id)
        _, made = Employee.objects.update_or_create(
            employee_id=employee_id,
            defaults={
                "name": name[:200],
                "district": districts.get(district_code),
                "division": divisions.get(division_code),
                "position": (row.get("position") or "").strip()[:200],
                "check_label": label,
                "check_hash": digest(proof),
                "is_active": True,
                "is_synthetic": synthetic,
            },
        )
        created, updated = created + made, updated + (not made)
    if problems:
        raise ValidationError(problems[:20])
    if not seen:
        raise ValidationError("ไฟล์ไม่มีรายชื่อพนักงาน")
    closed = 0
    if deactivate_missing:
        closed = (
            Employee.objects.filter(is_active=True)
            .exclude(employee_id__in=seen)
            .update(is_active=False)
        )
    matched, unmatched, review = match_requesters()
    summary = {
        "created": created,
        "updated": updated,
        "closed": closed,
        "matched": matched,
        "unmatched": unmatched,
        "review": review,
    }
    # Counts only: names and proof values never go to the audit trail.
    identity.audit(
        actor,
        "employees.imported",
        Employee.objects.get(employee_id=next(iter(seen))),
        actor_label="" if actor else "manage.py",
        **summary,
    )
    return summary


def name_key(name):
    """One person's name typed with or without a title, spaces or capitals is one key."""
    text = " ".join(str(name or "").replace("\u200b", "").split()).lower()
    return re.sub(r"\s", "", TITLE.sub("", text))


def name_index():
    """Active employees by name key."""
    people = defaultdict(list)
    for employee in Employee.objects.filter(is_active=True).select_related("district", "division"):
        people[name_key(employee.name)].append(employee)
    people.pop("", None)
    return people


def same_name(requester, people):
    """Active employees with this requester's name; none for anonymous and internal ones."""
    if requester.is_anonymous or requester.is_internal:
        return []
    return people.get(name_key(requester.display_name), [])


def name_match(requester, found):
    """The employee a name may tie by itself (user, 14 Oct 2569: the name matches exactly one
    active employee) when the list does not put them in another district, as in the employee
    check; otherwise None and an admin decides."""
    if len(found) != 1:
        return None
    employee = found[0]
    return employee if employee.district_id in (None, requester.district_id) else None


IDENTITY_STATES = (
    ("verified", "ยืนยันแล้ว"),
    ("name", "ชื่อตรง รอยืนยัน"),
    ("review", "ต้องตรวจ"),
    ("none", "ยังไม่ผูก"),
)


def identity_state(requester, found):
    """Where the requester stands against ทะเบียนพนักงาน: proved (or an admin set it), tied by
    name only, a name the system would not tie by itself (shared or another district), or none.
    A link an admin removed is not asked about again."""
    link = Requester.EmployeeLink
    if requester.employee_id and requester.employee_link in {link.VERIFIED, link.ADMIN}:
        return "verified"
    if requester.employee_id and requester.employee_link == link.NAME:
        return "name"
    if found and requester.employee_link != link.UNLINKED:
        return "review"
    return "none"


def review_note(requester, found):
    """Why the system left a matching name for an admin."""
    if len(found) > 1:
        return f"ชื่อนี้มี {len(found)} คนในทะเบียน"
    if found and found[0].district_id not in (None, requester.district_id):
        return f"ทะเบียนระบุ{found[0].district.name}"
    return "ชื่อตรงพนักงานในทะเบียน"


@transaction.atomic
def match_requesters(requesters=None):
    """Tie requesters to ทะเบียนพนักงาน by name, for the position on the requester list. A name
    match never proves who someone is. Verified links and links an admin made or removed are
    never touched; a name link that no longer matches one employee is cleared. Returns
    (matched, cleared, review): review counts the names left for an admin to check."""
    from operations import identity

    people = name_index()
    rows = Requester.objects.select_for_update().filter(
        is_anonymous=False,
        is_internal=False,
        employee_link__in=["", Requester.EmployeeLink.NAME],
    )
    rows = rows.exclude(status=Requester.Status.REVOKED)
    if requesters is not None:
        rows = rows.filter(pk__in=[r.pk for r in requesters])
    matched = cleared = review = 0
    for requester in rows:
        found = same_name(requester, people)
        employee = name_match(requester, found)
        if employee is None and found:
            review += 1
        if (employee.pk if employee else None) == requester.employee_id:
            continue
        requester.employee = employee
        requester.employee_link = Requester.EmployeeLink.NAME if employee else ""
        requester.save(update_fields=["employee", "employee_link"])
        if employee is None:
            cleared += 1
            identity.audit(None, "requester.employee_unmatched", requester, actor_label="system")
        else:
            matched += 1
            identity.audit(
                None,
                "requester.employee_matched",
                requester,
                actor_label="system",
                employee=employee.employee_id,
            )
    return matched, cleared, review


def failed_today(identity_row, now=None):
    now = now or timezone.now()
    return AuditEvent.objects.filter(
        action="identity.employee_check_failed",
        target_type="ChannelIdentity",
        target_id=str(identity_row.pk),
        created_at__gte=now - timedelta(days=1),
    ).count()


def proves(employee, proof):
    """The proof value of the old lists, or the roster e-mail typed (15 ต.ค. 2569 Q24)."""
    from operations import roster

    if employee.check_hash and hmac.compare_digest(employee.check_hash, digest(proof)):
        return True
    return bool(employee.email_hash) and hmac.compare_digest(
        employee.email_hash, roster.email_digest(proof)
    )


def lookup(identity_row, employee_id, now=None):
    """(employee, "") for an active employee with something to prove against; otherwise (None,
    reason): "locked" after the day's failures, or "no_match" (counted as a failure, so the list
    cannot be walked through LINE)."""
    from operations import identity

    if failed_today(identity_row, now) >= MAX_FAILED:
        return None, "locked"
    employee = (
        Employee.objects.filter(employee_id=(employee_id or "").strip(), is_active=True)
        .select_related("district")
        .first()
    )
    if employee is None or not (employee.check_hash or employee.email_hash):
        identity.audit(None, "identity.employee_check_failed", identity_row, actor_label="line")
        return None, "no_match"
    return employee, ""


def verify(identity_row, employee_id, proof, district=None, now=None):
    """(employee, "") when the ID and proof match an active employee (and their district, when
    the list has one); otherwise (None, reason): "locked", "no_match" or "district"."""
    from operations import identity

    if failed_today(identity_row, now) >= MAX_FAILED:
        return None, "locked"
    employee = Employee.objects.filter(
        employee_id=(employee_id or "").strip(), is_active=True
    ).first()
    matched = employee is not None and proves(employee, proof)
    reason = "" if matched else "no_match"
    if matched and district is not None and employee.district_id not in (None, district.pk):
        reason = "district"
    if reason:
        # Neither the typed ID nor the proof is stored, only that a check failed.
        identity.audit(None, "identity.employee_check_failed", identity_row, actor_label="line")
        return None, reason
    identity.audit(
        None,
        "identity.employee_check_passed",
        identity_row,
        actor_label="line",
        employee=employee.employee_id,
    )
    return employee, ""
