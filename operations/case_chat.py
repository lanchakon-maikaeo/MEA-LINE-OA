"""การคุยกับเจ้าหน้าที่ในเรื่อง (ADR 0024, grill กคว. 27 ต.ค. 2569 Q37 ถึง Q39, Q54).

Staff type on the case card; nothing is pushed. A message goes at once as a free LINE reply when
the requester's last message is under 50 seconds old and its reply token is still unused, which
is the case while they chat: in prompt "case_chat" the bot keeps quiet so staff can answer.
Otherwise the message is held and goes out with the reply to the requester's next message. The
first message of each staff member in a case carries how to reach them. The requester's typed
answers join the case until they tap จบการคุย or 24 hours pass after the last staff message.
"""

from datetime import datetime, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from operations import identity
from operations.models import (
    CaseMessage,
    ChannelIdentity,
    Conversation,
    StaffProfile,
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


def contact_of(user):
    """What the user signed with last time, else their name (staff edit it on the card)."""
    last = M.objects.filter(author=user).exclude(contact="").order_by("-created_at").first()
    if last is not None:
        return last.contact
    profile = StaffProfile.objects.filter(user=user).first()
    return profile.display_name if profile else user.get_username()


def needs_contact(user, case):
    return not M.objects.filter(case=case, author=user, direction=M.Direction.STAFF).exists()


def _bubble(case, messages):
    from operations import conversation  # local: conversation builds on this module

    lines = [f"ข้อความจากเจ้าหน้าที่ เรื่อง {case.number}"]
    for message in messages:
        lines.append(message.text)
        if message.contact:
            lines.append(f"ติดต่อ: {message.contact}")
    lines.append("พิมพ์ตอบในแชตนี้ได้เลย ข้อความจะเข้าเรื่องนี้")
    quick = next((m.quick for m in reversed(messages) if m.quick), [])
    return conversation.text_message(
        "\n".join(lines), [*(tuple(q) for q in quick), (END_LABEL, END_POSTBACK)]
    )


def _open_chat(found, case, now):
    conversation_row, _ = Conversation.objects.get_or_create(identity=found)
    conversation_row.prompt = "case_chat"
    conversation_row.prompt_data = {"case": str(case.pk), "until": (now + OPEN_FOR).isoformat()}
    conversation_row.save(update_fields=["prompt", "prompt_data", "updated_at"])


@transaction.atomic
def send(actor, case, text, contact=""):
    """A staff message from the case card: sent now when it can go free, else held."""
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
    contact = " ".join(str(contact or "").split())[:200]
    if needs_contact(actor, case) and not contact:
        raise ValidationError("ข้อความแรกในเรื่องนี้ ใส่ชื่อและเบอร์ติดต่อของคุณด้วย")
    message = M.objects.create(
        case=case,
        direction=M.Direction.STAFF,
        text=text,
        author=actor,
        contact=contact if needs_contact(actor, case) else "",
        status=M.Status.HELD,
    )
    identity.audit(actor, "case.chat_sent", case, number=case.number)
    return _try_now(case, found, message)


def send_system(actor, case, text, quick=None):
    """A message the system writes for staff (an appointment, the issue day), with buttons;
    nothing happens for a case filed without LINE."""
    found = _identity(case)
    if found is None:
        return None
    message = M.objects.create(
        case=case,
        direction=M.Direction.STAFF,
        text=text[:MAX_TEXT],
        author=actor,
        quick=[list(q) for q in quick or []],
        status=M.Status.HELD,
    )
    return _try_now(case, found, message)


def _try_now(case, found, message):
    from operations import conversation  # local: conversation builds on this module

    inbox = fresh_inbox(case)
    if inbox is not None:
        held = list(M.objects.filter(case=case, status=M.Status.HELD).order_by("created_at"))
        now = timezone.now()
        _open_chat(found, case, now)
        M.objects.filter(pk__in=[m.pk for m in held]).update(
            status=M.Status.SENT, sent_at=now, inbox=inbox
        )
        transaction.on_commit(lambda: conversation.send_reply(inbox.pk, [_bubble(case, held)]))
        message.status = M.Status.SENT
    return message


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
    """The case card's chat: staff and requester messages in order."""
    return list(M.objects.filter(case=case).select_related("author__staff_profile"))
