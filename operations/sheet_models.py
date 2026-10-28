"""ค้นในชีท (ADR 0027): a status column looked up by a number column of someone else's sheet."""

from django.conf import settings
from django.db import models
from django.utils import timezone


class SheetSource(models.Model):
    """A sheet the chat reads two columns of, read-only; the sheet stays its owner's."""

    key = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=120)
    enabled = models.BooleanField(default=False)
    spreadsheet_id = models.CharField(max_length=200)
    sheet_name = models.CharField(max_length=100)
    key_column = models.CharField(max_length=3, default="J")
    status_column = models.CharField(max_length=3, default="R")
    # รหัสพนักงาน of the reporter, comma separated (เจษ: "ใช้ Employee_ID แมพ"); a form whose
    # questions moved over the years keeps the ID in more than one column ("B,M").
    owner_columns = models.CharField(max_length=20, blank=True)
    first_row = models.PositiveSmallIntegerField(default=2)  # below the header
    last_synced_at = models.DateTimeField(null=True, blank=True)
    next_sync_at = models.DateTimeField(default=timezone.now)
    refresh_requested = models.BooleanField(default=False)
    syncing_until = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=200, blank=True)
    rows_read = models.PositiveIntegerField(default=0)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "pk"]

    def __str__(self):
        return self.name


class SheetEntry(models.Model):
    """One number of one sheet row with that row's status and reporter, as of the last good
    read; a row with an employee ID and no number keeps one entry with an empty number."""

    source = models.ForeignKey(SheetSource, on_delete=models.CASCADE, related_name="entries")
    number = models.CharField(max_length=40, blank=True)
    owner = models.CharField(max_length=32, blank=True, db_index=True)
    row = models.PositiveIntegerField()
    status = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["source", "number", "row"]
        indexes = [models.Index(fields=["source", "number"])]


class SheetStatusRun(models.Model):
    """The AI words one lookup's statuses (ADR 0027); the program's own reply is kept as the
    fallback and goes out if the AI fails or is late."""

    class Status(models.TextChoices):
        PENDING = "pending", "รอ"
        WORDED = "worded", "AI เรียบเรียงแล้ว"
        FALLBACK = "fallback", "ใช้ข้อความของโปรแกรม"

    inbox = models.OneToOneField(
        "operations.WebhookInbox", on_delete=models.PROTECT, related_name="sheet_status_run"
    )
    config_version = models.ForeignKey(
        "operations.AIConfigVersion", on_delete=models.PROTECT, related_name="+"
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    items = models.JSONField(default=list)  # [{"number", "count", "status"}] with a status
    replies = models.JSONField(default=list)  # the program's own messages
    worded = models.TextField(blank=True)
    attempts = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
