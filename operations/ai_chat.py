"""AI in the LINE chat (M05): free text fills the draft, the requester still confirms everything.

A free-text message (where the chat expects a button, or with no draft yet) becomes an AIRun plus
a `conversation.ai_requested` outbox event and no immediate reply. n8n claims the event and calls
`process` through the internal API; Django calls the model, validates the proposal, fills the
draft and answers with the message's own reply token. If nothing has answered after
AI_REPLY_FALLBACK_SECONDS the worker's watchdog answers with the field-by-field question instead,
so every message gets exactly one reply before the LINE deadline and no push is needed.
"""

import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from operations import ai, ai_grounding, conversation, line_api, orchestration
from operations.conversation import FIELDS, KIND_LABELS, D, Turn
from operations.models import (
    AIRun,
    ChannelIdentity,
    Conversation,
    ConversationDraft,
    ConversationMessage,
    EquipmentType,
    OutboxEvent,
    Requester,
    WebhookInbox,
)

R = AIRun.Status
BUTTON_STEPS = {"kind", "category", "item", "more", "summary"}


def wants_ai(turn, draft, text):
    """Free text that the guided flow cannot use as an answer, when AI is configured."""
    if not settings.AI_ENABLED or len(text.strip()) < 4 or ai.active_version() is None:
        return False
    if draft is None:
        # Several open drafts and none chosen: the user must pick one first (no guessing).
        return not conversation._open_drafts(turn).exists()
    return draft.step in BUTTON_STEPS


def queue(turn, draft, text):
    version = ai.active_version()
    run = AIRun.objects.create(
        identity=turn.identity,
        inbox=turn.inbox,
        draft=draft,
        draft_revision=draft.revision if draft else None,
        config_version=version,
        prompt_version=ai.prompt_version_for(version),
        schema_version=ai.SCHEMA_VERSION,
        # Messages already prepared this turn (e.g. "มีอัปเดตใหม่") go out with the AI answer.
        result={"prefix": turn.replies[:2]},
    )
    turn.replies.clear()
    OutboxEvent.objects.create(
        event_type=orchestration.EVENT_AI,
        subject_type="ai_run",
        subject_id=str(run.pk),
        correlation_id=uuid.uuid4(),
        payload={"ai_run_id": run.pk, "inbox_id": turn.inbox.pk},
        occurred_at=turn.now,
    )
    return run


def start_loading(inbox_id):
    """LINE's free typing indicator while the model works (only where replies are allowed)."""
    inbox = WebhookInbox.objects.filter(pk=inbox_id, ai_run__status=R.PENDING).first()
    if inbox is None or not conversation._may_send(inbox.source_user_id)[0]:
        return
    try:
        line_api.call(
            "/v2/bot/chat/loading/start", {"chatId": inbox.source_user_id, "loadingSeconds": 30}
        )
    except (line_api.LineApiError, OSError):
        pass  # Cosmetic only.


def _turn_for(run):
    identity = ChannelIdentity.objects.select_for_update().get(pk=run.identity_id)
    conv, _ = Conversation.objects.get_or_create(identity=identity)
    turn = Turn(run.inbox, run.inbox.payload, identity, conv, timezone.now())
    requester = identity.requester
    if (
        identity.status != ChannelIdentity.Status.ACTIVE
        or requester is None
        or requester.status != Requester.Status.ACTIVE
        or run.inbox.source_user_id != identity.external_id
        or run.inbox.channel != identity.channel
        or (
            conv.active_draft is not None
            and (
                conv.active_draft.identity_id != identity.pk
                or conv.active_draft.requester_id != requester.pk
            )
        )
    ):
        turn.say("บัญชีผู้แจ้งของคุณใช้งานไม่ได้ในขณะนี้ กรุณาติดต่อ ฝอจ.")
        return turn, False
    turn.requester = requester
    return turn, True


def _current_draft(turn):
    draft = turn.conversation.active_draft
    return draft if draft is not None and draft.status == D.OPEN else None


def _blocked(run, turn, usable):
    """Check current authority and revision both before disclosure and before applying output."""
    if not usable:
        return R.FAILED
    draft = _current_draft(turn)
    if (draft.pk if draft else None) != run.draft_id or (
        draft is not None and draft.revision != run.draft_revision
    ):
        turn.say("ข้อความก่อนหน้าถูกแทนด้วยขั้นตอนล่าสุด")
        conversation._reprompt(turn, draft)
        return R.STALE
    if not settings.AI_ENABLED:
        turn.say("ระบบอ่านข้อความปิดอยู่ ขอถามทีละข้อ")
        conversation._reprompt(turn, draft)
        return R.FALLBACK
    return None


def _finish(run, status, attempts, extraction, turn):
    run.status = status
    run.attempts = attempts
    run.finished_at = timezone.now()
    if extraction is not None:
        run.result = {
            **run.result,
            "requests": [
                {
                    "intent": p.intent,
                    "items": [
                        {
                            "code": i.code,
                            "text": i.text,
                            "quantity": str(i.quantity) if i.quantity else None,
                        }
                        for i in p.items
                    ],
                    "fields": p.fields,
                    "field_sources": p.field_sources,
                }
                for p in extraction.requests
            ],
        }
    run.save(update_fields=["status", "attempts", "finished_at", "result"])
    turn.conversation.updates_seen_at = timezone.now()
    turn.conversation.save(update_fields=["updates_seen_at", "updated_at"])
    return run.result.get("prefix", []) + turn.replies


def process(run_id):
    """Called for a claimed `conversation.ai_requested` event (n8n → internal API)."""
    if not AIRun.objects.filter(pk=run_id, status=R.PENDING).update(status=R.RUNNING):
        return "skipped"  # The watchdog already answered this message.
    run = AIRun.objects.select_related("inbox", "draft", "config_version").get(pk=run_id)
    with transaction.atomic():
        turn, usable = _turn_for(run)
        run = (
            AIRun.objects.select_for_update()
            .select_related("inbox", "config_version")
            .get(pk=run_id)
        )
        if run.status != R.RUNNING:
            return "late"
        status = _blocked(run, turn, usable)
        message = ConversationMessage.objects.filter(
            inbox=run.inbox, identity_id=run.identity_id
        ).first()
        source_text = conversation._event_text(turn)
        if message is None or source_text is None:
            status = R.FAILED
        if status:
            replies = _finish(run, status, [{"outcome": "blocked before provider"}], None, turn)
        context = ai.draft_context(_current_draft(turn)) if not status else None
    if status:
        conversation.send_reply(run.inbox_id, replies[:5])
        return status
    received = run.inbox.received_at
    budget = min(
        settings.AI_TOTAL_BUDGET_SECONDS,
        (
            received + timedelta(seconds=settings.AI_REPLY_FALLBACK_SECONDS - 3) - timezone.now()
        ).total_seconds(),
    )
    extraction, attempts = (None, [{"outcome": "skipped: no time left"}])
    if message is not None and budget >= 2:
        extraction, attempts = ai.extract(source_text, context, run.config_version, budget)
    with transaction.atomic():
        turn, usable = _turn_for(run)
        run = AIRun.objects.select_for_update().get(pk=run_id)
        if run.status != R.RUNNING:
            # Answered by the watchdog meanwhile: keep the result for review, send nothing.
            AIRun.objects.filter(pk=run_id).update(attempts=attempts)
            return "late"
        draft = _current_draft(turn) if usable else None
        status = _blocked(run, turn, usable)
        if status:
            extraction = None
        elif extraction is None:
            status = R.FAILED
            turn.say("ระบบอ่านข้อความไม่สำเร็จ ขอถามทีละข้อ")
            conversation._reprompt(turn, draft)
        else:
            status = R.APPLIED
            apply(turn, draft, extraction, run)
        replies = _finish(run, status, attempts, extraction, turn)
    conversation.send_reply(run.inbox_id, replies[:5])
    return status


def expire_waiting(now=None):
    """Watchdog: answer with the field question when AI has not answered in time."""
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=settings.AI_REPLY_FALLBACK_SECONDS)
    handled = 0
    for run_id in AIRun.objects.filter(
        status__in=[R.PENDING, R.RUNNING], inbox__received_at__lte=cutoff
    ).values_list("pk", flat=True):
        with transaction.atomic():
            run = AIRun.objects.select_related("inbox").get(pk=run_id)
            turn, usable = _turn_for(run)
            run = AIRun.objects.select_for_update().select_related("inbox").get(pk=run_id)
            if run.status not in (R.PENDING, R.RUNNING):
                continue
            if usable:
                turn.say("ระบบอ่านข้อความไม่ทันเวลา ขอถามทีละข้อ")
                conversation._reprompt(turn, _current_draft(turn))
            replies = _finish(run, R.FALLBACK, run.attempts, None, turn)
        conversation.send_reply(run.inbox_id, replies[:5])
        handled += 1
    return handled


# --- Filling drafts from a validated proposal -------------------------------------------------


def apply(turn, draft, extraction, run):
    proposals = [p for p in extraction.requests if p.intent in ai.KIND_FOR_INTENT]
    if not proposals:
        turn.say("ยังไม่เข้าใจข้อความ กรุณาเลือกจากปุ่ม หรือพิมพ์รายละเอียดเพิ่ม")
        return conversation._reprompt(turn, draft)
    first, extra = proposals[0], proposals[1:]
    kind = ai.KIND_FOR_INTENT[first.intent]
    has_content = draft is not None and (draft.data.get("lines") or draft.data.get("fields"))
    if draft is None or (draft.kind and draft.kind != kind and has_content):
        draft = conversation._new_draft(turn)
    notes = _merge(turn, draft, first, run, extraction.screened_draft_fields)
    for proposal in extra:
        other = ConversationDraft.objects.create(
            identity=turn.identity,
            requester=turn.requester,
            step="kind",
            data={"lines": [], "fields": {}},
            updated_at=turn.now,
        )
        _merge(turn, other, proposal, run)
    lines = [f"เติมข้อมูลจากข้อความแล้ว ({KIND_LABELS[draft.kind]}) กรุณาตรวจก่อนยืนยัน"]
    lines += notes
    if extra:
        lines.append(f"พบอีก {len(extra)} เรื่องในข้อความ บันทึกเป็นร่างไว้แล้ว (เลือกได้จาก 'ส่งเรื่องใหม่')")
    turn.say("\n".join(lines))
    conversation._ask(turn, draft)


def _format(quantity):
    return format(quantity.normalize(), "f")


def _merge(turn, draft, proposal, run, screened=None):
    """Merge into the draft; anything missing or unknown is asked by the guided flow next.

    `screened`: the draft's text fields with directive segments removed by the harness screen
    ({name: text or None}); the requester's own words in a field survive an appended attack.
    """
    kind = ai.KIND_FOR_INTENT[proposal.intent]
    if draft.kind and draft.kind != kind:
        kind = draft.kind
    allowed = {name for name, _, _ in FIELDS[kind]}
    stored = dict(draft.data.get("fields", {}))
    for name, value in (screened or {}).items():
        if name in stored:
            stored[name] = value
    data = {
        "lines": list(draft.data.get("lines", [])),
        "fields": {
            k: v
            for k, v in stored.items()
            if k in allowed
            and v is not None  # "" is a skipped optional field and stays skipped
            and ai_grounding.safe_text(v)
            and k not in proposal.rejected_fields
        },
    }
    items = {
        i.code: i
        for i in EquipmentType.objects.filter(code__in=[p.code for p in proposal.items if p.code])
    }
    lines = {line["item_code"]: dict(line) for line in data["lines"]}
    pending, unknown = None, []
    recognised = any(items.get(p.code) for p in proposal.items)
    for proposed in proposal.items:
        equipment = items.get(proposed.code)
        if equipment is None:
            # Descriptive text the model listed as an item is ignored; an unrecognised thing
            # the user asked a number of (or the only thing mentioned) is asked about.
            if proposed.text and (proposed.stated_quantity or not recognised):
                unknown.append(proposed.text)
            continue
        line = lines.get(equipment.code, {"item_code": equipment.code})
        for name in proposed.rejected:
            line.pop(name, None)
        if proposed.quantity is not None:
            line["quantity"] = _format(proposed.quantity)
        if proposed.serials:
            line["serials"] = proposed.serials
        needs_quantity = kind != "problem" and "quantity" not in line
        needs_serials = (
            kind == "return"
            and equipment.is_serialized
            and "quantity" in line
            and len(line.get("serials", [])) != int(float(line["quantity"]))
        )
        if needs_quantity or needs_serials:
            lines.pop(equipment.code, None)
            if pending is None:
                pending = {
                    "category": equipment.category,
                    **{k: v for k, v in line.items() if k != "serials"},
                }
            continue
        lines[equipment.code] = line
    data["lines"] = list(lines.values())
    for name, value in proposal.fields.items():
        if name in allowed and value:
            data["fields"][name] = value
    notes = []
    if proposal.rejected_fields or any(i.rejected for i in proposal.items):
        notes.append("บางช่องยังตรวจสอบกับข้อความไม่ได้ ขอถามเฉพาะข้อมูลที่ขาด")
    if unknown:
        notes.append(f"ไม่พบ '{', '.join(unknown[:3])}' ในทะเบียน กรุณาเลือกจากหมวด")
    if pending is not None:
        data["line"] = pending
        step = "serials" if "quantity" in pending else "quantity"
    elif unknown or (kind != "problem" and not data["lines"]):
        data["line"] = {}
        step = "category"
    else:
        data["line"] = {}
        draft.kind = kind
        draft.data = data
        step = conversation._next_field(draft)
    data["ai_run_id"] = run.pk
    data["ai_proposal"] = {
        "kind": kind,
        "items": {code: line.get("quantity") for code, line in lines.items()},
    }
    conversation._touch(draft, turn.now, kind=kind, step=step, data=data)
    return notes


def record_corrections(draft):
    """At submission: how many proposed values the requester changed (evaluation metric)."""
    proposal = (draft.data or {}).get("ai_proposal")
    run_id = (draft.data or {}).get("ai_run_id")
    if not proposal or not run_id:
        return
    final = {line["item_code"]: line.get("quantity") for line in draft.data.get("lines", [])}
    changed = int(proposal["kind"] != draft.kind)
    changed += sum(
        1
        for code in set(proposal["items"]) | set(final)
        if proposal["items"].get(code) != final.get(code)
    )
    AIRun.objects.filter(pk=run_id).update(corrections=changed)
