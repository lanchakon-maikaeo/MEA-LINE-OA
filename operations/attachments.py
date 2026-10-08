"""Private attachment storage, type checks and ClamAV scanning (M03).

Files are stored under a random name outside any served path and stay quarantined until
clamd reports them clean. A scanner outage leaves files pending; nothing is passed unscanned.
"""

import hashlib
import secrets
import socket
import struct
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from operations import line_api
from operations.models import Attachment

S = Attachment.ScanStatus
MAX_ATTEMPTS = 5
SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"%PDF-", "application/pdf"),
)
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}


class ScannerUnavailable(Exception):
    pass


def storage_dir():
    path = Path(settings.MEDIA_ROOT) / "attachments"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def detect_type(data):
    """Type from the bytes themselves; names and declared types are not trusted."""
    for magic, content_type in SIGNATURES:
        if data.startswith(magic):
            return content_type
    return None


def clamd_scan(data, chunk=64 * 1024):
    """clamd INSTREAM: returns (clean, detail). Raises ScannerUnavailable on any failure."""
    try:
        with socket.create_connection(
            (settings.CLAMAV_HOST, settings.CLAMAV_PORT), timeout=30
        ) as conn:
            conn.sendall(b"zINSTREAM\0")
            for start in range(0, len(data), chunk):
                part = data[start : start + chunk]
                conn.sendall(struct.pack(">I", len(part)) + part)
            conn.sendall(struct.pack(">I", 0))
            response = b""
            while not response.endswith(b"\0"):
                received = conn.recv(4096)
                if not received:
                    break
                response += received
    except OSError as exc:
        raise ScannerUnavailable(str(exc)) from exc
    text = response.rstrip(b"\0").decode(errors="replace")
    if text.endswith("OK"):
        return True, ""
    if text.endswith("FOUND"):
        return False, text.split(":", 1)[-1].strip()[:200]
    raise ScannerUnavailable(text[:200] or "empty response")


def _update(attachment, expected_status, **fields):
    """Conditional update so a concurrent worker never overwrites a newer result."""
    updated = Attachment.objects.filter(pk=attachment.pk, scan_status=expected_status).update(
        **fields
    )
    return bool(updated)


def _retry_or_reject(attachment, detail):
    attempts = attachment.attempts + 1
    fields = {"attempts": attempts, "scan_detail": detail[:200]}
    if attempts >= MAX_ATTEMPTS:
        fields["scan_status"] = S.REJECTED
    _update(attachment, attachment.scan_status, **fields)


def fetch(attachment):
    try:
        data = line_api.fetch_content(attachment.line_message_id, settings.ATTACHMENT_MAX_BYTES)
    except line_api.ContentTooLarge:
        return _update(attachment, S.PENDING_FETCH, scan_status=S.REJECTED, scan_detail="too large")
    except line_api.LineApiError as exc:
        if exc.status in {400, 404, 410}:
            # Content expired or never existed: the user has to send the file again.
            return _update(
                attachment, S.PENDING_FETCH, scan_status=S.REJECTED, scan_detail="unavailable"
            )
        return _retry_or_reject(attachment, f"fetch {exc.status}")
    except OSError as exc:
        return _retry_or_reject(attachment, f"fetch {type(exc).__name__}")
    content_type = detect_type(data)
    if content_type is None:
        return _update(
            attachment, S.PENDING_FETCH, scan_status=S.REJECTED, scan_detail="type not allowed"
        )
    name = secrets.token_hex(16)
    path = storage_dir() / name
    path.write_bytes(data)
    path.chmod(0o600)
    return _update(
        attachment,
        S.PENDING_FETCH,
        scan_status=S.PENDING_SCAN,
        storage_name=name,
        content_type=content_type,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        attempts=0,
        scan_detail="",
    )


def scan(attachment):
    data = (storage_dir() / attachment.storage_name).read_bytes()
    if hashlib.sha256(data).hexdigest() != attachment.sha256:
        return _update(attachment, S.PENDING_SCAN, scan_status=S.REJECTED, scan_detail="changed")
    try:
        clean, detail = clamd_scan(data)
    except ScannerUnavailable as exc:
        # Never pass a file unscanned; it waits for the scanner instead.
        return _update(attachment, S.PENDING_SCAN, scan_detail=f"scanner: {exc}"[:200])
    return _update(
        attachment,
        S.PENDING_SCAN,
        scan_status=S.CLEAN if clean else S.INFECTED,
        scan_detail=detail,
        scanned_at=timezone.now(),
    )


def process_pending(limit=20):
    """Fetch then scan waiting attachments; returns how many were looked at."""
    pending = list(
        Attachment.objects.filter(scan_status__in=[S.PENDING_FETCH, S.PENDING_SCAN]).order_by(
            "created_at"
        )[:limit]
    )
    for attachment in pending:
        if attachment.scan_status == S.PENDING_FETCH:
            fetch(attachment)
            attachment.refresh_from_db()
        if attachment.scan_status == S.PENDING_SCAN:
            scan(attachment)
        if attachment.registry_record_id:
            from operations import registry  # M11: evidence that failed opens a discrepancy

            attachment.refresh_from_db()
            registry.after_scan(attachment)
    return len(pending)


def purge_rejected(days=30):
    """Delete stored bytes of rejected/infected files after the review window."""
    cutoff = timezone.now() - timezone.timedelta(days=days)
    old = Attachment.objects.filter(
        scan_status__in=[S.REJECTED, S.INFECTED], created_at__lt=cutoff
    ).exclude(storage_name="")
    for attachment in old:
        (storage_dir() / attachment.storage_name).unlink(missing_ok=True)
        Attachment.objects.filter(pk=attachment.pk).update(storage_name="")
