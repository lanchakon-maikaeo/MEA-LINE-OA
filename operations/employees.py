"""ทะเบียนพนักงาน: checking who someone is against the organisation's employee list (ADR 0010).

The user (10 Oct 2569 Q1, Q3): a requester or staff member proves who they are with an
employee ID that matches the list, or by another way, never one fixed way. An admin imports the
list as CSV; the proof value (for example a birth date) is kept only as a keyed hash, so a leak
of this table does not leak the values. Failed checks are counted per LINE account and stop
after MAX_FAILED a day, so the list cannot be guessed through LINE.

23 Oct 2569: the list had no names. 1 Nov 2569 (ADR 0041): it keeps the first name only; a
verified employee is shown by it, or by the employee ID when the list has none (`label`). A
LINE OA user is still tied to the list only by proving the ID or by an admin.
"""

import csv
import hashlib
import hmac
import io
import re
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
# How a verified employee without a name in the list is shown.
LABEL = "พนักงาน {}"
LABELLED = re.compile(r"^พนักงาน \S+$")


def label(employee):
    return employee.name or LABEL.format(employee.employee_id)


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


def units():
    """หน่วยตามทะเบียน (ADR 0060 Q10): the active units the roster puts someone in, by name, as
    the choices of a person's unit. The full-name เขต stay for meters and the warehouse only.
    Before a roster is imported (a new install, the demo), every active unit."""
    rows = District.objects.filter(is_active=True)
    named = rows.filter(employee__is_active=True).distinct()
    return (named if named.exists() else rows).order_by("name")


def follow_roster(actor=None):
    """ADR 0060 Q8: a requester tied to the roster (proved or linked by an admin) belongs to the
    roster's unit; after each import, those it moved are put there. Returns how many moved."""
    from django.db.models import F

    from operations import identity

    link = Requester.EmployeeLink
    rows = (
        Requester.objects.filter(
            employee_link__in=[link.VERIFIED, link.ADMIN], employee__district__is_active=True
        )
        .exclude(district=F("employee__district"))
        .select_related("employee")
    )
    moved = 0
    for requester in rows:
        before = requester.district_id
        requester.district_id = requester.employee.district_id
        requester.save(update_fields=["district"])
        identity.audit(
            actor,
            "requester.unit_followed_roster",
            requester,
            from_district=before,
            to_district=requester.district_id,
        )
        moved += 1
    return moved


@transaction.atomic
def import_csv(actor, data, check_label_text, deactivate_missing=False, synthetic=False):
    """Admin only (actor None = manage.py). Upserts by employee_id; returns a summary."""
    from operations import identity, roster

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
    if not reader.fieldnames or not {"employee_id", "check"} <= set(reader.fieldnames):
        raise ValidationError(f"หัวคอลัมน์ต้องมี {', '.join(COLUMNS)}")
    districts = {d.code: d for d in District.objects.all()}
    divisions = {d.code: d for d in Division.objects.all()}
    seen, problems, created, updated = set(), [], 0, 0
    for number, row in enumerate(reader, start=2):
        employee_id = (row.get("employee_id") or "").strip()
        proof = row.get("check") or ""
        if not employee_id or not normalise(proof):
            problems.append(f"แถว {number}: ต้องมีรหัสพนักงานและข้อมูลยืนยัน")
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
                "district": districts.get(district_code),
                "division": divisions.get(division_code),
                "position": (row.get("position") or "").strip()[:200],
                "name": roster.first_name(row.get("name")),
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
    follow_roster(actor)
    summary = {"created": created, "updated": updated, "closed": closed}
    # Counts only: proof values never go to the audit trail.
    identity.audit(
        actor,
        "employees.imported",
        Employee.objects.get(employee_id=next(iter(seen))),
        actor_label="" if actor else "manage.py",
        **summary,
    )
    return summary


IDENTITY_STATES = (
    ("verified", "ยืนยันแล้ว"),
    ("none", "ยังไม่ผูก"),
)


def identity_state(requester):
    """Where the requester stands against ทะเบียนพนักงาน: proved, or an admin linked it; else
    not linked."""
    link = Requester.EmployeeLink
    if requester.employee_id and requester.employee_link in {link.VERIFIED, link.ADMIN}:
        return "verified"
    return "none"


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
    """(employee, "") for an active employee, one with something to prove against while OTP is
    on (ADR 0059); otherwise (None, reason): "locked" after the day's failures, or "no_match"
    (counted as a failure, so the list cannot be walked through LINE)."""
    from operations import identity, options

    if failed_today(identity_row, now) >= MAX_FAILED:
        return None, "locked"
    employee = (
        Employee.objects.filter(employee_id=(employee_id or "").strip(), is_active=True)
        .select_related("district")
        .first()
    )
    proofless = employee is not None and not (employee.check_hash or employee.email_hash)
    if employee is None or (proofless and options.otp_on()):
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
