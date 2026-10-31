"""LINE-verified, Broadcast-only sessions; never accept client-supplied staff roles."""

import json
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.middleware.csrf import rotate_token
from django.utils import timezone
from django.views.decorators.http import require_POST

from operations import broadcasts
from operations.models import ChannelIdentity, StaffProfile

SESSION_KEY = "broadcast.line_session"


def verify_token(token):
    if not settings.LINE_MINI_CHANNEL_ID:
        raise PermissionDenied("ยังไม่ได้ตั้งค่าการเข้าสู่ระบบ LINE Mini App")
    if not isinstance(token, str) or not token or len(token) > 10000:
        raise PermissionDenied("กรุณาเข้าสู่ระบบ LINE อีกครั้ง")
    data = urllib.parse.urlencode(
        {"id_token": token, "client_id": settings.LINE_MINI_CHANNEL_ID}
    ).encode()
    request = urllib.request.Request(
        "https://api.line.me/oauth2/v2.1/verify",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            claims = json.loads(response.read(65536))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise PermissionDenied("ไม่สามารถยืนยันบัญชี LINE ได้ กรุณาเข้าสู่ระบบอีกครั้ง") from exc
    now = timezone.now().timestamp()
    if (
        not isinstance(claims, dict)
        or claims.get("iss") != "https://access.line.me"
        or claims.get("aud") != settings.LINE_MINI_CHANNEL_ID
        or type(claims.get("exp")) is not int
        or claims["exp"] <= now
        or not isinstance(claims.get("sub"), str)
        or not claims["sub"]
    ):
        raise PermissionDenied("บัญชี LINE ไม่ตรงกับช่องทางที่กำหนดหรือหมดอายุแล้ว")
    return claims


def _linked_user(external_id):
    link = (
        ChannelIdentity.objects.select_related("staff_user")
        .filter(channel="line", external_id=external_id, staff_user__is_active=True)
        .exclude(status=ChannelIdentity.Status.REVOKED)
        .first()
    )
    if link is None:
        raise PermissionDenied("กรุณาเชื่อมบัญชี LINE กับบัญชีเจ้าหน้าที่ก่อนใช้งาน")
    broadcasts.require_supervisor(link.staff_user)
    return link


def actor(request):
    """Recheck role, linkage, expiry and administrative revocation on every request."""
    session = request.session.get(SESSION_KEY)
    if session is not None:
        now = timezone.now().timestamp()
        if not isinstance(session, dict) or session.get("expires", 0) <= now:
            raise PermissionDenied("การเข้าสู่ระบบหมดอายุ กรุณาเข้าสู่ระบบ LINE อีกครั้ง")
        link = _linked_user(session.get("subject"))
        if link.pk != session.get("identity") or link.staff_user_id != session.get("user"):
            raise PermissionDenied("บัญชีที่เชื่อมมีการเปลี่ยนแปลง กรุณาเข้าสู่ระบบอีกครั้ง")
        profile = StaffProfile.objects.filter(user=link.staff_user).first()
        if (
            profile
            and profile.sessions_valid_after
            and profile.sessions_valid_after.timestamp() >= session.get("issued", 0)
        ):
            raise PermissionDenied("การเข้าสู่ระบบถูกยกเลิก กรุณาเข้าสู่ระบบอีกครั้ง")
        return link.staff_user
    if not request.user.is_authenticated:
        raise PermissionDenied("กรุณาเข้าสู่ระบบก่อนใช้งาน")
    broadcasts.require_supervisor(request.user)
    return request.user


@require_POST
def login(request):
    try:
        if len(request.body) > 12000:
            raise PermissionDenied("ข้อมูลเข้าสู่ระบบไม่ถูกต้อง")
        body = json.loads(request.body)
        if not isinstance(body, dict):
            raise PermissionDenied("ข้อมูลเข้าสู่ระบบไม่ถูกต้อง")
        claims = verify_token(body.get("id_token"))
        link = _linked_user(claims["sub"])
        now = timezone.now().timestamp()
        request.session.cycle_key()
        request.session[SESSION_KEY] = {
            "subject": claims["sub"],
            "identity": link.pk,
            "user": link.staff_user_id,
            "issued": now,
            "expires": min(claims["exp"], now + 3600),
        }
        rotate_token(request)
        response = JsonResponse({"ok": True})
    except (ValueError, PermissionDenied) as exc:
        message = str(exc) if isinstance(exc, PermissionDenied) else "ข้อมูลเข้าสู่ระบบไม่ถูกต้อง"
        response = JsonResponse({"error": message}, status=403)
    response["Cache-Control"] = "no-store"
    return response


@require_POST
def logout(request):
    request.session.pop(SESSION_KEY, None)
    rotate_token(request)
    return JsonResponse({"ok": True})
