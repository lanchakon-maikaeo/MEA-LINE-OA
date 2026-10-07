"""Internal API for n8n (M04): `/internal/v1/...`, bearer service token, JSON only.

The token only claims n8n-owned event types, sends notifications through Django and runs
maintenance. It cannot reach admin actions, user actions, LINE credentials or AI keys, and the
workflow never chooses recipients: Django resolves and re-checks them.
"""

import json
import secrets
from functools import wraps

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from operations import ai_chat, notifications, orchestration
from operations.models import OutboxEvent


def service_endpoint(view):
    @csrf_exempt
    @require_POST
    @wraps(view)
    def wrapper(request):
        token = settings.ORCHESTRATOR_TOKEN
        header = request.headers.get("Authorization", "")
        if len(token) < 32 or not secrets.compare_digest(header, f"Bearer {token}"):
            return JsonResponse({"error": "AUTH_REQUIRED"}, status=401)
        try:
            body = json.loads(request.body or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return JsonResponse({"error": "VALIDATION_ERROR"}, status=400)
        try:
            return JsonResponse(view(body))
        except orchestration.LeaseError:
            return JsonResponse({"error": "LEASE_INVALID"}, status=409)
        except (ValidationError, ValueError, TypeError, KeyError):
            return JsonResponse({"error": "VALIDATION_ERROR"}, status=400)

    return wrapper


def _event_json(event):
    return {
        "event_id": str(event.event_id),
        "event_type": event.event_type,
        "subject_type": event.subject_type,
        "subject_id": event.subject_id,
        "case_id": str(event.case_id) if event.case_id else None,
        "case_revision": event.case_revision,
        "schema_version": event.schema_version,
        "correlation_id": str(event.correlation_id),
        "occurred_at": event.occurred_at.isoformat(),
        "attempt": event.attempts,
        "lease_token": event.lease_token,
        "lease_expires_at": event.lease_expires_at.isoformat(),
        "payload": event.payload,
    }


@service_endpoint
def claim(body):
    requested = body.get("event_types") or sorted(orchestration.N8N_TYPES)
    allowed = [t for t in requested if t in orchestration.N8N_TYPES]
    events = orchestration.claim(allowed, body.get("limit", 5), body.get("lease_seconds", 60))
    return {"events": [_event_json(e) for e in events]}


@service_endpoint
def renew(body):
    event = orchestration.renew(
        body.get("event_id"), body.get("lease_token"), body.get("lease_seconds", 60)
    )
    return {"lease_expires_at": event.lease_expires_at.isoformat()}


@service_endpoint
def complete(body):
    orchestration.complete(body.get("event_id"), body.get("lease_token"))
    return {"status": "done"}


@service_endpoint
def fail(body):
    event = orchestration.fail(
        body.get("event_id"),
        body.get("lease_token"),
        body.get("error", ""),
        body.get("retry_after"),
    )
    return {"status": event.status}


@service_endpoint
def send_notification(body):
    """Send the delivery behind a claimed notification event and settle the event."""
    event_id, lease_token = body.get("event_id"), body.get("lease_token")
    event = OutboxEvent.objects.filter(event_id=event_id).first()
    if event is None or event.event_type != orchestration.EVENT_NOTIFICATION:
        raise orchestration.LeaseError
    # Validate the lease before any network call, so a stale execution sends nothing.
    orchestration.renew(event_id, lease_token, 60)
    result = notifications.send(event.payload["delivery_id"])
    if result.final:
        orchestration.complete(event_id, lease_token)
        return {"outcome": result.outcome, "event": "done"}
    settled = orchestration.fail(
        event_id, lease_token, f"push {result.outcome}", result.retry_after
    )
    return {"outcome": result.outcome, "event": settled.status}


@service_endpoint
def ai_extract(body):
    """Run the AI step behind a claimed `conversation.ai_requested` event, then settle it."""
    event_id, lease_token = body.get("event_id"), body.get("lease_token")
    event = OutboxEvent.objects.filter(event_id=event_id).first()
    if event is None or event.event_type != orchestration.EVENT_AI:
        raise orchestration.LeaseError
    orchestration.renew(event_id, lease_token, 60)
    outcome = ai_chat.process(event.payload["ai_run_id"])
    orchestration.complete(event_id, lease_token)
    return {"outcome": outcome}


@service_endpoint
def ai_triage(body):
    """Run the AI triage behind a claimed `case.triage_ai_requested` event, then settle it."""
    from operations import ai_triage as triage

    event_id, lease_token = body.get("event_id"), body.get("lease_token")
    event = OutboxEvent.objects.filter(event_id=event_id).first()
    if event is None or event.event_type != orchestration.EVENT_TRIAGE_AI:
        raise orchestration.LeaseError
    orchestration.renew(event_id, lease_token, 60)
    outcome = triage.process(event.payload["ai_triage_run_id"])
    orchestration.complete(event_id, lease_token)
    return {"outcome": outcome}


@service_endpoint
def maintenance(body):
    return notifications.maintenance()
