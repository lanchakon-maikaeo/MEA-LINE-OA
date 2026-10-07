"""M01 identity services: central permission checks and audited account/role changes.

Every check reads current database state, so role, division and suspension changes apply to the
next request of sessions that are already open. Callers never pass roles or districts as proof.
"""

import secrets
import uuid
from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from operations import orchestration
from operations.models import (
    DIVISION_ROLES,
    GLOBAL_ROLES,
    AuditEvent,
    ChannelIdentity,
    District,
    Division,
    OutboxEvent,
    RegistrationRequest,
    Requester,
    Role,
    RoleGrant,
    StaffProfile,
)

User = get_user_model()


# --- Scope resolution -----------------------------------------------------------------------


@dataclass(frozen=True)
class StaffScope:
    user_id: int | None
    is_admin: bool = False
    is_developer_admin: bool = False
    division_roles: dict[int, frozenset[str]] = field(default_factory=dict)

    @property
    def has_any_access(self):
        return self.is_admin or bool(self.division_roles)


NO_SCOPE = StaffScope(user_id=None)


def staff_scope(user):
    """Current scope of a web user; inactive, anonymous or role-less users get no access."""
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return NO_SCOPE
    # Read activity from the database too, so a stale user object cannot keep access.
    grants = RoleGrant.objects.filter(
        user_id=user.pk, user__is_active=True, revoked_at__isnull=True
    ).filter(
        # A grant on a deactivated division no longer gives access to it.
        Q(division__isnull=True) | Q(division__is_active=True)
    )
    roles = {}
    global_roles = set()
    for role, division_id in grants.values_list("role", "division_id"):
        if division_id is None:
            global_roles.add(role)
        else:
            roles.setdefault(division_id, set()).add(role)
    is_admin = Role.ADMIN in global_roles
    return StaffScope(
        user_id=user.pk,
        is_admin=is_admin,
        # Developer-admin is an extra duty of an admin, never a standalone path.
        is_developer_admin=is_admin and Role.DEVELOPER_ADMIN in global_roles,
        division_roles={division: frozenset(r) for division, r in roles.items()},
    )


def can_access_division(scope, division_id, roles=None):
    """Own-division rule: admins see all divisions, staff only divisions they hold a role in."""
    if scope.is_admin:
        return True
    held = scope.division_roles.get(division_id, frozenset())
    return bool(held) if roles is None else bool(held & set(roles))


def filter_by_division(queryset, scope, field_name):
    """Restrict a queryset of division-owned rows to the scope; deny by default."""
    if scope.is_admin:
        return queryset
    return queryset.filter(**{f"{field_name}__in": list(scope.division_roles)})


def resolve_requester(channel, external_id):
    """Requester behind an approved, active channel identity, otherwise None."""
    return (
        Requester.objects.filter(
            channelidentity__channel=channel,
            channelidentity__external_id=external_id,
            channelidentity__status=ChannelIdentity.Status.ACTIVE,
            status=Requester.Status.ACTIVE,
        )
        .select_related("district")
        .first()
    )


def filter_by_requester(queryset, requester, field_name="requester"):
    """Own-requester rule: a requester sees only rows they own, even within the same district."""
    if requester is None:
        return queryset.none()
    return queryset.filter(**{field_name: requester})


def require_admin(user):
    if not staff_scope(user).is_admin:
        raise PermissionDenied("Admin permission required.")


def require_developer_admin(user):
    if not staff_scope(user).is_developer_admin:
        raise PermissionDenied("Developer-admin permission required.")


# --- Audit ------------------------------------------------------------------------------------


def audit(actor, action, target, reason="", actor_label="", **data):
    AuditEvent.objects.create(
        actor=actor,
        actor_label=actor_label,
        action=action,
        target_type=type(target).__name__,
        target_id=str(target.pk),
        reason=reason,
        data=data,
    )


def _require_reason(reason):
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("A reason is required.")
    return reason


# --- Staff accounts and roles -----------------------------------------------------------------


def _new_temporary_password():
    return secrets.token_urlsafe(12)


def _lock_active_admins():
    """Serialise changes that could remove the last admin or developer-admin."""
    return list(
        RoleGrant.objects.select_for_update()
        .filter(role__in=sorted(GLOBAL_ROLES), revoked_at__isnull=True)
        .values_list("user_id", "role", "user__is_active")
    )


def _require_admin_locked(actor, locked):
    """Re-check the actor after locking: a concurrent change may have removed their admin."""
    if (actor.pk, Role.ADMIN, True) not in locked:
        raise PermissionDenied("Admin permission required.")


def _require_account_admin(actor, user, locked):
    """A password reset or reactivation must not bypass the developer-role boundary."""
    _require_admin_locked(actor, locked)
    if any(u == user.pk and role == Role.DEVELOPER_ADMIN for u, role, _ in locked):
        if (actor.pk, Role.DEVELOPER_ADMIN, True) not in locked:
            raise PermissionDenied("Only a developer-admin can manage this account.")


def _ensure_admins_remain(locked, removing_user_id=None, removing_role=None):
    """Raise if the change would leave no active admin or developer-admin."""
    remaining = [
        (user_id, role)
        for user_id, role, active in locked
        if active
        and not (user_id == removing_user_id and (removing_role is None or role == removing_role))
    ]
    admins = {user_id for user_id, role in remaining if role == Role.ADMIN}
    developers = {u for u, role in remaining if role == Role.DEVELOPER_ADMIN and u in admins}
    if not admins:
        raise ValidationError("Cannot remove the last active admin.")
    had_developer = any(role == Role.DEVELOPER_ADMIN and active for _, role, active in locked)
    if had_developer and not developers:
        raise ValidationError("Cannot remove the last active developer-admin.")


@transaction.atomic
def create_staff_user(actor, username, display_name, email=""):
    """Create an individual web account with no role; returns (user, one-time password)."""
    require_admin(actor)
    username = username.strip()
    display_name = display_name.strip()
    if not username or not display_name:
        raise ValidationError("Username and display name are required.")
    if User.objects.filter(username__iexact=username).exists():
        raise ValidationError("Username already exists.")
    password = _new_temporary_password()
    user = User.objects.create_user(username=username, email=email.strip(), password=password)
    StaffProfile.objects.create(user=user, display_name=display_name, must_change_password=True)
    audit(actor, "staff.created", user, username=username)
    return user, password


@transaction.atomic
def grant_role(actor, user, role, division=None, reason=""):
    scope = staff_scope(actor)
    if not scope.is_admin:
        raise PermissionDenied("Admin permission required.")
    if role not in set(Role):
        raise ValidationError("Unknown role.")
    if role == Role.DEVELOPER_ADMIN and not scope.is_developer_admin:
        raise PermissionDenied("Only a developer-admin can grant developer-admin.")
    if role in GLOBAL_ROLES and division is not None:
        raise ValidationError("Global roles cannot be scoped to a division.")
    if role in DIVISION_ROLES and (division is None or not division.is_active):
        raise ValidationError("Division roles need an active division.")
    locked = _lock_active_admins()
    _require_account_admin(actor, user, locked)
    if role == Role.DEVELOPER_ADMIN and (actor.pk, Role.DEVELOPER_ADMIN, True) not in locked:
        raise PermissionDenied("Only a developer-admin can grant developer-admin.")
    user = User.objects.select_for_update().get(pk=user.pk)
    if not user.is_active:
        raise ValidationError("Cannot grant roles to a suspended account.")
    exists = RoleGrant.objects.filter(
        user=user, role=role, division=division, revoked_at__isnull=True
    ).exists()
    if exists:
        raise ValidationError("Role is already granted.")
    grant = RoleGrant.objects.create(
        user=user, role=role, division=division, granted_by=actor, reason=reason.strip()
    )
    audit(
        actor,
        "role.granted",
        user,
        reason,
        role=role,
        division=division.code if division else None,
    )
    return grant


@transaction.atomic
def revoke_role(actor, grant, reason):
    reason = _require_reason(reason)
    scope = staff_scope(actor)
    if not scope.is_admin:
        raise PermissionDenied("Admin permission required.")
    if grant.role == Role.DEVELOPER_ADMIN and not scope.is_developer_admin:
        raise PermissionDenied("Only a developer-admin can revoke developer-admin.")
    locked = _lock_active_admins()
    _require_admin_locked(actor, locked)
    grant = RoleGrant.objects.select_for_update().get(pk=grant.pk)
    _require_account_admin(actor, grant.user, locked)
    if grant.revoked_at is not None:
        raise ValidationError("Role is already revoked.")
    if grant.role in GLOBAL_ROLES:
        # Losing admin also ends developer-admin, so check both on admin removal.
        _ensure_admins_remain(
            locked, grant.user_id, None if grant.role == Role.ADMIN else grant.role
        )
    grant.revoked_at = timezone.now()
    grant.revoked_by = actor
    grant.reason = reason
    grant.save(update_fields=["revoked_at", "revoked_by", "reason"])
    audit(
        actor,
        "role.revoked",
        grant.user,
        reason,
        role=grant.role,
        division=grant.division.code if grant.division else None,
    )


@transaction.atomic
def change_division(actor, user, from_division, to_division, reason):
    """Move every active role a user holds in one division to another division."""
    reason = _require_reason(reason)
    require_admin(actor)
    if not to_division.is_active or from_division == to_division:
        raise ValidationError("Choose a different, active destination division.")
    grants = list(
        RoleGrant.objects.select_for_update().filter(
            user=user, division=from_division, revoked_at__isnull=True
        )
    )
    if not grants:
        raise ValidationError("User has no active role in the source division.")
    for grant in grants:
        revoke_role(actor, grant, reason)
        if not RoleGrant.objects.filter(
            user=user, role=grant.role, division=to_division, revoked_at__isnull=True
        ).exists():
            grant_role(actor, user, grant.role, to_division, reason)


def _profile(user):
    profile, _ = StaffProfile.objects.get_or_create(
        user=user, defaults={"display_name": user.get_full_name() or user.username}
    )
    return profile


def _end_sessions(user):
    profile = _profile(user)
    profile.sessions_valid_after = timezone.now()
    profile.save(update_fields=["sessions_valid_after"])


@transaction.atomic
def set_staff_active(actor, user, active, reason):
    reason = _require_reason(reason)
    require_admin(actor)
    locked = _lock_active_admins()
    _require_account_admin(actor, user, locked)
    user = User.objects.select_for_update().get(pk=user.pk)
    if user.is_active == active:
        raise ValidationError("Account already has this status.")
    if not active:
        _ensure_admins_remain(locked, user.pk)
    user.is_active = active
    user.save(update_fields=["is_active"])
    _end_sessions(user)
    audit(actor, "staff.activated" if active else "staff.suspended", user, reason)


@transaction.atomic
def revoke_sessions(actor, user, reason):
    reason = _require_reason(reason)
    require_admin(actor)
    _require_account_admin(actor, user, _lock_active_admins())
    _end_sessions(user)
    audit(actor, "staff.sessions_revoked", user, reason)


@transaction.atomic
def reset_password(actor, user, reason):
    """Admin-initiated reset: one-time password, forced change, and all sessions ended."""
    reason = _require_reason(reason)
    require_admin(actor)
    _require_account_admin(actor, user, _lock_active_admins())
    user = User.objects.select_for_update().get(pk=user.pk)
    password = _new_temporary_password()
    user.set_password(password)
    user.save(update_fields=["password"])
    profile = _profile(user)
    profile.must_change_password = True
    profile.sessions_valid_after = timezone.now()
    profile.save(update_fields=["must_change_password", "sessions_valid_after"])
    audit(actor, "staff.password_reset", user, reason)
    return password


def password_changed(user):
    profile = _profile(user)
    if profile.must_change_password:
        profile.must_change_password = False
        profile.save(update_fields=["must_change_password"])


@transaction.atomic
def bootstrap_admin(username, display_name, password, developer=True):
    """First admin only: refuses while any active admin exists (recovery is a DB-owner task)."""
    _lock_active_admins()
    if RoleGrant.objects.filter(
        role=Role.ADMIN, revoked_at__isnull=True, user__is_active=True
    ).exists():
        raise PermissionDenied("An active admin already exists; use the admin console.")
    user = User.objects.create_user(username=username, password=password)
    StaffProfile.objects.create(user=user, display_name=display_name)
    roles = [Role.ADMIN] + ([Role.DEVELOPER_ADMIN] if developer else [])
    for role in roles:
        RoleGrant.objects.create(user=user, role=role, reason="bootstrap")
    audit(None, "staff.bootstrap_admin", user, actor_label="system:bootstrap", roles=roles)
    return user


@transaction.atomic
def create_division(actor, code, name, kind=Division.Kind.DIVISION):
    require_admin(actor)
    code, name = code.strip().upper(), name.strip()
    if not code or not name:
        raise ValidationError("Division code and name are required.")
    if Division.objects.filter(code=code).exists():
        raise ValidationError("Division code already exists.")
    division = Division.objects.create(code=code, name=name, kind=kind)
    audit(actor, "division.created", division, code=code)
    return division


@transaction.atomic
def set_division_active(actor, division, active, reason):
    reason = _require_reason(reason)
    require_admin(actor)
    division = Division.objects.select_for_update().get(pk=division.pk)
    if not active and division.is_intake:
        raise ValidationError("Choose another intake division before deactivating this one.")
    division.is_active = active
    division.save(update_fields=["is_active"])
    audit(actor, "division.activated" if active else "division.deactivated", division, reason)


@transaction.atomic
def set_intake_division(actor, division, reason):
    """Make one active unit the central intake (กองรับเรื่องกลาง) for new cases."""
    reason = _require_reason(reason)
    require_admin(actor)
    previous = list(Division.objects.select_for_update().filter(is_intake=True))
    division = Division.objects.select_for_update().get(pk=division.pk)
    if not division.is_active:
        raise ValidationError("The intake division must be active.")
    if division.is_intake:
        raise ValidationError("This is already the intake division.")
    Division.objects.filter(pk__in=[d.pk for d in previous]).update(is_intake=False)
    division.is_intake = True
    division.save(update_fields=["is_intake"])
    audit(actor, "division.intake_set", division, reason, previous=[d.code for d in previous])


# --- Requester registration (LINE identity from a signed event, wired in M03) -----------------


@transaction.atomic
def submit_registration(channel, external_id, submitted_name, district_code):
    """Record a registration request. Choosing a district is a claim, never a permission."""
    submitted_name = " ".join((submitted_name or "").split())
    if not submitted_name or len(submitted_name) > 200:
        raise ValidationError("Name is required (max 200 characters).")
    district = District.objects.filter(code=district_code, is_active=True).first()
    if district is None:
        raise ValidationError("Unknown district.")
    identity, _ = ChannelIdentity.objects.select_for_update().get_or_create(
        channel=channel, external_id=external_id
    )
    if identity.status == ChannelIdentity.Status.ACTIVE:
        raise ValidationError("Identity is already approved.")
    pending = identity.registrations.filter(status=RegistrationRequest.Status.PENDING).first()
    if pending is not None:
        return pending
    if identity.status == ChannelIdentity.Status.REVOKED:
        # Relinking needs a fresh approval; the revoked requester is not reused.
        identity.status = ChannelIdentity.Status.PENDING
        identity.requester = None
        identity.save(update_fields=["status", "requester"])
    return RegistrationRequest.objects.create(
        identity=identity, submitted_name=submitted_name, requested_district=district
    )


def registration_status(channel, external_id):
    """Latest registration of this identity only; other identities are never visible."""
    return (
        RegistrationRequest.objects.filter(
            identity__channel=channel, identity__external_id=external_id
        )
        .order_by("-created_at", "-pk")
        .first()
    )


def _registration_event(registration):
    """Dispatch intent for the registration-result notification (planned by the worker, M04)."""
    OutboxEvent.objects.create(
        event_type=orchestration.EVENT_REGISTRATION,
        subject_type="registration",
        subject_id=str(registration.pk),
        correlation_id=uuid.uuid4(),
        payload={"status": registration.status, "identity_id": registration.identity_id},
        occurred_at=timezone.now(),
    )


@transaction.atomic
def cancel_registration(channel, external_id):
    """The requester withdraws their own pending request (from the signed chat identity)."""
    identity = ChannelIdentity.objects.select_for_update().get(
        channel=channel, external_id=external_id
    )
    pending = identity.registrations.select_for_update().filter(
        status=RegistrationRequest.Status.PENDING
    )
    registration = pending.first()
    if registration is None:
        raise ValidationError("No pending registration.")
    registration.status = RegistrationRequest.Status.CANCELLED
    registration.decided_at = timezone.now()
    registration.save(update_fields=["status", "decided_at"])
    audit(None, "registration.cancelled", registration, actor_label=f"identity:{identity.pk}")
    return registration


@transaction.atomic
def approve_registration(actor, registration, reason=""):
    require_admin(actor)
    registration = RegistrationRequest.objects.select_for_update().get(pk=registration.pk)
    if registration.status != RegistrationRequest.Status.PENDING:
        raise ValidationError("Registration is not pending.")
    identity = ChannelIdentity.objects.select_for_update().get(pk=registration.identity_id)
    requester = Requester.objects.create(
        display_name=registration.submitted_name,
        district=registration.requested_district,
        approved_by=actor,
    )
    identity.requester = requester
    identity.status = ChannelIdentity.Status.ACTIVE
    identity.save(update_fields=["requester", "status"])
    registration.status = RegistrationRequest.Status.APPROVED
    registration.decided_by = actor
    registration.decided_at = timezone.now()
    registration.decision_reason = reason.strip()
    registration.save(update_fields=["status", "decided_by", "decided_at", "decision_reason"])
    _registration_event(registration)
    audit(
        actor,
        "registration.approved",
        registration,
        reason,
        district=registration.requested_district.code,
        requester_id=requester.pk,
    )
    return requester


@transaction.atomic
def reject_registration(actor, registration, reason):
    reason = _require_reason(reason)
    require_admin(actor)
    registration = RegistrationRequest.objects.select_for_update().get(pk=registration.pk)
    if registration.status != RegistrationRequest.Status.PENDING:
        raise ValidationError("Registration is not pending.")
    registration.status = RegistrationRequest.Status.REJECTED
    registration.decided_by = actor
    registration.decided_at = timezone.now()
    registration.decision_reason = reason
    registration.save(update_fields=["status", "decided_by", "decided_at", "decision_reason"])
    _registration_event(registration)
    audit(actor, "registration.rejected", registration, reason)


@transaction.atomic
def revoke_identity(actor, identity, reason):
    reason = _require_reason(reason)
    require_admin(actor)
    identity = ChannelIdentity.objects.select_for_update().get(pk=identity.pk)
    if identity.status == ChannelIdentity.Status.REVOKED:
        raise ValidationError("Identity is already revoked.")
    identity.status = ChannelIdentity.Status.REVOKED
    identity.save(update_fields=["status"])
    audit(actor, "identity.revoked", identity, reason)


@transaction.atomic
def set_requester_active(actor, requester, active, reason):
    reason = _require_reason(reason)
    require_admin(actor)
    requester.status = Requester.Status.ACTIVE if active else Requester.Status.SUSPENDED
    requester.save(update_fields=["status"])
    audit(actor, "requester.activated" if active else "requester.suspended", requester, reason)


@transaction.atomic
def create_requester(actor, display_name, district_code, reason):
    """Approve a requester who does not use LINE; staff file their cases on their behalf."""
    reason = _require_reason(reason)
    require_admin(actor)
    display_name = " ".join((display_name or "").split())
    if not display_name or len(display_name) > 200:
        raise ValidationError("Name is required (max 200 characters).")
    district = District.objects.filter(code=district_code, is_active=True).first()
    if district is None:
        raise ValidationError("Unknown district.")
    requester = Requester.objects.create(
        display_name=display_name, district=district, approved_by=actor
    )
    audit(actor, "requester.created_direct", requester, reason, district=district.code)
    return requester
