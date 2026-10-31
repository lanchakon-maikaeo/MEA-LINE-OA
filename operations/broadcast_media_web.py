"""Authenticated upload/status endpoints and unguessable, scanned public media URLs."""

import re

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from operations import broadcast_auth, broadcast_media, broadcasts
from operations.models import BroadcastMedia


def info(row):
    return {
        "id": str(row.pk),
        "name": row.name,
        "content_type": row.content_type,
        "status": row.status,
        "status_label": row.get_status_display(),
        "detail": row.detail,
        **broadcast_media.urls(row),
    }


@require_POST
def upload(request):
    try:
        user = broadcast_auth.actor(request)
        if len(request.FILES) != 1 or len(request.FILES.getlist("file")) != 1:
            raise ValidationError("กรุณาเลือกไฟล์หนึ่งไฟล์")
        row = broadcast_media.store(user, request.FILES["file"])
        response = JsonResponse({"media": info(row)}, status=202)
    except PermissionDenied as exc:
        response = JsonResponse({"error": str(exc)}, status=403)
    except ValidationError as exc:
        response = JsonResponse({"error": " / ".join(exc.messages)}, status=400)
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def status(request, pk):
    try:
        user = broadcast_auth.actor(request)
        row = get_object_or_404(BroadcastMedia, pk=pk)
        if (
            row.uploaded_by_id != user.pk
            and not broadcasts.require_supervisor(user).is_department_supervisor
        ):
            raise PermissionDenied("ท่านไม่มีสิทธิ์ดูไฟล์นี้")
        response = JsonResponse({"media": info(row)})
    except PermissionDenied as exc:
        response = JsonResponse({"error": str(exc)}, status=403)
    except ValidationError as exc:
        response = JsonResponse({"error": " / ".join(exc.messages)}, status=400)
    response["Cache-Control"] = "no-store"
    return response


def _chunks(stream, count):
    try:
        while count:
            chunk = stream.read(min(count, 64 * 1024))
            if not chunk:
                break
            count -= len(chunk)
            yield chunk
    finally:
        stream.close()


@require_http_methods(["GET", "HEAD"])
def public(request, pk, variant):
    row = get_object_or_404(BroadcastMedia, pk=pk, status=BroadcastMedia.Status.CLEAN)
    if variant not in {"original", "preview"} or not row.storage_name:
        raise Http404
    if variant == "preview" and row.content_type == "video/mp4":
        raise Http404
    path = broadcast_media.root() / (
        row.storage_name + (".preview" if variant == "preview" else "")
    )
    if not path.is_file():
        raise Http404
    mime = "image/jpeg" if variant == "preview" else row.content_type
    size = path.stat().st_size
    start, end = 0, size - 1
    raw_range = request.headers.get("Range")
    if raw_range:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", raw_range)
        try:
            if not match or not any(match.groups()):
                raise ValueError
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(int(last), size - 1) if last else size - 1
            else:
                start = max(0, size - int(last))
            if start > end or start < 0:
                raise ValueError
        except ValueError:
            return HttpResponse(status=416, headers={"Content-Range": f"bytes */{size}"})
    if request.method == "HEAD":
        response = HttpResponse(content_type=mime, status=206 if raw_range else 200)
    elif raw_range:
        stream = path.open("rb")
        stream.seek(start)
        response = StreamingHttpResponse(
            _chunks(stream, end - start + 1), status=206, content_type=mime
        )
    else:
        response = FileResponse(path.open("rb"), content_type=mime)
    response["Content-Length"] = str(end - start + 1)
    response["Accept-Ranges"] = "bytes"
    if raw_range:
        response["Content-Range"] = f"bytes {start}-{end}/{size}"
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "public, max-age=3600"
    response["Content-Disposition"] = 'inline; filename="broadcast-media"'
    return response
