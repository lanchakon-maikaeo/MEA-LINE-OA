"""Identity and audit (M01), cases (M02), LINE intake (M03), orchestration (M04), AI (M05),
routing (M06), withdrawal fulfilment with the inventory seam (M07), returns (M08) and problems,
child work and service-calendar deadlines (M09)."""

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower
from django.utils import timezone

from operations.koj_models import (  # noqa: F401 - register and re-export กอจ. models
    KojHistory,
    KojNotice,
    KojRecipient,
    KojRecord,
    KojSource,
    KojWatch,
)
from operations.sheet_models import (  # noqa: F401 - register and re-export ค้นในชีท models
    SheetEditor,
    SheetEntry,
    SheetSource,
    SheetStatusRun,
)


class District(models.Model):
    """Real MEA district name; `code` is the prototype D01-D18 key, never an official code."""

    code = models.CharField(max_length=16, unique=True)
    name = models.CharField(max_length=200, unique=True)
    official_code = models.CharField(max_length=32, null=True, blank=True, unique=True)
    # ชื่อย่อ ฟข. (grill กคอ. 28 ต.ค. 2569 Q26) such as ฟขป., used by the divisions' truck
    # tables; an admin sets it, unique when set.
    short_name = models.CharField(max_length=16, blank=True)
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
    # The central intake unit (กองรับเรื่องกลาง): owner of every case at submission.
    is_intake = models.BooleanField(default=False)
    # หน้าที่ของกอง (ADR 0013, Q14 ข): what the division does, in the admin's words; the AI reads
    # it when no duty entry fits. The examples test the AI and never reach its prompt (Q25).
    duty_overview = models.TextField(blank=True)
    duty_examples = models.JSONField(default=list, blank=True)
    # เลขใบเบิก/ใบส่งคืน SAP (user, 23 Oct 2569): a withdrawal or return this division takes
    # follows a slip already made in SAP, so the LINE chat asks for its number. Admins switch it.
    requires_sap = models.BooleanField(default=False)
    # ชื่อย่อ (ADR 0031 Q3): what the chart and the สังกัด show; the code when empty.
    short_name = models.CharField(max_length=32, blank=True)

    class Meta:
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(
                fields=["is_intake"], condition=Q(is_intake=True), name="division_single_intake"
            )
        ]

    def __str__(self):
        return f"{self.code} {self.name}"


class Section(models.Model):
    """แผนก (งาน in ศอค.): a group inside a division for queues and reports. The division stays
    the owner of the work and of every right (real structure, 9 Oct 2569 Q1, Q13)."""

    division = models.ForeignKey(Division, on_delete=models.PROTECT, related_name="sections")
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=32, blank=True)  # an official abbreviation, when one exists
    order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    is_synthetic = models.BooleanField(default=False)

    class Meta:
        ordering = ["division__code", "order", "name"]
        constraints = [
            models.UniqueConstraint(fields=["division", "name"], name="section_unique_name")
        ]

    def __str__(self):
        return self.code or self.name


class WorkGroup(models.Model):
    """กลุ่มงาน (ADR 0031): a group inside a แผนก, the smallest unit. Only admins create one
    (Q13); it owns no case and holds no right."""

    section = models.ForeignKey(Section, on_delete=models.PROTECT, related_name="work_groups")
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=32, blank=True)  # ชื่อย่อ, when the unit has one
    order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["section__division__code", "section__order", "order", "name"]
        constraints = [
            models.UniqueConstraint(fields=["section", "name"], name="work_group_unique_name")
        ]

    def __str__(self):
        return self.code or self.name


class Role(models.TextChoices):
    # Global roles (division must be empty).
    ADMIN = "admin", "แอดมินระบบ"
    DEVELOPER_ADMIN = "developer_admin", "แอดมินผู้พัฒนา"
    # ADR 0007: one person (the system's designer) acts for every division, unbound by the
    # separation of duties; granted only on the host (manage.py system_support).
    SYSTEM_SUPPORT = "system_support", "ผู้ช่วยเหลือระบบ"
    # 16 Oct 2569 (Q5): oversees every division (ผอ. ฝอจ. at first); assigns work anywhere.
    DEPARTMENT_SUPERVISOR = "department_supervisor", "Supervisor ระดับฝ่าย"
    # Division roles. With ตัวเลือก "หน้าที่ในกอง" = member (16 Oct 2569 Q3) every member does
    # every step and holds the division's authority; the duties below matter only when the
    # organisation splits them again.
    STAFF = "staff", "สมาชิกกอง"
    APPROVER = "approver", "ผู้รับเรื่องของกอง"
    ISSUER = "issuer", "ผู้จ่าย"
    RECEIVER = "receiver", "ผู้รับคืน"
    INSPECTOR = "inspector", "ผู้ตรวจสภาพ"
    RESOLVER = "resolver", "ผู้แก้ปัญหา"
    SUPERVISOR = "supervisor", "Supervisor ของกอง"
    # ADR 0039: sees what a Supervisor of the scope sees and does nothing.
    DIVISION_VIEWER = "division_viewer", "ผู้ดูข้อมูลของกอง"
    DEPARTMENT_VIEWER = "department_viewer", "ผู้ดูข้อมูลระดับฝ่าย"


GLOBAL_ROLES = frozenset(
    {
        Role.ADMIN,
        Role.DEVELOPER_ADMIN,
        Role.SYSTEM_SUPPORT,
        Role.DEPARTMENT_SUPERVISOR,
        Role.DEPARTMENT_VIEWER,
    }
)
DIVISION_ROLES = frozenset(set(Role) - GLOBAL_ROLES)
VIEWER_ROLES = frozenset({Role.DIVISION_VIEWER, Role.DEPARTMENT_VIEWER})
# What a member does when the division's work is not split (16 Oct 2569 Q3).
MEMBER_ROLES = frozenset(DIVISION_ROLES - {Role.SUPERVISOR, Role.DIVISION_VIEWER})
# The duties a split division hands out one by one; merged into สมาชิกกอง on 16 Oct 2569.
DUTY_ROLES = frozenset(MEMBER_ROLES - {Role.STAFF})


class StaffProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="staff_profile"
    )
    display_name = models.CharField(max_length=200)
    # รหัสพนักงาน (10 Oct 2569 Q3): links LINE to this account after an employee check.
    employee_id = models.CharField(max_length=32, blank=True)
    # สังกัด (ADR 0031): one unit, the deepest of กอง › แผนก › กลุ่มงาน; each level may be
    # empty (N/A). Set from the roster when the employee is verified, moved by an admin.
    division = models.ForeignKey(
        Division, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # แผนก (Q13): at most one; empty for heads of division and staff outside any section.
    section = models.ForeignKey(
        Section, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    work_group = models.ForeignKey(
        WorkGroup, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # ไม่อยู่ ถึงวันที่ (ADR 0031 Q21): set by the person; the system skips them until then.
    away_until = models.DateField(null=True, blank=True)
    must_change_password = models.BooleanField(default=False)
    notify_registrations = models.BooleanField(default=False)
    # ADR 0030 Q25: an admin hears on LINE of each case waiting for the admins (on unless set off).
    notify_admin_waiting = models.BooleanField(default=True)
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
            # ADR 0007: at most one active ผู้ช่วยเหลือระบบ in the whole system.
            models.UniqueConstraint(
                fields=["role"],
                condition=Q(role="system_support", revoked_at__isnull=True),
                name="rolegrant_single_system_support",
            ),
        ]


class Requester(models.Model):
    """An approved person who may submit cases for exactly one district."""

    class Status(models.TextChoices):
        ACTIVE = "active", "ใช้งาน"
        SUSPENDED = "suspended", "ระงับ"
        # การถอนการรับรอง (14 Oct 2569): final; the LINE account has to register again and gets a
        # new requester. Cases and messages stay with this one.
        REVOKED = "revoked", "ถอนแล้ว"

    class EmployeeLink(models.TextChoices):
        """How `employee` was set: the person proved the employee ID, or an admin linked it.
        The roster has no names (user, 23 Oct 2569), so nothing is linked by a name any more."""

        VERIFIED = "verified", "ยืนยันด้วยรหัสพนักงาน"
        ADMIN = "admin", "แอดมินผูก"
        UNLINKED = "unlinked", "แอดมินยกเลิกการผูก"

    display_name = models.CharField(max_length=200)
    district = models.ForeignKey(District, on_delete=models.PROTECT)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    approved_at = models.DateTimeField(auto_now_add=True)
    # ไม่ระบุตัวตน (10 Oct 2569 Q1): a LINE user who reports a problem or suggestion without
    # registering; only problem/suggestion cases, always with a location. Staff never see who.
    is_anonymous = models.BooleanField(default=False)
    anonymous_identity = models.OneToOneField(
        "ChannelIdentity",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="anonymous_requester",
    )

    # ผู้แจ้งภายใน (10 Oct 2569 Q8): a staff member reporting a problem or suggestion in LINE in the
    # name of their own division; problems and suggestions only.
    is_internal = models.BooleanField(default=False)
    is_external = models.BooleanField(default=False)
    division = models.ForeignKey(
        "Division", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # ทะเบียนพนักงาน behind this person, for the position and staff roles on the requester list.
    employee = models.ForeignKey(
        "Employee", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    employee_link = models.CharField(max_length=16, choices=EmployeeLink.choices, blank=True)
    # รหัสแทนตัว (user, 14 Oct 2569): a random code for an anonymous reporter, shown only on the
    # admin's problem list so reports from one LINE account can be told apart without knowing
    # who it is. Never derived from the LINE ID; empty for everyone else.
    alias = models.CharField(max_length=8, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["alias"], condition=~Q(alias=""), name="requester_unique_alias"
            )
        ]

    def __str__(self):
        return "ไม่ระบุตัวตน" if self.is_anonymous else self.display_name


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
    # เจ้าหน้าที่ที่เชื่อม LINE (10 Oct 2569 Q3, Q7): the staff account this LINE acts for. One LINE
    # may be a requester and staff at once (ผอ.ฝอจ.); each side keeps its own rights.
    staff_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="line_identity",
    )
    staff_linked_at = models.DateTimeField(null=True, blank=True)
    # Unfollow/block is recorded for delivery decisions (M04); it never changes permissions.
    is_following = models.BooleanField(default=True)
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
        CANCELLED = "cancelled", "ผู้ขอยกเลิก"

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
    # การยืนยันตัวตน (10 Oct 2569 Q1, Q3): the employee the person proved to be, if any.
    employee = models.ForeignKey(
        "Employee", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    # How the employee proved who they are (15 ต.ค. 2569 Q24): shown to approvers and audited.
    verified_by = models.CharField(
        max_length=16,
        blank=True,
        choices=[("otp", "รหัส OTP ทางอีเมล"), ("email", "พิมพ์อีเมลตรงทะเบียน"), ("proof", "ข้อมูลยืนยัน")],
    )

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["identity"],
                condition=Q(status="pending"),
                name="registration_one_pending_per_identity",
            )
        ]


class AppendOnlyModel(models.Model):
    """History rows are written once; corrections are new rows, never edits or deletes."""

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError(f"{type(self).__name__} is append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError(f"{type(self).__name__} is append-only.")


class AuditEvent(AppendOnlyModel):
    """Append-only record of who changed what and why, for accounts and cases alike."""

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


# --- M02: case domain ---------------------------------------------------------------------------


class EquipmentType(models.Model):
    """รายการของ: one code, one unit. Divisible units allow 2 decimals; others are whole."""

    class Category(models.TextChoices):
        METER = "meter", "มิเตอร์"
        TRANSFORMER = "transformer", "หม้อแปลง"
        OTHER = "other", "อุปกรณ์อื่น"

    class LineGroup(models.TextChoices):
        METER = "meter", "มิเตอร์"
        CT = "ct", "CT"
        VT = "vt", "VT"
        OTHER = "other", "อุปกรณ์อื่น"
        # The กอจ. tile (user, 24 Oct 2569): typed rows like the กคว. groups.
        TRANSFORMER = "transformer", "หม้อแปลง"
        LBS = "lbs", "LBS"
        RMU = "rmu", "RMU"

    # The old category menus (เบิก/คืนด้วยรายการ) offer these four groups only.
    MENU_GROUPS = ("meter", "ct", "vt", "other")

    line_group = models.CharField(max_length=16, blank=True, choices=LineGroup.choices)

    code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=16, choices=Category.choices)
    unit = models.CharField(max_length=32)
    is_serialized = models.BooleanField(default=False)
    is_divisible = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    is_synthetic = models.BooleanField(default=False)
    # M11 Q5: the registry's (SAP) material code; one per item, empty until mapped.
    official_code = models.CharField(max_length=40, blank=True)
    # ช่องที่ผู้ใช้พิมพ์ (user, 23 Oct 2569): the one shared item of a LINE group whose lines are
    # typed (meter numbers, size, quantity) instead of chosen; never offered in a list.
    entry_group = models.CharField(max_length=16, blank=True, choices=LineGroup.choices)

    class Meta:
        ordering = ["code"]
        constraints = [
            models.CheckConstraint(
                name="equipment_serialized_is_whole",
                condition=~Q(is_serialized=True, is_divisible=True),
            ),
            models.UniqueConstraint(
                fields=["official_code"],
                condition=~Q(official_code=""),
                name="equipment_official_code_unique",
            ),
        ]

    def __str__(self):
        return f"{self.code} {self.name}"


class Case(models.Model):
    """เรื่อง: exists only once submitted (drafts live in the conversation layer, M03)."""

    class Kind(models.TextChoices):
        WITHDRAWAL = "withdrawal", "ติดตามใบเบิก"
        RETURN = "return", "ติดตามใบส่งคืน"
        PROBLEM = "problem", "แจ้งปัญหา/ข้อเสนอแนะ"

    class Status(models.TextChoices):
        SUBMITTED = "submitted", "รอคัดกรอง"
        TRIAGE = "triage", "กำลังคัดกรอง"
        AWAITING_APPROVAL = "awaiting_approval", "รอกองรับเรื่อง"
        AWAITING_EXECUTION = "awaiting_execution", "รอดำเนินการ"
        IN_PROGRESS = "in_progress", "กำลังดำเนินการ"
        AWAITING_CONFIRMATION = "awaiting_confirmation", "รอหน่วยงาน (เขต/ฝ่าย)ยืนยัน"
        AWAITING_INFO = "awaiting_info", "รอข้อมูลเพิ่ม"
        ON_HOLD = "on_hold", "พักเรื่อง"
        CLOSED = "closed", "ปิดเรื่อง"
        REJECTED = "rejected", "ไม่รับเรื่อง"
        CANCELLED = "cancelled", "ยกเลิก"

    class Topic(models.TextChoices):
        METER = "meter", "มิเตอร์"
        TRANSFORMER = "transformer", "หม้อแปลง"
        OTHER = "other", "อุปกรณ์อื่น"
        USAGE = "usage", "การใช้ไฟฟ้า"
        SUGGESTION = "suggestion", "ข้อเสนอแนะ"
        # เมนู LINE (15 ต.ค. 2569 Q18, Q20): a question to a division, and a work system's problem.
        INQUIRY = "inquiry", "สอบถาม/ติดตาม"
        SYSTEM = "system", "ระบบงาน"
        # ปุ่ม Meter, CT, VT (user, 24 Oct 2569): follow a repair by its SAP number.
        REPAIR = "repair", "ติดตามสถานะการซ่อม"
        # grill กคว. 27 Oct 2569 (Q4, Q5): โน้ตบุ๊กและหัวอ่าน, and ขอเครื่องวัดฯ กลับคืน by MEA No.
        SERVICE = "service", "ขอรับบริการ"
        METER_BACK = "meter_back", "ขอเครื่องวัดฯ กลับคืน"
        # ADR 0030: the requester picks a division and types; staff say which it is at the result.
        GENERAL = "general", "ยังไม่ระบุ"
        PROBLEM = "problem", "ปัญหา"

    class Channel(models.TextChoices):
        LINE = "line", "LINE"
        PHONE = "phone", "โทรศัพท์"
        LETTER = "letter", "หนังสือ"
        COUNTER = "counter", "หน้าเคาน์เตอร์"
        # M12 Q6: ช่องทางรับเรื่องสำรอง while the intake system is down (the organisation's form).
        FALLBACK_FORM = "fallback_form", "แบบฟอร์มสำรอง"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    number = models.CharField(max_length=16, unique=True)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.SUBMITTED)
    # Where awaiting_info/on_hold returns to; set exactly while paused.
    resume_status = models.CharField(max_length=32, choices=Status.choices, blank=True)
    revision = models.PositiveIntegerField(default=1)

    requester = models.ForeignKey(Requester, on_delete=models.PROTECT, related_name="cases")
    district = models.ForeignKey(District, on_delete=models.PROTECT, related_name="+")
    owner_division = models.ForeignKey(Division, on_delete=models.PROTECT, related_name="+")
    # แผนกที่ดูแล (Q12): proposed by the policy, changed by the owner division; never an owner.
    section = models.ForeignKey(
        Section, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # ADR 0031: the งานที่หน่วยรับ the case matched, and whether the system (not a person) chose
    # the assignee; only such cases move on by themselves (Q24).
    unit_work = models.ForeignKey(
        "UnitWork", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    auto_assigned = models.BooleanField(default=False)
    # Empty when the requester submitted on LINE; the staff member when filed on their behalf.
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    channel = models.CharField(max_length=16, choices=Channel.choices)
    # หมวดเรื่อง the requester chose for a problem (Q3): equipment, or การใช้ไฟฟ้า for กตฟ.
    topic = models.CharField(max_length=16, choices=Topic.choices, blank=True)
    intake_note = models.TextField(blank=True)
    # ตำแหน่งที่แจ้ง (10 Oct 2569 Q1): required from an anonymous reporter, optional otherwise.
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    # เวลาแจ้งจริง (M12 Q8): when the requester reported on a staff channel, before it was recorded
    # here. Shown only; clocks start at submitted_at.
    reported_at = models.DateTimeField(null=True, blank=True)

    purpose = models.TextField(blank=True)
    needed_by = models.DateField(null=True, blank=True)
    condition = models.TextField(blank=True)
    return_reason = models.TextField(blank=True)
    source_reference = models.CharField(max_length=100, blank=True)
    # เลขใบเบิก/ใบส่งคืน SAP the requester gave (user, 23 Oct 2569); staff may correct it.
    # grill กคว. 27 Oct 2569 Q9, Q21: kept as typed ("4500012345/10"), and which document it is.
    # รอบใหม่ Q1–Q2: เลขติดตาม may also be a meter number or a project name (up to 60).
    sap_number = models.CharField(max_length=60, blank=True, db_index=True)
    sap_kind = models.CharField(max_length=16, blank=True)  # slip, return_slip, work_order,
    # meter, project or memo (M/N, grill กคอ. Q20)
    description = models.TextField(blank=True)
    impact = models.TextField(blank=True)
    related_case = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    submitted_at = models.DateTimeField()
    updated_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    # ตัวอย่าง (15 ต.ค. 2569 Q33): kept from the demo for staff to look at; in the queues with a
    # tag, never in reports, deadlines or messages (purge_demo_data sets it).
    is_example = models.BooleanField(default=False)
    # The LINE menu choice the case came from, e.g. "ระบบ SMG, ระบบ AMR · การจัดทำบิล" (Q20).
    menu_label = models.CharField(max_length=120, blank=True)
    # เรื่องการใช้ไฟฟ้า (25 Oct 2569 Q11, Q23): [{"number": …, "district": District.code}].
    meters = models.JSONField(default=list, blank=True)
    # Values a published LINE flow collected beyond the fixed fields (ADR 0023): the multiplier
    # check, the meter a CT set goes with, MEA numbers, warnings for staff.
    intake_data = models.JSONField(default=dict, blank=True)
    # ปัญหาการใช้งานหรืออื่นๆ (ADR 0030): no division was picked, so an admin holds the case. The
    # coordinator stands in as owner_division; queues, clocks and reports leave it to the admins.
    held_by_admin = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["-submitted_at"]
        constraints = [
            models.CheckConstraint(name="case_revision_positive", condition=Q(revision__gte=1)),
            models.CheckConstraint(
                name="case_resume_status_only_while_paused",
                condition=(
                    Q(status__in=["awaiting_info", "on_hold"]) & ~Q(resume_status="")
                    | ~Q(status__in=["awaiting_info", "on_hold"]) & Q(resume_status="")
                ),
            ),
            models.CheckConstraint(
                name="case_reported_at_only_on_behalf",
                condition=Q(reported_at__isnull=True) | ~Q(channel="line"),
            ),
            models.CheckConstraint(
                name="case_submitter_matches_channel",
                condition=(
                    Q(channel="line", submitted_by__isnull=True)
                    | (~Q(channel="line") & Q(submitted_by__isnull=False))
                ),
            ),
        ]
        indexes = [
            models.Index(fields=["requester", "status", "submitted_at"]),
            models.Index(fields=["district", "status", "submitted_at"]),
            models.Index(fields=["owner_division", "status", "submitted_at"]),
        ]

    def __str__(self):
        return self.number

    @property
    def owner_label(self):
        """The owner as people read it: แอดมิน for a case an admin holds (ADR 0030 Q19)."""
        return "แอดมิน" if self.held_by_admin else self.owner_division.code


class CaseLine(models.Model):
    """รายการของในเรื่อง. Problem lines may omit quantity.

    Withdrawal counters (M07, E.5): requested, approved (None = not decided yet), issued,
    received and terminated are kept apart; ส่วนค้างจ่าย = approved - issued - terminated. The
    database refuses counters that would issue more than was approved.
    """

    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="lines")
    line_no = models.PositiveSmallIntegerField()
    item = models.ForeignKey(EquipmentType, on_delete=models.PROTECT, related_name="+")
    requested_quantity = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    serials = models.JSONField(default=list, blank=True)
    # What the requester typed for a shared item (typed_lines): [{size, quantity, numbers,
    # name, extra}], one entry per typed row; empty for an item chosen from the list.
    details = models.JSONField(default=list, blank=True)
    approved_quantity = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    approval_reason = models.TextField(blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    issued_quantity = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    received_quantity = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    terminated_quantity = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    # Return counters (M08): arrived and counted for this line / outstanding return stopped.
    returned_quantity = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    return_terminated_quantity = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0")
    )

    class Meta:
        ordering = ["case", "line_no"]
        constraints = [
            models.UniqueConstraint(fields=["case", "line_no"], name="caseline_unique_no"),
            models.CheckConstraint(
                name="caseline_quantity_positive",
                condition=Q(requested_quantity__isnull=True) | Q(requested_quantity__gt=0),
            ),
            models.CheckConstraint(
                name="caseline_approved_within_request",
                condition=Q(approved_quantity__isnull=True)
                | Q(
                    approved_quantity__gte=0, approved_quantity__lte=models.F("requested_quantity")
                ),
            ),
            models.CheckConstraint(
                name="caseline_counters_not_negative",
                condition=Q(issued_quantity__gte=0, received_quantity__gte=0)
                & Q(terminated_quantity__gte=0)
                & Q(returned_quantity__gte=0, return_terminated_quantity__gte=0),
            ),
            models.CheckConstraint(
                name="caseline_fulfilment_within_approval",
                condition=Q(
                    approved_quantity__isnull=True, issued_quantity=0, terminated_quantity=0
                )
                | Q(
                    approved_quantity__gte=models.F("issued_quantity")
                    + models.F("terminated_quantity")
                ),
            ),
        ]

    @property
    def outstanding(self):
        """ส่วนค้างจ่าย: approved but neither issued nor terminated (0 before a decision)."""
        if self.approved_quantity is None:
            return Decimal("0")
        return self.approved_quantity - self.issued_quantity - self.terminated_quantity

    @property
    def return_outstanding(self):
        """ส่วนค้างส่งคืน: asked to return, not arrived (counted) and not stopped."""
        left = (
            (self.requested_quantity or Decimal("0"))
            - self.returned_quantity
            - self.return_terminated_quantity
        )
        return max(left, Decimal("0"))


class CaseNumberCounter(models.Model):
    """Gap-free sequence per case prefix and Buddhist-era year (row-locked when issuing)."""

    prefix = models.CharField(max_length=1)
    year_be = models.PositiveSmallIntegerField()
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["prefix", "year_be"], name="case_counter_key")
        ]


class Decision(AppendOnlyModel):
    """One accepted case command: who did what, from which status to which, and why."""

    action_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="decisions")
    action = models.CharField(max_length=32)
    from_status = models.CharField(max_length=32, blank=True)
    to_status = models.CharField(max_length=32)
    revision = models.PositiveIntegerField()
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    actor_label = models.CharField(max_length=64)
    reason = models.TextField(blank=True)
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["case", "revision"], name="decision_one_per_revision")
        ]


class ActionReceipt(models.Model):
    """Idempotency record: the same actor, action and key replays the stored result."""

    actor_key = models.CharField(max_length=64)
    action = models.CharField(max_length=32)
    idempotency_key = models.CharField(max_length=128)
    request_hash = models.CharField(max_length=64)
    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="+")
    result = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["actor_key", "action", "idempotency_key"], name="action_receipt_key"
            )
        ]


class FormSubmission(models.Model):
    """ADR 0015 Q21: the one-time key of a staff web form. The same key sent again (a submit
    repeated after an outage or a double click) is not applied twice; rows go after a day."""

    key = models.CharField(max_length=64)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    path = models.CharField(max_length=300)
    location = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "key"], name="form_submission_once")]
        indexes = [models.Index(fields=["created_at"])]


class OutboxEvent(models.Model):
    """Intent to notify or orchestrate, committed with the change it describes (envelope v1).

    Consumers claim events with a lease (M04); FAILED is the dead-letter state that only a
    person with rights may requeue.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "รอทำ"
        LEASED = "leased", "กำลังทำ"
        DONE = "done", "เสร็จ"
        FAILED = "failed", "ล้มเหลว (dead-letter)"

    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    event_type = models.CharField(max_length=64)
    schema_version = models.PositiveSmallIntegerField(default=1)
    subject_type = models.CharField(max_length=32)
    subject_id = models.CharField(max_length=64)
    case = models.ForeignKey(Case, null=True, on_delete=models.PROTECT, related_name="+")
    case_revision = models.PositiveIntegerField(null=True)
    correlation_id = models.UUIDField()
    payload = models.JSONField(default=dict)
    occurred_at = models.DateTimeField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    lease_token = models.CharField(max_length=64, blank=True)
    leased_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["status", "id"]),
            models.Index(fields=["event_type", "status", "next_attempt_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                name="outbox_case_event_has_revision",
                condition=Q(case__isnull=True) | Q(case_revision__isnull=False),
            )
        ]


# --- M03: LINE intake ----------------------------------------------------------------------------


class WebhookInbox(models.Model):
    """One signed LINE event, stored before the webhook answers 2xx (dedup on webhookEventId)."""

    class Status(models.TextChoices):
        PENDING = "pending", "รอประมวลผล"
        DONE = "done", "ประมวลผลแล้ว"
        IGNORED = "ignored", "ไม่ประมวลผล"
        FAILED = "failed", "ล้มเหลว"

    class ReplyStatus(models.TextChoices):
        NONE = "", "-"
        SENT = "sent", "ส่งแล้ว"
        EXPIRED = "expired", "เลยเวลา reply"
        SKIPPED = "skipped", "ไม่ส่ง (ปิดการส่ง/ไม่อยู่ใน allowlist)"
        FAILED = "failed", "ส่งไม่สำเร็จ"
        UNKNOWN = "unknown", "ไม่ทราบผล"

    channel = models.CharField(max_length=16, default="line")
    webhook_event_id = models.CharField(max_length=64)
    event_type = models.CharField(max_length=32)
    source_type = models.CharField(max_length=16, blank=True)
    source_user_id = models.CharField(max_length=64, blank=True)
    payload = models.JSONField()
    payload_hash = models.CharField(max_length=64)
    is_redelivery = models.BooleanField(default=False)
    occurred_at = models.DateTimeField()
    received_at = models.DateTimeField()
    reply_token = models.CharField(max_length=128, blank=True)
    reply_deadline = models.DateTimeField(null=True, blank=True)
    reply_used_at = models.DateTimeField(null=True, blank=True)
    reply_status = models.CharField(max_length=16, choices=ReplyStatus.choices, blank=True)
    reply_detail = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    processed_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["occurred_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["channel", "webhook_event_id"], name="webhook_inbox_event_key"
            )
        ]
        indexes = [models.Index(fields=["status", "occurred_at"])]


class ConversationDraft(models.Model):
    """ร่างเรื่อง collected in a 1:1 chat; becomes a Case only when the requester confirms."""

    class Status(models.TextChoices):
        OPEN = "open", "กำลังกรอก"
        SUBMITTED = "submitted", "ยื่นแล้ว"
        DISCARDED = "discarded", "ทิ้งแล้ว"
        EXPIRED = "expired", "หมดอายุ"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    identity = models.ForeignKey(ChannelIdentity, on_delete=models.PROTECT, related_name="drafts")
    requester = models.ForeignKey(Requester, on_delete=models.PROTECT, related_name="drafts")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    kind = models.CharField(max_length=16, choices=Case.Kind.choices, blank=True)
    step = models.CharField(max_length=32)
    data = models.JSONField(default=dict, blank=True)
    revision = models.PositiveIntegerField(default=1)
    # Expiry is told to the user once, at their next message (never pushed).
    expiry_notified = models.BooleanField(default=False)
    case = models.OneToOneField(
        Case, null=True, blank=True, on_delete=models.PROTECT, related_name="draft"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField()

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.CheckConstraint(
                name="draft_case_iff_submitted",
                condition=Q(status="submitted", case__isnull=False)
                | (~Q(status="submitted") & Q(case__isnull=True)),
            )
        ]


class Conversation(models.Model):
    """Where one LINE identity is in the chat: the active draft and the pending question."""

    identity = models.OneToOneField(
        ChannelIdentity, on_delete=models.PROTECT, related_name="conversation"
    )
    active_draft = models.ForeignKey(
        ConversationDraft, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    prompt = models.CharField(max_length=32, blank=True)
    prompt_data = models.JSONField(default=dict, blank=True)
    reply_choices = models.JSONField(default=dict, blank=True)
    # Case changes after this instant are shown as "มีอัปเดตใหม่" at the next turn (M04).
    updates_seen_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class ConversationMessage(models.Model):
    """Text the user typed, kept verbatim as source material (AI extraction arrives in M05).
    `case`: the requester's answer to staff who asked for more information on that case.
    `requester`: who the user was when typing it (anonymous included), so the requester page
    never shows what the same LINE account typed while reporting anonymously (ADR 0010)."""

    identity = models.ForeignKey(ChannelIdentity, on_delete=models.PROTECT, related_name="+")
    requester = models.ForeignKey(
        Requester, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    inbox = models.OneToOneField(WebhookInbox, on_delete=models.PROTECT, related_name="+")
    draft = models.ForeignKey(
        ConversationDraft, null=True, blank=True, on_delete=models.PROTECT, related_name="messages"
    )
    case = models.ForeignKey(
        "Case", null=True, blank=True, on_delete=models.PROTECT, related_name="info_replies"
    )
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)


class PostbackToken(models.Model):
    """A chat button: bound to one identity, optionally a draft revision, and an expiry."""

    token = models.CharField(max_length=32, unique=True)
    identity = models.ForeignKey(ChannelIdentity, on_delete=models.CASCADE, related_name="+")
    draft = models.ForeignKey(
        ConversationDraft, null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    draft_revision = models.PositiveIntegerField(null=True, blank=True)
    action = models.CharField(max_length=32)
    value = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)


class Attachment(models.Model):
    """A file kept private and quarantined until scanned: from LINE (fetched via the content
    API) or uploaded by staff on the case page (M10 Q11, never sent to the requester)."""

    class ScanStatus(models.TextChoices):
        PENDING_FETCH = "pending_fetch", "รอดึงไฟล์"
        PENDING_SCAN = "pending_scan", "รอสแกน"
        CLEAN = "clean", "ผ่าน"
        INFECTED = "infected", "พบมัลแวร์"
        REJECTED = "rejected", "ไม่รับไฟล์"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    identity = models.ForeignKey(
        ChannelIdentity, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    inbox = models.OneToOneField(
        WebhookInbox, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    line_message_id = models.CharField(max_length=64, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    note = models.CharField(max_length=200, blank=True)
    draft = models.ForeignKey(
        ConversationDraft,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="attachments",
    )
    case = models.ForeignKey(
        Case, null=True, blank=True, on_delete=models.PROTECT, related_name="attachments"
    )
    # M11 Q3/Q13: evidence of the registry document behind a manual_reference fact.
    registry_record = models.ForeignKey(
        "RegistryRecord", null=True, blank=True, on_delete=models.PROTECT, related_name="evidence"
    )
    work_order = models.ForeignKey(
        "WorkOrder", null=True, blank=True, on_delete=models.PROTECT, related_name="attachments"
    )
    declared_name = models.CharField(max_length=200, blank=True)
    content_type = models.CharField(max_length=64, blank=True)
    size = models.PositiveIntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    storage_name = models.CharField(max_length=64, blank=True)
    scan_status = models.CharField(
        max_length=16, choices=ScanStatus.choices, default=ScanStatus.PENDING_FETCH
    )
    scan_detail = models.CharField(max_length=200, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    scanned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["scan_status", "created_at"])]
        constraints = [
            models.CheckConstraint(
                name="attachment_one_source",
                condition=(
                    Q(identity__isnull=False, inbox__isnull=False, uploaded_by__isnull=True)
                    # Staff uploads belong to a case or a งานสั่ง (16 Oct 2569 Q14).
                    | (
                        Q(identity__isnull=True, inbox__isnull=True, uploaded_by__isnull=False)
                        & (Q(case__isnull=False) | Q(work_order__isnull=False))
                    )
                ),
            )
        ]


# --- M04: delivery and message budget -------------------------------------------------------------


class NotificationDelivery(models.Model):
    """One notification to one recipient and its real outcome (never assumed delivered)."""

    class Channel(models.TextChoices):
        LINE_PUSH = "line_push", "LINE push"
        EMAIL = "email", "อีเมล (ยังไม่เปิดใช้)"

    class Level(models.TextChoices):
        CRITICAL = "critical", "สำคัญ"
        NORMAL = "normal", "ปกติ"

    class Status(models.TextChoices):
        PLANNED = "planned", "รอส่ง"
        SENT = "sent", "LINE รับแล้ว"
        UNKNOWN = "unknown", "ไม่ทราบผล"
        FAILED = "failed", "ส่งไม่สำเร็จ"
        DEFERRED = "deferred", "เลื่อน (งบข้อความ)"
        SKIPPED = "skipped", "ไม่ส่ง"

    channel = models.CharField(max_length=16, choices=Channel.choices, default=Channel.LINE_PUSH)
    kind = models.CharField(max_length=32)
    level = models.CharField(max_length=16, choices=Level.choices)
    identity = models.ForeignKey(
        ChannelIdentity, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    email = models.EmailField(blank=True)
    # M10: the staff member an email is for, re-checked (active, still in scope) when sending.
    recipient_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    case = models.ForeignKey(
        Case, null=True, blank=True, on_delete=models.PROTECT, related_name="deliveries"
    )
    registration = models.ForeignKey(
        RegistrationRequest, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    work_order = models.ForeignKey(
        "WorkOrder", null=True, blank=True, on_delete=models.PROTECT, related_name="deliveries"
    )
    text = models.TextField()
    # The domain event that caused it: replanning the same event never notifies twice.
    source_event = models.UUIDField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PLANNED)
    send_after = models.DateTimeField()
    # Pushes sharing one request (debounce batch) share the leader's retry key and messages.
    batch_leader = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    retry_key = models.UUIDField(default=uuid.uuid4)
    # A prepared LINE message (e.g. a receipt card with buttons); plain `text` otherwise.
    message = models.JSONField(default=dict, blank=True)
    messages = models.JSONField(default=list, blank=True)
    counted = models.PositiveSmallIntegerField(default=0)
    tier = models.PositiveSmallIntegerField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    detail = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["identity", "status", "send_after"]),
            models.Index(fields=["status", "sent_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["source_event", "kind"],
                condition=Q(source_event__isnull=False),
                name="delivery_once_per_source_event",
            )
        ]


class LineQuotaSnapshot(models.Model):
    """Monthly quota and usage reported by LINE, reconciled hourly; one row per month."""

    month = models.CharField(max_length=7, unique=True)
    quota = models.PositiveIntegerField(null=True, blank=True)
    usage = models.PositiveIntegerField(default=0)
    limit_reached = models.BooleanField(default=False)
    alerted_levels = models.JSONField(default=list, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)


# --- M05: AI provider configuration and runs ----------------------------------------------------


class AICredential(models.Model):
    """ผู้ให้บริการ AI (ADR 0038): endpoint, API key and the defaults its models get. The key is
    Fernet-encrypted and never shown again; a provider may wait without a key."""

    name = models.CharField(max_length=64, unique=True)
    label = models.CharField(max_length=100, blank=True)
    base_url = models.URLField(max_length=200)
    encrypted_key = models.TextField(blank=True)
    key_hint = models.CharField(max_length=8, blank=True)
    key_url = models.URLField(max_length=200, blank=True)
    notice = models.CharField(max_length=200, blank=True)
    # Replaces AI_SCHEMA_ENFORCED_HOSTS: the provider enforces response_format json_schema.
    enforces_schema = models.BooleanField(default=False, db_default=False)
    # timeout_seconds, max_tokens and temperature for the profiles of this provider's models.
    defaults = models.JSONField(default=dict, blank=True)
    is_hidden = models.BooleanField(default=False, db_default=False)
    checked_at = models.DateTimeField(null=True, blank=True)
    check_ok = models.BooleanField(null=True, blank=True)
    check_detail = models.CharField(max_length=100, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    rotated_at = models.DateTimeField(null=True, blank=True)

    @property
    def has_key(self):
        return bool(self.encrypted_key)

    @property
    def title(self):
        return self.label or self.name

    def __str__(self):
        return f"{self.title} (••••{self.key_hint})" if self.has_key else self.title


class AIModel(models.Model):
    """A model a provider offers, as the admin page lists it (ADR 0038 Q6). Its call settings
    live in AIProfile rows; a model that ever served in a version is hidden, never deleted."""

    provider = models.ForeignKey(AICredential, on_delete=models.PROTECT, related_name="models")
    model_id = models.CharField(max_length=100)
    label = models.CharField(max_length=100, blank=True)
    position = models.PositiveSmallIntegerField(default=0)
    is_hidden = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["provider_id", "position", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "model_id"], name="ai_model_once_per_provider"
            )
        ]

    @property
    def title(self):
        return self.label or self.model_id

    def __str__(self):
        return self.title


class AIProfile(AppendOnlyModel):
    """One model with its call limits. Changing anything means a new profile row."""

    credential = models.ForeignKey(AICredential, on_delete=models.PROTECT, related_name="profiles")
    model_id = models.CharField(max_length=100)
    timeout_seconds = models.PositiveSmallIntegerField(default=10)
    max_tokens = models.PositiveSmallIntegerField(default=800)
    temperature = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal("0.10"))
    use_json_schema = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                name="ai_profile_timeout_bounds",
                condition=Q(timeout_seconds__gte=2, timeout_seconds__lte=30),
            ),
            models.CheckConstraint(
                name="ai_profile_tokens_bounds",
                condition=Q(max_tokens__gte=64, max_tokens__lte=2048),
            ),
            models.CheckConstraint(
                name="ai_profile_temperature_bounds",
                condition=Q(temperature__gte=0, temperature__lte=1),
            ),
        ]

    def __str__(self):
        return f"#{self.pk} {self.model_id}"


class AIConfigVersion(models.Model):
    """Primary model plus ordered fallbacks; one version per AI task is active at a time."""

    class Task(models.TextChoices):
        EXTRACT = "extract", "สกัดร่างจากแชต"
        TRIAGE = "triage", "เสนอข้อเท็จจริงเพื่อคัดกรองเรื่อง"
        ANSWER = "answer", "ตอบแชตผู้ใช้ LINE OA จากคลังคำตอบ"
        ROUTING = "routing", "เสนอกองจากหน้าที่ของกอง"
        KEYWORDS = "keywords", "ยกคำสำคัญให้ระบบจำ"
        SHEET_STATUS = "sheet_status", "เรียบเรียงสถานะจากชีท"

    class Status(models.TextChoices):
        CANDIDATE = "candidate", "รอทดสอบ/ประเมิน"
        ACTIVE = "active", "ใช้งานอยู่"
        RETIRED = "retired", "เลิกใช้"
        REJECTED = "rejected", "ไม่ผ่าน"

    class Pipeline(models.TextChoices):
        SINGLE = "single", "เรียกครั้งเดียว (extract-v1)"
        HARNESS = "harness", "Harness (กรองคำสั่งแฝง + ตัวตรวจ + แก้ 1 รอบ + hedge)"

    number = models.PositiveIntegerField(unique=True)
    # ADR 0038: the CalVer label (2026.10.31.1) shared by one version per task, made on the
    # evaluating machine and kept on import. Blank for the earlier per-task versions.
    release = models.CharField(max_length=16, blank=True, default="", db_index=True)
    # Each task has its own evaluation gate and its own active version (ADR 0002).
    task = models.CharField(max_length=16, choices=Task.choices, default=Task.EXTRACT)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.CANDIDATE)
    # How many similar reviewed examples are retrieved into the prompt (0 = none).
    few_shot = models.PositiveSmallIntegerField(default=0)
    # What runs around the models (operations/ai_harness.py); part of what is evaluated.
    pipeline = models.CharField(max_length=16, choices=Pipeline.choices, default=Pipeline.SINGLE)
    # Harness only: the model that labels message segments before extraction (ai_screen.py);
    # empty = the primary. It only removes text; it never extracts or decides anything.
    screen_profile = models.ForeignKey(
        AIProfile, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # Extraction only (user, 23 Oct 2569): also read the SAP slip number and the typed fields
    # of the equipment-first groups (typed_lines). Older versions keep the prompt they passed.
    typed_fields = models.BooleanField(default=False, db_default=False)
    # Also reads where a message belongs on the LINE menu (owner, 25 Oct 2569 Q9): needs
    # typed_fields; evaluated on menu-v1 as well.
    menu_fields = models.BooleanField(default=False, db_default=False)
    # ADR 0032 Q21: the LINE agent also knows what DED Center does and does not do (it follows
    # slips issued in SAP, never requisitions, has no stock); needs menu_fields; dialogue-v2.
    slip_facts = models.BooleanField(default=False, db_default=False)
    profiles = models.ManyToManyField(AIProfile, through="AIConfigEntry", related_name="+")
    test_passed = models.BooleanField(null=True)
    test_result = models.JSONField(default=dict, blank=True)
    eval_passed = models.BooleanField(null=True)
    eval_result = models.JSONField(default=dict, blank=True)
    note = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    activated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    activated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(
                fields=["task"], condition=Q(status="active"), name="ai_single_active_per_task"
            ),
            models.CheckConstraint(name="ai_few_shot_bounds", condition=Q(few_shot__lte=5)),
            models.UniqueConstraint(
                fields=["release", "task"],
                condition=~Q(release=""),
                name="ai_one_version_per_task_per_release",
            ),
        ]

    def chain(self):
        return [entry.profile for entry in self.entries.select_related("profile__credential")]

    @property
    def label(self):
        return self.release or f"v{self.number}"

    def __str__(self):
        return self.label


class AIConfigEntry(models.Model):
    version = models.ForeignKey(AIConfigVersion, on_delete=models.CASCADE, related_name="entries")
    position = models.PositiveSmallIntegerField()  # 0 = primary, then fallbacks in order
    profile = models.ForeignKey(AIProfile, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["version", "position"], name="ai_entry_position"),
            models.UniqueConstraint(fields=["version", "profile"], name="ai_entry_profile_once"),
        ]


class AIRun(models.Model):
    """One AI extraction for one chat message, tied to the config version it started with."""

    class Status(models.TextChoices):
        PENDING = "pending", "รอ"
        RUNNING = "running", "กำลังเรียก AI"
        APPLIED = "applied", "เติมร่างแล้ว"
        FALLBACK = "fallback", "ถามทีละช่องแทน (เวลาไม่พอ)"
        FAILED = "failed", "AI ใช้ไม่ได้ ถามทีละช่องแทน"
        STALE = "stale", "ร่างเปลี่ยนระหว่างรอ"
        ANSWERED = "answered", "ตอบคำถามจากคลังคำตอบ (ADR 0012)"
        TAKEN = "taken", "ขั้นที่ให้พิมพ์รับข้อความไว้ (ADR 0012)"

    identity = models.ForeignKey(ChannelIdentity, on_delete=models.PROTECT, related_name="+")
    inbox = models.OneToOneField(WebhookInbox, on_delete=models.PROTECT, related_name="ai_run")
    draft = models.ForeignKey(
        ConversationDraft, null=True, blank=True, on_delete=models.PROTECT, related_name="ai_runs"
    )
    draft_revision = models.PositiveIntegerField(null=True, blank=True)
    config_version = models.ForeignKey(AIConfigVersion, on_delete=models.PROTECT, related_name="+")
    prompt_version = models.CharField(max_length=32)
    schema_version = models.CharField(max_length=32)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    attempts = models.JSONField(default=list, blank=True)
    result = models.JSONField(default=dict, blank=True)
    corrections = models.PositiveSmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["status", "created_at"])]


class AIEvalJob(models.Model):
    """A held-out evaluation requested from the web; the worker runs it off the chat path."""

    class Status(models.TextChoices):
        QUEUED = "queued", "รอคิว"
        RUNNING = "running", "กำลังประเมิน"
        DONE = "done", "เสร็จ"
        FAILED = "failed", "ล้มเหลว"
        WAITING = "waiting", "รอโควตา"

    version = models.ForeignKey(AIConfigVersion, on_delete=models.PROTECT, related_name="eval_jobs")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    passed = models.BooleanField(null=True)
    error = models.CharField(max_length=200, blank=True)
    # A waiting job (the provider answered HTTP 429) runs again from this time (ADR 0038 Q20).
    retry_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["version"],
                condition=Q(status__in=["queued", "running", "waiting"]),
                name="ai_one_open_eval_per_version",
            )
        ]


class AIExample(models.Model):
    """A reviewed message → extraction pair, retrieved by similarity as a prompt example."""

    text = models.TextField(unique=True)
    draft = models.JSONField(null=True, blank=True)
    requests = models.JSONField(default=list)
    source = models.CharField(max_length=64)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)


# --- M06: routing, priority and authority -------------------------------------------------------


class Priority(models.TextChoices):
    """Proposed prototype levels (B.4), not official service levels. Empty = รอคัดกรอง."""

    P0 = "P0", "P0 ต้องคัดกรองทันที"
    P1 = "P1", "P1 เร่งด่วน"
    P2 = "P2", "P2 ตามกำหนดงาน"
    P3 = "P3", "P3 งานปกติ"


class PolicyVersion(models.Model):
    """One imported rule set (B.3-B.4). The document never changes after import; only the
    status does. Exactly one version is active; an active version outside its dates applies
    no rules (every case goes to people, R10)."""

    class Status(models.TextChoices):
        IMPORTED = "imported", "นำเข้าแล้ว (ยังไม่ใช้)"
        ACTIVE = "active", "ใช้งานอยู่"
        RETIRED = "retired", "เลิกใช้"

    FROZEN = ("code", "document", "digest", "is_synthetic", "effective_from", "effective_until")

    code = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.IMPORTED)
    document = models.JSONField()
    digest = models.CharField(max_length=64)
    is_synthetic = models.BooleanField(default=False)
    effective_from = models.DateTimeField()
    effective_until = models.DateTimeField(null=True, blank=True)
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    imported_at = models.DateTimeField(auto_now_add=True)
    # Who approved the rules (the rule owner) and their document; the admin only installs.
    approved_by_name = models.CharField(max_length=200, blank=True)
    approval_reference = models.CharField(max_length=200, blank=True)
    activated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    activated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-imported_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["status"], condition=Q(status="active"), name="policy_single_active"
            ),
            models.CheckConstraint(
                name="policy_dates_ordered",
                condition=Q(effective_until__isnull=True)
                | Q(effective_until__gt=models.F("effective_from")),
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            stored = type(self).objects.filter(pk=self.pk).values(*self.FROZEN).first()
            if stored and any(stored[name] != getattr(self, name) for name in self.FROZEN):
                raise ValueError("An imported policy cannot be edited; import a new version.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Policy versions are kept for the cases that cite them.")

    def __str__(self):
        return self.code


class AuthorityGrant(models.Model):
    """Authority for one activity (B.2): who may decide what, separate from duties (RoleGrant).

    A role never implies authority. Scope narrows a grant: an empty division means every
    division; category/item narrow M07 activities, and max_quantity caps the approved quantity
    of one line, so it needs an item (units differ within a category, M07 Q2).
    """

    class Activity(models.TextChoices):
        PRIORITY_EXCEPTION = "priority_exception", "ข้อยกเว้นระดับความเร่งด่วน"
        ROUTING_DECISION = "routing_decision", "ตัดสินเส้นทาง"
        WITHDRAWAL_APPROVAL = "withdrawal_approval", "รับเรื่องของกอง"
        ALLOCATION = "allocation", "จัดสรรของขาด"
        CONDITION_CERTIFICATION = "condition_certification", "รับรองสภาพ"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="authority_grants"
    )
    activity = models.CharField(max_length=32, choices=Activity.choices)
    division = models.ForeignKey(Division, null=True, blank=True, on_delete=models.PROTECT)
    category = models.CharField(max_length=16, choices=EquipmentType.Category.choices, blank=True)
    item = models.ForeignKey(EquipmentType, null=True, blank=True, on_delete=models.PROTECT)
    max_quantity = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    valid_from = models.DateTimeField()
    valid_until = models.DateTimeField(null=True, blank=True)
    reason = models.TextField()
    # The approver or document behind the grant (G02 replaces synthetic grants).
    approval_reference = models.CharField(max_length=200)
    is_synthetic = models.BooleanField(default=False)
    # 16 Oct 2569 Q4: authority that comes with membership of this division. It counts only
    # while the person is still a member and the division's work is not split (ตัวเลือก
    # "หน้าที่ในกอง"); empty for authority an admin granted on its own.
    member_of = models.ForeignKey(
        Division, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # Empty for synthetic demo seeds and ผู้ช่วยเหลือระบบ's own grants (made on the host, ADR 0007);
    # an admin never grants authority to themselves.
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    granted_at = models.DateTimeField(auto_now_add=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoke_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-granted_at"]
        constraints = [
            models.CheckConstraint(
                name="authority_not_self_granted", condition=~Q(granted_by=models.F("user"))
            ),
            models.CheckConstraint(
                name="authority_dates_ordered",
                condition=Q(valid_until__isnull=True) | Q(valid_until__gt=models.F("valid_from")),
            ),
            models.CheckConstraint(
                name="authority_quantity_positive",
                condition=Q(max_quantity__isnull=True) | Q(max_quantity__gt=0),
            ),
            models.CheckConstraint(
                name="authority_quantity_needs_item",
                condition=Q(max_quantity__isnull=True) | Q(item__isnull=False),
            ),
        ]


class AITriageRun(models.Model):
    """AI facts for one case text (ADR 0002). Bound to the input it read, not to the revision:
    decisions that do not change the requester's text keep the facts valid."""

    class Status(models.TextChoices):
        PENDING = "pending", "รอ"
        RUNNING = "running", "กำลังเรียก AI"
        DONE = "done", "ได้ข้อเท็จจริงแล้ว"
        FAILED = "failed", "AI ไม่ตอบ"
        STALE = "stale", "ข้อความในเรื่องเปลี่ยนระหว่างรอ"
        CANCELLED = "cancelled", "หยุดใช้ AI คัดกรองก่อนถึงคิว"

    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="ai_triage_runs")
    case_revision = models.PositiveIntegerField()
    input_digest = models.CharField(max_length=64)
    config_version = models.ForeignKey(AIConfigVersion, on_delete=models.PROTECT, related_name="+")
    prompt_version = models.CharField(max_length=32)
    schema_version = models.CharField(max_length=32)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    attempts = models.JSONField(default=list, blank=True)
    facts = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["case", "input_digest", "config_version"], name="ai_triage_once_per_input"
            )
        ]


class Recommendation(AppendOnlyModel):
    """What the rules propose for one case revision under one policy version (B.7/B.8).

    Never written onto the case: owner and priority change only through people's decisions.
    A recommendation whose revision is older than the case is kept as a stale proposal.
    """

    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="recommendations")
    case_revision = models.PositiveIntegerField()
    policy_version = models.ForeignKey(
        PolicyVersion, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    trigger = models.CharField(max_length=32)
    facts = models.JSONField(default=dict)
    rules = models.JSONField(default=list)
    current_owner = models.ForeignKey(Division, on_delete=models.PROTECT, related_name="+")
    proposed_owner = models.ForeignKey(Division, on_delete=models.PROTECT, related_name="+")
    support_units = models.JSONField(default=list)
    child_work_owners = models.JSONField(default=list)
    suggested_priority = models.CharField(max_length=2, choices=Priority.choices, blank=True)
    priority_floor = models.CharField(max_length=2, choices=Priority.choices, blank=True)
    floor_basis = models.JSONField(default=list)
    missing = models.JSONField(default=list)
    next_action = models.TextField(blank=True)
    notes = models.JSONField(default=list)
    ai_run = models.ForeignKey(
        AITriageRun, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    digest = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["case", "case_revision"])]


class CaseTriage(models.Model):
    """M06 state beside the revisioned case: facts people confirmed, the priority levels kept
    apart (B.4) and the current proposal. Rule-derived values here change without a revision."""

    case = models.OneToOneField(
        Case, primary_key=True, on_delete=models.PROTECT, related_name="triage"
    )
    # name -> {"value", "by", "at", "reason"}; people's facts win over AI and keyword flags.
    staff_facts = models.JSONField(default=dict, blank=True)
    suggested_priority = models.CharField(max_length=2, choices=Priority.choices, blank=True)
    priority_floor = models.CharField(max_length=2, choices=Priority.choices, blank=True)
    confirmed_priority = models.CharField(max_length=2, choices=Priority.choices, blank=True)
    # The owner division when the level was confirmed: a new owner confirms again before
    # completing triage (Q3), while the earlier level keeps the case's place in the queue.
    confirmed_division = models.ForeignKey(
        Division, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # An authority exception waives one floor for one basis: {"floor", "basis", "by", "at", ...}.
    floor_exception = models.JSONField(default=dict, blank=True)
    # What queues use: the higher of confirmed and floor; empty = รอคัดกรอง.
    effective_priority = models.CharField(max_length=2, choices=Priority.choices, blank=True)
    current_recommendation = models.ForeignKey(
        Recommendation, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    policy_change_pending = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)


class Handoff(models.Model):
    """การส่งต่อเรื่อง: the owner stays until the target accepts (B.3)."""

    class Status(models.TextChoices):
        PENDING = "pending", "รอตอบรับ"
        ACCEPTED = "accepted", "ตอบรับแล้ว"
        DECLINED = "declined", "ไม่ตอบรับ"
        WITHDRAWN = "withdrawn", "ปิดเพื่อให้ผู้ประสานตัดสิน"
        ENDED = "ended", "ปิดพร้อมเรื่อง"

    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="handoffs")
    from_division = models.ForeignKey(Division, on_delete=models.PROTECT, related_name="+")
    to_division = models.ForeignKey(Division, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    reason_category = models.CharField(max_length=32, blank=True)
    reason = models.TextField(blank=True)
    recommendation = models.ForeignKey(
        Recommendation, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    requested_at = models.DateTimeField()
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.TextField(blank=True)
    # คำที่ระบบจำ (ADR 0013, Q4, Q8): words ฝอจ. picked from an R10 case; learned on acceptance.
    keywords = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["case"], condition=Q(status="pending"), name="handoff_one_pending_per_case"
            ),
            models.CheckConstraint(
                name="handoff_changes_division",
                condition=~Q(from_division=models.F("to_division")),
            ),
        ]


class RoutingReview(models.Model):
    """A routing decision by a coordinator with authority (การตัดสินเส้นทาง)."""

    class Trigger(models.TextChoices):
        DISPUTE = "dispute", "ข้อโต้แย้งเส้นทาง"
        REQUESTED = "requested", "ขอผู้ประสานตัดสิน"

    class Status(models.TextChoices):
        OPEN = "open", "รอผู้ประสานตัดสิน"
        DECIDED = "decided", "ตัดสินแล้ว"

    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="routing_reviews")
    trigger = models.CharField(max_length=16, choices=Trigger.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    attempted_division = models.ForeignKey(
        Division, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    opened_at = models.DateTimeField()
    reason = models.TextField(blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    chosen_division = models.ForeignKey(
        Division, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    decision_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["case"], condition=Q(status="open"), name="routing_review_one_open_per_case"
            )
        ]


class AckClock(models.Model):
    """Time to respond (B.3/B.4): intake (submitted → triage started) and handoff (requested →
    accepted/declined), and since M09 the work deadlines (กรอบเวลา) of FAOJ's own steps.
    Synthetic durations from the active policy, counted on its service calendar (P0 continuous);
    never an official SLA. Work deadlines stop while the district has the turn or a supervisor
    approved a pause (paused_minutes, B.4); the case's total age keeps running."""

    class Kind(models.TextChoices):
        INTAKE = "intake", "รอเริ่มคัดกรอง"
        HANDOFF = "handoff", "รอตอบรับการส่งต่อ"
        FIRST_RESPONSE = "first_response", "ตรวจครั้งแรก"
        RESOLVE = "resolve", "แก้ไข"
        APPROVAL = "approval", "รอกองรับเรื่อง"
        ISSUE = "issue", "รอจ่าย"
        INSPECTION = "inspection", "รอตรวจของคืน"
        CHILD_ACCEPT = "child_accept", "รอตอบรับงานย่อย"
        CHILD_RESULT = "child_result", "รอผลงานย่อย"

    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="ack_clocks")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    handoff = models.OneToOneField(
        Handoff, null=True, blank=True, on_delete=models.PROTECT, related_name="clock"
    )
    child_work = models.ForeignKey(
        "ChildWork", null=True, blank=True, on_delete=models.PROTECT, related_name="clocks"
    )
    # Business minutes of approved pauses since the start; while paused the sweep skips it.
    paused_minutes = models.PositiveIntegerField(default=0)
    paused_at = models.DateTimeField(null=True, blank=True)
    division = models.ForeignKey(Division, on_delete=models.PROTECT, related_name="+")
    started_at = models.DateTimeField()
    priority = models.CharField(max_length=4, blank=True)
    due_at = models.DateTimeField()
    escalate_at = models.DateTimeField()
    reminded_at = models.DateTimeField(null=True, blank=True)
    escalated_at = models.DateTimeField(null=True, blank=True)
    escalation_held_at = models.DateTimeField(null=True, blank=True)
    hold_reason = models.CharField(max_length=200, blank=True)
    stopped_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["case", "kind"],
                condition=Q(stopped_at__isnull=True, child_work__isnull=True),
                name="ack_clock_one_open_per_kind",
            ),
            models.UniqueConstraint(
                fields=["child_work", "kind"],
                condition=Q(stopped_at__isnull=True, child_work__isnull=False),
                name="ack_clock_one_open_per_child",
            ),
        ]
        indexes = [models.Index(fields=["stopped_at", "due_at"])]

    @property
    def work(self):
        """A FAOJ work deadline (not an answer to a handoff or a child request)."""
        return self.kind not in {self.Kind.INTAKE, self.Kind.HANDOFF, self.Kind.CHILD_ACCEPT}


# --- M07: withdrawal fulfilment and the inventory seam (ADR 0003) ------------------------------


class Delivery(models.Model):
    """การส่งมอบ: goods that really left the store once (Q5); the unit the district confirms."""

    class Status(models.TextChoices):
        AWAITING = "awaiting", "รอยืนยันรับ"
        CONFIRMED = "confirmed", "ยืนยันรับครบ"
        MISMATCH = "mismatch", "แจ้งไม่ตรง"
        RESOLVED = "resolved", "ปิดส่วนต่างแล้ว"
        UNCONFIRMED = "unconfirmed", "ปิดโดยผู้แจ้งไม่ตอบ"

    class Mode(models.TextChoices):
        FAKE = "fake", "คลังจำลอง"
        MANUAL = "manual_reference", "อ้างเลขเอกสาร"

    class Resolution(models.TextChoices):
        ISSUE_ERROR = "issue_error", "บันทึกจ่ายผิด"
        LOST = "lost", "ของหายหรือเสียหายระหว่างส่ง"

    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="deliveries_out")
    seq = models.PositiveSmallIntegerField()
    mode = models.CharField(max_length=16, choices=Mode.choices)
    document_number = models.CharField(max_length=64)
    recipient_name = models.CharField(max_length=200)
    note = models.TextField(blank=True)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    issued_at = models.DateTimeField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.AWAITING)
    # Manual mode only: recorded although the imported figures looked short (reconcile, M11).
    stock_flag = models.CharField(max_length=200, blank=True)
    resolution = models.CharField(max_length=16, choices=Resolution.choices, blank=True)
    resolution_reason = models.TextField(blank=True)
    resolution_reference = models.CharField(max_length=100, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["case", "seq"]
        constraints = [
            models.UniqueConstraint(fields=["case", "seq"], name="delivery_unique_seq"),
            models.CheckConstraint(
                name="delivery_resolution_when_resolved",
                condition=Q(status="resolved", resolution__gt="")
                | (~Q(status="resolved") & Q(resolution="")),
            ),
        ]

    @property
    def settled(self):
        return self.status in {self.Status.CONFIRMED, self.Status.RESOLVED, self.Status.UNCONFIRMED}


class DeliveryLine(models.Model):
    """What one delivery recorded for one line; corrections are ledger rows, never edits."""

    delivery = models.ForeignKey(Delivery, on_delete=models.PROTECT, related_name="lines")
    line = models.ForeignKey(CaseLine, on_delete=models.PROTECT, related_name="delivery_lines")
    quantity = models.DecimalField(max_digits=12, decimal_places=2)
    serials = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["delivery", "line__line_no"]
        constraints = [
            models.UniqueConstraint(fields=["delivery", "line"], name="delivery_line_unique"),
            models.CheckConstraint(name="delivery_line_positive", condition=Q(quantity__gt=0)),
        ]


class FulfilmentEntry(AppendOnlyModel):
    """Line ledger (E.5): each change of a withdrawal counter, who made it and at which revision."""

    class Kind(models.TextChoices):
        APPROVED = "approved", "อนุมัติ"
        ISSUED = "issued", "จ่าย"
        CORRECTED = "corrected", "แก้รายการจ่าย"
        RECEIVED = "received", "ยืนยันรับ"
        TERMINATED = "terminated", "ยุติส่วนค้าง"
        RETURNED = "returned", "รับของคืน"
        RETURN_TERMINATED = "return_terminated", "ยุติส่วนค้างส่งคืน"

    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="+")
    line = models.ForeignKey(CaseLine, on_delete=models.PROTECT, related_name="ledger")
    kind = models.CharField(max_length=24, choices=Kind.choices)
    # A signed change of the counter (corrections and recounts may be negative).
    quantity = models.DecimalField(max_digits=12, decimal_places=2)
    serials = models.JSONField(default=list, blank=True)
    delivery = models.ForeignKey(
        Delivery, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    actor_label = models.CharField(max_length=64)
    case_revision = models.PositiveIntegerField()
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


class Receipt(AppendOnlyModel):
    """การยืนยันรับของ for one delivery. A later receipt (a recount) supersedes; history stays."""

    class Result(models.TextChoices):
        FULL = "full", "ได้รับครบ"
        MISMATCH = "mismatch", "ไม่ตรง"

    delivery = models.ForeignKey(Delivery, on_delete=models.PROTECT, related_name="receipts")
    result = models.CharField(max_length=16, choices=Result.choices)
    # [{"line_no": 1, "quantity": "2", "serials": [...]}]: what the district really got.
    lines = models.JSONField(default=list)
    requester = models.ForeignKey(
        Requester, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # Intake staff recording the district's answer on its behalf (with channel and note, Q4).
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    channel = models.CharField(max_length=16, choices=Case.Channel.choices)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                name="receipt_by_requester_or_staff",
                condition=Q(requester__isnull=False, recorded_by__isnull=True)
                | Q(requester__isnull=True, recorded_by__isnull=False),
            )
        ]


class IssuedSerial(models.Model):
    """A serial out with a district: at most one active issue per item and serial (Q11)."""

    item = models.ForeignKey(EquipmentType, on_delete=models.PROTECT, related_name="+")
    serial = models.CharField(max_length=64)
    delivery_line = models.ForeignKey(
        DeliveryLine, on_delete=models.PROTECT, related_name="issued_serials"
    )
    active = models.BooleanField(default=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(max_length=100, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["item", "serial"],
                condition=Q(active=True),
                name="issued_serial_one_active",
            )
        ]


class Allocation(models.Model):
    """การจัดสรร (Q8/Q20): how much of short stock one case line may receive."""

    class End(models.TextChoices):
        USED = "used", "จ่ายครบตามที่จัดสรร"
        REPLACED = "replaced", "จัดสรรใหม่"
        VOID_STOCK = "void_stock", "ยอดลดต่ำกว่าที่จัดสรร"
        CASE_ENDED = "case_ended", "ไม่มีส่วนค้างแล้ว"

    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="allocations")
    line = models.ForeignKey(CaseLine, on_delete=models.PROTECT, related_name="allocations")
    item = models.ForeignKey(EquipmentType, on_delete=models.PROTECT, related_name="+")
    quantity = models.DecimalField(max_digits=12, decimal_places=2)
    used_quantity = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    reason = models.TextField()
    allocated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    grant = models.ForeignKey(AuthorityGrant, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(max_length=16, choices=End.choices, blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                name="allocation_used_within_quantity",
                condition=Q(quantity__gt=0, used_quantity__gte=0)
                & Q(used_quantity__lte=models.F("quantity")),
            ),
            models.UniqueConstraint(
                fields=["line"], condition=Q(ended_at__isnull=True), name="allocation_one_active"
            ),
        ]

    @property
    def remaining(self):
        return self.quantity - self.used_quantity


class FakeStock(models.Model):
    """คลังจำลอง (Q1): stands in for the registry outside production; synthetic numbers only."""

    item = models.OneToOneField(EquipmentType, on_delete=models.PROTECT, related_name="fake_stock")
    available = models.DecimalField(max_digits=12, decimal_places=2)
    reserved = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    awaiting_inspection = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    awaiting_repair = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["item__code"]
        constraints = [
            models.CheckConstraint(
                name="fake_stock_not_negative",
                condition=Q(available__gte=0, reserved__gte=0)
                & Q(awaiting_inspection__gte=0, awaiting_repair__gte=0),
            )
        ]


class FakeSerial(models.Model):
    """A serial in the fake inventory and where it is now."""

    class Status(models.TextChoices):
        AVAILABLE = "available", "พร้อมจ่าย"
        RESERVED = "reserved", "กันไว้ในระบบเดิม"
        ISSUED = "issued", "จ่ายแล้ว"
        AWAITING_INSPECTION = "awaiting_inspection", "รอตรวจ"
        AWAITING_REPAIR = "awaiting_repair", "รอซ่อม"
        DISPOSED = "disposed", "ออกจากคลังแล้ว"

    item = models.ForeignKey(EquipmentType, on_delete=models.PROTECT, related_name="+")
    serial = models.CharField(max_length=64)
    status = models.CharField(max_length=24, choices=Status.choices)
    case = models.ForeignKey(
        Case, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["item__code", "serial"]
        constraints = [
            models.UniqueConstraint(fields=["item", "serial"], name="fake_serial_unique")
        ]


class StockMovement(AppendOnlyModel):
    """Every change of the fake inventory, with a reason (adjustments mirror registry fixes)."""

    class Kind(models.TextChoices):
        SEED = "seed", "ตั้งต้นข้อมูลจำลอง"
        ISSUE = "issue", "จ่าย"
        ADJUST = "adjust", "ปรับยอด"
        RECEIVE = "receive", "รับของคืนรอตรวจ"
        INSPECT = "inspect", "ผลตรวจสภาพ"
        RETURN_OUT = "return_out", "ส่งกลับหน่วยงาน (เขต/ฝ่าย)"

    item = models.ForeignKey(EquipmentType, on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    quantity = models.DecimalField(max_digits=12, decimal_places=2)
    serials = models.JSONField(default=list, blank=True)
    document_number = models.CharField(max_length=64, blank=True)
    case = models.ForeignKey(
        Case, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    actor_label = models.CharField(max_length=64, blank=True)
    reason = models.TextField(blank=True)
    # Bucket changes besides "available" (M08), e.g. {"awaiting_inspection": "1"}.
    changes = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


class StockSnapshot(AppendOnlyModel):
    """Registry figures imported for manual_reference mode (Q10): advisory, never official here."""

    item = models.ForeignKey(EquipmentType, on_delete=models.PROTECT, related_name="+")
    available = models.DecimalField(max_digits=12, decimal_places=2)
    reserved = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    awaiting_inspection = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    awaiting_repair = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    total_on_hand = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    as_of = models.DateTimeField()
    source = models.CharField(max_length=200)
    is_synthetic = models.BooleanField(default=False)
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["item", "-as_of", "-id"]
        indexes = [models.Index(fields=["item", "as_of"])]
        constraints = [
            models.CheckConstraint(
                name="snapshot_available_not_negative", condition=Q(available__gte=0)
            )
        ]


# --- M08: returns, inspection and discrepancies ---------------------------------------------------


class ReturnArrival(models.Model):
    """การรับของคืน: what physically arrived once (Q2), counted by a receiver."""

    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="arrivals")
    seq = models.PositiveSmallIntegerField()
    mode = models.CharField(max_length=16, choices=Delivery.Mode.choices)
    document_number = models.CharField(max_length=64, blank=True)
    carrier_name = models.CharField(max_length=200)
    note = models.TextField(blank=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    received_at = models.DateTimeField()

    class Meta:
        ordering = ["case", "seq"]
        constraints = [
            models.UniqueConstraint(fields=["case", "seq"], name="return_arrival_unique_seq")
        ]


class ReturnUnit(models.Model):
    """One inspectable unit: a serial, or a counted quantity of one item in one arrival (Q9).

    A unit outside the request (ส่วนต่างการรับคืน, Q3) counts for no line and cannot be judged
    until a person records what it is (Q10).
    """

    class Status(models.TextChoices):
        AWAITING = "awaiting", "รอตรวจ"
        FINAL = "final", "ตรวจแล้ว"
        CONFLICT = "conflict", "ผลตรวจขัดกัน"
        SENT_BACK = "sent_back", "ส่งกลับหน่วยงาน (เขต/ฝ่าย)"

    class Discrepancy(models.TextChoices):
        OUTSIDE_REQUEST = "outside_request", "ไม่อยู่ในคำขอ"
        EXCESS = "excess", "จำนวนเกินคำขอ"
        DUPLICATE_STORE = "duplicate_store", "เลขซ้ำกับของในคลัง"
        DUPLICATE_CASE = "duplicate_case", "เลขซ้ำกับของที่รับไว้แล้ว"
        OTHER_DISTRICT = "other_district", "จ่ายให้หน่วยงาน (เขต/ฝ่าย)อื่น"

    class Resolution(models.TextChoices):
        ACCEPTED = "accepted", "นับเป็นของคืนของเรื่องนี้"
        SENT_BACK = "sent_back", "ไม่รับ ส่งกลับหน่วยงาน (เขต/ฝ่าย)"
        REGISTRY_ADJUSTED = "registry_adjusted", "ปรับในทะเบียนแล้ว"

    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="return_units")
    seq = models.PositiveSmallIntegerField()
    arrival = models.ForeignKey(ReturnArrival, on_delete=models.PROTECT, related_name="units")
    line = models.ForeignKey(
        CaseLine, null=True, blank=True, on_delete=models.PROTECT, related_name="return_units"
    )
    item = models.ForeignKey(EquipmentType, on_delete=models.PROTECT, related_name="+")
    serial = models.CharField(max_length=64, blank=True)
    quantity = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.AWAITING)
    discrepancy = models.CharField(max_length=24, choices=Discrepancy.choices, blank=True)
    discrepancy_note = models.CharField(max_length=200, blank=True)
    resolution = models.CharField(max_length=24, choices=Resolution.choices, blank=True)
    resolution_reason = models.TextField(blank=True)
    resolution_reference = models.CharField(max_length=100, blank=True)
    replaces_serial = models.CharField(max_length=64, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    # The issue of this serial by this system that the arrival closed (Q4), if any.
    issued_serial = models.ForeignKey(
        IssuedSerial, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    final_split = models.JSONField(default=dict, blank=True)
    # Where the fake store holds this unit now, e.g. {"awaiting_inspection": "1"}; empty = not held.
    placed = models.JSONField(default=dict, blank=True)
    conflict_note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["case", "seq"]
        constraints = [
            models.UniqueConstraint(fields=["case", "seq"], name="return_unit_unique_seq"),
            models.CheckConstraint(name="return_unit_positive", condition=Q(quantity__gt=0)),
            models.CheckConstraint(
                name="return_unit_resolution_needs_discrepancy",
                condition=Q(resolution="") | ~Q(discrepancy=""),
            ),
        ]

    @property
    def open_discrepancy(self):
        return bool(self.discrepancy) and not self.resolution

    @property
    def counted(self):
        """Counts as returned for its line (matching, or a discrepancy a person accepted)."""
        if self.line_id is None or self.status == self.Status.SENT_BACK:
            return False
        return not self.discrepancy or self.resolution in {
            self.Resolution.ACCEPTED,
            self.Resolution.REGISTRY_ADJUSTED,
        }


class Inspection(AppendOnlyModel):
    """การตรวจสภาพ of one unit: a split {outcome: quantity}; never overwritten (Q9)."""

    class Kind(models.TextChoices):
        INSPECT = "inspect", "ผลตรวจ"
        DECIDE = "decide", "ตัดสินผลตรวจขัดกัน"

    unit = models.ForeignKey(ReturnUnit, on_delete=models.PROTECT, related_name="inspections")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    split = models.JSONField()
    reason = models.TextField(blank=True)
    reference = models.CharField(max_length=100, blank=True)
    inspector = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    grant = models.ForeignKey(
        AuthorityGrant, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


# --- M09: problems, child work and deadline pauses ----------------------------------------------


class ChildWork(models.Model):
    """งานย่อย (M09 Q6): part of a case given to another division; the case keeps its owner.

    For R09 withdrawals and returns the child work names the lines it covers; once accepted,
    those lines are handled by the child division (ADR 0004).
    """

    class Status(models.TextChoices):
        PENDING = "pending", "รอตอบรับ"
        DISPUTED = "disputed", "รอผู้ประสานตัดสิน"
        ACCEPTED = "accepted", "รับแล้ว"
        DECLINED = "declined", "ไม่รับ"
        DONE = "done", "เสร็จ"
        FAILED = "failed", "ทำไม่ได้"
        CANCELLED = "cancelled", "ยกเลิก"

    OPEN = ("pending", "disputed", "accepted")

    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="child_works")
    seq = models.PositiveSmallIntegerField()
    division = models.ForeignKey(Division, on_delete=models.PROTECT, related_name="+")
    rule = models.CharField(max_length=8, blank=True)
    task = models.TextField()
    deliverable = models.TextField(blank=True)
    lines = models.ManyToManyField(CaseLine, blank=True, related_name="child_works")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    requested_at = models.DateTimeField()
    answered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    answered_at = models.DateTimeField(null=True, blank=True)
    answer_reason = models.TextField(blank=True)
    result_summary = models.TextField(blank=True)
    finished_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["case", "seq"]
        constraints = [
            models.UniqueConstraint(fields=["case", "seq"], name="child_work_unique_seq"),
            models.UniqueConstraint(
                fields=["case", "division"],
                condition=Q(status__in=["pending", "disputed", "accepted"]),
                name="child_work_one_open_per_division",
            ),
        ]

    @property
    def is_open(self):
        return self.status in self.OPEN


class DeadlinePause(models.Model):
    """A stop of the case's work deadlines (B.4, M09 Q12): the district's turn while waiting for
    information, or a pause a supervisor approved while the case is on hold."""

    class Kind(models.TextChoices):
        AWAITING_INFO = "awaiting_info", "รอข้อมูลจากหน่วยงาน (เขต/ฝ่าย)"
        SUPERVISOR = "supervisor", "Supervisor อนุมัติพัก"

    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="deadline_pauses")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    reason = models.TextField(blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["case", "started_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["case"], condition=Q(ended_at__isnull=True), name="deadline_one_open_pause"
            )
        ]


class ProblemResolution(AppendOnlyModel):
    """ผลการแก้ปัญหา (M09 Q1/Q15): what the owner division did, for the requester to accept."""

    class Category(models.TextChoices):
        FIXED = "fixed", "แก้แล้ว"
        NO_FAULT = "no_fault", "ตรวจแล้วไม่พบปัญหา"
        EXTERNAL = "external", "ส่งหน่วยงานนอก ฝอจ. ตามแผนที่รับรอง"
        NEEDS_CASE = "needs_case", "ต้องมีเรื่องเบิกหรือคืนแยก"
        # ข้อเสนอแนะ (10 Oct 2569 Q1): the answer to a suggestion, not a repair.
        CONSIDERED = "considered", "รับไว้พิจารณา"
        ADOPTED = "adopted", "นำไปปรับปรุงแล้ว"
        NOT_FEASIBLE = "not_feasible", "ยังทำไม่ได้"

    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="resolutions")
    category = models.CharField(max_length=16, choices=Category.choices)
    # Shown to the requester in LINE; the internal note stays with staff (Q15).
    message = models.TextField()
    internal_note = models.TextField(blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


# --- M11: registry (SAP) reconciliation and external operations --------------------------------


class StaffLinkCode(models.Model):
    """A one-time code from บัญชีของฉัน that links a LINE account to this staff account (ADR
    0010). Six digits, ten minutes, used once; only its hash is stored."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="line_link_codes"
    )
    code_hash = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Employee(models.Model):
    """ทะเบียนพนักงาน for identity checks (10 Oct 2569 Q1, Q3), imported by an admin. The
    proof value (e.g. a birth date) is kept only as a keyed hash, never in plain text.

    15 ต.ค. 2569 (Q10, Q16): the organisation's roster columns. `district` is the ฝ่าย as a
    organisational unit (Q13); the e-mail is kept twice and never in plain text: a hash to compare
    what someone types, and sealed with the server's PII key to send a one-time code. Phone
    columns are never read. 23 ต.ค. 2569: names are not read either; a verified employee is
    shown by the employee ID (employees.label)."""

    employee_id = models.CharField(max_length=32, unique=True)
    # Emptied and never read or written since 23 ต.ค. 2569; kept one release so the previous
    # release still runs during a deploy, then dropped.
    name = models.CharField(max_length=200, blank=True, default="", db_default="")
    district = models.ForeignKey(District, null=True, blank=True, on_delete=models.PROTECT)
    division = models.ForeignKey(Division, null=True, blank=True, on_delete=models.PROTECT)
    position = models.CharField(max_length=200, blank=True)
    line_of_work = models.CharField(max_length=200, blank=True)  # สายงาน
    department = models.CharField(max_length=200, blank=True)  # ฝ่าย as the file says it
    division_name = models.CharField(max_length=200, blank=True)  # กอง
    section_name = models.CharField(max_length=200, blank=True)  # แผนก
    work_group = models.CharField(max_length=200, blank=True)  # กลุ่มงาน
    email_hash = models.CharField(max_length=64, blank=True)
    email_sealed = models.TextField(blank=True)
    check_label = models.CharField(max_length=100, blank=True)
    check_hash = models.CharField(max_length=128, blank=True)
    is_active = models.BooleanField(default=True)
    is_synthetic = models.BooleanField(default=False)
    imported_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["employee_id"]

    def __str__(self):
        return self.employee_id


class AIKeywordRun(models.Model):
    """AI ยกคำสำคัญ (15 ต.ค. 2569 Q9, Q34): up to three words from an unplaced case's masked
    text, offered in ฝอจ.'s handoff form; the AI never chooses the division."""

    class Status(models.TextChoices):
        PENDING = "pending", "รอ"
        DONE = "done", "ได้คำ"
        NONE = "none", "ไม่มีคำที่ใช้ได้"
        FAILED = "failed", "AI ใช้ไม่ได้"
        SKIPPED = "skipped", "ไม่ต้องถามแล้ว"

    case = models.OneToOneField("Case", on_delete=models.PROTECT, related_name="+")
    config_version = models.ForeignKey(AIConfigVersion, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    keywords = models.JSONField(default=list, blank=True)
    attempts = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)


class LineMenuItem(models.Model):
    """เมนู LINE (15 ต.ค. 2569 Q17–Q20, Q32): a tile or a bottom-bar button of the rich menu, or
    a choice inside a tile. Admins change labels, destinations and order with a reason; the
    rich menu on LINE changes only when an admin publishes it."""

    class Place(models.TextChoices):
        TILE = "tile", "ปุ่มใหญ่ของเมนู"
        BAR = "bar", "แถบล่างของเมนู"
        CHOICE = "choice", "ปุ่มย่อยในหัวข้อ"

    class Tab(models.TextChoices):
        # 16 Oct 2569 Q9, Q13, Q15: everyone sees แจ้งเรื่อง; staff who linked LINE also get
        # งานของกอง, and Supervisors สั่งงาน, as tabs of their own rich menu.
        REPORT = "report", "แจ้งเรื่อง"
        GUEST = "guest", "ผู้แจ้งทั่วไป"
        WORK = "work", "งานของกอง"
        ORDER = "order", "สั่งงาน"

    class Action(models.TextChoices):
        REGISTER = "register", "ลงทะเบียนหรือยืนยันตัวตน"
        TOPIC = "topic", "เปิดปุ่มย่อยของหัวข้อนี้"
        TRACK = "track", "ติดตาม: แสดงเรื่องของผู้ใช้ ถ้าไม่มีให้ถามกองโดยตรง"
        RETURN = "return", "เริ่มคำขอคืนส่งถึงกองนี้"
        EQUIPMENT = "equipment", "เลือกอุปกรณ์ แล้วเลือกเบิก ส่งคืน หรือแจ้งปัญหา"
        PROBLEM = "problem", "เริ่มแจ้งปัญหาส่งถึงกองนี้"
        GENERAL = "general", "แจ้งปัญหาหรือข้อเสนอแนะ (กติกาเลือกกอง)"
        NEW = "new", "ส่งเรื่องใหม่ (ผู้ใช้เลือกชนิดเอง)"
        MINE = "mine", "เรื่องของฉัน"
        FIND = "find", "ติดตามด้วยเลขเรื่อง"
        WORK_NEW = "work_new", "งานใหม่ของกอง"
        WORK_MINE = "work_mine", "เรื่องที่มอบให้ฉัน"
        WORK_ORDERS = "work_orders", "งานสั่งที่ได้รับ"
        WORK_DUE = "work_due", "งานใกล้หรือเกินกำหนด"
        WORK_FIND = "work_find", "ค้นหาด้วยเลขเรื่องหรือเลขงาน"
        OPEN_WEB = "open_web", "เปิดเว็บ"
        ORDER_NEW = "order_new", "สั่งงานใหม่"
        ORDER_ISSUED = "order_issued", "งานที่ฉันสั่งและความคืบหน้า"
        ORDER_ASSIGN = "order_assign", "มอบเรื่องที่มีอยู่ให้คนในกอง"
        ORDER_LATE = "order_late", "งานเกินกำหนดทุกกองที่ดูแล"
        ORDER_TODAY = "order_today", "สรุปวันนี้"

    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="choices"
    )
    place = models.CharField(max_length=8, choices=Place.choices)
    tab = models.CharField(max_length=8, choices=Tab.choices, default=Tab.REPORT)
    position = models.PositiveSmallIntegerField(default=0)
    label = models.CharField(max_length=20)  # LINE allows 20 characters on a button
    note = models.CharField(max_length=120, blank=True)  # the tile's second line, or a hint
    action = models.CharField(max_length=16, choices=Action.choices)
    division = models.ForeignKey(
        "Division", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    kind = models.CharField(max_length=16, blank=True)  # which own cases "track" lists
    topic = models.CharField(max_length=16, blank=True)  # the problem topic a choice starts
    is_active = models.BooleanField(default=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tab", "place", "position", "pk"]


class EntryField(models.Model):
    """ช่องที่ผู้ใช้พิมพ์ของหมวด (user, 23 Oct 2569): what the LINE chat asks for each line of a
    typed group, in order. The base fields (meter numbers, size, quantity, name) can be relabelled
    but not removed, since the system counts quantities and meter numbers with them; admins add
    text fields of their own. `required` holds for withdrawals and returns; a problem report may
    skip every field."""

    class Key(models.TextChoices):
        NUMBERS = "numbers", "หมายเลขเครื่องวัด"
        SIZE = "size", "ขนาด"
        QUANTITY = "quantity", "จำนวน"
        NAME = "name", "ชื่ออุปกรณ์"
        EQUIPMENT_NUMBER = "equipment_number", "หมายเลขอุปกรณ์"  # 25 Oct 2569 Q7

    group = models.CharField(max_length=16, choices=EquipmentType.LineGroup.choices)
    key = models.CharField(max_length=32)  # a Key for a base field, "x<pk>" for an added one
    label = models.CharField(max_length=40)
    kinds = models.JSONField(default=list)  # of withdrawal, return, problem
    required = models.BooleanField(default=True)
    position = models.PositiveSmallIntegerField(default=0)
    is_base = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["group", "position", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["group", "key"], name="entryfield_unique_key")
        ]

    def __str__(self):
        return f"{self.group} · {self.label}"


class MonitoringDay(models.Model):
    """One day of Monitoring (15 ต.ค. 2569 Q40–Q47): counts and rates only, never text or a
    person; computed by operations/monitoring.py."""

    day = models.DateField(unique=True)
    metrics = models.JSONField(default=dict)
    alerts = models.JSONField(default=list)
    computed_at = models.DateTimeField()

    class Meta:
        ordering = ["-day"]


class RosterUpload(models.Model):
    """A roster file between its preview and the import (15 ต.ค. 2569 Q27, Q28): the rows sealed
    with the server's PII key, removed by the import or after an hour."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    filename = models.CharField(max_length=200)
    rows = models.PositiveIntegerField(default=0)
    sealed = models.TextField()


class RosterSetting(models.Model):
    """What the last roster import chose, offered again next time (Q27, Q28): which file column
    is which field, and which หน่วยงาน (เขต/ฝ่าย)/หน่วยงาน each ฝ่าย is."""

    columns = models.JSONField(default=dict)  # field → header as the file wrote it
    units = models.JSONField(default=dict)  # ฝ่าย key → district code, or "" for none
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)


class EmailCode(models.Model):
    """A one-time code sent to an employee's roster e-mail during LINE registration (Q24–Q26).
    Only a keyed hash of the code is kept."""

    identity = models.ForeignKey("ChannelIdentity", on_delete=models.PROTECT, related_name="+")
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="+")
    code_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    tries = models.PositiveSmallIntegerField(default=0)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class SystemOption(models.Model):
    """ตัวเลือกระบบ: a choice the organisation can change without code (user, 10 Oct 2569:
    rules and flows change later; give options, keep them convenient and safe). The choices
    and their safe defaults live in operations/options.py; every change is audited."""

    key = models.CharField(max_length=64, unique=True)
    value = models.CharField(max_length=64)
    reason = models.TextField(blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.key}={self.value}"


class RegistryConfig(models.Model):
    """How SAP exports are read (one row, admin-edited, M11 Q14–Q16)."""

    column_aliases = models.JSONField(default=dict, blank=True)
    movement_types = models.JSONField(default=dict, blank=True)
    storage_locations = models.JSONField(default=list, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)


class RegistryImport(models.Model):
    """One uploaded SAP export, kept with its file and summary; never deleted (Q21)."""

    class Kind(models.TextChoices):
        DOCUMENTS = "documents", "รายการเอกสาร"
        SERIALS = "serials", "เลขเครื่องของเอกสาร"
        BALANCES = "balances", "ยอดคงเหลือ"

    kind = models.CharField(max_length=16, choices=Kind.choices)
    original_name = models.CharField(max_length=200)
    storage_name = models.CharField(max_length=64)
    sha256 = models.CharField(max_length=64, unique=True)
    size = models.PositiveIntegerField()
    period_from = models.DateField(null=True, blank=True)
    period_to = models.DateField(null=True, blank=True)
    exported_at = models.DateTimeField()
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    rows = models.PositiveIntegerField(default=0)
    summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-uploaded_at"]
        constraints = [
            models.CheckConstraint(
                name="registry_import_period_order",
                condition=Q(period_from__isnull=True)
                | Q(period_to__isnull=True)
                | Q(period_from__lte=F("period_to")),
            )
        ]


class RegistryDocumentLine(models.Model):
    """One line of a SAP document as exported (documents or serials file)."""

    class Kind(models.TextChoices):
        ISSUE = "issue", "จ่าย"
        RETURN = "return", "รับคืน"
        STATUS = "status", "เปลี่ยนสถานะของ"
        REVERSAL = "reversal", "กลับรายการ"
        OTHER = "other", "อื่น ๆ"

    source = models.ForeignKey(RegistryImport, on_delete=models.PROTECT, related_name="lines")
    document_number = models.CharField(max_length=64)
    line_no = models.PositiveIntegerField(default=0)
    movement_type = models.CharField(max_length=16, blank=True)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.OTHER)
    official_code = models.CharField(max_length=40, blank=True)
    quantity = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    serials = models.JSONField(default=list, blank=True)
    posting_date = models.DateField(null=True, blank=True)
    reference = models.CharField(max_length=200, blank=True)
    case_number = models.CharField(max_length=16, blank=True)
    reversed_document = models.CharField(max_length=64, blank=True)
    plant = models.CharField(max_length=16, blank=True)
    storage_location = models.CharField(max_length=16, blank=True)

    class Meta:
        ordering = ["source", "document_number", "line_no", "id"]
        indexes = [
            models.Index(fields=["document_number"]),
            models.Index(fields=["case_number"]),
        ]


class RegistryRecord(models.Model):
    """A fact this system recorded against a registry document (manual_reference, M11)."""

    class Kind(models.TextChoices):
        ISSUE = "issue", "จ่าย"
        RETURN = "return", "รับคืน"
        STATUS = "status", "ผลตรวจสภาพ"
        ADJUSTMENT = "adjustment", "ปิดส่วนต่าง"

    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="registry_records")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    action = models.CharField(max_length=40)
    source = models.CharField(max_length=40)  # e.g. "delivery:2", "unit:3"
    document_number = models.CharField(max_length=64)
    lines = models.JSONField(default=list)
    division = models.ForeignKey(Division, on_delete=models.PROTECT, related_name="+")
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    recorded_at = models.DateTimeField()

    class Meta:
        ordering = ["recorded_at", "id"]
        indexes = [models.Index(fields=["document_number"])]


class RegistryDiscrepancy(models.Model):
    """ส่วนต่างทะเบียน: this system and the registry disagree; a person decides (M11 Q10–Q12)."""

    class Kind(models.TextChoices):
        NOT_FOUND = "not_found", "ไม่พบเอกสารในทะเบียน"
        QUANTITY = "quantity", "จำนวนไม่ตรง"
        SERIALS = "serials", "เลขเครื่องไม่ตรง"
        TYPE = "type", "ประเภทไม่ตรงหรือถูกกลับรายการ"
        NO_CODE = "no_code", "ของไม่มีรหัสทางการ"
        UNRECORDED = "unrecorded", "เอกสารอ้างเลขเรื่องแต่ไม่มีบันทึก"
        EVIDENCE = "evidence", "หลักฐานใช้ไม่ได้"

    class Status(models.TextChoices):
        OPEN = "open", "รอตรวจ"
        WITH_DIVISION = "with_division", "รอกองที่บันทึกตรวจ"
        WAITING_REGISTRY = "waiting_registry", "รอทะเบียนแก้"
        RESOLVED = "resolved", "ปิดแล้ว"

    class Resolution(models.TextChoices):
        MATCHED = "matched", "เทียบตรงแล้ว"
        NUMBER_FIXED = "number_fixed", "แก้เลขเอกสารแล้ว"
        ACCEPTED = "accepted", "ยอมรับส่วนต่าง"
        EVIDENCE_REPLACED = "evidence_replaced", "แนบหลักฐานใหม่แล้ว"

    class Answer(models.TextChoices):
        REGISTRY_WRONG = "registry_wrong", "ระบบนี้ถูก ทะเบียนต้องแก้"
        FIXED_HERE = "fixed_here", "แก้ข้อมูลในระบบนี้แล้ว"

    kind = models.CharField(max_length=16, choices=Kind.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    case = models.ForeignKey(
        Case, null=True, blank=True, on_delete=models.PROTECT, related_name="registry_discrepancies"
    )
    record = models.ForeignKey(
        RegistryRecord,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="discrepancies",
    )
    document_number = models.CharField(max_length=64, blank=True)
    expected = models.JSONField(default=dict, blank=True)
    found = models.JSONField(default=dict, blank=True)
    detail = models.CharField(max_length=300, blank=True)
    found_by = models.ForeignKey(
        RegistryImport, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    opened_at = models.DateTimeField()
    division = models.ForeignKey(
        Division, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    division_note = models.TextField(blank=True)
    division_answer = models.CharField(max_length=16, choices=Answer.choices, blank=True)
    division_answer_note = models.TextField(blank=True)
    answered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    answered_at = models.DateTimeField(null=True, blank=True)
    registry_reference = models.CharField(max_length=64, blank=True)
    resolution = models.CharField(max_length=20, choices=Resolution.choices, blank=True)
    resolution_reason = models.TextField(blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["opened_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["record", "kind"],
                condition=~Q(status="resolved") & Q(record__isnull=False),
                name="registry_discrepancy_one_open_per_record",
            ),
            models.UniqueConstraint(
                fields=["document_number", "kind"],
                condition=~Q(status="resolved") & Q(record__isnull=True),
                name="registry_discrepancy_one_open_per_document",
            ),
        ]


class ExternalOperation(models.Model):
    """A write to the registry (api_confirmed, M11): one stable operation per idempotency key;
    an unknown outcome is reconciled by asking the registry, never by sending again blind."""

    class Status(models.TextChoices):
        PENDING = "pending", "รอส่ง"
        CONFIRMED = "confirmed", "ระบบเดิมยืนยันแล้ว"
        FAILED = "failed", "ระบบเดิมปฏิเสธ"
        UNKNOWN = "unknown", "ไม่ทราบผล"

    operation_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    kind = models.CharField(max_length=32)
    idempotency_key = models.CharField(max_length=128, unique=True)
    fingerprint = models.CharField(max_length=64)
    request = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    external_reference = models.CharField(max_length=64, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    queries = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField(max_length=200, blank=True)
    history = models.JSONField(default=list, blank=True)
    callbacks = models.JSONField(default=list, blank=True)
    case = models.ForeignKey(
        Case, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]


class ChatAnswer(models.Model):
    """คลังคำตอบ (ADR 0012): what the bot may say to a question in LINE. Only approved answers
    are used, by keyword (ก), chosen by the AI (ข) or as the ground the AI writes from (ค). An
    action adds what only the system knows (the requester's own cases, the policy's emergency
    guidance or service hours); the text itself is written and approved by people."""

    class Action(models.TextChoices):
        TEXT = "text", "ข้อความอย่างเดียว"
        MY_CASES = "my_cases", "ต่อด้วยเรื่องที่เปิดอยู่ของผู้แจ้ง"
        EMERGENCY = "emergency", "ต่อด้วยคำแนะนำเร่งด่วนจากกติกา (คำอันตรายในกติกาเป็นคำสำคัญด้วย)"
        HOURS = "hours", "ต่อด้วยเวลาทำการจากปฏิทินในกติกา"

    class Status(models.TextChoices):
        DRAFT = "draft", "ร่าง"
        APPROVED = "approved", "รับรองแล้ว ใช้ตอบได้"
        RETIRED = "retired", "เลิกใช้"

    key = models.SlugField(max_length=40, unique=True)
    title = models.CharField(max_length=100)
    keywords = models.JSONField(default=list, blank=True)
    # Greetings and thanks: answered only when the message is little more than the keyword, so
    # "สวัสดีครับ ขอแจ้งไฟดับ" is handled as a report, not a greeting.
    short_only = models.BooleanField(default=False)
    text = models.TextField(max_length=1000)
    action = models.CharField(max_length=16, choices=Action.choices, default=Action.TEXT)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    is_synthetic = models.BooleanField(default=False)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["key"]

    def __str__(self):
        return self.title


# --- Personal data out of AI calls (ADR 0014) ---------------------------------------------------


class MaskedWord(models.Model):
    """คำที่ต้องปิดเสมอ (ADR 0014, 14 Oct 2569 Q30, Q34): a word the AI must never see, such as a
    person's name the lists do not know. Any staff member adds one with a reason; only an admin
    removes one, because removing it lets the word reach the AI again."""

    word = models.CharField(max_length=100)
    reason = models.TextField()
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    added_at = models.DateTimeField(auto_now_add=True)
    removed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    removed_at = models.DateTimeField(null=True, blank=True)
    removal_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-added_at"]
        constraints = [
            models.UniqueConstraint(
                Lower("word"),
                condition=Q(removed_at__isnull=True),
                name="masked_word_unique_active",
            )
        ]

    def __str__(self):
        return self.word


class MaskCount(models.Model):
    """How many values the masking hid before an AI call, per day, task and kind; never the
    values themselves (ADR 0014, Q30)."""

    day = models.DateField()
    task = models.CharField(max_length=16)
    kind = models.CharField(max_length=16)
    count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-day", "task", "kind"]
        constraints = [
            models.UniqueConstraint(fields=["day", "task", "kind"], name="mask_count_unique")
        ]


# --- คำที่ระบบจำ and คำถามที่บอทตอบไม่ได้ (ADR 0013) ----------------------------------------------


class RoutingWord(models.Model):
    """คำที่ระบบจำ (ADR 0013; 14 Oct 2569 Q2-Q13, Q31, Q36): a word from a case the rules could
    not route, with the division that took it, so the next such case with the word goes there."""

    class Source(models.TextChoices):
        HANDOFF = "handoff", "ฝอจ. เลือกตอนส่งต่อ"
        AI = "ai", "คำที่ AI ยกมา กองรับเรื่องแล้ว"

    word = models.CharField(max_length=40)
    kind = models.CharField(max_length=16, choices=Case.Kind.choices)
    division = models.ForeignKey(Division, on_delete=models.PROTECT, related_name="+")
    source = models.CharField(max_length=16, choices=Source.choices)
    learned_from = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="+")
    taught_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    uses = models.PositiveIntegerField(default=0)  # cases it matched at submission
    moves = models.PositiveIntegerField(default=0)  # Q13: "ย้ายบ่อย" from two moves
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["kind", "word"]
        constraints = [
            models.UniqueConstraint(
                Lower("word"),
                "kind",
                condition=Q(cancelled_at__isnull=True),
                name="routing_word_unique_active",
            )
        ]

    def __str__(self):
        return self.word


class UnansweredQuestion(models.Model):
    """คำถามที่บอทตอบไม่ได้ (14 Oct 2569 Q1 ค, Q11): text typed outside a draft that the answer
    bank did not know, the same words counted together. Stored with personal data masked
    (ADR 0014) and without who typed it: an admin sees what people ask, not who asked."""

    class Status(models.TextChoices):
        OPEN = "open", "รอแอดมิน"
        ANSWERED = "answered", "เพิ่มเป็นคำตอบแล้ว"
        IGNORED = "ignored", "ไม่ต้องตอบ"

    key = models.CharField(max_length=64, unique=True)  # SHA-256 of the masked words
    text = models.TextField()
    channel = models.CharField(max_length=16, default="line")
    count = models.PositiveIntegerField(default=1)
    first_at = models.DateTimeField()
    last_at = models.DateTimeField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    answer_key = models.CharField(max_length=40, blank=True)
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    handled_at = models.DateTimeField(null=True, blank=True)
    # ADR 0037 Q31: who asked, only as keyed hashes of registered LINE accounts with the day,
    # so the nightly tuning counts people (at least 3 in 7 days) without knowing who they are.
    askers = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-last_at"]
        indexes = [models.Index(fields=["status", "-count"])]

    def __str__(self):
        return self.text[:60]


class KnowledgeDoc(models.Model):
    """เอกสารความรู้ (ADR 0037 Q9, Q25): text that is not secret, uploaded by staff, that the free
    chat agent may search. Personal data is refused at upload; another admin approves it, and only
    then does it reach Pinecone. Retiring it removes it from Pinecone again."""

    class Status(models.TextChoices):
        DRAFT = "draft", "รอรับรอง"
        APPROVED = "approved", "รับรองแล้ว ใช้ตอบได้"
        RETIRED = "retired", "เลิกใช้"

    title = models.CharField(max_length=200)
    text = models.TextField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    revision = models.PositiveIntegerField(default=1)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    chunks = models.PositiveIntegerField(default=0)  # records in Pinecone
    synced_at = models.DateTimeField(null=True, blank=True)
    sync_error = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["title"]

    def __str__(self):
        return self.title


class ChatGuide(models.Model):
    """คำแนะนำเฉพาะเรื่อง (ADR 0037 Q22, Q23, Q30 to Q32): one sub-prompt per topic that the free
    chat agent reads after its locked core. Admins edit it with a reason and the AI tunes it each
    night; every version is kept, passes rules and a small check suite before it is used, and an
    admin rolls back to any earlier one."""

    class Topic(models.TextChoices):
        ORG_UNITS = "org_units", "ผังหน่วยงาน"
        MY_CASES = "my_cases", "เรื่องของฉัน"
        HOW_TO_FILE = "how_to_file", "วิธีส่งเรื่อง"
        GENERAL = "general", "ความรู้ทั่วไป"
        TONE = "tone", "น้ำเสียง"

    class Status(models.TextChoices):
        CANDIDATE = "candidate", "รอชุดตรวจ"
        ACTIVE = "active", "ใช้อยู่"
        REFUSED = "refused", "ไม่ผ่าน"
        RETIRED = "retired", "ฉบับก่อน"

    class Source(models.TextChoices):
        SEED = "seed", "ฉบับเริ่มต้น"
        ADMIN = "admin", "แอดมินแก้"
        AI = "ai", "AI ปรับเอง"

    topic = models.CharField(max_length=16, choices=Topic.choices)
    number = models.PositiveIntegerField()
    text = models.TextField(max_length=800)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.CANDIDATE)
    source = models.CharField(max_length=8, choices=Source.choices)
    reason = models.TextField(blank=True)
    check_result = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    activated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["topic", "-number"]
        constraints = [
            models.UniqueConstraint(fields=["topic", "number"], name="chat_guide_number"),
            models.UniqueConstraint(
                fields=["topic"], condition=models.Q(status="active"), name="chat_guide_one_active"
            ),
        ]

    def __str__(self):
        return f"{self.get_topic_display()} ฉบับ {self.number}"


# --- หน้าที่ของกอง and the routing AI (ADR 0013) ---------------------------------------------------


class DutyEntry(models.Model):
    """งานที่ดูแล (ADR 0013, Q14 ก): one thing a division takes care of, as an admin wrote it.
    The routing AI answers which entry a message is about; the entry names the division. The
    examples (two or more) are the AI's test, never its prompt (Q25)."""

    division = models.ForeignKey(Division, on_delete=models.PROTECT, related_name="duties")
    title = models.CharField(max_length=100)
    description = models.TextField(max_length=1000)
    examples = models.JSONField(default=list)
    is_active = models.BooleanField(default=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["division__code", "title"]

    def __str__(self):
        return self.title

    @property
    def code(self):
        """How the AI names this entry (its schema allows only these)."""
        return f"E{self.pk}"


class AIRoutingRun(models.Model):
    """One ask of the routing AI for an unclear case (ADR 0013): the entry or division it
    proposed, the words it quoted and the keyword it gave, or why it gave nothing."""

    class Status(models.TextChoices):
        PENDING = "pending", "รอ AI"
        DONE = "done", "AI เสนอแล้ว"
        NONE = "none", "AI ไม่เสนอ"
        FAILED = "failed", "AI ใช้ไม่ได้"
        SKIPPED = "skipped", "ไม่ต้องถามแล้ว"

    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="routing_runs")
    case_revision = models.PositiveIntegerField()
    config_version = models.ForeignKey(
        AIConfigVersion, on_delete=models.PROTECT, related_name="routing_runs"
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    entry = models.ForeignKey(
        DutyEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    division = models.ForeignKey(
        Division, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    quote = models.TextField(blank=True)
    keyword = models.CharField(max_length=40, blank=True)
    # The case went straight to the division (ตัวเลือกระบบ routing_ai "direct", entries only).
    applied = models.BooleanField(default=False)
    attempts = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        # One ask per case: the case sits with ส่วนกลาง across many decisions (revisions).
        constraints = [models.UniqueConstraint(fields=["case"], name="routing_run_one_per_case")]


# --- งานสั่ง (16 Oct 2569 Q6, Q14) ----------------------------------------------------------------


class WorkOrder(models.Model):
    """Work a Supervisor gives a division or one of its members: not a case, so it never counts
    in the district reports. มอบหมาย → รับทราบ → กำลังทำ → เสร็จ; the assignee may send it back
    with a reason, and a Supervisor closes or cancels it."""

    class Status(models.TextChoices):
        ASSIGNED = "assigned", "มอบหมายแล้ว"
        ACKNOWLEDGED = "acknowledged", "รับทราบแล้ว"
        IN_PROGRESS = "in_progress", "กำลังทำ"
        DONE = "done", "ทำเสร็จแล้ว"
        SENT_BACK = "sent_back", "ส่งกลับ"
        CLOSED = "closed", "ปิดงาน"
        CANCELLED = "cancelled", "ยกเลิก"

    class Channel(models.TextChoices):
        LINE = "line", "LINE"
        WEB = "web", "เว็บ"

    number = models.CharField(max_length=16, unique=True)
    title = models.CharField(max_length=200)
    detail = models.TextField(blank=True)
    division = models.ForeignKey(Division, on_delete=models.PROTECT, related_name="work_orders")
    # Empty: anyone in the division does it (its Supervisor may name a person later).
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    # Blank only for historical orders whose original channel was not recorded.
    channel = models.CharField(
        max_length=8, choices=Channel.choices, blank=True, default="", db_default=""
    )
    due_on = models.DateField(null=True, blank=True)
    is_urgent = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ASSIGNED)
    result = models.TextField(blank=True)  # what was done, written when finishing
    reason = models.TextField(blank=True)  # the latest send-back or cancel reason
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    done_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)  # closed or cancelled
    # One reminder near the due date and one after it (Q14).
    reminded_soon = models.BooleanField(default=False)
    reminded_late = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["division", "status"], name="work_order_queue")]

    OPEN = (Status.ASSIGNED, Status.ACKNOWLEDGED, Status.IN_PROGRESS)
    ENDED = (Status.CLOSED, Status.CANCELLED)

    def __str__(self):
        return self.number


class JudgeCalibration(models.Model):
    """การทดสอบตัวตัดสินคำตอบแชต (16 Oct 2569 Q12): Evidently's judges on a frozen set of
    synthetic good and bad answers, with one model. Only a model that scored at least 90% on
    both faithfulness and relevance may judge real answers (monitor_judge)."""

    class Status(models.TextChoices):
        QUEUED = "queued", "รอคิว"
        RUNNING = "running", "กำลังทดสอบ"
        DONE = "done", "เสร็จ"
        FAILED = "failed", "ทดสอบไม่สำเร็จ"

    version = models.ForeignKey(AIConfigVersion, on_delete=models.PROTECT, related_name="+")
    profile = models.ForeignKey(AIProfile, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    faithful = models.FloatField(null=True, blank=True)  # share judged as labelled
    relevant = models.FloatField(null=True, blank=True)
    passed = models.BooleanField(default=False)
    details = models.JSONField(default=list, blank=True)  # labels and verdicts, no text
    error = models.CharField(max_length=200, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class LineMenuRelease(models.Model):
    """Durable publication progress so a failed LINE call resumes without losing menu IDs."""

    digest = models.CharField(max_length=64, unique=True)
    menus = models.JSONField(default=dict)
    uploaded = models.JSONField(default=list)
    previous = models.JSONField(default=dict)
    audience_hash = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=16, default="pending")
    error = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class LineFlow(models.Model):
    """flow ผู้แจ้งของปุ่ม rich menu หนึ่งปุ่ม (ADR 0023): ขั้นสนทนาเป็นข้อมูลที่มีรุ่น แอดมินแก้ในร่าง
    แล้วเผยแพร่ในระบบ ไม่ต้อง deploy โค้ด"""

    key = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=120)
    # The rich menu tile whose tap this flow answers once published (its label).
    tile = models.CharField(max_length=60, blank=True)
    division = models.ForeignKey(
        Division, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class LineFlowVersion(models.Model):
    """One version of a flow's graph ({"nodes": [...], "edges": [...]}). A flow has at most one
    draft and one published version; publishing retires the previous one, which stays for
    rollback. `revision` counts saves of a draft so two editors never overwrite each other."""

    class Status(models.TextChoices):
        DRAFT = "draft", "ร่าง"
        PUBLISHED = "published", "เผยแพร่อยู่"
        RETIRED = "retired", "รุ่นเก่า"

    flow = models.ForeignKey(LineFlow, on_delete=models.CASCADE, related_name="versions")
    number = models.PositiveIntegerField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    graph = models.JSONField(default=dict)
    note = models.TextField(blank=True)
    checks = models.JSONField(default=list, blank=True)
    revision = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    published_at = models.DateTimeField(null=True, blank=True)
    publish_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["flow", "-number"]
        constraints = [
            models.UniqueConstraint(fields=["flow", "number"], name="line_flow_version_number"),
            models.UniqueConstraint(
                fields=["flow"], condition=Q(status="draft"), name="line_flow_one_draft"
            ),
            models.UniqueConstraint(
                fields=["flow"], condition=Q(status="published"), name="line_flow_one_published"
            ),
        ]

    def __str__(self):
        return f"{self.flow} รุ่น {self.number}"


class DivisionRequirement(models.Model):
    """requirement ของกอง (grill กคว. 27 ต.ค. 2569 Q28, Q36). Not used since the กคอ. grill
    (28 ต.ค. 2569 Q41) took its page away: the menu designer on Vercel keeps the requirements.
    The table stays until a release that may drop it (dropping is a one-way migration)."""

    class Topic(models.TextChoices):
        PROBLEM = "problem", "ปัญหาที่เจอตอนนี้"
        WANT = "want", "สิ่งที่อยากได้"
        ASK = "ask", "ข้อมูลที่ต้องถามผู้แจ้ง"
        TIME = "time", "ความเร่งด่วนและระยะเวลา"
        OTHER = "other", "อื่น ๆ"

    class Status(models.TextChoices):
        OPEN = "open", "รอคุย"
        AGREED = "agreed", "ตกลงทำ"
        DECLINED = "declined", "ไม่ทำ"
        DONE = "done", "ทำแล้ว"

    class Priority(models.TextChoices):
        HIGH = "high", "สูง"
        MEDIUM = "medium", "กลาง"
        LOW = "low", "ต่ำ"

    division = models.ForeignKey(Division, on_delete=models.PROTECT, related_name="requirements")
    topic = models.CharField(max_length=12, choices=Topic.choices, default=Topic.WANT)
    text = models.TextField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    priority = models.CharField(max_length=8, choices=Priority.choices, default=Priority.MEDIUM)
    remark = models.TextField(blank=True)
    proposed_by = models.CharField(max_length=120, blank=True)
    flow = models.ForeignKey(
        LineFlow, null=True, blank=True, on_delete=models.SET_NULL, related_name="requirements"
    )
    node_id = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["division", "topic", "created_at", "pk"]


# --- กคว. งานคลัง ปฏิทิน และการคุยในเรื่อง (grill กคว. 27 ต.ค. 2569 Q13 ถึง Q39) ----------------


class Calendar(models.Model):
    """ปฏิทินนัดของกอง (Q25, Q26): one per kind of job, e.g. โน้ตบุ๊กและหัวอ่าน or นัดรับของ.
    A flow's book box names it by key; any member of the division manages its slots."""

    key = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=120)
    division = models.ForeignKey(Division, on_delete=models.PROTECT, related_name="calendars")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["division", "name"]

    def __str__(self):
        return self.name


class CalendarSlot(models.Model):
    """ช่วงเวลาที่เจ้าหน้าที่เปิดให้ขอนัด (Q13, Q34): staff choose the times, freely."""

    calendar = models.ForeignKey(Calendar, on_delete=models.PROTECT, related_name="slots")
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    capacity = models.PositiveSmallIntegerField(default=1)
    is_open = models.BooleanField(default=True)
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["starts_at", "pk"]
        indexes = [models.Index(fields=["calendar", "starts_at"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_at__gt=models.F("starts_at")), name="calendar_slot_ends_after"
            )
        ]


class Appointment(models.Model):
    """คำขอนัด (Q25): a request holds its slot until staff confirm, move or reject it; an
    unconfirmed request counts as taken. `method` is how ฟข. gets the goods (Q31, Q46)."""

    class Status(models.TextChoices):
        REQUESTED = "requested", "ขอนัด รอยืนยัน"
        CONFIRMED = "confirmed", "ยืนยันนัดแล้ว"
        REJECTED = "rejected", "ไม่รับนัด"
        CANCELLED = "cancelled", "ยกเลิก"
        DONE = "done", "ทำเสร็จแล้ว"

    class Method(models.TextChoices):
        NONE = "", "-"
        TRUCK = "truck", "รถของกองไปส่ง"
        PICKUP = "pickup", "ฟข. มารับเอง"

    HOLDING = (Status.REQUESTED, Status.CONFIRMED)

    calendar = models.ForeignKey(Calendar, on_delete=models.PROTECT, related_name="appointments")
    slot = models.ForeignKey(
        CalendarSlot, null=True, blank=True, on_delete=models.PROTECT, related_name="appointments"
    )
    case = models.ForeignKey(
        "Case", null=True, blank=True, on_delete=models.PROTECT, related_name="appointments"
    )
    draft = models.ForeignKey(
        ConversationDraft, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.REQUESTED)
    method = models.CharField(max_length=8, choices=Method.choices, blank=True)
    note = models.TextField(blank=True)
    requested_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-requested_at"]


class CaseMilestone(models.Model):
    """ขั้นงานคลังของใบเบิก (Q16, Q18): staff record each stage, many slips at once. `on` is the
    day it happened; for คีย์จ่ายแล้ว `planned_on` is the issue day กคว. set (Q20)."""

    class Stage(models.TextChoices):
        RECEIVED = "received", "รับใบเบิกแล้ว"
        KEYED = "keyed", "คีย์จ่ายแล้ว"
        ISSUED = "issued", "จ่ายแล้ว"
        DELIVERED = "delivered", "เขตได้รับแล้ว"

    ORDER = [Stage.RECEIVED, Stage.KEYED, Stage.ISSUED, Stage.DELIVERED]

    case = models.ForeignKey("Case", on_delete=models.PROTECT, related_name="milestones")
    stage = models.CharField(max_length=12, choices=Stage.choices)
    on = models.DateField()
    planned_on = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=300, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["case", "on", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["case", "stage"], name="case_milestone_once")
        ]

    @property
    def label(self):
        """The stage as the owner division names it for this kind of case (grill กคอ. Q32)."""
        from operations import warehouse  # local: warehouse builds on the models

        return warehouse.stage_label(self.case, self.stage)


class DivisionSchedule(models.Model):
    """ตารางของกอง (Q17, Q30, Q45): กำหนดจ่ายตามขนาด (working days after วันรับงาน), the
    weekdays it issues, and วันส่งรถ of each ฟข. Staff of the division edit it with a reason."""

    division = models.OneToOneField(Division, on_delete=models.PROTECT, related_name="schedule")
    # [{"group": "meter", "size": "5(100)A", "days": 3}]; an empty size is the group's default.
    sla = models.JSONField(default=list, blank=True)
    issue_weekdays = models.JSONField(default=list, blank=True)  # 0 = Monday
    # {District.code or ชื่อย่อ ฟข.: [weekday]}: a table copied from a division's poster names
    # each ฟข. by its abbreviation until an admin says which district that is (กคอ. Q26).
    truck_days = models.JSONField(default=dict, blank=True)

    class Delivery(models.TextChoices):
        CHOOSE = "choose", "ฟข. เลือกรถส่งหรือมารับเอง"  # กคว. Q46
        TRUCK = "truck", "รถส่งเป็นค่าเริ่มต้น มีปุ่มมารับเอง"  # กคอ. Q16

    delivery = models.CharField(max_length=8, choices=Delivery.choices, default=Delivery.CHOOSE)
    # Working days between คีย์จ่ายแล้ว and the earliest truck day (กคอ. Q40, default 0).
    lead_days = models.PositiveSmallIntegerField(default=0)
    contact = models.CharField(max_length=100, blank=True)  # said when a ฟข. has no truck day
    # {"withdrawal"|"return": {stage: name}}: the division's own names of the stages (Q7, Q32).
    stage_labels = models.JSONField(default=dict, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)


class Holiday(models.Model):
    """วันหยุด (grill กคว. รอบใหม่ Q11): a day nobody works, chosen by an admin. It is not a
    working day for กำหนดจ่าย and shows on the appointment calendars."""

    day = models.DateField(unique=True)
    name = models.CharField(max_length=120)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["day"]

    def __str__(self):
        return f"{self.day} {self.name}"


class EmergencyContact(models.Model):
    """ผู้รับแจ้งเรื่องฉุกเฉินของกอง (grill กคว. รอบใหม่ Q33). Since ADR 0031 (Q12) the rows were
    copied to the กอง cards as ผู้รับแจ้งเรื่องสำคัญ (UnitRecipient) and nothing reads them; the
    table stays one release so the previous release still runs during a deploy."""

    division = models.ForeignKey(Division, on_delete=models.CASCADE, related_name="+")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["division", "user"], name="emergency_contact_once")
        ]


class UnitWork(models.Model):
    """งานที่หน่วยรับ (ADR 0031 Q8): a kind of case with a group of goods or a topic that one
    unit takes. The first active row of the กอง in `order` that the case matches wins (Q23)."""

    division = models.ForeignKey(Division, on_delete=models.PROTECT, related_name="unit_works")
    section = models.ForeignKey(Section, null=True, blank=True, on_delete=models.PROTECT)
    work_group = models.ForeignKey(WorkGroup, null=True, blank=True, on_delete=models.PROTECT)
    kind = models.CharField(max_length=16, choices=Case.Kind.choices)
    group = models.CharField(max_length=16)  # an EquipmentType.LineGroup or a Case.Topic value
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["division__code", "order", "pk"]


class UnitWorkOwner(models.Model):
    """ผู้รับผิดชอบประจำงาน (Q16, Q21): the people who take a matching case at once, คนหลัก
    first and then the backups."""

    work = models.ForeignKey(UnitWork, on_delete=models.CASCADE, related_name="owners")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["work", "user"], name="unit_work_owner_once")
        ]


class UnitRecipient(models.Model):
    """ผู้รับแจ้งเรื่องสำคัญของหน่วย (Q12, Q25): who hears of an important case on LINE when the
    case has no responsible person; a unit without any defers to the bigger unit."""

    division = models.ForeignKey(Division, on_delete=models.CASCADE, related_name="+")
    section = models.ForeignKey(Section, null=True, blank=True, on_delete=models.CASCADE)
    work_group = models.ForeignKey(WorkGroup, null=True, blank=True, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["pk"]


class CaseMessage(models.Model):
    """การคุยกับเจ้าหน้าที่ในเรื่อง (ADR 0024): what staff type on the case card and what the
    requester answers. A staff message goes as a free LINE reply while the requester's last
    message is under 50 seconds old, otherwise it is held for their next message."""

    class Direction(models.TextChoices):
        STAFF = "staff", "เจ้าหน้าที่"
        REQUESTER = "requester", "ผู้แจ้ง"

    class Status(models.TextChoices):
        HELD = "held", "รอส่งตอนผู้แจ้งส่งข้อความมา"
        SENT = "sent", "ส่งแล้ว"
        FAILED = "failed", "ส่งไม่ได้"
        RECEIVED = "received", "ได้รับ"

    case = models.ForeignKey("Case", on_delete=models.PROTECT, related_name="chat")
    direction = models.CharField(max_length=10, choices=Direction.choices)
    text = models.TextField()
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # Q54: the first message of each staff member in a case carries how to reach them.
    contact = models.CharField(max_length=200, blank=True)
    # Quick replies a system message carries ([label, postback data, text shown]).
    quick = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices)
    inbox = models.ForeignKey(
        WebhookInbox, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["case", "status"])]
