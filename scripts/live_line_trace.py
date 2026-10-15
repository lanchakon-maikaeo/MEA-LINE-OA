"""Trace of a live LINE session (M13 A01, G06) for the one LINE account in LINE_ALLOWED_RECIPIENTS:
what went through each part of the system since a time, with no LINE ID, names or message text.

    python3 -m uv run --locked --env-file .env python scripts/live_line_trace.py \
        --since 2026-10-12T15:00:00+07:00 --out reports/release/m13-rc1/evidence/live-line.json

A journey is one case the account submitted on LINE. It is complete when the case closed after
staff acted on the web and at least one push from the bot reached LINE.
"""

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True, help="ISO time with a zone")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    since = datetime.fromisoformat(args.since)
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()
    from django.conf import settings

    from operations.models import (
        AIRun,
        Case,
        ChannelIdentity,
        NotificationDelivery,
        OutboxEvent,
        WebhookInbox,
    )

    accounts = sorted(settings.LINE_ALLOWED_RECIPIENTS)
    if len(accounts) != 1:
        sys.exit("keep exactly one LINE ID in LINE_ALLOWED_RECIPIENTS for the live session")
    account = accounts[0]
    identity = ChannelIdentity.objects.filter(channel="line", external_id=account).first()
    inbox = WebhookInbox.objects.filter(source_user_id=account, received_at__gte=since)
    runs = AIRun.objects.filter(identity=identity, created_at__gte=since) if identity else []
    ai_events = OutboxEvent.objects.filter(
        event_type="conversation.ai_requested", occurred_at__gte=since
    )
    requester = identity.requester if identity else None
    cases = (
        Case.objects.filter(requester=requester, channel="line", submitted_at__gte=since)
        if requester
        else Case.objects.none()
    )
    journeys = []
    for case in cases.order_by("submitted_at"):
        decisions = list(case.decisions.order_by("created_at"))
        pushes = NotificationDelivery.objects.filter(case=case, channel="line_push")
        sent = pushes.filter(status="sent").order_by("sent_at")
        first_push = sent.first()
        staff = [d.action for d in decisions if d.actor_label.startswith("user:")]
        answered = [
            d.action
            for d in decisions
            if d.actor_label.startswith("requester:")
            and first_push
            and d.created_at > first_push.sent_at
        ]
        notify = OutboxEvent.objects.filter(case=case, event_type="notification.requested")
        journeys.append(
            {
                "case": case.number,
                "kind": case.kind,
                "owner_division": case.owner_division.code,
                "status": case.status,
                "staff_actions": staff,
                "pushes": dict(Counter(pushes.values_list("status", flat=True))),
                "push_kinds": list(sent.values_list("kind", flat=True)),
                "notifications_via_n8n": notify.filter(status="done").count(),
                "requester_answered_push": answered,
                "complete": case.status == Case.Status.CLOSED and bool(staff) and sent.exists(),
            }
        )
    trace = {
        "since": since.isoformat(),
        "account": {
            "registered": bool(requester),
            "status": identity.status if identity else None,
        },
        "summary": {
            "webhook_events": inbox.count(),
            "events_by_type": dict(Counter(inbox.values_list("event_type", flat=True))),
            "replies": dict(Counter(inbox.values_list("reply_status", flat=True))),
            "ai_runs": dict(Counter(r.status for r in runs)),
            "ai_models": sorted(
                {a.get("model", "") for r in runs for a in r.attempts if a.get("model")}
            ),
            "ai_turns_via_n8n": ai_events.filter(status="done").count(),
            "cases": len(journeys),
            "kinds": sorted({j["kind"] for j in journeys}),
            "complete": sum(j["complete"] for j in journeys),
        },
        "journeys": journeys,
    }
    text = json.dumps(trace, ensure_ascii=False, indent=2)
    if account in text:
        sys.exit("refusing to write: the trace would contain the LINE ID")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
