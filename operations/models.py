"""Identity and audit (M01), cases (M02), LINE intake (M03), orchestration (M04) and AI (M05)."""

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


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
    # Case changes after this instant are shown as "มีอัปเดตใหม่" at the next turn (M04).
    updates_seen_at = models.DateTimeField(null=True, blank=True)
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
    case = models.ForeignKey(
        Case, null=True, blank=True, on_delete=models.PROTECT, related_name="deliveries"
    )
    registration = models.ForeignKey(
        RegistrationRequest, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
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
    """Provider endpoint and API key. The key is Fernet-encrypted and never shown again."""

    name = models.CharField(max_length=64, unique=True)
    base_url = models.URLField(max_length=200)
    encrypted_key = models.TextField()
    key_hint = models.CharField(max_length=8)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    rotated_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.name} (••••{self.key_hint})"


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
        EXTRACT = "extract", "สกัดร่างจากแชต (M05)"
        TRIAGE = "triage", "เสนอข้อเท็จจริงเพื่อคัดกรองเรื่อง (M06)"

    class Status(models.TextChoices):
        CANDIDATE = "candidate", "รอทดสอบ/ประเมิน"
        ACTIVE = "active", "ใช้งานอยู่"
        RETIRED = "retired", "เลิกใช้"
        REJECTED = "rejected", "ไม่ผ่าน"

    class Pipeline(models.TextChoices):
        SINGLE = "single", "เรียกครั้งเดียว (extract-v1)"
        HARNESS = "harness", "Harness (กรองคำสั่งแฝง + ตัวตรวจ + แก้ 1 รอบ + hedge)"

    number = models.PositiveIntegerField(unique=True)
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
        ]

    def chain(self):
        return [entry.profile for entry in self.entries.select_related("profile__credential")]

    def __str__(self):
        return f"v{self.number}"


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

    class Meta:
        ordering = ["-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["version"],
                condition=Q(status__in=["queued", "running"]),
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
    division; item/category/quantity limits are for approval activities (M07).
    """

    class Activity(models.TextChoices):
        PRIORITY_EXCEPTION = "priority_exception", "ข้อยกเว้นระดับความเร่งด่วน"
        ROUTING_DECISION = "routing_decision", "ตัดสินเส้นทาง"

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
    # Empty only for synthetic demo seeds; an admin never grants authority to themselves.
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
    accepted/declined). Synthetic durations from the active policy; never an official SLA."""

    class Kind(models.TextChoices):
        INTAKE = "intake", "รอเริ่มคัดกรอง"
        HANDOFF = "handoff", "รอตอบรับการส่งต่อ"

    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="ack_clocks")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    handoff = models.OneToOneField(
        Handoff, null=True, blank=True, on_delete=models.PROTECT, related_name="clock"
    )
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
                condition=Q(stopped_at__isnull=True),
                name="ack_clock_one_open_per_kind",
            )
        ]
        indexes = [models.Index(fields=["stopped_at", "due_at"])]
