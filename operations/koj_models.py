"""External-document tracking for กอจ.; these rows never create stock transactions or Cases."""

from django.conf import settings
from django.db import models
from django.utils import timezone


class KojSource(models.Model):
    name = models.CharField(max_length=120)
    enabled = models.BooleanField(default=False)
    provider = models.CharField(
        max_length=16, default="google", choices=[("google", "Google Sheets"), ("csv", "ไฟล์ CSV")]
    )
    spreadsheet_id = models.CharField(max_length=200, blank=True)
    sheet_name = models.CharField(max_length=100, blank=True)
    service = models.CharField(max_length=16)
    default_equipment = models.CharField(max_length=32)
    columns = models.JSONField(default=dict)
    equipment_map = models.JSONField(default=dict, blank=True)
    district_map = models.JSONField(default=dict, blank=True)
    completed_statuses = models.JSONField(default=list, blank=True)
    contact = models.CharField(max_length=300, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    next_sync_at = models.DateTimeField(default=timezone.now)
    refresh_requested = models.BooleanField(default=False)
    sync_token = models.CharField(max_length=64, blank=True)
    syncing_until = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "pk"]

    def __str__(self):
        return self.name


class KojRecord(models.Model):
    source = models.ForeignKey(KojSource, on_delete=models.PROTECT, related_name="records")
    row_key = models.CharField(max_length=150)
    reference = models.CharField(max_length=100)
    reference_key = models.CharField(max_length=100, db_index=True)
    equipment = models.CharField(max_length=32)
    equipment_name = models.CharField(max_length=150, blank=True)
    district = models.ForeignKey(
        "operations.District", null=True, blank=True, on_delete=models.PROTECT
    )
    status = models.CharField(max_length=300)
    details = models.JSONField(default=dict, blank=True)
    present = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1)
    observed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["reference_key", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["source", "row_key"], name="koj_source_row_unique")
        ]
        indexes = [models.Index(fields=["source", "equipment", "reference_key"])]


class KojHistory(models.Model):
    record = models.ForeignKey(KojRecord, on_delete=models.PROTECT, related_name="history")
    status = models.CharField(max_length=300)
    observed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["observed_at", "pk"]


class KojWatch(models.Model):
    identity = models.ForeignKey(
        "operations.ChannelIdentity", on_delete=models.PROTECT, related_name="koj_watches"
    )
    source = models.ForeignKey(KojSource, on_delete=models.PROTECT, related_name="watches")
    reference_key = models.CharField(max_length=100)
    equipment = models.CharField(max_length=32)
    equipment_name = models.CharField(max_length=150, blank=True)
    status = models.CharField(
        max_length=16,
        default="active",
        choices=[("active", "กำลังติดตาม"), ("completed", "ติดตามจบแล้ว"), ("stopped", "เลิกติดตาม")],
    )
    cycle = models.PositiveIntegerField(default=1)
    # What the follower last saw (owner, 28 Oct 2569: no push to followers; a change waits for
    # their next message, as on cases).
    seen_stamp = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["identity", "source", "reference_key", "equipment", "equipment_name"],
                name="koj_watch_unique",
            )
        ]


class KojRecipient(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="koj_alerts"
    )
    group = models.CharField(max_length=16)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "group"], name="koj_recipient_unique")
        ]


class KojNotice(models.Model):
    delivery = models.OneToOneField(
        "operations.NotificationDelivery", on_delete=models.CASCADE, related_name="koj_notice"
    )
    watch = models.ForeignKey(KojWatch, on_delete=models.PROTECT)
    group = models.CharField(max_length=16, blank=True)
    record_ids = models.JSONField(default=list)
