"""ผู้ช่วยเหลือระบบ (ADR 0007): the one person, the system's designer, who acts for every division.

Granted only on the host (`manage.py system_support`), never from the web; the database keeps at
most one active holder. The role comes with a real authority grant for every activity, unbounded,
so each decision still cites a grant like anyone else's; revoking the role (on the host or by an
admin on the web, RB06) revokes those grants too. The separation of duties does not bind this
person. Every การทำแทนกอง needs a reason, is labelled in the case timeline, listed in the report
and planned as staff e-mail to the owner division's supervisors.
"""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from operations import identity
from operations.models import AuthorityGrant, Decision, Role, RoleGrant

APPROVAL_REFERENCE = "ADR 0007 ผู้ช่วยเหลือระบบ (ให้ด้วยคำสั่งบนเครื่อง)"
HOST_ACTOR = "host:manage.py system_support"


def holder():
    grant = RoleGrant.objects.filter(role=Role.SYSTEM_SUPPORT, revoked_at__isnull=True).first()
    return grant.user if grant else None


def _support_grants(user):
    return AuthorityGrant.objects.filter(
        user=user, approval_reference=APPROVAL_REFERENCE, revoked_at__isnull=True
    )


@transaction.atomic
def grant(user, reason):
    """Make `user` the ผู้ช่วยเหลือระบบ (host only); refused while someone else holds it."""
    reason = identity._require_reason(reason)
    if not user.is_active:
        raise ValidationError("Only an active account can be ผู้ช่วยเหลือระบบ.")
    current = holder()
    if current is not None and current.pk != user.pk:
        raise ValidationError(f"{current.username} is ผู้ช่วยเหลือระบบ; revoke that first.")
    if current is None:
        RoleGrant.objects.create(user=user, role=Role.SYSTEM_SUPPORT, reason=reason)
    now = timezone.now()
    held = set(_support_grants(user).values_list("activity", flat=True))
    for activity in AuthorityGrant.Activity.values:
        if activity not in held:
            AuthorityGrant.objects.create(
                user=user,
                activity=activity,
                valid_from=now,
                reason=reason,
                approval_reference=APPROVAL_REFERENCE,
            )
    identity.audit(None, "role.system_support_granted", user, reason, actor_label=HOST_ACTOR)


def revoke_grants(user, actor, reason):
    """The role's own authority grants end with it (called from identity.revoke_role too)."""
    _support_grants(user).update(revoked_at=timezone.now(), revoked_by=actor, revoke_reason=reason)


@transaction.atomic
def revoke(user, reason):
    """Host-side revoke; an admin can also revoke the role on the web (identity.revoke_role)."""
    reason = identity._require_reason(reason)
    role = RoleGrant.objects.filter(
        user=user, role=Role.SYSTEM_SUPPORT, revoked_at__isnull=True
    ).first()
    if role is None:
        raise ValidationError(f"{user.username} is not ผู้ช่วยเหลือระบบ.")
    role.revoked_at = timezone.now()
    role.reason = reason
    role.save(update_fields=["revoked_at", "reason"])
    revoke_grants(user, None, reason)
    identity.audit(None, "role.system_support_revoked", user, reason, actor_label=HOST_ACTOR)


def acting_for(scope, case, division=None):
    """The label a decision carries when ผู้ช่วยเหลือระบบ made it; None for everyone else."""
    if scope is None or not scope.is_support:
        return None
    return {"division": (division or case.owner_division).code}


def actions(scope_divisions=None, days=90):
    """การทำแทนกอง of the last `days`, newest first, for the report (all, or these divisions)."""
    since = timezone.now() - timedelta(days=days)
    rows = (
        Decision.objects.filter(data__has_key="support", created_at__gte=since)
        .select_related("case__owner_division", "actor__staff_profile")
        .order_by("-created_at")
    )
    if scope_divisions is not None:
        rows = rows.filter(case__owner_division_id__in=list(scope_divisions))
    return rows
