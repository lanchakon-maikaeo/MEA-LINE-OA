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
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from operations.models import AuditEvent, District, Division, Employee

MAX_FAILED = 3  # failed checks per LINE account per 24 hours
COLUMNS = ("employee_id", "name", "district_code", "division_code", "position", "check")
DEFAULT_LABEL = "ข้อมูลยืนยันตัวตน"
THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")


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
    return Employee.objects.filter(is_active=True).exclude(check_hash="").exists()


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
            problems.append(f"แถว {number}: ไม่รู้จักเขต {district_code}")
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
    summary = {"created": created, "updated": updated, "closed": closed}
    # Counts only: names and proof values never go to the audit trail.
    identity.audit(
        actor,
        "employees.imported",
        Employee.objects.get(employee_id=next(iter(seen))),
        actor_label="" if actor else "manage.py",
        **summary,
    )
    return summary


def failed_today(identity_row, now=None):
    now = now or timezone.now()
    return AuditEvent.objects.filter(
        action="identity.employee_check_failed",
        target_type="ChannelIdentity",
        target_id=str(identity_row.pk),
        created_at__gte=now - timedelta(days=1),
    ).count()


def verify(identity_row, employee_id, proof, district=None, now=None):
    """(employee, "") when the ID and proof match an active employee (and their district, when
    the list has one); otherwise (None, reason): "locked", "no_match" or "district"."""
    from operations import identity

    if failed_today(identity_row, now) >= MAX_FAILED:
        return None, "locked"
    employee = Employee.objects.filter(
        employee_id=(employee_id or "").strip(), is_active=True
    ).first()
    matched = employee is not None and hmac.compare_digest(employee.check_hash or "", digest(proof))
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
