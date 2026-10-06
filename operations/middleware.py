from datetime import datetime

from django.contrib.auth import logout
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

from operations.models import StaffProfile

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
