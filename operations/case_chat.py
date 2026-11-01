"""การคุยกับเจ้าหน้าที่ในเรื่อง (ADR 0024, grill กคว. 27 ต.ค. 2569 Q37 ถึง Q39, Q54).

Staff type on the case card or in the chat window of any staff page. A message goes as a free
LINE reply when the requester's last message is under 50 seconds old and its reply token is
still unused, which is the case while they chat: in prompt "case_chat" the bot keeps quiet so
staff can answer. ADR 0042 Q7, Q8: otherwise it is pushed at once (it counts against the OA's
monthly quota), and it reaches the requester exactly as typed, with a จบการคุย button only. The
requester's typed answers join the case until they tap จบการคุย or 24 hours pass after the last
staff message.
"""

import uuid
from datetime import datetime, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from operations import identity, orchestration
from operations.models import (
    CaseMessage,
    ChannelIdentity,
    Conversation,
    NotificationDelivery,
    OutboxEvent,
    WebhookInbox,
)

M = CaseMessage
WINDOW = timedelta(seconds=50)  # under LINE's reply token lifetime (about one minute)
OPEN_FOR = timedelta(hours=24)
END_LABEL = "จบการคุย"
END_POSTBACK = "m=chat_end"
MAX_TEXT = 2000


def _identity(case):
    return (
        ChannelIdentity.objects.filter(
            requester=case.requester, channel=ChannelIdentity.Channel.LINE
        )
        .exclude(status=ChannelIdentity.Status.REVOKED)
        .first()
    )


def fresh_inbox(case, now=None):
    """The requester's last message if it is under 50 seconds old and nothing answered it."""
    found = _identity(case)
    if found is None:
        return None
    now = now or timezone.now()
    return (
        WebhookInbox.objects.filter(
            source_user_id=found.external_id,
            reply_used_at__isnull=True,
            received_at__gte=now - WINDOW,
        )
        .exclude(reply_token="")
        .order_by("-received_at")
        .first()
    )


def _bubble(case, messages):
    """The staff messages as typed (ADR 0042 Q8), with จบการคุย and any buttons they carry."""
    from operations import conversation  # local: conversation builds on this module

    quick = next((m.quick for m in reversed(messages) if m.quick), [])
    return conversation.text_message(
        "\n".join(m.text for m in messages), [*(tuple(q) for q in quick), (END_LABEL, END_POSTBACK)]
    )


def _open_chat(found, case, now):
    conversation_row, _ = Conversation.objects.get_or_create(identity=found)
    conversation_row.prompt = "case_chat"
    conversation_row.prompt_data = {"case": str(case.pk), "until": (now + OPEN_FOR).isoformat()}
    conversation_row.save(update_fields=["prompt", "prompt_data", "updated_at"])


@transaction.atomic
def send(actor, case, text, contact=""):
    """A staff message: a free reply when the requester just wrote, else a push at once."""
    scope = identity.staff_scope(actor)
    if not identity.can_access_division(scope, case.owner_division_id):
        raise PermissionDenied
    text = str(text or "").strip()
    if not text:
        raise ValidationError("พิมพ์ข้อความก่อนส่ง")
    if len(text) > MAX_TEXT:
        raise ValidationError(f"ข้อความยาวไม่เกิน {MAX_TEXT} ตัวอักษร")
    found = _identity(case)
    if found is None:
        raise ValidationError("ผู้แจ้งเรื่องนี้ไม่ได้แจ้งผ่าน LINE ส่งข้อความทางนี้ไม่ได้")
    message = M.objects.create(
        case=case, direction=M.Direction.STAFF, text=text, author=actor, status=M.Status.HELD
    )
    identity.audit(actor, "case.chat_sent", case, number=case.number)
    return _try_now(case, found, message, push=True)


def send_system(actor, case, text, quick=None):
    """A message the system writes for staff (an appointment, the issue day), with buttons;
    nothing happens for a case filed without LINE."""
    found = _identity(case)
    if found is None:
        return None
    message = M.objects.create(
        case=case,
        direction=M.Direction.STAFF,
        # a requester may have several cases, so the message names its case
        text=(text if case.number in text else f"เรื่อง {case.number}\n{text}")[:MAX_TEXT],
        author=actor,
        quick=[list(q) for q in quick or []],
        status=M.Status.HELD,
    )
    return _try_now(case, found, message)


def _try_now(case, found, message, push=False):
    from operations import conversation  # local: conversation builds on this module

    inbox = fresh_inbox(case)
    if inbox is None and not push:
        return message
    held = list(M.objects.filter(case=case, status=M.Status.HELD).order_by("created_at"))
    now = timezone.now()
    _open_chat(found, case, now)
    M.objects.filter(pk__in=[m.pk for m in held]).update(
        status=M.Status.SENT, sent_at=now, inbox=inbox
    )
    bubble = _bubble(case, held)
    if inbox is not None:
        transaction.on_commit(lambda: conversation.send_reply(inbox.pk, [bubble]))
    else:
        _push(case, found, bubble, now)
    message.status = M.Status.SENT
    return message


def _push(case, found, bubble, now):
    """ADR 0042 Q7: the staff message goes now as a push, inside the monthly LINE budget."""
    N = NotificationDelivery
    delivery = N.objects.create(
        kind="case_chat",
        level=N.Level.NORMAL,
        identity=found,
        text=bubble["text"],
        message=bubble,
        case=case,
        send_after=now,
    )
    OutboxEvent.objects.create(
        event_type=orchestration.EVENT_NOTIFICATION,
        subject_type="delivery",
        subject_id=str(delivery.pk),
        case=case,
        case_revision=case.revision,
        correlation_id=uuid.uuid4(),
        payload={"delivery_id": delivery.pk, "kind": "case_chat", "level": N.Level.NORMAL},
        occurred_at=now,
        next_attempt_at=now,
    )


def deliver(turn):
    """At the requester's next message: the held staff messages, one bubble per case."""
    if turn.requester is None:
        return
    held = list(
        M.objects.filter(
            case__requester=turn.requester, direction=M.Direction.STAFF, status=M.Status.HELD
        )
        .select_related("case")
        .order_by("created_at")
    )
    if not held:
        return
    cases = {}
    for message in held:
        cases.setdefault(message.case, []).append(message)
    for case, messages in list(cases.items())[:3]:
        turn.replies.append(_bubble(case, messages))
        M.objects.filter(pk__in=[m.pk for m in messages]).update(
            status=M.Status.SENT, sent_at=turn.now, inbox=turn.inbox
        )
    _open_chat(turn.identity, list(cases)[min(len(cases), 3) - 1], turn.now)
    turn.conversation.refresh_from_db()


def chatting(turn):
    """The case this requester is chatting about, or None (the 24 hours ended it)."""
    if turn.conversation.prompt != "case_chat":
        return None
    data = turn.conversation.prompt_data
    until = data.get("until", "")
    if not until or datetime.fromisoformat(until) < turn.now:
        turn.set_prompt()
        return None
    from operations.models import Case

    return Case.objects.filter(pk=data.get("case"), requester=turn.requester).first()


def on_text(turn, case, text, message=None):
    """The requester's answer joins the case; the bot stays quiet so staff can reply free."""
    if " ".join(text.split()) == END_LABEL:
        return end(turn)
    M.objects.create(
        case=case,
        direction=M.Direction.REQUESTER,
        text=text[:5000],
        status=M.Status.RECEIVED,
        inbox=turn.inbox,
    )
    if message is not None:
        message.case = case
        message.save(update_fields=["case"])


def end(turn):
    from operations import conversation  # local: conversation builds on this module

    turn.set_prompt()
    conversation._menu(turn, "จบการคุยกับเจ้าหน้าที่แล้ว ดูเรื่องได้ที่ เรื่องของฉัน")


def thread(case):
    """The case card's chat (grill กคว. รอบใหม่ Q16), oldest first: staff and requester
    messages, the answers to staff who asked for more, and, faint, what the requester typed in
    LINE while filing the case. [{"who", "text", "at", "meta", "faint", "contact"}]"""
    from operations.models import ConversationMessage

    items, seen_inboxes = [], set()
    for m in M.objects.filter(case=case).select_related("author__staff_profile"):
        if m.inbox_id:
            seen_inboxes.add(m.inbox_id)
        if m.direction == M.Direction.STAFF:
            name = (
                m.author.staff_profile.display_name
                if m.author_id and hasattr(m.author, "staff_profile")
                else "ระบบ"
            )
            meta = f"{name} · {m.get_status_display()}"
        else:
            meta = "ผู้แจ้ง"
        items.append(
            {
                "who": m.direction,
                "text": m.text,
                "at": m.created_at,
                "meta": meta,
                "faint": False,
                "contact": m.contact,
                "id": f"m{m.pk}",
            }
        )
    for c in ConversationMessage.objects.filter(case=case).exclude(inbox_id__in=seen_inboxes):
        items.append(
            {
                "who": "requester",
                "text": c.text,
                "at": c.created_at,
                "meta": "ผู้แจ้ง · ข้อมูลเพิ่ม",
                "faint": False,
                "contact": "",
                "id": f"c{c.pk}",
            }
        )
    for c in ConversationMessage.objects.filter(draft__case=case, case__isnull=True):
        items.append(
            {
                "who": "requester",
                "text": c.text,
                "at": c.created_at,
                "meta": "ผู้แจ้ง · ตอนสร้างเรื่อง",
                "faint": True,
                "contact": "",
                "id": f"d{c.pk}",
            }
        )
    # ADR 0036 Q2: a photo or file from LINE, sent while filing (faint) or after.
    for a in case.attachments.filter(identity__isnull=False):
        noun = "ไฟล์" if "pdf" in (a.content_type or a.declared_name.lower()) else "รูป"
        items.append(
            {
                "who": "requester",
                "text": f"ผู้แจ้งส่ง{noun} 1 {noun} ดูได้ที่ส่วนไฟล์แนบ",
                "at": a.created_at,
                "meta": "ผู้แจ้ง · ตอนสร้างเรื่อง" if a.draft_id else "ผู้แจ้ง",
                "faint": bool(a.draft_id),
                "contact": "",
                "id": f"a{a.pk}",
            }
        )
    statuses = dict(case.Status.choices)
    for d in case.decisions.exclude(from_status="").select_related("actor__staff_profile"):
        if d.from_status == d.to_status:
            continue
        # ADR 0028 Q17: a status change is a strip across the chat, with the notice if any.
        notice = d.data.get("notice", "") if isinstance(d.data, dict) else ""
        items.append(
            {
                "who": "status",
                "text": notice,
                "at": d.created_at,
                "meta": statuses.get(d.to_status, d.to_status),
                "faint": False,
                "contact": "",
                "id": f"s{d.pk}",
            }
        )
    return sorted(items, key=lambda i: i["at"])


def last_from_requester(items):
    return next(
        (i["id"] for i in reversed(items) if i["who"] == "requester" and not i["faint"]), ""
    )
