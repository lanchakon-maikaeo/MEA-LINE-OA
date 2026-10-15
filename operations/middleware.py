import re
from datetime import datetime

from django.contrib import messages
from django.contrib.auth import logout
from django.db import IntegrityError, transaction
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

from operations.models import FormSubmission, StaffProfile

SESSION_AUTH_AT = "operations.auth_at"
# Paths a user with a forced password change may still reach.
PASSWORD_CHANGE_EXEMPT = ("/accounts/password/", "/accounts/logout/", "/health/", "/static/")


def record_login_time(sender, request, user, **kwargs):
    request.session[SESSION_AUTH_AT] = timezone.now().isoformat()


class StaffSessionMiddleware:
    """End sessions revoked by an admin and enforce one-time password changes."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated:
            profile = StaffProfile.objects.filter(user=user).first()
            if profile is not None:
                if self._revoked(request, profile):
                    logout(request)
                    return redirect(f"{reverse('login')}?next={request.path}")
                if profile.must_change_password and not request.path.startswith(
                    PASSWORD_CHANGE_EXEMPT
                ):
                    return redirect("password_change")
        return self.get_response(request)

    @staticmethod
    def _revoked(request, profile):
        if profile.sessions_valid_after is None:
            return False
        raw = request.session.get(SESSION_AUTH_AT)
        if raw is None:
            return True
        return datetime.fromisoformat(raw) <= profile.sessions_valid_after


# M10 Q3: pages load only their own CSS, HTMX, fonts and images; no inline script or style.
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)


class ContentSecurityPolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response


FORM_KEY = re.compile(r"^[A-Za-z0-9-]{16,64}$")
FORM_DONE_COOKIE = "faoj_form_done"


class FormOnceMiddleware:
    """ADR 0015 Q21: a staff form posted again with the same `_form_key` (added by app.js) is
    not applied twice; the person lands where the first submit went. After a submit that went
    through, a short cookie tells app.js to drop the draft it kept for that form."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        key = request.POST.get("_form_key", "") if request.method == "POST" else ""
        if not key or not FORM_KEY.match(key) or not request.user.is_authenticated:
            return self.get_response(request)
        try:
            with transaction.atomic():
                row = FormSubmission.objects.create(
                    key=key, user=request.user, path=request.path[:300]
                )
        except IntegrityError:
            first = FormSubmission.objects.filter(user=request.user, key=key).first()
            messages.info(request, "รายการนี้บันทึกไปแล้ว ระบบไม่บันทึกซ้ำ")
            return redirect((first.location if first else "") or request.path)
        response = self.get_response(request)
        if 300 <= response.status_code < 400:
            FormSubmission.objects.filter(pk=row.pk).update(
                location=response.get("Location", "")[:500]
            )
            response.set_cookie(
                FORM_DONE_COOKIE, key, max_age=300, samesite="Lax", secure=request.is_secure()
            )
        else:  # form errors (200) or a failure: the person may correct it and send again
            FormSubmission.objects.filter(pk=row.pk).delete()
        return response
