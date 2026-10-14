"""Outbox claim/lease (M04): the only way any consumer takes work.

Two consumers share the outbox without competing, because each event type has one owner:
the Django worker runs chat turns and notification planning (fast, internal), and n8n runs
notification sending and AI steps (scheduled, retried). A claim hands out a lease token with an
expiry; complete/fail with a stale or expired token changes nothing, and expired leases are
claimed again with the same event ID so retries stay idempotent downstream.
"""

import random
import secrets
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from operations.models import OutboxEvent

S = OutboxEvent.Status

EVENT_CONVERSATION = "conversation.message_received"
EVENT_CASE_SUBMITTED = "case.submitted"
EVENT_DECISION = "decision.recorded"
EVENT_REGISTRATION = "registration.decided"
EVENT_NOTIFICATION = "notification.requested"
EVENT_AI = "conversation.ai_requested"
EVENT_TRIAGE_AI = "case.triage_ai_requested"  # M06: facts for routing (ADR 0002)
EVENT_ROUTING_AI = "case.routing_ai_requested"  # ADR 0013: a division from the duties

# One owner per event type (E.6: no second dispatcher competes for the same work).
DJANGO_TYPES = frozenset(
    {
        EVENT_CONVERSATION,
        EVENT_CASE_SUBMITTED,
        EVENT_DECISION,
        EVENT_REGISTRATION,
        EVENT_ROUTING_AI,
    }
)
N8N_TYPES = frozenset({EVENT_NOTIFICATION, EVENT_AI, EVENT_TRIAGE_AI})
MAX_ATTEMPTS = {EVENT_NOTIFICATION: 8, EVENT_AI: 2, EVENT_TRIAGE_AI: 2, EVENT_ROUTING_AI: 2}
DEFAULT_MAX_ATTEMPTS = 5
LEASE_SECONDS = (10, 300)
MAX_LEASE_AGE = timedelta(minutes=10)


class LeaseError(Exception):
    """Unknown event, wrong or expired lease token: the caller must not act on it."""


def max_attempts(event_type):
    return MAX_ATTEMPTS.get(event_type, DEFAULT_MAX_ATTEMPTS)


def backoff(attempt, retry_after=None):
    """Exponential backoff with ±20% jitter, at least LINE's Retry-After, at most one hour."""
    delay = min(5 * 2 ** max(attempt - 1, 0), 3600) * random.uniform(0.8, 1.2)  # noqa: S311
    return timedelta(seconds=max(delay, retry_after or 0))


def _clamp_lease(seconds):
    low, high = LEASE_SECONDS
    return timedelta(seconds=min(max(int(seconds or low), low), high))


def claim(event_types, limit=1, lease_seconds=30, now=None):
    """Atomically lease due events of the given types; events past their attempts go dead."""
    now = now or timezone.now()
    claimed = []
    with transaction.atomic():
        due = (
            OutboxEvent.objects.select_for_update(skip_locked=True)
            .filter(event_type__in=list(event_types))
            .filter(
                Q(status=S.PENDING, next_attempt_at__lte=now)
                | Q(status=S.LEASED, lease_expires_at__lt=now)
            )
            .order_by("next_attempt_at", "id")[: max(1, min(int(limit), 50))]
        )
        for event in due:
            if event.attempts >= max_attempts(event.event_type):
                # An expired lease after the last allowed attempt goes to the dead-letter view.
                event.status = S.FAILED
                event.lease_token = ""
                event.last_error = event.last_error or "lease expired on last attempt"
                event.save(update_fields=["status", "lease_token", "last_error"])
                continue
            event.status = S.LEASED
            event.attempts += 1
            event.lease_token = secrets.token_urlsafe(24)
            event.leased_at = now
            event.lease_expires_at = now + _clamp_lease(lease_seconds)
            event.save(
                update_fields=["status", "attempts", "lease_token", "leased_at", "lease_expires_at"]
            )
            claimed.append(event)
    return claimed


def _leased(event_id, lease_token, now):
    event = OutboxEvent.objects.select_for_update().filter(event_id=event_id).first()
    # Which check failed goes into the traceback only (the API answers LEASE_INVALID), so a
    # rare failure can be explained (a full test run once failed here and never again).
    if event is None:
        raise LeaseError("Lease is not valid (no such event).")
    if event.status != S.LEASED:
        raise LeaseError(f"Lease is not valid (status {event.status}).")
    if not lease_token or not secrets.compare_digest(event.lease_token, str(lease_token)):
        raise LeaseError("Lease is not valid (token).")
    if event.lease_expires_at < now:
        expired = event.lease_expires_at.isoformat()
        raise LeaseError(f"Lease is not valid (expired {expired} < {now.isoformat()}).")
    return event


def renew(event_id, lease_token, lease_seconds=30, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        event = _leased(event_id, lease_token, now)
        expires = now + _clamp_lease(lease_seconds)
        if expires - event.leased_at > MAX_LEASE_AGE:
            raise LeaseError("Lease cannot be extended further.")
        event.lease_expires_at = expires
        event.save(update_fields=["lease_expires_at"])
        return event


def complete(event_id, lease_token, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        event = _leased(event_id, lease_token, now)
        event.status = S.DONE
        event.lease_token = ""
        event.completed_at = now
        event.save(update_fields=["status", "lease_token", "completed_at"])
        return event


def fail(event_id, lease_token, error="", retry_after=None, now=None):
    """Record a failed attempt: retry later with backoff, or dead-letter after the last one."""
    now = now or timezone.now()
    with transaction.atomic():
        event = _leased(event_id, lease_token, now)
        event.lease_token = ""
        event.last_error = str(error)[:200]
        if event.attempts >= max_attempts(event.event_type):
            event.status = S.FAILED
        else:
            event.status = S.PENDING
            event.next_attempt_at = now + backoff(event.attempts, retry_after)
        event.save(update_fields=["status", "lease_token", "last_error", "next_attempt_at"])
        return event


def requeue(event, now=None):
    """Dead-letter retry by a person with rights (admin console): fresh attempts, due now."""
    now = now or timezone.now()
    updated = OutboxEvent.objects.filter(pk=event.pk, status=S.FAILED).update(
        status=S.PENDING, attempts=0, next_attempt_at=now, lease_token="", last_error=""
    )
    return bool(updated)


def health(now=None):
    """Oldest due-but-unclaimed age per event type and dead-letter count, for alerts."""
    now = now or timezone.now()
    oldest = {}
    for event_type, due_at in (
        OutboxEvent.objects.filter(status=S.PENDING, next_attempt_at__lte=now)
        .order_by("event_type", "next_attempt_at")
        .distinct("event_type")
        .values_list("event_type", "next_attempt_at")
    ):
        oldest[event_type] = int((now - due_at).total_seconds())
    return {
        "oldest_pending_seconds": oldest,
        "dead_letter": OutboxEvent.objects.filter(status=S.FAILED).count(),
    }
