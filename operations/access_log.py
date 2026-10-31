"""Sign-in records for Log การใช้งาน (31 Oct 2569 Q14, Q15), connected in apps.py: a sign-in
keeps the IP address and the browser; a failed one keeps the username typed, never the password.
"""

from django.conf import settings
from django.contrib.auth import get_user_model

from operations import identity
from operations.models import AuditEvent


def client_ip(request):
    """The caller's address; behind Caddy (M12) the first X-Forwarded-For hop is the client."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded and getattr(settings, "SECURE_PROXY_SSL_HEADER", None):
        return forwarded.split(",")[0].strip()[:64]
    return request.META.get("REMOTE_ADDR", "")[:64]


def browser(request):
    """A short name such as Chrome บน Windows, from the User-Agent."""
    agent = request.META.get("HTTP_USER_AGENT", "")
    name = next(
        (
            n
            for key, n in (
                ("Edg/", "Edge"),
                ("Line/", "LINE"),
                ("Firefox/", "Firefox"),
                ("Chrome/", "Chrome"),
                ("Safari/", "Safari"),
            )
            if key in agent
        ),
        "",
    )
    system = next(
        (
            s
            for key, s in (
                ("iPhone", "iOS"),
                ("iPad", "iPadOS"),
                ("Android", "Android"),
                ("Windows", "Windows"),
                ("Mac OS X", "macOS"),
                ("Linux", "Linux"),
            )
            if key in agent
        ),
        "",
    )
    return " บน ".join(p for p in (name, system) if p) or "ไม่ทราบ"


def _seen(request):
    return {"ip": client_ip(request), "browser": browser(request)} if request is not None else {}


def record_login(sender, request, user, **kwargs):
    identity.audit(user, "staff.login", user, actor_label="web", **_seen(request))


def record_logout(sender, request, user, **kwargs):
    if user is not None:
        identity.audit(user, "staff.logout", user, actor_label="web")


def record_login_failed(sender, credentials, request=None, **kwargs):
    """Q14: the username typed, never the password."""
    username = str(credentials.get("username", ""))[:150]
    user = get_user_model().objects.filter(username=username).first()
    AuditEvent.objects.create(
        actor=None,
        actor_label="web",
        action="staff.login_failed",
        target_type="User",
        target_id=str(user.pk) if user else "",
        data={"username": username, **_seen(request)},
    )
