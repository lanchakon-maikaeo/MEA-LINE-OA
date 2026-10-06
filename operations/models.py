"""Identity, organisation and audit (M01), the case domain (M02) and LINE intake (M03)."""

import uuid

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
    # The central intake unit (กองรับเรื่องกลาง): owner of every case at submission.
    is_intake = models.BooleanField(default=False)

    class Meta:
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(
                fields=["is_intake"], condition=Q(is_intake=True), name="division_single_intake"
            )
        ]

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

    code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=16, choices=Category.choices)
    unit = models.CharField(max_length=32)
    is_serialized = models.BooleanField(default=False)
    is_divisible = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    is_synthetic = models.BooleanField(default=False)

    class Meta:
        ordering = ["code"]
        constraints = [
            models.CheckConstraint(
                name="equipment_serialized_is_whole",
                condition=~Q(is_serialized=True, is_divisible=True),
            )
        ]

    def __str__(self):
        return f"{self.code} {self.name}"


class Case(models.Model):
    """เรื่อง: exists only once submitted (drafts live in the conversation layer, M03)."""

    class Kind(models.TextChoices):
        WITHDRAWAL = "withdrawal", "คำขอเบิก"
        RETURN = "return", "คำขอคืน"
        PROBLEM = "problem", "เรื่องแจ้งปัญหา"

    class Status(models.TextChoices):
        SUBMITTED = "submitted", "รอคัดกรอง"
        TRIAGE = "triage", "กำลังคัดกรอง"
        AWAITING_APPROVAL = "awaiting_approval", "รออนุมัติ"
        AWAITING_EXECUTION = "awaiting_execution", "รอดำเนินการ"
        IN_PROGRESS = "in_progress", "กำลังดำเนินการ"
        AWAITING_CONFIRMATION = "awaiting_confirmation", "รอเขตยืนยัน"
        AWAITING_INFO = "awaiting_info", "รอข้อมูลเพิ่ม"
        ON_HOLD = "on_hold", "พักเรื่อง"
        CLOSED = "closed", "ปิดเรื่อง"
        REJECTED = "rejected", "ไม่รับเรื่อง/ไม่อนุมัติ"
        CANCELLED = "cancelled", "ยกเลิก"

    class Channel(models.TextChoices):
        LINE = "line", "LINE"
        PHONE = "phone", "โทรศัพท์"
        LETTER = "letter", "หนังสือ"
        COUNTER = "counter", "หน้าเคาน์เตอร์"

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
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # Empty when the requester submitted on LINE; the staff member when filed on their behalf.
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    channel = models.CharField(max_length=16, choices=Channel.choices)
    intake_note = models.TextField(blank=True)

    purpose = models.TextField(blank=True)
    needed_by = models.DateField(null=True, blank=True)
    condition = models.TextField(blank=True)
    return_reason = models.TextField(blank=True)
    source_reference = models.CharField(max_length=100, blank=True)
    description = models.TextField(blank=True)
    impact = models.TextField(blank=True)
    related_case = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    submitted_at = models.DateTimeField()
    updated_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)

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


class CaseLine(models.Model):
    """รายการของในเรื่อง. Problem lines may omit quantity; M07-M09 add fulfilment counters."""

    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="lines")
    line_no = models.PositiveSmallIntegerField()
    item = models.ForeignKey(EquipmentType, on_delete=models.PROTECT, related_name="+")
    requested_quantity = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    serials = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["case", "line_no"]
        constraints = [
            models.UniqueConstraint(fields=["case", "line_no"], name="caseline_unique_no"),
            models.CheckConstraint(
                name="caseline_quantity_positive",
                condition=Q(requested_quantity__isnull=True) | Q(requested_quantity__gt=0),
            ),
        ]


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


class OutboxEvent(models.Model):
    """Intent to notify or orchestrate, committed with the change it describes (envelope v1)."""

    class Status(models.TextChoices):
        PENDING = "pending", "รอส่ง"
        DONE = "done", "เสร็จ"
        FAILED = "failed", "ล้มเหลว"

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

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["status", "id"])]
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
    updated_at = models.DateTimeField(auto_now=True)


class ConversationMessage(models.Model):
    """Text the user typed, kept verbatim as source material (AI extraction arrives in M05)."""

    identity = models.ForeignKey(ChannelIdentity, on_delete=models.PROTECT, related_name="+")
    inbox = models.OneToOneField(WebhookInbox, on_delete=models.PROTECT, related_name="+")
    draft = models.ForeignKey(
        ConversationDraft, null=True, blank=True, on_delete=models.PROTECT, related_name="messages"
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
    """A file from LINE kept private: fetched via the content API, quarantined until scanned."""

    class ScanStatus(models.TextChoices):
        PENDING_FETCH = "pending_fetch", "รอดึงไฟล์"
        PENDING_SCAN = "pending_scan", "รอสแกน"
        CLEAN = "clean", "ผ่าน"
        INFECTED = "infected", "พบมัลแวร์"
        REJECTED = "rejected", "ไม่รับไฟล์"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    identity = models.ForeignKey(ChannelIdentity, on_delete=models.PROTECT, related_name="+")
    inbox = models.OneToOneField(WebhookInbox, on_delete=models.PROTECT, related_name="+")
    line_message_id = models.CharField(max_length=64)
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
