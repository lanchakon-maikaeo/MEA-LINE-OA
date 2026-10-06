"""Identity, organisation and audit models (M01). Case models belong to M02."""

from django.conf import settings
from django.db import models
from django.db.models import Q


class District(models.Model):
    """Real MEA district name; `code` is the prototype D01-D18 key, never an official code."""

    code = models.CharField(max_length=16, unique=True)
    name = models.CharField(max_length=200, unique=True)
    official_code = models.CharField(max_length=32, null=True, blank=True, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return self.name


class Division(models.Model):
    """A unit that can own work: a division (กอง) or a coordination desk such as SIM-TRIAGE."""

    class Kind(models.TextChoices):
        DIVISION = "division", "กอง"
        COORDINATION = "coordination", "บทบาทประสานงาน"

    code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.DIVISION)
    is_active = models.BooleanField(default=True)
    is_synthetic = models.BooleanField(default=False)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} {self.name}"


class Role(models.TextChoices):
    # Global roles (division must be empty).
    ADMIN = "admin", "แอดมินระบบ"
    DEVELOPER_ADMIN = "developer_admin", "แอดมินผู้พัฒนา"
    # Division roles. A role is a duty, not approval authority (AuthorityGrant is M06).
    STAFF = "staff", "เจ้าหน้าที่กอง"
    APPROVER = "approver", "ผู้อนุมัติ"
    ISSUER = "issuer", "ผู้จ่าย"
    RECEIVER = "receiver", "ผู้รับคืน"
    INSPECTOR = "inspector", "ผู้ตรวจสภาพ"
    RESOLVER = "resolver", "ผู้แก้ปัญหา"
    SUPERVISOR = "supervisor", "ผู้กำกับงานในกอง"


GLOBAL_ROLES = frozenset({Role.ADMIN, Role.DEVELOPER_ADMIN})
DIVISION_ROLES = frozenset(set(Role) - GLOBAL_ROLES)


class StaffProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="staff_profile"
    )
    display_name = models.CharField(max_length=200)
    must_change_password = models.BooleanField(default=False)
    # Sessions authenticated before this instant are rejected (forced logout).
    sessions_valid_after = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.display_name


class RoleGrant(models.Model):
    """History-preserving role assignment; a grant is active while `revoked_at` is empty."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="role_grants"
    )
    role = models.CharField(max_length=32, choices=Role.choices)
    division = models.ForeignKey(Division, null=True, blank=True, on_delete=models.PROTECT)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    granted_at = models.DateTimeField(auto_now_add=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                name="rolegrant_scope_matches_role",
                condition=(
                    Q(role__in=sorted(GLOBAL_ROLES), division__isnull=True)
                    | (~Q(role__in=sorted(GLOBAL_ROLES)) & Q(division__isnull=False))
                ),
            ),
            models.UniqueConstraint(
                fields=["user", "role"],
                condition=Q(revoked_at__isnull=True, division__isnull=True),
                name="rolegrant_one_active_global_role",
            ),
            models.UniqueConstraint(
                fields=["user", "role", "division"],
                condition=Q(revoked_at__isnull=True, division__isnull=False),
                name="rolegrant_one_active_division_role",
            ),
        ]


class Requester(models.Model):
    """An approved person who may submit cases for exactly one district."""

    class Status(models.TextChoices):
        ACTIVE = "active", "ใช้งาน"
        SUSPENDED = "suspended", "ระงับ"

    display_name = models.CharField(max_length=200)
    district = models.ForeignKey(District, on_delete=models.PROTECT)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    approved_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.display_name


class ChannelIdentity(models.Model):
    """External chat identity (LINE user ID from a signed event); grants nothing until approved."""

    class Channel(models.TextChoices):
        LINE = "line", "LINE"

    class Status(models.TextChoices):
        PENDING = "pending", "รอรับรอง"
        ACTIVE = "active", "รับรองแล้ว"
        REVOKED = "revoked", "ถอนแล้ว"

    channel = models.CharField(max_length=16, choices=Channel.choices)
    external_id = models.CharField(max_length=128)
    requester = models.ForeignKey(Requester, null=True, blank=True, on_delete=models.PROTECT)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["channel", "external_id"], name="channel_identity_key"),
            models.CheckConstraint(
                name="channel_identity_active_has_requester",
                condition=~Q(status="active") | Q(requester__isnull=False),
            ),
        ]


class RegistrationRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "รอรับรอง"
        APPROVED = "approved", "อนุมัติ"
        REJECTED = "rejected", "ปฏิเสธ"

    identity = models.ForeignKey(
        ChannelIdentity, on_delete=models.PROTECT, related_name="registrations"
    )
    submitted_name = models.CharField(max_length=200)
    requested_district = models.ForeignKey(District, on_delete=models.PROTECT)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["identity"],
                condition=Q(status="pending"),
                name="registration_one_pending_per_identity",
            )
        ]


class AuditEvent(models.Model):
    """Append-only record of who changed what and why. M02 adds case events to the same log."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    actor_label = models.CharField(max_length=64, blank=True)
    action = models.CharField(max_length=64)
    target_type = models.CharField(max_length=64)
    target_id = models.CharField(max_length=64)
    reason = models.TextField(blank=True)
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("AuditEvent is append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("AuditEvent is append-only.")
