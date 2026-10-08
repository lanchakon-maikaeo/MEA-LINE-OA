"""Files staff attach on the case page (M10 round 1 Q11).

JPG, PNG or PDF (checked by content, not by name), at most 5 per save and 10 MB each, stored
like LINE files under a random private name and quarantined until ClamAV reports them clean
(attachments.process_pending in the worker). Files are never deleted by staff and never sent
to the requester.
"""

import hashlib
import secrets

from django.conf import settings

from operations import attachments, identity
from operations.models import Attachment

MAX_FILES = 5


def may_upload(st):
    """Members of the owner division, or of a division with work on the case (ADR 0004)."""
    if st.member(st.owner):
        return True
    divisions = {c.division_id for c in st.children} | set(st.line_division.values())
    return any(st.member(d) for d in divisions)


def check(files):
    """Problems with the chosen files in plain Thai (empty when they may be stored)."""
    problems = []
    if len(files) > MAX_FILES:
        problems.append(f"แนบได้ครั้งละไม่เกิน {MAX_FILES} ไฟล์")
    for upload in files:
        if upload.size > settings.ATTACHMENT_MAX_BYTES:
            problems.append(f"{upload.name}: ใหญ่เกิน 10 MB")
            continue
        head = upload.read(16)
        upload.seek(0)
        if attachments.detect_type(head) is None:
            problems.append(f"{upload.name}: รับเฉพาะ JPG, PNG หรือ PDF")
    return problems


def _clean_name(name):
    name = "".join(ch for ch in (name or "") if ch.isprintable() and ch not in '/\\:*?"<>|')
    return name.strip()[:200] or "ไฟล์แนบ"


def store(user, case, files, note=""):
    """Store checked files as pending scan; returns the new attachments."""
    stored = []
    for upload in files:
        data = upload.read()
        content_type = attachments.detect_type(data)
        if content_type is None or len(data) > settings.ATTACHMENT_MAX_BYTES:
            continue
        name = secrets.token_hex(16)
        path = attachments.storage_dir() / name
        path.write_bytes(data)
        path.chmod(0o600)
        attachment = Attachment.objects.create(
            uploaded_by=user,
            case=case,
            declared_name=_clean_name(upload.name),
            content_type=content_type,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            storage_name=name,
            scan_status=Attachment.ScanStatus.PENDING_SCAN,
            note=note[:200],
        )
        identity.audit(
            user,
            "case.attachment_added",
            case,
            note,
            number=case.number,
            attachment=str(attachment.pk),
            content_type=content_type,
            size=len(data),
        )
        stored.append(attachment)
    return stored


def discard(stored):
    """Bytes of files whose rows were rolled back with a refused action."""
    for attachment in stored:
        (attachments.storage_dir() / attachment.storage_name).unlink(missing_ok=True)
