"""Quarantined Broadcast-only media. Public URLs exist only after successful scanning."""

import hashlib
import struct
import uuid
import warnings
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from PIL import Image, ImageOps

from operations import attachments, broadcasts, identity, uploads
from operations.models import BroadcastMedia

M = BroadcastMedia
IMAGE_LIMIT = 10_000_000
VIDEO_LIMIT = 200_000_000
PREVIEW_LIMIT = 1_000_000


def root():
    folder = Path(settings.MEDIA_ROOT) / "broadcast"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder


def public_base():
    base = (settings.BROADCAST_MEDIA_BASE_URL or settings.STAFF_WEB_BASE_URL).rstrip("/")
    broadcasts._url(base)
    return base


def urls(row):
    if row.status != M.Status.CLEAN:
        return {}
    base = public_base()
    return {
        "url": f"{base}/broadcasts/media/{row.pk}/original/",
        "preview": f"{base}/broadcasts/media/{row.pk}/preview/"
        if row.content_type != "video/mp4"
        else "",
    }


def store(user, upload):
    broadcasts.require_supervisor(user)
    public_base()  # Do not accept an upload that LINE cannot later fetch over HTTPS.
    head = upload.read(32)
    upload.seek(0)
    mime = attachments.detect_type(head)
    if head[4:8] == b"ftyp" and any(
        brand in head[8:] for brand in (b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"M4V ")
    ):
        mime = "video/mp4"
    if mime not in {"image/jpeg", "image/png", "video/mp4"}:
        raise ValidationError("กรุณาเลือกไฟล์ JPEG, PNG หรือวิดีโอ MP4")
    limit = VIDEO_LIMIT if mime == "video/mp4" else IMAGE_LIMIT
    if upload.size > limit:
        raise ValidationError("วิดีโอต้องไม่เกิน 200 MB และรูปภาพต้องไม่เกิน 10 MB")
    row = M(uploaded_by=user, name=uploads._clean_name(upload.name), content_type=mime)
    path = root() / f"{row.pk}.upload"
    digest, size = hashlib.sha256(), 0
    try:
        with path.open("xb") as output:
            path.chmod(0o600)
            for part in upload.chunks(64 * 1024):
                size += len(part)
                if size > limit:
                    raise ValidationError("ไฟล์มีขนาดเกินที่กำหนด")
                digest.update(part)
                output.write(part)
        if not size:
            raise ValidationError("ไฟล์ไม่มีข้อมูล")
        row.size, row.sha256 = size, digest.hexdigest()
        with transaction.atomic():
            row.save()
            identity.audit(user, "broadcast.media_uploaded", row, size=size, content_type=mime)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return row


def _images(path, original, preview, mime):
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path, formats=["JPEG", "PNG"]) as image:
            if image.width * image.height > 40_000_000:
                raise ValueError("image exceeds 40 megapixels")
            image.load()
            clean = ImageOps.exif_transpose(image).copy()
            if clean.mode == "P" and "transparency" in clean.info:
                clean = clean.convert("RGBA")
            clean.info.clear()
            if mime == "image/jpeg":
                clean = clean.convert("RGB")
                clean.save(original, "JPEG", quality=95)
            else:
                clean.save(original, "PNG")
            if original.stat().st_size > IMAGE_LIMIT:
                raise ValueError("encoded image exceeds 10 MB")
            clean.thumbnail((1024, 1024))
            if clean.mode in {"RGBA", "LA", "P"}:
                rgba = clean.convert("RGBA")
                thumb = Image.new("RGB", rgba.size, "white")
                thumb.paste(rgba, mask=rgba.getchannel("A"))
            else:
                thumb = clean.convert("RGB")
            thumb.save(preview, "JPEG", quality=80, optimize=True)
            if preview.stat().st_size > PREVIEW_LIMIT:
                raise ValueError("preview exceeds 1 MB")


def _check_mp4(stream, size):
    """Check bounded top-level boxes, video handler and actual media data, without decoding."""
    found, video = set(), False
    offset = 0
    while offset < size:
        stream.seek(offset)
        header = stream.read(8)
        if len(header) != 8:
            raise ValueError("truncated MP4")
        length, kind = struct.unpack(">I4s", header)
        header_size = 8
        if length == 1:
            length = int.from_bytes(stream.read(8), "big")
            header_size = 16
        if length == 0:
            length = size - offset
        if length < header_size or offset + length > size:
            raise ValueError("invalid MP4 box")
        found.add(kind)
        if kind == b"moov":
            if length > 10_000_000:
                raise ValueError("MP4 metadata too large")
            video = b"vide" in stream.read(length - header_size)
        offset += length
    if not {b"ftyp", b"moov", b"mdat"} <= found or not video:
        raise ValueError("MP4 video track missing")


def scan(pk):
    now = timezone.now()
    with transaction.atomic():
        row = M.objects.select_for_update().get(pk=pk)
        if row.status not in {M.Status.PENDING, M.Status.SCANNING} or row.next_scan_at > now:
            return
        token = uuid.uuid4()
        row.status, row.lease_token = M.Status.SCANNING, token
        row.next_scan_at = now + timedelta(minutes=10)
        row.save(update_fields=["status", "lease_token", "next_scan_at"])
    path = root() / f"{row.pk}.upload"
    stem = f"{row.pk}-{token}"
    original, preview = root() / stem, root() / f"{stem}.preview"
    fields = {"status": M.Status.REJECTED, "detail": "ไฟล์ไม่ถูกต้อง กรุณาเลือกไฟล์ใหม่"}
    try:
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != row.sha256:
                raise ValueError("file changed")
            stream.seek(0)
            clean, detail = attachments.clamd_scan(stream)
            if not clean:
                fields["detail"] = "ไฟล์ไม่ผ่านการตรวจความปลอดภัย กรุณาเลือกไฟล์ใหม่"
            else:
                if row.content_type == "video/mp4":
                    _check_mp4(stream, row.size)
                    stream.seek(0)
                    with original.open("xb") as output:
                        for part in iter(lambda: stream.read(64 * 1024), b""):
                            output.write(part)
                else:
                    _images(path, original, preview, row.content_type)
                original.chmod(0o600)
                if preview.exists():
                    preview.chmod(0o600)
                fields = {"status": M.Status.CLEAN, "detail": "", "storage_name": stem}
    except attachments.ScannerUnavailable:
        fields = {
            "status": M.Status.PENDING,
            "detail": "กำลังรอระบบตรวจไฟล์ กรุณารอสักครู่",
            "next_scan_at": timezone.now() + timedelta(minutes=1),
        }
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        pass  # Invalid or missing media never becomes public.
    updated = M.objects.filter(pk=pk, lease_token=token).update(**fields)
    if not updated or fields["status"] != M.Status.CLEAN:
        original.unlink(missing_ok=True)
        preview.unlink(missing_ok=True)
    elif updated:
        path.unlink(missing_ok=True)


def process_pending(limit=2):
    pending = M.objects.filter(
        status__in=[M.Status.PENDING, M.Status.SCANNING], next_scan_at__lte=timezone.now()
    ).order_by("next_scan_at")[:limit]
    for row in pending:
        scan(row.pk)


def resolve_content(content):
    """Resolve upload IDs only after scanning; client URLs never override those files."""
    result = dict(content)
    for field in ("media_id", "preview_id"):
        if not content.get(field):
            continue
        try:
            pk = uuid.UUID(str(content[field]))
            row = M.objects.get(pk=pk, status=M.Status.CLEAN)
        except (ValueError, M.DoesNotExist) as exc:
            raise ValidationError("กรุณารอไฟล์ผ่านการตรวจสอบก่อนส่งประกาศ") from exc
        if field == "preview_id":
            if row.content_type == "video/mp4":
                raise ValidationError("ภาพปกต้องเป็น JPEG หรือ PNG")
            result["preview"] = urls(row)["preview"]
        else:
            is_video = content.get("kind") == "video"
            if is_video != (row.content_type == "video/mp4"):
                raise ValidationError("ชนิดไฟล์ไม่ตรงกับรูปแบบประกาศ")
            result["url"] = urls(row)["url"]
            if not is_video:
                result["preview"] = urls(row)["preview"]
    return result
