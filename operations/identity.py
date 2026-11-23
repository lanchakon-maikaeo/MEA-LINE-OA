"""M01 identity services: central permission checks and audited account/role changes.

Every check reads current database state, so role, division and suspension changes apply to the
next request of sessions that are already open. Callers never pass roles or districts as proof.
"""

import secrets
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, replace

from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from operations import orchestration
from operations.models import (
    DIVISION_ROLES,
    DUTY_ROLES,
    GLOBAL_ROLES,
    MEMBER_ROLES,
    UNIT_ROLES,
    VIEWER_ROLES,
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
    division_roles: dict[int, frozenset[str]] = field(default_factory=dict)
    # ADR 0007: acts for every division with every role; bound by no separation of duties.
    is_support: bool = False
    # 16 Oct 2569 Q5: divisions this person supervises (assigns work, sees every case); a
    # แอดมินฝ่าย supervises every active division without being a member of it.
    overseen: frozenset[int] = frozenset()
    is_department_supervisor: bool = False
    # ADR 0039: divisions this person only reads (ผู้ดูข้อมูลของกอง, or every active division
    # for ผู้ดูข้อมูลระดับฝ่าย). They never count as access for acting.
    viewed: frozenset[int] = frozenset()
    is_department_viewer: bool = False
    # ADR 0052: the แผนก and กลุ่มงาน this person is admin of, and their กอง. They give no
    # membership of the กอง: for_case adds it one case at a time.
    sections: frozenset[int] = frozenset()
    work_groups: frozenset[int] = frozenset()
    unit_divisions: frozenset[int] = frozenset()

    @property
    def has_any_access(self):
        """May act somewhere; every command and LINE staff path asks this."""
        return (
            self.is_admin
            or self.is_support
            or bool(self.division_roles or self.overseen or self.unit_divisions)
        )

    @property
    def unit_only(self):
        """ADR 0052: sees only the cases of a แผนก or กลุ่มงาน, nothing of a whole กอง."""
        # Not division_roles: for_case fills them for one case, and the answer must stay.
        return bool(self.unit_divisions) and not (
            self.is_admin or self.is_support or self.overseen or self.viewed
        )

    @property
    def may_read(self):
        """May open the staff pages: acting access, or a viewer's read-only access."""
        return self.has_any_access or bool(self.viewed)

    @property
    def read_only(self):
        return not self.has_any_access and bool(self.viewed)

    @property
    def sees_department(self):
        """ADR 0039: ทุกกอง and ปัญหาและข้อเสนอแนะ, as the admins see them."""
        return (
            self.is_admin
            or self.is_support
            or self.is_department_supervisor
            or self.is_department_viewer
        )


NO_SCOPE = StaffScope(user_id=None)


def work_is_split():
    """ตัวเลือก "หน้าที่ในกอง" (16 Oct 2569 Q3): False = every member does every step. ADR 0041
    took it off the settings page (one role per person leaves no duties to split); only the
    SIM test world still splits."""
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
        # A grant on a deactivated division (or แผนก, กลุ่มงาน) no longer gives access to it.
        Q(division__isnull=True) | Q(division__is_active=True),
        Q(section__isnull=True) | Q(section__is_active=True),
        Q(work_group__isnull=True) | Q(work_group__is_active=True),
    )
    roles = {}
    global_roles = set()
    viewed = set()
    sections, groups, unit_divisions = set(), set(), set()
    for role, division_id, section_id, group_id in grants.values_list(
        "role", "division_id", "section_id", "work_group_id"
    ):
        if role == Role.DIVISION_VIEWER:
            viewed.add(division_id)
        elif role in UNIT_ROLES:
            if section_id:
                sections.add(section_id)
            else:
                groups.add(group_id)
            unit_divisions.add(division_id)
        elif division_id is None:
            global_roles.add(role)
        else:
            roles.setdefault(division_id, set()).add(role)
    is_admin = Role.ADMIN in global_roles
    split = work_is_split() if roles else True
    # ADR 0041: แอดมินระบบ does everything a แอดมินฝ่าย does, on top of the settings.
    department = Role.DEPARTMENT_SUPERVISOR in global_roles or is_admin
    overseen = {d for d, r in roles.items() if Role.SUPERVISOR in r}
    viewer = Role.DEPARTMENT_VIEWER in global_roles
    if department or viewer:
        active = set(Division.objects.filter(is_active=True).values_list("pk", flat=True))
        overseen |= active if department else set()
        viewed |= active if viewer else set()
    return StaffScope(
        user_id=user.pk,
        is_admin=is_admin,
        division_roles={division: _as_member(r, split) for division, r in roles.items()},
        is_support=Role.SYSTEM_SUPPORT in global_roles,
        overseen=frozenset(overseen),
        is_department_supervisor=department,
        viewed=frozenset(viewed - overseen - set(roles)),
        is_department_viewer=viewer,
        sections=frozenset(sections),
        work_groups=frozenset(groups),
        unit_divisions=frozenset(unit_divisions),
    )


def in_unit(scope, case):
    """ADR 0052 Q5: the case's แผนกที่ดูแล is a แผนก of this admin, or its กลุ่มงานที่ดูแล is
    one of their กลุ่มงาน."""
    return case.section_id in scope.sections or case.work_group_id in scope.work_groups


def for_case(scope, case):
    """ADR 0052: a แอดมินแผนก/กลุ่มงาน acts in a case of their unit as its กอง's แอดมินกอง (Q2),
    in a case they are responsible for as a member (Q16), and on a handoff or งานย่อย waiting
    for their unit as a member of the receiving กอง (Q17). Every other scope comes back as is."""
    if not scope.unit_divisions:
        return scope
    from operations.models import ChildWork, Handoff  # local: models import nothing of ours

    roles = dict(scope.division_roles)

    def add(division_id, held):
        roles[division_id] = frozenset(roles.get(division_id, frozenset()) | held)

    if in_unit(scope, case):
        add(case.owner_division_id, MEMBER_ROLES | {Role.SUPERVISOR})
    elif case.assignee_id is not None and case.assignee_id == scope.user_id:
        add(case.owner_division_id, MEMBER_ROLES)
    for division_id in ChildWork.objects.filter(
        Q(section_id__in=scope.sections) | Q(work_group_id__in=scope.work_groups),
        case_id=case.pk,
        status__in=ChildWork.OPEN,
    ).values_list("division_id", flat=True):
        add(division_id, MEMBER_ROLES)
    for division_id in Handoff.objects.filter(
        Q(to_section_id__in=scope.sections) | Q(to_work_group_id__in=scope.work_groups),
        case_id=case.pk,
        status=Handoff.Status.PENDING,
    ).values_list("to_division_id", flat=True):
        add(division_id, MEMBER_ROLES)
    return replace(scope, division_roles=roles)


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


def may_read_division(scope, division_id):
    """ADR 0039: what may be opened to read; a viewer's divisions count here only."""
    return can_access_division(scope, division_id) or division_id in scope.viewed


def may_open_division(scope, division_id):
    """The กอง's pages (queue, งานคลัง, ปฏิทินนัด) open; ADR 0052: a แอดมินแผนก/กลุ่มงาน opens
    their กอง's, and the pages show only their unit's cases."""
    return may_read_division(scope, division_id) or division_id in scope.unit_divisions


def filter_by_division(queryset, scope, field_name):
    """Restrict a queryset of division-owned rows to what the scope reads; deny by default."""
    if scope.is_admin or scope.is_support:
        return queryset
    divisions = [*scope.division_roles, *scope.overseen, *scope.viewed]
    return queryset.filter(**{f"{field_name}__in": divisions})


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


def may_change_ai(user):
    """ADR 0041: แอดมินผู้พัฒนา is gone; every แอดมินระบบ changes the AI settings."""
    return staff_scope(user).is_admin


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
    return (reason or "").strip()


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
    _require_admin_locked(actor, locked)


def _ensure_admins_remain(locked, removing_user_id=None, removing_role=None):
    """Raise if the change would leave no active admin."""
    remaining = [
        (user_id, role)
        for user_id, role, active in locked
        if active
        and not (user_id == removing_user_id and (removing_role is None or role == removing_role))
    ]
    if not {user_id for user_id, role in remaining if role == Role.ADMIN}:
        raise ValidationError("Cannot remove the last active admin.")


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
    from operations import roster_match  # local: roster_match builds on identity

    roster_match.link(user, actor)  # ADR 0050 Q1: the สังกัด comes from the roster by itself
    return user, password


@transaction.atomic
def grant_role(actor, user, role, division=None, reason="", section=None, work_group=None):
    """ADR 0041: one role per person. The new role replaces every other one the account holds
    (ผู้ช่วยเหลือระบบ stays: it is no role an admin hands out). ADR 0052: a แอดมินแผนก names
    its `section`, a แอดมินกลุ่มงาน its `work_group`; their กอง is the division."""
    if role == Role.SECTION_ADMIN:
        work_group = None
        if section is None or not section.is_active:
            raise ValidationError("แอดมินแผนกต้องมีแผนกที่ใช้งานอยู่")
        division = section.division
    elif role == Role.WORK_GROUP_ADMIN:
        section = None
        if work_group is None or not work_group.is_active:
            raise ValidationError("แอดมินกลุ่มงานต้องมีกลุ่มงานที่ใช้งานอยู่")
        division = work_group.section.division
    else:
        section = work_group = None
    scope = staff_scope(actor)
    if not scope.is_admin:
        raise PermissionDenied("Admin permission required.")
    if role not in set(Role):
        raise ValidationError("Unknown role.")
    if role == Role.DEVELOPER_ADMIN:
        raise ValidationError("ยกเลิกบทบาทแอดมินผู้พัฒนาแล้ว แอดมินระบบมีสิทธิ์ครบทุกส่วน")
    if role == Role.SYSTEM_SUPPORT:
        raise PermissionDenied("ผู้ช่วยเหลือระบบให้ได้ด้วยคำสั่งบนเครื่องเท่านั้น (ADR 0007).")
    if role in GLOBAL_ROLES and division is not None:
        raise ValidationError("Global roles cannot be scoped to a division.")
    if role in DIVISION_ROLES and (division is None or not division.is_active):
        raise ValidationError("Division roles need an active division.")
    if role in DUTY_ROLES and not work_is_split():
        raise ValidationError("สมาชิกกองทำได้ทุกขั้นตอน ให้บทบาทสมาชิกกองหรือแอดมินกองแทน")
    locked = _lock_active_admins()
    _require_account_admin(actor, user, locked)
    user = User.objects.select_for_update().get(pk=user.pk)
    if not user.is_active:
        raise ValidationError("Cannot grant roles to a suspended account.")
    exists = RoleGrant.objects.filter(
        user=user,
        role=role,
        division=division,
        section=section,
        work_group=work_group,
        revoked_at__isnull=True,
    ).exists()
    if exists:
        raise ValidationError("Role is already granted.")
    others = list(
        RoleGrant.objects.filter(user=user, revoked_at__isnull=True).exclude(
            role=Role.SYSTEM_SUPPORT
        )
    )
    grant = RoleGrant.objects.create(
        user=user,
        role=role,
        division=division,
        section=section,
        work_group=work_group,
        granted_by=actor,
        reason=reason.strip(),
    )
    audit(
        actor,
        "role.granted",
        user,
        reason,
        role=role,
        division=division.code if division else None,
        **({"unit": (section or work_group).name} if section or work_group else {}),
    )
    # After the new grant, so a move within one กอง hands no case on (ADR 0031 Q24).
    for other in others:
        revoke_role(actor, other, reason or f"เปลี่ยนเป็น{Role(role).label}")
    if division is not None:
        sync_membership_authority(user, actor)
    _menu_follows(user)
    return grant


EMPLOYEE = "employee"  # เจ้าหน้าที่ (ADR 0041): a pick that means no staff role
# The one role an admin picks for a person, in the order the pickers show them.
PICKS = (
    EMPLOYEE,
    Role.STAFF,
    Role.WORK_GROUP_ADMIN,
    Role.SECTION_ADMIN,
    Role.SUPERVISOR,
    Role.DIVISION_VIEWER,
    Role.DEPARTMENT_SUPERVISOR,
    Role.DEPARTMENT_VIEWER,
    Role.ADMIN,
)


def role_label(role):
    return "เจ้าหน้าที่" if role == EMPLOYEE else Role(role).label


@transaction.atomic
def set_role(actor, user, role, division=None, reason="", section=None, work_group=None):
    """The one role of an account (ADR 0041). A กอง role without a กอง takes the account's
    สังกัด, and so does a แอดมินแผนก/กลุ่มงาน without its unit (ADR 0052 Q7); asking for the
    role it already holds changes nothing."""
    require_admin(actor)
    if role not in PICKS:
        raise ValidationError("โปรดเลือกบทบาทจากรายการ")
    held = RoleGrant.objects.filter(user=user, revoked_at__isnull=True).exclude(
        role=Role.SYSTEM_SUPPORT
    )
    if role == EMPLOYEE:
        if held.exists():
            clear_roles(actor, user, reason)
        return None
    profile = StaffProfile.objects.filter(user=user).select_related("work_group__section").first()
    if role == Role.SECTION_ADMIN and section is None and work_group is not None:
        section = work_group.section  # a กลุ่มงาน picked for a แอดมินแผนก names its แผนก
    if role == Role.SECTION_ADMIN and section is None:
        section = profile and (
            profile.work_group.section if profile.work_group else profile.section
        )
        if section is None:
            raise ValidationError("บุคคลนี้ไม่มีแผนกในสังกัด โปรดเลือกแผนก")
    if role == Role.WORK_GROUP_ADMIN and work_group is None:
        work_group = profile.work_group if profile else None
        if work_group is None:
            raise ValidationError("บุคคลนี้ไม่มีกลุ่มงานในสังกัด โปรดเลือกกลุ่มงาน")
    if role in UNIT_ROLES:
        division = (section or work_group.section).division
        section, work_group = (section, None) if role == Role.SECTION_ADMIN else (None, work_group)
    else:
        section = work_group = None
    if role in DIVISION_ROLES and division is None:
        division = profile.division if profile else None
        if division is None:
            raise ValidationError("บุคคลนี้ไม่มีกองในสังกัด โปรดเลือกกอง")
    if role in GLOBAL_ROLES:
        division = None
    same = held.filter(role=role, division=division, section=section, work_group=work_group)
    if not same.exists():
        return grant_role(actor, user, role, division, reason, section, work_group)
    for other in held.exclude(pk__in=same):
        revoke_role(actor, other, reason or f"เปลี่ยนเป็น{Role(role).label}")
    return None


@transaction.atomic
def clear_roles(actor, user, reason=""):
    """เจ้าหน้าที่ (ADR 0041): no staff role; a verified employee still withdraws, returns and
    reports from LINE as registered."""
    require_admin(actor)
    grants = list(
        RoleGrant.objects.filter(user=user, revoked_at__isnull=True).exclude(
            role=Role.SYSTEM_SUPPORT
        )
    )
    if not grants:
        raise ValidationError("บัญชีนี้เป็นเจ้าหน้าที่อยู่แล้ว")
    for grant in grants:
        revoke_role(actor, grant, reason or "เปลี่ยนเป็นเจ้าหน้าที่")


def _menu_follows(user):
    """Refresh LINE access after any role grant or removal, including the last membership."""
    from operations import line_menu

    line_menu.relink_user(user)


@transaction.atomic
def revoke_role(actor, grant, reason):
    reason = _require_reason(reason)
    scope = staff_scope(actor)
    if not scope.is_admin:
        raise PermissionDenied("Admin permission required.")
    locked = _lock_active_admins()
    _require_admin_locked(actor, locked)
    grant = RoleGrant.objects.select_for_update().get(pk=grant.pk)
    _require_account_admin(actor, grant.user, locked)
    if grant.revoked_at is not None:
        raise ValidationError("Role is already revoked.")
    if grant.role == Role.ADMIN:
        _ensure_admins_remain(locked, grant.user_id, Role.ADMIN)
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
    _menu_follows(grant.user)
    if (
        grant.division_id
        and not RoleGrant.objects.filter(
            user=grant.user, division_id=grant.division_id, revoked_at__isnull=True
        ).exists()
    ):
        from operations import units  # local: units builds on identity

        # ADR 0031 Q24: the cases the system gave this person move to the next one.
        units.release(grant.user, f"{_name(grant.user)} ไม่มีสิทธิ์ใน {grant.division.code} แล้ว")


def _name(user):
    profile = getattr(user, "staff_profile", None)
    return profile.display_name if profile else user.username


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
        if grant.role in UNIT_ROLES:
            continue  # ADR 0052: its แผนก/กลุ่มงาน stays in the old กอง, so the role ends
        if not RoleGrant.objects.filter(
            user=user, role=grant.role, division=to_division, revoked_at__isnull=True
        ).exists():
            grant_role(actor, user, grant.role, to_division, reason)
    # ADR 0031: the สังกัด moves with the roles, to the new กอง itself.
    StaffProfile.objects.filter(user=user, division=from_division).update(
        division=to_division, section=None, work_group=None
    )


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
        from operations import units  # local: units builds on identity

        units.release(user, f"บัญชีของ {_name(user)} ถูกระงับ")  # ADR 0031 Q24
    if not active:
        # ADR 0010: a suspended account loses its LINE link too, and the staff menu with it.
        from operations import line_menu  # local: line_menu builds on identity

        rows = list(ChannelIdentity.objects.filter(staff_user=user))
        ChannelIdentity.objects.filter(staff_user=user).update(
            staff_user=None, staff_linked_at=None
        )
        for row in rows:
            row.staff_user = None
            transaction.on_commit(lambda row=row: line_menu.relink(row))
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
        pk__in=RoleGrant.objects.filter(user=user, revoked_at__isnull=True, division__isnull=False)
        .exclude(role__in=VIEWER_ROLES)  # ADR 0039: a viewer holds no authority
        .values("division")
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
    if not approval_reference:
        raise ValidationError("ระบุเอกสารอ้างอิงการอนุมัติ")
    division = Division.objects.select_for_update().get(pk=division.pk)
    user = User.objects.select_for_update().get(pk=user.pk)
    if not division.is_active or not user.is_active:
        raise ValidationError("ใช้ได้กับกองและบัญชีที่ใช้งานอยู่เท่านั้น")
    if not work_is_split():
        # 16 Oct 2569 Q3: membership already brings every step and the division's authority.
        if (
            RoleGrant.objects.filter(user=user, division=division, revoked_at__isnull=True)
            .exclude(role__in=VIEWER_ROLES)
            .exists()
        ):
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
    for role in sorted(DIVISION_ROLES - VIEWER_ROLES - UNIT_ROLES):
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


LINE_ACCOUNT = "line-employee-"  # the prefix of an account provision_employee_staff made


@transaction.atomic
def set_employee_id(actor, user, employee_id, reason=""):
    """รหัสพนักงาน of a staff account, used to link LINE by an employee check (ADR 0010). An ID
    held by an account the system made from LINE merges into this one (ADR 0044)."""
    require_admin(actor)
    employee_id = (employee_id or "").strip()[:32]
    others = StaffProfile.objects.filter(employee_id=employee_id).exclude(user=user)
    if employee_id and others.exclude(user__username__startswith=LINE_ACCOUNT).exists():
        raise ValidationError("รหัสพนักงานนี้อยู่ในบัญชีอื่นแล้ว")
    if employee_id and others.exists():
        employee = Employee.objects.filter(employee_id=employee_id).first()
        if employee is None:
            raise ValidationError("ไม่พบรหัสพนักงานนี้ในทะเบียน")
        merge_accounts(actor, employee, user, reason)
        return
    profile = StaffProfile.objects.select_for_update().get(user=user)
    previous = profile.employee_id
    profile.employee_id = employee_id
    profile.save(update_fields=["employee_id"])
    audit(actor, "staff.employee_id_set", user, reason, previous=previous, value=employee_id)
    employee = Employee.objects.filter(employee_id=employee_id).first() if employee_id else None
    if employee is not None:
        from operations import staff_line, units  # local: both build on identity

        units.bind_from_roster(user, employee)  # ADR 0050 Q1
        staff_line.tie_account(user, actor)  # ADR 0053 Q6


@transaction.atomic
def merge_accounts(actor, employee, user, reason=""):
    """One person, one account (ADR 0044): a web account such as user-4 takes the employee's ID,
    and the account the system made when they came through LINE hands over its LINE link, role,
    ผู้รับผิดชอบประจำงาน, alert lists and open cases, then closes. Its history stays with it."""
    from operations import units  # local: units builds on identity
    from operations.koj_models import KojRecipient
    from operations.models import Case, UnitRecipient, UnitWorkOwner, WorkOrder

    require_admin(actor)
    reason = reason or f"รวมบัญชีของพนักงาน {employee.employee_id}"
    target = StaffProfile.objects.select_for_update().get(user=user)
    if target.employee_id and target.employee_id != employee.employee_id:
        raise ValidationError("บัญชีนี้ผูกกับพนักงานคนอื่นแล้ว")
    if user.username.startswith(LINE_ACCOUNT) or not user.is_active:
        raise ValidationError("เลือกบัญชีเว็บที่ใช้งานอยู่")
    olds = [
        p.user
        for p in StaffProfile.objects.select_for_update()
        .filter(employee_id=employee.employee_id)
        .exclude(user=user)
        .select_related("user")
    ]
    if any(not old.username.startswith(LINE_ACCOUNT) for old in olds):
        raise ValidationError("รหัสพนักงานนี้อยู่ในบัญชีเว็บอื่นแล้ว")
    for old in olds:
        StaffProfile.objects.filter(user=old).update(employee_id="")
        if not ChannelIdentity.objects.filter(staff_user=user).exists():
            ChannelIdentity.objects.filter(staff_user=old).update(staff_user=user)
        held = (
            RoleGrant.objects.filter(user=old, revoked_at__isnull=True)
            .exclude(role=Role.SYSTEM_SUPPORT)
            .first()
        )
        has_role = RoleGrant.objects.filter(user=user, revoked_at__isnull=True).exists()
        if held is not None and not has_role:
            set_role(actor, user, held.role, held.division, reason, held.section, held.work_group)
        for model in (UnitWorkOwner, UnitRecipient, KojRecipient):
            for row in model.objects.filter(user=old):
                keys = {f: getattr(row, f) for f in ("work_id", "group") if hasattr(row, f)}
                if keys and model.objects.filter(user=user, **keys).exists():
                    row.delete()
                else:
                    model.objects.filter(pk=row.pk).update(user=user)
        Case.objects.filter(assignee=old, ended_at__isnull=True).update(assignee=user)
        Case.objects.filter(accepted_by=old, ended_at__isnull=True).update(accepted_by=user)
        WorkOrder.objects.filter(assignee=old, ended_at__isnull=True).update(assignee=user)
        if old.is_active:
            set_staff_active(actor, old, False, reason)
    target.employee_id = employee.employee_id
    target.save(update_fields=["employee_id"])
    units.bind_from_roster(user, employee)
    from operations import staff_line  # local: staff_line builds on identity

    staff_line.tie_account(user, actor)  # ADR 0053 Q6: when no old account brought a LINE
    audit(
        actor,
        "staff.accounts_merged",
        user,
        reason,
        employee=employee.employee_id,
        closed=[old.username for old in olds],
    )


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
    profile.work_group = None  # ADR 0031: a กลุ่มงาน belongs to one แผนก
    if section is not None:
        profile.division = section.division
    profile.save(update_fields=["section", "work_group", "division"])
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
    password = _new_temporary_password()
    _set_password(actor, user, password, reason, "staff.password_reset")
    return password


@transaction.atomic
def set_password(actor, user, password, reason):
    """ADR 0049 Q13: the admin types the password; it works once, then the person sets their own."""
    require_admin(actor)  # before the password rules, so only an admin learns what they say
    password_validation.validate_password(password, user)
    _set_password(actor, user, password, reason, "staff.password_set")


def _set_password(actor, user, password, reason, action):
    reason = _require_reason(reason)
    require_admin(actor)
    _require_account_admin(actor, user, _lock_active_admins())
    user = User.objects.select_for_update().get(pk=user.pk)
    user.set_password(password)
    user.save(update_fields=["password"])
    profile = _profile(user)
    profile.must_change_password = True
    profile.sessions_valid_after = timezone.now()
    profile.save(update_fields=["must_change_password", "sessions_valid_after"])
    audit(actor, action, user, reason)


@transaction.atomic
def update_account(actor, user, username, display_name, email, reason=""):
    """ADR 0049 Q12: an admin corrects the sign-in name, the name people see and the email. A
    `line-employee-…` account given another name counts as a web account from then on."""
    require_admin(actor)
    _require_account_admin(actor, user, _lock_active_admins())
    user = User.objects.select_for_update().get(pk=user.pk)
    username, email = username.strip(), email.strip()
    display_name = " ".join(display_name.split())
    if not username or not display_name:
        raise ValidationError("ต้องมีชื่อผู้ใช้และชื่อที่แสดง")
    for value, model, name, label in (
        (username, User, "username", "ชื่อผู้ใช้"),
        (display_name, StaffProfile, "display_name", "ชื่อที่แสดง"),
        (email, User, "email", "อีเมล"),
    ):
        limit = model._meta.get_field(name).max_length
        if len(value) > limit:
            raise ValidationError(f"{label}ยาวได้ไม่เกิน {limit} ตัวอักษร")
    if username.lower().startswith(LINE_ACCOUNT) and username != user.username:
        # merge_accounts folds `line-employee-…` accounts into a web account (ADR 0044).
        raise ValidationError(f"ชื่อผู้ใช้ที่ขึ้นต้นด้วย {LINE_ACCOUNT} สงวนไว้ให้บัญชีที่สร้างจาก LINE")
    try:
        User.username_validator(username)
    except ValidationError:
        raise ValidationError("ชื่อผู้ใช้ใช้ได้เฉพาะตัวอักษร ตัวเลข และ @ . + - _") from None
    if email:
        try:
            validate_email(email)
        except ValidationError:
            raise ValidationError("อีเมลไม่ถูกต้อง") from None
    if User.objects.filter(username__iexact=username).exclude(pk=user.pk).exists():
        raise ValidationError("ชื่อผู้ใช้นี้มีบัญชีอื่นใช้แล้ว")
    profile = _profile(user)
    before = {"username": user.username, "display_name": profile.display_name, "email": user.email}
    after = {"username": username, "display_name": display_name, "email": email}
    changed = {k: v for k, v in after.items() if before[k] != v}
    if not changed:
        raise ValidationError("ข้อมูลเหมือนเดิม")
    user.username, user.email = username, email
    user.save(update_fields=["username", "email"])
    profile.display_name = display_name
    profile.save(update_fields=["display_name"])
    audit(
        actor,
        "staff.account_changed",
        user,
        reason,
        **changed,
        **{f"previous_{k}": before[k] for k in changed},
    )
    from operations import roster_match  # local: roster_match builds on identity

    roster_match.link(user, actor)  # ADR 0050 Q1: the username, email and name all match the roster


def password_changed(user):
    profile = _profile(user)
    if profile.must_change_password:
        profile.must_change_password = False
        profile.save(update_fields=["must_change_password"])


@transaction.atomic
def bootstrap_admin(username, display_name, password):
    """First admin only: refuses while any active admin exists (recovery is a DB-owner task)."""
    _lock_active_admins()
    if RoleGrant.objects.filter(
        role=Role.ADMIN, revoked_at__isnull=True, user__is_active=True
    ).exists():
        raise PermissionDenied("An active admin already exists; use the admin console.")
    user = User.objects.create_user(username=username, password=password)
    StaffProfile.objects.create(user=user, display_name=display_name)
    roles = [Role.ADMIN]
    RoleGrant.objects.create(user=user, role=Role.ADMIN, reason="bootstrap")
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
    verified_by="",
):
    """Record a registration request. Choosing a district is a claim, never a permission; a
    matched employee (10 Oct 2569 Q1) is evidence for the decider."""
    submitted_name = " ".join((submitted_name or "").split())
    if not submitted_name or len(submitted_name) > 200:
        raise ValidationError("Name is required (max 200 characters).")
    district = District.objects.filter(code=district_code, is_active=True).first()
    if district is None:
        raise ValidationError("Unknown district.")
    identity, _ = ChannelIdentity.objects.select_for_update().get_or_create(
        channel=channel, external_id=external_id
    )
    if identity.status == ChannelIdentity.Status.ACTIVE and not (
        identity.requester_id and identity.requester.is_external
    ):
        raise ValidationError("Identity is already approved.")
    pending = identity.registrations.filter(status=RegistrationRequest.Status.PENDING).first()
    if pending is not None:
        return pending
    if identity.status == ChannelIdentity.Status.REVOKED:
        # Relinking needs a fresh approval; the revoked requester is not reused.
        identity.status = ChannelIdentity.Status.PENDING
        identity.requester = None
        identity.save(update_fields=["status", "requester"])
    registration = RegistrationRequest.objects.create(
        identity=identity,
        submitted_name=submitted_name,
        requested_district=district,
        employee=employee,
        verified_by=verified_by if employee is not None else "",
    )

    from operations import admin_line

    admin_line.notify(registration)
    return registration


@transaction.atomic
def external_requester(identity_row, name):
    """A named problem-only reporter, including an employee awaiting manual verification."""
    name = " ".join(name.split())
    if not 2 <= len(name) <= 200:
        raise ValidationError("ระบุชื่อ 2–200 ตัวอักษร")
    row = ChannelIdentity.objects.select_for_update().get(pk=identity_row.pk)
    if row.status == ChannelIdentity.Status.REVOKED:
        raise ValidationError("บัญชีนี้ถูกถอนการรับรอง ติดต่อแอดมินก่อน")
    if row.requester_id:
        if row.requester.status != Requester.Status.ACTIVE:
            raise ValidationError("บัญชีผู้แจ้งถูกระงับ ติดต่อแอดมินก่อน")
        return row.requester
    district, _ = District.objects.get_or_create(
        code="D00", defaults={"name": "ไม่ระบุหน่วยงาน (เขต/ฝ่าย)", "is_active": False}
    )
    requester = Requester.objects.create(
        display_name=name,
        district=district,
        approved_by=system_user(),
        is_external=True,
    )
    row.requester, row.status = requester, ChannelIdentity.Status.ACTIVE
    row.save(update_fields=["requester", "status"])
    audit(system_user(), "requester.external_registered", requester, actor_label="line")
    return requester


@transaction.atomic
def provision_employee_staff(identity_row, employee, actor):
    """Called only after identity verification; grant the roster's member role, never Supervisor."""
    employee = Employee.objects.select_for_update().get(pk=employee.pk)
    if not employee.is_active:
        raise ValidationError("พนักงานไม่อยู่ในทะเบียนที่ใช้งานอยู่")
    profiles = list(
        StaffProfile.objects.filter(employee_id=employee.employee_id).select_related("user")
    )
    if len(profiles) > 1:
        raise ValidationError("รหัสพนักงานผูกหลายบัญชี ให้แอดมินตรวจ")
    if profiles:
        user = profiles[0].user
        if not user.is_active:
            _reopen_without_old_roles(user, actor)
    else:
        username = f"line-employee-{employee.pk}"
        if User.objects.filter(username=username).exists():
            raise ValidationError("บัญชีพนักงานซ้ำ ให้แอดมินตรวจ")
        user = User.objects.create_user(username=username, password=None)
        from operations import employees  # local: employees audits through this module

        StaffProfile.objects.create(
            user=user, display_name=employees.label(employee), employee_id=employee.employee_id
        )
    if identity_row.staff_user_id and identity_row.staff_user_id != user.pk:
        raise ValidationError("LINE นี้เชื่อมกับพนักงานอีกคน ให้แอดมินตรวจ")
    if ChannelIdentity.objects.filter(staff_user=user).exclude(pk=identity_row.pk).exists():
        raise ValidationError("พนักงานนี้เชื่อม LINE อื่นแล้ว ให้แอดมินตรวจ")
    # ADR 0041: one role; a person who holds one keeps it, others start as สมาชิกกอง of the
    # roster's กอง, or as เจ้าหน้าที่ (no role) when the กอง is not in the system.
    has_role = (
        RoleGrant.objects.filter(user=user, revoked_at__isnull=True)
        .exclude(role=Role.SYSTEM_SUPPORT)
        .exists()
    )
    if employee.division_id and employee.division.is_active and not has_role:
        grant, created = RoleGrant.objects.get_or_create(
            user=user,
            role=Role.STAFF,
            division=employee.division,
            revoked_at__isnull=True,
            defaults={"granted_by": actor, "reason": "ยืนยันตัวตนและกองตามทะเบียนพนักงาน"},
        )
        if created:
            audit(
                actor,
                "role.granted",
                user,
                grant.reason,
                role=Role.STAFF,
                division=employee.division.code,
            )
            sync_membership_authority(user, actor)
    from operations import units  # local: units builds on identity

    units.bind_from_roster(user, employee)  # ADR 0031 Q5: the สังกัด the roster names
    identity_row.staff_user, identity_row.staff_linked_at = user, timezone.now()
    identity_row.save(update_fields=["staff_user", "staff_linked_at"])
    from operations import line_menu

    transaction.on_commit(lambda: line_menu.relink_user(user))
    return user


def _reopen_without_old_roles(user, actor):
    """User, 31 Oct 2569 (Q9): the person is verified and not suspended today, so a suspended
    staff account left from before (e.g. the 29 Oct LINE wipe) opens again with no old role;
    the old roles stay in its history."""
    reason = "เปิดบัญชีเดิมอีกครั้งเพราะผู้ใช้ LINE OA คนนี้ยังใช้งานได้ สิทธิ์เก่าถอนออกทั้งหมด"
    user = User.objects.select_for_update().get(pk=user.pk)
    for grant in RoleGrant.objects.select_for_update().filter(user=user, revoked_at__isnull=True):
        grant.revoked_at, grant.revoked_by, grant.reason = timezone.now(), actor, reason
        grant.save(update_fields=["revoked_at", "revoked_by", "reason"])
        if grant.role == Role.SYSTEM_SUPPORT:
            from operations import support  # ADR 0007: its authority ends with the role

            support.revoke_grants(user, actor, reason)
        audit(
            actor,
            "role.revoked",
            user,
            reason,
            role=grant.role,
            division=grant.division.code if grant.division else None,
        )
    user.is_active = True
    user.save(update_fields=["is_active"])
    audit(actor, "staff.activated", user, reason)


@transaction.atomic
def grant_requester_role(
    actor, requester, role, division=None, reason="", section=None, work_group=None
):
    """Manage a verified LINE employee without a second web registration."""
    require_admin(actor)
    reason = _require_reason(reason)
    requester = Requester.objects.select_for_update().get(pk=requester.pk)
    if (
        requester.status != Requester.Status.ACTIVE
        or not requester.employee_id
        or (
            requester.employee_link
            not in {Requester.EmployeeLink.VERIFIED, Requester.EmployeeLink.ADMIN}
        )
    ):
        raise ValidationError("ต้องยืนยันตัวตนพนักงานก่อนให้สิทธิ์เจ้าหน้าที่")
    lines = list(
        ChannelIdentity.objects.select_for_update().filter(
            requester=requester, status=ChannelIdentity.Status.ACTIVE
        )
    )
    if len(lines) != 1:
        raise ValidationError("ต้องมีบัญชี LINE ที่ใช้งานได้เพียงบัญชีเดียว")
    user = provision_employee_staff(lines[0], requester.employee, actor)
    if role in DIVISION_ROLES and division is None:
        division = requester.employee.division
    set_role(actor, user, role, division, reason, section, work_group)
    requester.is_external = False
    requester.save(update_fields=["is_external"])
    audit(actor, "requester.staff_access", requester, reason, user=user.pk, role=role)
    return user


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
    """Approve only verified active employees; internal division membership is separate."""
    registration = RegistrationRequest.objects.select_for_update().get(pk=registration.pk)
    if registration.employee is None or not registration.verified_by:
        raise ValidationError("Automatic approval needs verified employee evidence.")
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
    from operations import admin_line

    admin_line.notify(registration)
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
def approve_registration(actor, registration, reason="", requester=None):
    _require_registration_decider(actor)
    if requester is not None:
        return _approve_as(actor, registration, requester, reason)
    return _approve(actor, registration, reason)


def _approve_as(actor, registration, requester, reason):
    """ย้าย LINE OA (28 Oct 2569): the bot moved to an OA under another LINE provider, so people
    come back with a new LINE user ID. An admin says which requester this was; the new LINE
    takes over that requester (its cases and notices) and the old LINE is withdrawn."""
    require_admin(actor)
    registration = RegistrationRequest.objects.select_for_update().get(pk=registration.pk)
    if registration.status != RegistrationRequest.Status.PENDING:
        raise ValidationError("Registration is not pending.")
    identity = ChannelIdentity.objects.select_for_update().get(pk=registration.identity_id)
    if identity.status == ChannelIdentity.Status.REVOKED:
        raise ValidationError("บัญชีถูกระงับหรือถอนการรับรอง ให้แอดมินตรวจ")
    requester = Requester.objects.select_for_update().get(pk=requester.pk)
    if requester.status != Requester.Status.ACTIVE or requester.is_anonymous:
        raise ValidationError("ผูกได้เฉพาะผู้แจ้งที่ใช้งานอยู่")
    if registration.employee_id and registration.employee_id != requester.employee_id:
        raise ValidationError("คำขอนี้ตรงกับรหัสพนักงานของคนอื่น ผูกกับผู้แจ้งคนนี้ไม่ได้")
    old = (
        ChannelIdentity.objects.select_for_update()
        .filter(requester=requester, channel=identity.channel)
        .exclude(pk=identity.pk)
    )
    moved_from = list(old.values_list("pk", flat=True))
    old.update(status=ChannelIdentity.Status.REVOKED, is_following=False)
    identity.requester = requester
    identity.status = ChannelIdentity.Status.ACTIVE
    identity.save(update_fields=["requester", "status"])
    _mark_approved(actor, registration, reason)
    from operations import line_menu

    transaction.on_commit(lambda: line_menu.relink(identity))
    audit(
        actor,
        "registration.approved",
        registration,
        reason,
        district=registration.requested_district.code,
        requester_id=requester.pk,
        employee=registration.employee.employee_id if registration.employee else None,
        moved_from=moved_from,
    )
    return requester


def _mark_approved(actor, registration, reason):
    registration.status = RegistrationRequest.Status.APPROVED
    registration.decided_by = actor
    registration.decided_at = timezone.now()
    registration.decision_reason = reason.strip()
    registration.save(update_fields=["status", "decided_by", "decided_at", "decision_reason"])
    _registration_event(registration)


def _approve(actor, registration, reason="", automatic=False):
    registration = RegistrationRequest.objects.select_for_update().get(pk=registration.pk)
    if registration.status == RegistrationRequest.Status.APPROVED:
        return registration.identity.requester
    if registration.status != RegistrationRequest.Status.PENDING:
        raise ValidationError("Registration is not pending.")
    identity = ChannelIdentity.objects.select_for_update().get(pk=registration.identity_id)
    if identity.status == ChannelIdentity.Status.REVOKED or (
        identity.requester_id and identity.requester.status != Requester.Status.ACTIVE
    ):
        raise ValidationError("บัญชีถูกระงับหรือถอนการรับรอง ให้แอดมินตรวจ")
    employee = registration.employee
    if employee is not None:
        if not employee.is_active:
            raise ValidationError("พนักงานไม่อยู่ในทะเบียนที่ใช้งานอยู่")
        if employee.division_id and employee.division.is_active:
            provision_employee_staff(identity, employee, actor)
    values = {
        "display_name": registration.submitted_name,
        "district": registration.requested_district,
        "approved_by": actor,
        "employee": employee,
        "employee_link": (
            Requester.EmployeeLink.VERIFIED
            if registration.verified_by
            else Requester.EmployeeLink.ADMIN
        )
        if employee
        else "",
        "is_external": employee is None,
    }
    if identity.requester_id and identity.requester.status == Requester.Status.ACTIVE:
        requester = identity.requester
        for field, value in values.items():
            setattr(requester, field, value)
        requester.save(update_fields=list(values))
    else:
        requester = Requester.objects.create(**values)
    identity.requester = requester
    identity.status = ChannelIdentity.Status.ACTIVE
    identity.save(update_fields=["requester", "status"])
    _mark_approved(actor, registration, reason)
    from operations import line_menu

    transaction.on_commit(lambda: line_menu.relink(identity))
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
    if requester.employee_id:
        from operations import roster_match  # local: roster_match builds on identity

        roster_match.link_requester(requester, actor)
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
        code=UNKNOWN_DISTRICT, defaults={"name": "ไม่ระบุหน่วยงาน (เขต/ฝ่าย)", "is_active": False}
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
    """The requester row, locked, for an audited admin change. Anonymous reporters are not
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
    checking who they are."""
    requester, reason = _managed_requester(actor, requester, reason)
    employee = Employee.objects.filter(
        employee_id=(employee_id or "").strip(), is_active=True
    ).first()
    if employee is None:
        raise ValidationError("ไม่พบรหัสพนักงานนี้ในทะเบียนพนักงานที่ใช้งานอยู่")
    requester.employee = employee
    requester.employee_link = Requester.EmployeeLink.ADMIN
    if not requester.district.is_active and employee.district_id and employee.district.is_active:
        requester.district = employee.district
    requester.save(update_fields=["employee", "employee_link", "district"])
    audit(actor, "requester.employee_linked", requester, reason, employee=employee.employee_id)
    from operations import roster_match  # local: roster_match builds on identity

    roster_match.link_requester(requester, actor)
    lines = list(
        ChannelIdentity.objects.select_for_update().filter(
            requester=requester, status=ChannelIdentity.Status.ACTIVE
        )
    )
    if len(lines) == 1:
        line = lines[0]
        if employee.division_id and employee.division.is_active:
            provision_employee_staff(line, employee, actor)
        requester.is_external = False
        requester.save(update_fields=["is_external"])
        from operations import line_menu

        transaction.on_commit(lambda: line_menu.relink(line))
        for registration in line.registrations.filter(status=RegistrationRequest.Status.PENDING):
            registration.employee = employee
            registration.save(update_fields=["employee"])
            _approve(actor, registration, reason)
    return requester


@transaction.atomic
def set_requester_district(actor, requester, district_code, reason):
    """An admin corrects the district a requester chose (for example when ทะเบียนพนักงาน
    puts them in another one). Cases keep the district they were filed with; new cases use
    this one."""
    requester, reason = _managed_requester(actor, requester, reason)
    if requester.is_internal:
        raise ValidationError("ผู้แจ้งภายในแจ้งในนามกอง ไม่มีหน่วยงาน (เขต/ฝ่าย)")
    district = District.objects.filter(code=district_code, is_active=True).first()
    if district is None:
        raise ValidationError("ไม่รู้จักหน่วยงาน (เขต/ฝ่าย)นี้")
    if district.pk == requester.district_id:
        raise ValidationError("ผู้แจ้งอยู่หน่วยงาน (เขต/ฝ่าย)นี้อยู่แล้ว")
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
    return requester


@transaction.atomic
def unlink_requester_employee(actor, requester, reason):
    """An admin removes a wrong link."""
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


def requester_grants(requesters):
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
    grants = defaultdict(list)
    active = RoleGrant.objects.filter(
        user_id__in={u for ids in users.values() for u in ids},
        revoked_at__isnull=True,
        user__is_active=True,
    ).select_related("division")
    for grant in active:
        grants[grant.user_id].append(grant)
    return {
        requester.pk: [g for u in sorted(users[requester.pk]) for g in grants[u]]
        for requester in requesters
    }


def requester_roles(requesters):
    """The role labels of requester_grants, by requester pk."""
    return {
        pk: sorted({_grant_label(g) for g in held})
        for pk, held in requester_grants(requesters).items()
    }


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


def approve_verified_registrations():
    """Retry verified registrations after roster corrections; no name-only approvals."""
    pending = (
        RegistrationRequest.objects.filter(
            status=RegistrationRequest.Status.PENDING, employee__is_active=True
        )
        .exclude(verified_by="")
        .order_by("pk")
    )
    approved = 0
    for registration in pending.iterator():
        try:
            auto_approve_registration(registration)
        except ValidationError:
            continue
        approved += 1
    legacy = Requester.objects.filter(
        status=Requester.Status.ACTIVE,
        is_external=True,
        employee__is_active=True,
        employee_link__in=[Requester.EmployeeLink.VERIFIED, Requester.EmployeeLink.ADMIN],
    )
    for requester in legacy.iterator():
        try:
            with transaction.atomic():
                requester = Requester.objects.select_for_update().get(pk=requester.pk)
                if not requester.is_external or requester.status != Requester.Status.ACTIVE:
                    continue
                lines = list(
                    ChannelIdentity.objects.select_for_update().filter(
                        requester=requester,
                        status=ChannelIdentity.Status.ACTIVE,
                    )
                )
                if len(lines) != 1:
                    continue
                employee = requester.employee
                if employee.division_id and employee.division.is_active:
                    provision_employee_staff(lines[0], employee, system_user())
                requester.is_external = False
                if (
                    not requester.district.is_active
                    and employee.district_id
                    and employee.district.is_active
                ):
                    requester.district = employee.district
                requester.save(update_fields=["is_external", "district"])
                audit(system_user(), "requester.employee_access_restored", requester)
        except ValidationError:
            continue
    return approved
