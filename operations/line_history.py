"""The LINE chat of one requester as they saw it (ADR 0042 Q9), for the admin's message page.

Each webhook event the person sent as this requester (never while anonymous, ADR 0010) shows
what they typed or tapped, then what the bot answered; pushes sent to them join in time order.
Bot answers are kept since migration 0094; before it, only the typed messages exist, so the
older part of the chat shows those alone.
"""

from operations import conversation
from operations.models import (
    ChannelIdentity,
    ConversationMessage,
    LineMenuItem,
    NotificationDelivery,
    WebhookInbox,
)

LIMIT = 300  # newest events on one page
MENU_LABELS = {data: label for label, data in conversation.MENU_WORDS.items()}


def _actions(node):
    if isinstance(node, dict):
        if isinstance(node.get("action"), dict):
            yield node["action"]
        for value in node.values():
            yield from _actions(value)
    elif isinstance(node, list):
        for value in node:
            yield from _actions(value)


def _buttons(node):
    if isinstance(node, dict):
        if node.get("type") == "button" and isinstance(node.get("action"), dict):
            yield node["action"].get("label", "")
        for value in node.values():
            yield from _buttons(value)
    elif isinstance(node, list):
        for value in node:
            yield from _buttons(value)


def _texts(node):
    if isinstance(node, dict):
        if node.get("type") == "text" and node.get("text"):
            yield node["text"]
        for key in ("header", "hero", "body", "footer", "contents"):
            if key in node:
                yield from _texts(node[key])
    elif isinstance(node, list):
        for value in node:
            yield from _texts(value)


def bot_message(message):
    """One LINE message as the page draws it: text with its quick replies, or cards."""
    quick = [
        item["action"].get("label", "")
        for item in (message.get("quickReply") or {}).get("items", [])
    ]
    if message.get("type") == "text":
        return {"kind": "text", "text": message.get("text", ""), "quick": quick}
    if message.get("type") == "flex":
        contents = message.get("contents") or {}
        bubbles = contents.get("contents", []) if contents.get("type") == "carousel" else [contents]
        cards = [{"lines": list(_texts(b)), "buttons": list(_buttons(b))} for b in bubbles]
        return {"kind": "cards", "cards": cards, "quick": quick, "text": message.get("altText", "")}
    if message.get("type") in {"image", "video"}:
        return {"kind": "text", "text": "[รูปภาพ]" if message["type"] == "image" else "[วิดีโอ]"}
    return {
        "kind": "text",
        "text": message.get("altText") or message.get("text") or "",
        "quick": quick,
    }


def _tap_label(data, params, earlier):
    """What a tap showed in the chat: the button's words in an earlier answer, else the menu."""
    if params.get("date"):
        return f"เลือกวันที่ {params['date']}"
    for replies in reversed(earlier):
        for action in _actions(replies):
            if action.get("data") == data:
                return action.get("displayText") or action.get("label") or "เลือกปุ่ม"
    if data.startswith("m=item&id="):
        item = LineMenuItem.objects.filter(pk=data.split("=")[-1]).first()
        if item is not None:
            return item.label
    return MENU_LABELS.get(data, "เลือกเมนู")


def _user_side(inbox, earlier):
    payload = inbox.payload or {}
    if inbox.event_type == "follow":
        return {"kind": "system", "text": "เพิ่มเพื่อน"}
    if inbox.event_type == "postback":
        postback = payload.get("postback") or {}
        label = _tap_label(postback.get("data", ""), postback.get("params") or {}, earlier)
        return {"kind": "tap", "text": label}
    message = payload.get("message") or {}
    kinds = {"image": "[รูปภาพ]", "video": "[วิดีโอ]", "location": "[ตำแหน่ง]", "sticker": "[สติกเกอร์]"}
    if message.get("type") == "text":
        return {"kind": "text", "text": message.get("text", "")}
    return {"kind": "text", "text": kinds.get(message.get("type"), "[ข้อความ]")}


def timeline(requester):
    """[{side, at, ...}] oldest first: "me" for the requester, "bot" for the OA."""
    lines = list(
        ChannelIdentity.objects.filter(requester=requester).values_list("pk", "external_id")
    )
    ids = [external for _, external in lines]
    events = list(
        WebhookInbox.objects.filter(requester=requester, source_user_id__in=ids).order_by(
            "-occurred_at", "-pk"
        )[:LIMIT]
    )[::-1]
    items, earlier = [], []
    for inbox in events:
        items.append({"side": "me", "at": inbox.occurred_at, **_user_side(inbox, earlier)})
        for message in inbox.replies or []:
            items.append(
                {
                    "side": "bot",
                    "at": inbox.reply_used_at or inbox.occurred_at,
                    **bot_message(message),
                }
            )
        earlier.append(inbox.replies or [])
    start = events[0].occurred_at if events else None
    typed = ConversationMessage.objects.filter(requester=requester)
    if start is not None:
        typed = typed.filter(created_at__lt=start)  # older than the kept answers
    for message in typed.order_by("-created_at")[:LIMIT]:
        items.append({"side": "me", "at": message.created_at, "kind": "text", "text": message.text})
    pushes = NotificationDelivery.objects.filter(
        identity_id__in=[pk for pk, _ in lines],
        channel=NotificationDelivery.Channel.LINE_PUSH,
        sent_at__isnull=False,
    )
    for push in pushes.select_related("case").order_by("-sent_at")[:LIMIT]:
        if push.case_id and push.case.requester_id != requester.pk:
            continue  # a notice about an anonymous report of the same LINE account
        shown = bot_message(push.message or {"type": "text", "text": push.text})
        items.append({"side": "bot", "at": push.sent_at, "push": True, **shown})
    items.sort(key=lambda item: item["at"])
    return items[-LIMIT * 2 :]
