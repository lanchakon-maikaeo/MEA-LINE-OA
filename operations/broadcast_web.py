"""One API for the staff website and the LINE Mini App."""

import json
from functools import wraps

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods

from operations import broadcast_auth, broadcasts, notifications
from operations.models import BroadcastCampaign, BroadcastSegment, Division, Role

# ADR 0041: the roles a person holds now; the retired developer and duty roles stay out.
ROLES = (
    Role.STAFF,
    Role.WORK_GROUP_ADMIN,
    Role.SECTION_ADMIN,
    Role.SUPERVISOR,
    Role.DIVISION_VIEWER,
    Role.DEPARTMENT_SUPERVISOR,
    Role.DEPARTMENT_VIEWER,
    Role.ADMIN,
)


def endpoint(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            user = broadcast_auth.actor(request)
            body = {}
            if request.method == "POST":
                if len(request.body) > 50000:
                    raise ValidationError("ข้อมูลประกาศมีขนาดใหญ่เกินไป")
                body = json.loads(request.body)
                if not isinstance(body, dict):
                    raise ValidationError("ข้อมูลไม่ถูกต้อง")
                for key in ("id", "revision", "segment_id", "campaign_id"):
                    if body.get(key) is not None and (type(body[key]) is not int or body[key] < 1):
                        raise ValidationError("หมายเลขรายการหรือรุ่นข้อมูลไม่ถูกต้อง")
            response = JsonResponse(view(request, user, body, *args, **kwargs))
        except PermissionDenied as exc:
            response = JsonResponse({"error": str(exc)}, status=403)
        except ObjectDoesNotExist:
            response = JsonResponse({"error": "ไม่พบรายการที่ระบุ"}, status=404)
        except (ValidationError, ValueError, TypeError) as exc:
            message = (
                " / ".join(exc.messages) if isinstance(exc, ValidationError) else "ข้อมูลไม่ถูกต้อง"
            )
            response = JsonResponse({"error": message}, status=400)
        response["Cache-Control"] = "no-store"
        return response

    return wrapped


def campaign_data(row, user):
    return {
        "id": row.pk,
        "is_test": row.test_source_id is not None,
        "title": row.title,
        "revision": row.revision,
        "status": row.status,
        "status_label": row.get_status_display(),
        "audience": row.audience,
        "segment_id": row.segment_id,
        "content": row.content,
        "scheduled_at": row.scheduled_at,
        "created_at": row.created_at,
        "creator": row.created_by.get_full_name() or row.created_by.get_username(),
        "recipient_count": row.recipient_count,
        "detail": row.detail,
        "may_edit": not row.test_source_id
        and broadcasts.may_edit(user, row)
        and row.status in broadcasts.EDITABLE
        and not row.batches.exists(),
    }


@require_http_methods(["GET"])
@endpoint
def bootstrap(request, user, body):
    scope = broadcasts.require_supervisor(user)
    budget = notifications.budget_state()
    return {
        "user": user.get_full_name() or user.get_username(),
        "department": scope.is_department_supervisor,
        "segments": [
            {
                "id": row.pk,
                "name": row.name,
                "criteria": row.criteria,
                "revision": row.revision,
                "may_edit": broadcasts.may_edit(user, row),
            }
            for row in BroadcastSegment.objects.order_by("name")
        ],
        "divisions": list(
            Division.objects.filter(is_active=True, is_synthetic=False).values("id", "name")
        ),
        # หน่วยงาน: the 18 เขต and every ฝ่าย the roster brought (ฟขข., ฝบก., ...), one entry
        # per unit (ADR 0059). Inactive ones include D00, the holder of anonymous reporters.
        "districts": broadcasts.units(),
        "roles": [{"id": role.value, "name": role.label} for role in ROLES],
        "quota": {"budget": budget.budget, "used": budget.used},
    }


@require_http_methods(["GET", "POST"])
@endpoint
def campaigns(request, user, body):
    if request.method == "POST":
        row = broadcasts.save_draft(
            user,
            title=body.get("title"),
            content=body.get("content"),
            audience=body.get("audience", "all"),
            segment_id=body.get("segment_id"),
            campaign_id=body.get("id"),
            revision=body.get("revision"),
        )
        return {"campaign": campaign_data(row, user)}
    rows = BroadcastCampaign.objects.select_related("created_by").order_by("-created_at", "-pk")
    status = request.GET.get("status")
    if status:
        rows = rows.filter(status=status)
    page = Paginator(rows, 20).get_page(request.GET.get("page", 1))
    return {
        "items": [campaign_data(row, user) for row in page],
        "page": page.number,
        "pages": page.paginator.num_pages,
        "total": page.paginator.count,
    }


@require_http_methods(["GET"])
@endpoint
def detail(request, user, body, pk):
    row = BroadcastCampaign.objects.select_related("created_by").get(pk=pk)
    result = campaign_data(row, user)
    # Recipient identifiers and retry keys are private to the sender service.
    result["batches"] = list(
        row.batches.order_by("sequence").values(
            "sequence", "status", "recipient_count", "attempts", "detail", "accepted_at"
        )
    )
    if row.status in broadcasts.SENT and not row.test_source_id:
        count, strangers = broadcasts.missed_counts(row)
        result["missed"] = {
            "count": count,
            "unregistered": strangers,
            "may_send": broadcasts.may_edit(user, row),
        }
    return {"campaign": result}


@require_http_methods(["POST"])
@endpoint
def action(request, user, body, pk, action):
    revision = body.get("revision")
    if action == "edit":
        row = broadcasts.begin_edit(user, pk, revision)
    elif action == "cancel":
        row = broadcasts.cancel(user, pk, revision)
    elif action == "confirm":
        when = body.get("scheduled_at")
        parsed = parse_datetime(when) if isinstance(when, str) and when else None
        if when is not None and parsed is None:
            raise ValidationError("กรุณาระบุวันและเวลาส่งให้ถูกต้อง")
        row = broadcasts.confirm(user, pk, revision, parsed)
    elif action == "test":
        row = broadcasts.test_to_self(user, pk, revision, body.get("request_key"))
    elif action == "copy":
        row = broadcasts.copy_draft(user, pk)
    elif action == "missed":
        row = broadcasts.send_missed(user, pk)
    elif action == "preview":
        row = BroadcastCampaign.objects.select_related("created_by", "segment").get(pk=pk)
        broadcasts._revision(row, revision)
        messages = broadcasts.build_messages(row.content)
        audience = broadcasts.recipients(row)
        return {
            "messages": messages,
            "recipient_count": len(audience) if audience is not None else None,
            "estimate": True,
            "includes_creator": True,
        }
    else:
        raise ValidationError("ไม่พบคำสั่งที่ระบุ")
    return {"campaign": campaign_data(row, user)}


@require_http_methods(["POST"])
@endpoint
def segments(request, user, body):
    row = broadcasts.save_segment(
        user,
        body.get("name", ""),
        body.get("criteria"),
        segment_id=body.get("id"),
        revision=body.get("revision"),
    )
    return {
        "segment": {
            "id": row.pk,
            "name": row.name,
            "criteria": row.criteria,
            "revision": row.revision,
            "may_edit": True,
        },
        "members": broadcasts.segment_members(row).count(),
    }


@require_http_methods(["GET"])
def page(request, pk=None):
    from django.conf import settings
    from django.shortcuts import render
    from django.views.decorators.csrf import ensure_csrf_cookie

    @ensure_csrf_cookie
    def render_page(request):
        response = render(
            request,
            "operations/broadcast.html",
            {
                "broadcast_config": {"liff_id": settings.LINE_LIFF_ID, "campaign_id": pk},
            },
        )
        response["Cache-Control"] = "no-store"
        response["Content-Security-Policy"] = "; ".join(
            [
                "default-src 'self'",
                "script-src 'self' https://static.line-scdn.net",
                "style-src 'self'",
                "font-src 'self'",
                "img-src 'self' https: data: blob:",
                "media-src 'self' https: blob:",
                "connect-src 'self' https://api.line.me https://liff.line.me https://access.line.me",
                "object-src 'none'",
                "base-uri 'self'",
                "form-action 'self'",
                "frame-ancestors 'none'",
            ]
        )
        return response

    return render_page(request)
