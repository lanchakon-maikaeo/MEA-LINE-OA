"""รหัส OTP ทางอีเมลตอนลงทะเบียน LINE (15 ต.ค. 2569 Q11, Q24–Q26).

An employee who typed their employee ID may ask for a 6-digit code sent to the e-mail in the
roster, and types it back in LINE. The code lives as a keyed hash only, expires after the
minutes ตัวเลือกระบบ sets, allows a few wrong tries, and a LINE account may ask for a few codes a
day. The mail holds the code and one line of warning: no name and no link. A code whose tries
run out counts as one of the day's failed checks (employees.MAX_FAILED).
"""

import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from operations import identity, options, sealing
from operations.models import EmailCode

SUBJECT = "รหัสยืนยันตัวตน LINE ฝอจ."


def sending_ready():
    """The server can send mail: a file or memory backend (local), or SMTP with a host."""
    backend = settings.EMAIL_BACKEND
    if backend.endswith(("filebased.EmailBackend", "locmem.EmailBackend")):
        return True
    return backend.endswith("smtp.EmailBackend") and bool(settings.EMAIL_HOST)


def methods(employee):
    """What this employee may prove themselves with, as ตัวเลือกระบบ allows: "otp", "email"."""
    allowed = options.get(options.EMAIL_METHODS)
    out = []
    if (
        allowed in ("both", "otp")
        and employee.email_sealed
        and sealing.available()
        and sending_ready()
    ):
        out.append("otp")
    if allowed in ("both", "typed") and employee.email_hash:
        out.append("email")
    return out


def _digest(identity_row, employee, code):
    key = hashlib.sha256(f"email-code:{settings.SECRET_KEY}".encode()).digest()
    message = f"{identity_row.pk}:{employee.pk}:{code}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def sent_today(identity_row, now=None):
    now = now or timezone.now()
    return EmailCode.objects.filter(
        identity=identity_row, created_at__gte=now - timedelta(days=1)
    ).count()


@transaction.atomic
def issue(identity_row, employee, now=None):
    """Send a new code; returns "" or why not: "limit" (the day's codes used), "unavailable"."""
    now = now or timezone.now()
    if "otp" not in methods(employee):
        return "unavailable"
    if sent_today(identity_row, now) >= int(options.get(options.CODE_PER_DAY)):
        return "limit"
    address = sealing.unseal(employee.email_sealed)
    if not address:
        return "unavailable"
    code = f"{secrets.randbelow(10**6):06d}"
    minutes = int(options.get(options.CODE_MINUTES))
    EmailCode.objects.filter(identity=identity_row, used_at__isnull=True).update(used_at=now)
    EmailCode.objects.create(
        identity=identity_row,
        employee=employee,
        code_hash=_digest(identity_row, employee, code),
        expires_at=now + timedelta(minutes=minutes),
    )
    body = (
        f"รหัสยืนยันตัวตนของคุณคือ {code}\n"
        f"พิมพ์รหัสนี้ในแชต LINE ฝอจ. ภายใน {minutes} นาที\n\n"
        "ถ้าคุณไม่ได้ขอรหัสนี้ ไม่ต้องทำอะไร"
    )
    transaction.on_commit(lambda: send_mail(SUBJECT, body, settings.DEFAULT_FROM_EMAIL, [address]))
    # Neither the address nor the code goes to the audit trail.
    identity.audit(
        None,
        "identity.email_code_sent",
        identity_row,
        actor_label="line",
        employee=employee.employee_id,
    )
    return ""


@transaction.atomic
def check(identity_row, employee, typed, now=None):
    """(True, "", 0) when the code is right; else (False, reason, tries left): "expired" (none
    waiting, used or past its time) or "wrong"."""
    now = now or timezone.now()
    code = (
        EmailCode.objects.select_for_update()
        .filter(identity=identity_row, employee=employee, used_at__isnull=True)
        .first()
    )
    allowed = int(options.get(options.CODE_TRIES))
    if code is None or code.expires_at <= now or code.tries >= allowed:
        return False, "expired", 0
    text = str(typed or "").translate(str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789"))
    digits = "".join(ch for ch in text if ch in "0123456789")[-6:]
    if hmac.compare_digest(code.code_hash, _digest(identity_row, employee, digits)):
        code.used_at = now
        code.save(update_fields=["used_at"])
        identity.audit(
            None,
            "identity.email_code_passed",
            identity_row,
            actor_label="line",
            employee=employee.employee_id,
        )
        return True, "", 0
    code.tries += 1
    code.save(update_fields=["tries"])
    left = allowed - code.tries
    if left <= 0:
        code.used_at = now
        code.save(update_fields=["used_at"])
        identity.audit(None, "identity.employee_check_failed", identity_row, actor_label="line")
        return False, "expired", 0
    return False, "wrong", left
