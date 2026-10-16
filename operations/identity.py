"""M01 identity services: central permission checks and audited account/role changes.

Every check reads current database state, so role, division and suspension changes apply to the
next request of sessions that are already open. Callers never pass roles or districts as proof.
"""

import secrets
import uuid
from collections import defaultdict
from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from operations import orchestration
from operations.models import (
    DIVISION_ROLES,
    DUTY_ROLES,
    GLOBAL_ROLES,
    MEMBER_ROLES,
    AuditEvent,
    AuthorityGrant,
    ChannelIdentity,
    District,
    Division,
    Employee,
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
    # ADR 0007: acts for every division with every role; bound by no separation of duties.
    is_support: bool = False
    # 16 Oct 2569 Q5: divisions this person supervises (assigns work, sees every case); a
    # Supervisor ระดับฝ่าย supervises every active division without being a member of it.
    overseen: frozenset[int] = frozenset()
    is_department_supervisor: bool = False

    @property
    def has_any_access(self):
        return self.is_admin or self.is_support or bool(self.division_roles or self.overseen)


NO_SCOPE = StaffScope(user_id=None)


def work_is_split():
    """ตัวเลือก "หน้าที่ในกอง" (16 Oct 2569 Q3): False = every member does every step."""
    from operations import options  # local: options builds on models only

    return options.get(options.DIVISION_WORK) == "roles"


def _as_member(held, split):
    """A member does every step of the division unless its work is split; a Supervisor of the
    division is a member too."""
    if split or not held:
        return frozenset(held)
    return frozenset(held | MEMBER_ROLES)


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
    split = work_is_split() if roles else True
    department = Role.DEPARTMENT_SUPERVISOR in global_roles
    overseen = {d for d, r in roles.items() if Role.SUPERVISOR in r}
    if department:
        overseen |= set(Division.objects.filter(is_active=True).values_list("pk", flat=True))
    return StaffScope(
        user_id=user.pk,
        is_admin=is_admin,
        # Developer-admin is an extra duty of an admin, never a standalone path.
        is_developer_admin=is_admin and Role.DEVELOPER_ADMIN in global_roles,
        division_roles={division: _as_member(r, split) for division, r in roles.items()},
        is_support=Role.SYSTEM_SUPPORT in global_roles,
        overseen=frozenset(overseen),
        is_department_supervisor=department,
    )


def intake_division():
    """The central intake desk, or None: the real structure has none (ADR 0008)."""
    return Division.objects.filter(is_intake=True, is_active=True).first()


def holds(scope, division_id, roles=None):
    """Membership for acting: a role in the division (any of `roles`), or ผู้ช่วยเหลือระบบ.
    Admin rights alone never make someone a member."""
    if scope is None:
        return False
    if scope.is_support:
        return True
    if roles is not None and Role.SUPERVISOR in roles and division_id in scope.overseen:
        return True
    held = scope.division_roles.get(division_id, frozenset())
    return bool(held if roles is None else held & set(roles))


def separated(ctx_or_scope):
    """Whether the separation of duties (approver ≠ issuer, receiver ≠ inspector, …) binds
    this actor: ผู้ช่วยเหลือระบบ is exempt (ADR 0007), and the organisation may turn it off for
    everyone in ตัวเลือกระบบ (ADR 0009, e.g. one account per division)."""
    from operations import options  # local: options builds on models only

    scope = getattr(ctx_or_scope, "scope", ctx_or_scope)
    if scope is not None and getattr(scope, "is_support", False):
        return False
    return options.get(options.SEPARATION) == "on"


def can_access_division(scope, division_id, roles=None):
    """Own-division rule: admins see all divisions, staff only divisions they hold a role in."""
    if scope.is_admin or scope.is_support:
        return True
    if division_id in scope.overseen and (roles is None or Role.SUPERVISOR in roles):
        return True
    held = scope.division_roles.get(division_id, frozenset())
    return bool(held) if roles is None else bool(held & set(roles))


def filter_by_division(queryset, scope, field_name):
    """Restrict a queryset of division-owned rows to the scope; deny by default."""
    if scope.is_admin or scope.is_support:
        return queryset
    return queryset.filter(**{f"{field_name}__in": [*scope.division_roles, *scope.overseen]})


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


def may_change_ai(user):
    """ตัวเลือกระบบ ai_editors (15 ต.ค. 2569 Q23): developer admins, or every admin. Every admin
    may open the AI page to look."""
    from operations import options  # local: options builds on models only

    scope = staff_scope(user)
    if scope.is_developer_admin:
        return True
    return scope.is_admin and options.get(options.AI_EDITORS) == "admin"


def require_ai_editor(user):
    if not may_change_ai(user):
        raise PermissionDenied("AI settings permission required.")


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
    if role == Role.SYSTEM_SUPPORT:
        raise PermissionDenied("ผู้ช่วยเหลือระบบให้ได้ด้วยคำสั่งบนเครื่องเท่านั้น (ADR 0007).")
    if role in GLOBAL_ROLES and division is not None:
        raise ValidationError("Global roles cannot be scoped to a division.")
    if role in DIVISION_ROLES and (division is None or not division.is_active):
        raise ValidationError("Division roles need an active division.")
    if role in DUTY_ROLES and not work_is_split():
        raise ValidationError(
            "ตอนนี้สมาชิกกองทำได้ทุกขั้นตอน ให้บทบาทสมาชิกกองหรือ Supervisor แทน "
            "(ถ้าจะแยกหน้าที่ย่อย เปลี่ยนตัวเลือกระบบ หน้าที่ในกอง ก่อน)"
        )
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
    if division is not None:
        sync_membership_authority(user, actor)
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
    if grant.role == Role.SYSTEM_SUPPORT:
        from operations import support  # ADR 0007: its authority ends with the role (RB06)

        support.revoke_grants(grant.user, actor, reason)
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
    if not active:
        # ADR 0010: a suspended account loses its LINE link too.
        ChannelIdentity.objects.filter(staff_user=user).update(
            staff_user=None, staff_linked_at=None
        )
    audit(actor, "staff.activated" if active else "staff.suspended", user, reason)


# What a division holds over its own work, granted with ทำได้ทุกงานในกอง.
DIVISION_AUTHORITY = (
    AuthorityGrant.Activity.WITHDRAWAL_APPROVAL,
    AuthorityGrant.Activity.ALLOCATION,
    AuthorityGrant.Activity.CONDITION_CERTIFICATION,
    AuthorityGrant.Activity.PRIORITY_EXCEPTION,
)


def membership_authority(division):
    """What membership brings (16 Oct 2569 Q4): ฝอจ. decides routes for every division; any
    other division holds its own division's authority, with no category, item or quantity cap."""
    if division.kind == Division.Kind.COORDINATION:
        return [(AuthorityGrant.Activity.ROUTING_DECISION, None)]
    return [(activity, division) for activity in DIVISION_AUTHORITY]


def sync_membership_authority(user, actor=None):
    """Make the authority rows each of the user's memberships brings, once. They count only
    while the membership lasts and the division's work is not split (policy.active_grants), so
    nothing is revoked here. `actor` None = a migration or manage.py. Returns rows added."""
    divisions = Division.objects.filter(
        pk__in=RoleGrant.objects.filter(
            user=user, revoked_at__isnull=True, division__isnull=False
        ).values("division")
    ).order_by("pk")
    now = timezone.now()
    added = 0
    for division in divisions:
        for activity, scope in membership_authority(division):
            if AuthorityGrant.objects.filter(
                user=user,
                member_of=division,
                activity=activity,
                division=scope,
                revoked_at__isnull=True,
            ).exists():
                continue
            reason = f"มาพร้อมการเป็นสมาชิก {division.code}"
            grant = AuthorityGrant.objects.create(
                user=user,
                activity=activity,
                division=scope,
                member_of=division,
                valid_from=now,
                reason=reason,
                approval_reference="สมาชิกกอง",
                # An admin never grants authority to themselves (authority_not_self_granted).
                granted_by=actor if actor is not None and actor.pk != user.pk else None,
            )
            audit(
                actor,
                "authority.granted",
                grant,
                reason,
                user=user.pk,
                activity=activity,
                division=scope.code if scope else None,
                member_of=division.code,
            )
            added += 1
    return added


@transaction.atomic
def grant_full_division(actor, user, division, reason, approval_reference):
    """ทำได้ทุกงานในกอง (user, 10 Oct 2569: one account per division for now): every division
    role, and the authority a division holds over its own work (routing decisions for the
    coordinator), in one step. Each grant stays its own row, so a later split of duties revokes
    them one by one; nothing that already exists is granted twice. `actor` None = manage.py."""
    if actor is not None:
        require_admin(actor)
        if actor.pk == user.pk:
            raise ValidationError("ให้สิทธิ์แก่ตัวเองไม่ได้ (Q16)")
    reason = _require_reason(reason)
    approval_reference = _require_reason(approval_reference)
    division = Division.objects.select_for_update().get(pk=division.pk)
    user = User.objects.select_for_update().get(pk=user.pk)
    if not division.is_active or not user.is_active:
        raise ValidationError("ใช้ได้กับกองและบัญชีที่ใช้งานอยู่เท่านั้น")
    if not work_is_split():
        # 16 Oct 2569 Q3: membership already brings every step and the division's authority.
        if RoleGrant.objects.filter(user=user, division=division, revoked_at__isnull=True).exists():
            return 0, sync_membership_authority(user, actor)
        grant = RoleGrant.objects.create(
            user=user, role=Role.STAFF, division=division, granted_by=actor, reason=reason
        )
        audit(
            actor,
            "role.granted",
            grant,
            reason,
            user=user.pk,
            role=Role.STAFF,
            division=division.code,
        )
        return 1, sync_membership_authority(user, actor)
    roles = 0
    for role in sorted(DIVISION_ROLES):
        if RoleGrant.objects.filter(
            user=user, role=role, division=division, revoked_at__isnull=True
        ).exists():
            continue
        grant = RoleGrant.objects.create(
            user=user, role=role, division=division, granted_by=actor, reason=reason
        )
        audit(actor, "role.granted", grant, reason, user=user.pk, role=role, division=division.code)
        roles += 1
    coordinator = division.kind == Division.Kind.COORDINATION
    wanted = (
        [(AuthorityGrant.Activity.ROUTING_DECISION, None)]
        if coordinator
        else [(activity, division) for activity in DIVISION_AUTHORITY]
    )
    now = timezone.now()
    granted = 0
    for activity, scope_division in wanted:
        held = AuthorityGrant.objects.filter(
            user=user,
            activity=activity,
            division=scope_division,
            category="",
            item__isnull=True,
            revoked_at__isnull=True,
        ).exclude(valid_until__lte=now)
        if held.exists():
            continue
        grant = AuthorityGrant.objects.create(
            user=user,
            activity=activity,
            division=scope_division,
            valid_from=now,
            reason=reason,
            approval_reference=approval_reference,
            granted_by=actor,
        )
        audit(
            actor,
            "authority.granted",
            grant,
            reason,
            user=user.pk,
            activity=activity,
            division=scope_division.code if scope_division else None,
            reference=approval_reference,
        )
        granted += 1
    return roles, granted


@transaction.atomic
def set_employee_id(actor, user, employee_id, reason=""):
    """รหัสพนักงาน of a staff account, used to link LINE by an employee check (ADR 0010)."""
    require_admin(actor)
    employee_id = (employee_id or "").strip()[:32]
    if (
        employee_id
        and StaffProfile.objects.filter(employee_id=employee_id).exclude(user=user).exists()
    ):
        raise ValidationError("รหัสพนักงานนี้อยู่ในบัญชีอื่นแล้ว")
    profile = StaffProfile.objects.select_for_update().get(user=user)
    previous = profile.employee_id
    profile.employee_id = employee_id
    profile.save(update_fields=["employee_id"])
    audit(actor, "staff.employee_id_set", user, reason, previous=previous, value=employee_id)


@transaction.atomic
def set_section(actor, user, section, reason=""):
    """แผนก of a staff member (real structure Q13): one, in a division where they hold a role,
    or none. It changes no right; queues and reports use it."""
    require_admin(actor)
    profile = StaffProfile.objects.select_for_update().get(user=user)
    if section is not None:
        held = RoleGrant.objects.filter(
            user=user, division_id=section.division_id, revoked_at__isnull=True
        ).exists()
        if not held or not section.is_active:
            raise ValidationError("เลือกแผนกในกองที่บัญชีนี้มีบทบาทอยู่")
    if profile.section_id == (section.pk if section else None):
        raise ValidationError("บัญชีนี้อยู่แผนกนี้แล้ว")
    previous = profile.section
    profile.section = section
    profile.save(update_fields=["section"])
    audit(
        actor,
        "staff.section_changed",
        user,
        reason,
        section=section.name if section else "",
        previous=previous.name if previous else "",
    )


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


@transaction.atomic
def clear_intake_division(actor, reason):
    """ADR 0008: no central intake; new cases go straight to the division the rules name."""
    reason = _require_reason(reason)
    require_admin(actor)
    previous = list(Division.objects.select_for_update().filter(is_intake=True))
    if not previous:
        raise ValidationError("There is no intake division.")
    Division.objects.filter(pk__in=[d.pk for d in previous]).update(is_intake=False)
    for division in previous:
        audit(actor, "division.intake_cleared", division, reason)


# --- Requester registration (LINE identity from a signed event, wired in M03) -----------------


@transaction.atomic
def submit_registration(
    channel,
    external_id,
    submitted_name,
    district_code,
    employee=None,
    location=None,
    verified_by="",
):
    """Record a registration request. Choosing a district is a claim, never a permission; a
    matched employee and a shared location (10 Oct 2569 Q1, Q3) are evidence for the decider."""
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
    latitude, longitude = location or (None, None)
    return RegistrationRequest.objects.create(
        identity=identity,
        submitted_name=submitted_name,
        requested_district=district,
        employee=employee,
        latitude=latitude,
        longitude=longitude,
        verified_by=verified_by if employee is not None else "",
    )


SYSTEM_USERNAME = "system"


def system_user():
    """The account that records what the system decides by itself (e.g. an approval that
    matched the employee list); it cannot log in."""
    user, created = User.objects.get_or_create(
        username=SYSTEM_USERNAME, defaults={"is_active": False}
    )
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
        StaffProfile.objects.create(user=user, display_name="ระบบ (อนุมัติอัตโนมัติ)")
    return user


@transaction.atomic
def auto_approve_registration(registration):
    """การอนุมัติอัตโนมัติ (ADR 0010): only for a request whose employee check passed, while
    ตัวเลือกระบบ says so; recorded in the audit trail with the employee it matched."""
    from operations import options

    registration = RegistrationRequest.objects.select_for_update().get(pk=registration.pk)
    if options.get(options.REGISTRATION_CHECK) != "employee_auto" or registration.employee is None:
        raise ValidationError("Automatic approval is not enabled for this request.")
    how = registration.get_verified_by_display() if registration.verified_by else "ข้อมูลยืนยัน"
    return _approve(
        system_user(),
        registration,
        f"ตรงทะเบียนพนักงาน {registration.employee.employee_id} ยืนยันด้วย{how}",
        automatic=True,
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


def may_decide_registrations(user):
    """ตัวเลือกระบบ (ADR 0009): admins always; supervisors or any division staff when chosen."""
    from operations import options

    scope = staff_scope(user)
    if scope.is_admin or scope.is_support:
        return True
    choice = options.get(options.REGISTRATION)
    if choice == "staff":
        return bool(scope.division_roles)
    if choice == "supervisor":
        return bool(scope.overseen)
    return False


def _require_registration_decider(actor):
    if not may_decide_registrations(actor):
        raise PermissionDenied("ผู้ที่ได้รับเลือกในตัวเลือกระบบเท่านั้นที่อนุมัติการลงทะเบียนได้")


@transaction.atomic
def approve_registration(actor, registration, reason=""):
    _require_registration_decider(actor)
    return _approve(actor, registration, reason)


def _approve(actor, registration, reason="", automatic=False):
    from operations import employees  # local: employees audits through this module

    registration = RegistrationRequest.objects.select_for_update().get(pk=registration.pk)
    if registration.status != RegistrationRequest.Status.PENDING:
        raise ValidationError("Registration is not pending.")
    identity = ChannelIdentity.objects.select_for_update().get(pk=registration.identity_id)
    requester = Requester.objects.create(
        display_name=registration.submitted_name,
        district=registration.requested_district,
        approved_by=actor,
        employee=registration.employee,
        employee_link=Requester.EmployeeLink.VERIFIED if registration.employee else "",
    )
    if registration.employee is None:
        employees.match_requesters([requester])
        requester.refresh_from_db()
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
        "registration.auto_approved" if automatic else "registration.approved",
        registration,
        reason,
        actor_label="system" if automatic else "",
        district=registration.requested_district.code,
        requester_id=requester.pk,
        employee=registration.employee.employee_id if registration.employee else None,
    )
    return requester


UNKNOWN_DISTRICT = "D00"
ALIAS_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # no 0/O, 1/I/L  # pragma: allowlist secret


def new_alias():
    """รหัสแทนตัวผู้แจ้งไม่ระบุตัวตน: four random characters, unique, unrelated to the LINE ID."""
    while True:
        alias = "".join(secrets.choice(ALIAS_ALPHABET) for _ in range(4))
        if not Requester.objects.filter(alias=alias).exists():
            return alias


@transaction.atomic
def anonymous_requester(identity_row):
    """ผู้แจ้งไม่ระบุตัวตน (ADR 0010): one per LINE account, without a district or a name;
    it may only report problems and suggestions (services check the kind)."""
    requester = Requester.objects.filter(anonymous_identity=identity_row).first()
    if requester is not None:
        return requester
    district, _ = District.objects.get_or_create(
        code=UNKNOWN_DISTRICT, defaults={"name": "ไม่ระบุเขต", "is_active": False}
    )
    requester = Requester.objects.create(
        display_name="ไม่ระบุตัวตน",
        district=district,
        approved_by=system_user(),
        is_anonymous=True,
        anonymous_identity=identity_row,
        alias=new_alias(),
    )
    audit(None, "requester.anonymous_created", requester, actor_label="line")
    return requester


@transaction.atomic
def reject_registration(actor, registration, reason):
    reason = _require_reason(reason)
    _require_registration_decider(actor)
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


def _managed_requester(actor, requester, reason):
    """The requester row, locked, for an admin change with a reason. Anonymous reporters are not
    managed here (staff never see who they are) and a revoked requester is final."""
    reason = _require_reason(reason)
    require_admin(actor)
    requester = Requester.objects.select_for_update().get(pk=requester.pk)
    if requester.is_anonymous:
        raise ValidationError("ผู้แจ้งไม่ระบุตัวตนจัดการจากหน้านี้ไม่ได้")
    if requester.status == Requester.Status.REVOKED:
        raise ValidationError("ผู้แจ้งรายนี้ถอนการรับรองแล้ว ต้องลงทะเบียนใหม่")
    return requester, reason


@transaction.atomic
def set_requester_active(actor, requester, active, reason):
    """การระงับผู้แจ้ง: the person keeps their LINE link and history but can do nothing until
    an admin opens the account again; the bot tells them when they next write."""
    requester, reason = _managed_requester(actor, requester, reason)
    status = Requester.Status.ACTIVE if active else Requester.Status.SUSPENDED
    if requester.status == status:
        raise ValidationError("ผู้แจ้งรายนี้ใช้งานได้อยู่แล้ว" if active else "ผู้แจ้งรายนี้ถูกระงับอยู่แล้ว")
    requester.status = status
    requester.save(update_fields=["status"])
    audit(actor, "requester.activated" if active else "requester.suspended", requester, reason)
    return requester


@transaction.atomic
def revoke_requester(actor, requester, reason):
    """การถอนการรับรองผู้แจ้ง (user, 14 Oct 2569): final, and no data is deleted. Each LINE
    account of the requester is revoked too, so it has to register again and gets a new
    requester; cases, drafts and messages stay with this one. A staff link on the same LINE
    account is kept: each side keeps its own rights (ADR 0010). Nothing is pushed to the person;
    the bot asks them to register again when they next write."""
    requester, reason = _managed_requester(actor, requester, reason)
    requester.status = Requester.Status.REVOKED
    requester.save(update_fields=["status"])
    lines = ChannelIdentity.objects.filter(
        requester=requester, status=ChannelIdentity.Status.ACTIVE
    ).update(status=ChannelIdentity.Status.REVOKED)
    audit(actor, "requester.revoked", requester, reason, line_accounts=lines)
    return requester


@transaction.atomic
def link_requester_employee(actor, requester, employee_id, reason):
    """An admin ties a requester to ทะเบียนพนักงาน (for the position and staff roles) after
    checking who they are; it replaces a name match, which never overrides it. Confirming the
    employee a name already matched is recorded as such."""
    requester, reason = _managed_requester(actor, requester, reason)
    employee = Employee.objects.filter(
        employee_id=(employee_id or "").strip(), is_active=True
    ).first()
    if employee is None:
        raise ValidationError("ไม่พบรหัสพนักงานนี้ในทะเบียนพนักงานที่ใช้งานอยู่")
    link = Requester.EmployeeLink
    if requester.employee_id == employee.pk and requester.employee_link == link.ADMIN:
        raise ValidationError("ผูกกับพนักงานคนนี้อยู่แล้ว")
    confirmed = requester.employee_id == employee.pk and requester.employee_link == link.NAME
    requester.employee = employee
    requester.employee_link = link.ADMIN
    requester.save(update_fields=["employee", "employee_link"])
    action = "requester.employee_confirmed" if confirmed else "requester.employee_linked"
    audit(actor, action, requester, reason, employee=employee.employee_id)
    return requester


@transaction.atomic
def set_requester_district(actor, requester, district_code, reason):
    """An admin corrects the district a requester chose (for example when ทะเบียนพนักงาน
    puts them in another one). Cases keep the district they were filed with; new cases use
    this one. The name match is checked again for the new district."""
    from operations import employees  # local: employees audits through this module

    requester, reason = _managed_requester(actor, requester, reason)
    if requester.is_internal:
        raise ValidationError("ผู้แจ้งภายในแจ้งในนามกอง ไม่มีเขต")
    district = District.objects.filter(code=district_code, is_active=True).first()
    if district is None:
        raise ValidationError("ไม่รู้จักเขตนี้")
    if district.pk == requester.district_id:
        raise ValidationError("ผู้แจ้งอยู่เขตนี้อยู่แล้ว")
    previous = requester.district.code
    requester.district = district
    requester.save(update_fields=["district"])
    audit(
        actor,
        "requester.district_changed",
        requester,
        reason,
        previous=previous,
        district=district.code,
    )
    employees.match_requesters([requester])
    requester.refresh_from_db()
    return requester


@transaction.atomic
def unlink_requester_employee(actor, requester, reason):
    """An admin removes a wrong link; a later name match will not tie it again."""
    requester, reason = _managed_requester(actor, requester, reason)
    if requester.employee_id is None:
        raise ValidationError("ผู้แจ้งรายนี้ยังไม่ได้ผูกกับทะเบียนพนักงาน")
    previous = requester.employee.employee_id
    requester.employee = None
    requester.employee_link = Requester.EmployeeLink.UNLINKED
    requester.save(update_fields=["employee", "employee_link"])
    audit(actor, "requester.employee_unlinked", requester, reason, employee=previous)
    return requester


def _grant_label(grant):
    label = Role(grant.role).label
    return f"{label} {grant.division.code}" if grant.division_id else label


def requester_roles(requesters):
    """Staff roles of the people on the requester list (user, 14 Oct 2569: "user, user และ
    admin"), by requester pk. A person is a staff account when it is linked to their LINE
    (ADR 0010), or holds the same employee ID that they proved or an admin set; a name match
    alone never makes anyone staff. Only active grants of active accounts count."""
    requesters = list(requesters)
    users = defaultdict(set)
    linked = ChannelIdentity.objects.filter(
        requester__in=requesters, staff_user__isnull=False
    ).values_list("requester_id", "staff_user_id")
    for requester_id, user_id in linked:
        users[requester_id].add(user_id)
    proven = defaultdict(list)
    for requester in requesters:
        if requester.employee_id and requester.employee_link in {
            Requester.EmployeeLink.VERIFIED,
            Requester.EmployeeLink.ADMIN,
        }:
            proven[requester.employee.employee_id].append(requester.pk)
    profiles = StaffProfile.objects.filter(employee_id__in=list(proven)).values_list(
        "user_id", "employee_id"
    )
    for user_id, employee_id in profiles:
        for requester_id in proven[employee_id]:
            users[requester_id].add(user_id)
    grants = defaultdict(set)
    active = RoleGrant.objects.filter(
        user_id__in={u for ids in users.values() for u in ids},
        revoked_at__isnull=True,
        user__is_active=True,
    ).select_related("division")
    for grant in active:
        grants[grant.user_id].add(_grant_label(grant))
    return {
        requester.pk: sorted({label for u in users[requester.pk] for label in grants[u]})
        for requester in requesters
    }


@transaction.atomic
def create_requester(actor, display_name, district_code, reason):
    """Approve a requester who does not use LINE; staff file their cases on their behalf."""
    from operations import employees  # local: employees audits through this module

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
    employees.match_requesters([requester])
    requester.refresh_from_db()
    return requester
