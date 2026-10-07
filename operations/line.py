"""Signed LINE webhook (M03): verify, persist inbox + dispatch intent, then answer quickly.

Nothing here waits for conversation logic, AI or outbound calls. An event is durable before the
2xx response, so LINE redelivery after a failure never loses or duplicates work.
"""

import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.db import DatabaseError, IntegrityError, transaction
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from operations.models import OutboxEvent, WebhookInbox
from operations.orchestration import EVENT_CONVERSATION

# Event types the conversation handles; others are kept but not dispatched.
HANDLED = {"message", "postback", "follow", "unfollow", "join"}


class InvalidEvent(ValueError):
    pass


def valid_signature(body, signature, secret):
    expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest())
    return hmac.compare_digest(expected, (signature or "").encode())


def _event_fields(event, now):
    if not isinstance(event, dict):
        raise InvalidEvent("event")
    event_id = event.get("webhookEventId")
    event_type = event.get("type")
    timestamp = event.get("timestamp")
    if not isinstance(event_id, str) or not 0 < len(event_id) <= 64:
        raise InvalidEvent("webhookEventId")
    if not isinstance(event_type, str) or not isinstance(timestamp, int):
        raise InvalidEvent("type/timestamp")
    source = event.get("source") if isinstance(event.get("source"), dict) else {}
    reply_token = event.get("replyToken") if isinstance(event.get("replyToken"), str) else ""
    context = event.get("deliveryContext") if isinstance(event.get("deliveryContext"), dict) else {}
    canonical = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        "webhook_event_id": event_id,
        "event_type": event_type[:32],
        "source_type": str(source.get("type", ""))[:16],
        "source_user_id": str(source.get("userId", ""))[:64],
        "payload": event,
        "payload_hash": hashlib.sha256(canonical.encode()).hexdigest(),
        "is_redelivery": bool(context.get("isRedelivery")),
        "occurred_at": datetime.fromtimestamp(timestamp / 1000, tz=UTC),
        "received_at": now,
        "reply_token": reply_token[:128],
        "reply_deadline": now + timedelta(seconds=settings.LINE_REPLY_DEADLINE_SECONDS)
        if reply_token
        else None,
    }


def store_events(events):
    """Persist new events and their dispatch intent; known event IDs are skipped (dedup)."""
    now = timezone.now()
    rows = [_event_fields(event, now) for event in events]
    created = 0
    with transaction.atomic():
        for row in rows:
            handled = row["event_type"] in HANDLED
            try:
                with transaction.atomic():
                    inbox = WebhookInbox.objects.create(
                        **row,
                        status=WebhookInbox.Status.PENDING
                        if handled
                        else WebhookInbox.Status.IGNORED,
                    )
            except IntegrityError:
                continue  # Same webhookEventId already stored: redelivery or retry.
            created += 1
            if handled:
                OutboxEvent.objects.create(
                    event_type=EVENT_CONVERSATION,
                    subject_type="line_inbox",
                    subject_id=str(inbox.pk),
                    correlation_id=uuid.uuid4(),
                    payload={"inbox_id": inbox.pk, "event_type": inbox.event_type},
                    occurred_at=inbox.occurred_at,
                    # Turns run in event-time order even when LINE delivers out of order.
                    next_attempt_at=min(inbox.occurred_at, now),
                )
    return created


@csrf_exempt
@require_POST
def webhook(request):
    secret = settings.LINE_CHANNEL_SECRET
    if not secret:
        return HttpResponse(status=503)
    if int(request.META.get("CONTENT_LENGTH") or 0) > settings.LINE_WEBHOOK_MAX_BYTES:
        return HttpResponse(status=413)
    body = request.body
    if not valid_signature(body, request.headers.get("X-Line-Signature"), secret):
        return HttpResponse(status=401)
    try:
        events = json.loads(body)["events"]
        if not isinstance(events, list):
            raise InvalidEvent("events")
        store_events(events)
    except (ValueError, KeyError, TypeError):
        return HttpResponse(status=400)
    except DatabaseError:
        # Not stored, so not acknowledged: LINE may redeliver.
        return HttpResponse(status=503)
    return HttpResponse(status=200)
