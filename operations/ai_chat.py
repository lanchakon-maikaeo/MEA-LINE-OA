"""AI in the LINE chat (M05): free text fills the draft, the requester still confirms everything.

A free-text message (where the chat expects a button, or with no draft yet) becomes an AIRun plus
a `conversation.ai_requested` outbox event and no immediate reply. n8n claims the event and calls
`process` through the internal API; Django calls the model, validates the proposal, fills the
draft and answers with the message's own reply token. If nothing has answered after
AI_REPLY_FALLBACK_SECONDS the worker's watchdog answers with the field-by-field question instead,
so every message gets exactly one reply before the LINE deadline and no push is needed.
"""

import time
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from operations import (
    ai,
    ai_grounding,
    conversation,
    line_api,
    orchestration,
    policy,
    questions,
    typed_lines,
)
from operations.conversation import FIELDS, KIND_LABELS, D, Turn
from operations.models import (
    AIConfigVersion,
    AIRun,
    Case,
    ChannelIdentity,
    Conversation,
    ConversationDraft,
    ConversationMessage,
    EquipmentType,
    LineMenuItem,
    OutboxEvent,
    Requester,
    WebhookInbox,
)

R = AIRun.Status
# "typed_group" is the group card of a typed draft (only a version that reads typed rows).
BUTTON_STEPS = {"kind", "category", "item", "more", "summary", "typed_group"}


def private_text(draft, text):
    """การใช้ไฟฟ้า matters often hold a customer's details: kept from the AI only while
    policy.usage_kept_from_ai() (Q14, lifted by ADR 0021 on 25 Oct 2569)."""
    if not policy.usage_kept_from_ai():
        return False
    in_usage_draft = draft is not None and draft.data.get("topic") == Case.Topic.USAGE
    return in_usage_draft or policy.usage_text(text)


def version_for_chat():
    """The version a chat message starts with: extraction, else chat answers (ADR 0012)."""
    from operations import ai_answer, answers

    version = ai.active_version()
    if version is None and answers.mode() in ("choose", "write"):
        version = ai_answer.active_version()
    return version


def wants_ai(turn, draft, text):
    """Free text that the guided flow cannot use as an answer, when AI is configured."""
    if not settings.AI_ENABLED or len(text.strip()) < 4 or version_for_chat() is None:
        return False
    if private_text(draft, text):
        return False
    if getattr(turn, "anonymous", False):
        # ผู้แจ้งไม่ระบุตัวตน: keyword answers only; the AI path needs a registered requester.
        return False
    if draft is None:
        # Several open drafts and none chosen: the user must pick one first (no guessing).
        return not conversation._open_drafts(turn).exists()
    if (
        draft.data.get("typed")
        and not ai.reads_typed(ai.active_version())
        and conversation.reads_as(text) != "question"
    ):
        # Typed rows (23 Oct 2569) are proposed only by an extraction version that reads them:
        # a question may still reach the answer AI; anything else is the step's own answer.
        return False
    if draft.step in BUTTON_STEPS:
        return True
    # At a step that takes typed answers, only a new request, or a question while the AI
    # answers questions (ADR 0012); anything else is the step's answer at once.
    shape = conversation.off_step(draft, text)
    if shape == "question":
        from operations import ai_answer, answers

        return answers.mode() in ("choose", "write") and ai_answer.active_version() is not None
    return shape == "request"


def queue(turn, draft, text, message=None):
    """`message`: the requester's message to read, when it is not this event's own (the
    requester said 'ไม่ใช่ข้อมูลเพิ่ม' to a message first offered to staff)."""
    version = version_for_chat()
    # Messages already prepared this turn (e.g. "มีอัปเดตใหม่") go out with the AI answer.
    from operations import agent_chat

    result = {"prefix": turn.replies[:2]}
    if agent_chat.enabled():
        result["agent_context"] = agent_chat.state(turn)
    if message is not None:
        result["message"] = message.pk
    shape = conversation.off_step(draft, text)
    if shape:
        # Typed at a step that takes typed answers: when the AI finds no request and no
        # approved answer, the step takes the text as it would without AI. A question goes to
        # the answer task only (the live model once read "ต้องรอนานแค่ไหน..." as a new problem).
        result["typed"], result["typed_as"] = draft.step, shape
    run = AIRun.objects.create(
        identity=turn.identity,
        inbox=turn.inbox,
        draft=draft,
        draft_revision=draft.revision if draft else None,
        config_version=version,
        prompt_version=ai.prompt_version_for(version),
        schema_version=ai.schema_version_for(version),
        result=result,
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
    from operations import staff_line

    if (
        run.result.get("agent_context") is not None
        and requester is None
        and staff_line.staff_of(identity) is not None
        and run.inbox.source_user_id == identity.external_id
        and run.inbox.channel == identity.channel
    ):
        return turn, True
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


def _reprompt(turn, draft):
    from operations import staff_line

    staff = staff_line.staff_of(turn.identity)
    if turn.requester is None and staff is not None:
        staff_line.menu(turn, staff)
    else:
        conversation._reprompt(turn, draft)


def _current_draft(turn):
    draft = turn.conversation.active_draft
    return draft if draft is not None and draft.status == D.OPEN else None


def _blocked(run, turn, usable):
    """Check current authority and revision both before disclosure and before applying output."""
    if not usable:
        return R.FAILED
    draft = _current_draft(turn)
    from operations import agent_chat

    if run.result.get("agent_context") is not None and (
        run.result["agent_context"] != agent_chat.state(turn)
        or ConversationMessage.objects.filter(
            identity=turn.identity, inbox_id__gt=run.inbox_id
        ).exists()
    ):
        turn.say("ขั้นตอนเปลี่ยนแล้ว กรุณาใช้เมนูล่าสุด")
        return R.STALE
    if (draft.pk if draft else None) != run.draft_id or (
        draft is not None and draft.revision != run.draft_revision
    ):
        turn.say("ข้อความก่อนหน้าถูกแทนด้วยขั้นตอนล่าสุด")
        _reprompt(turn, draft)
        return R.STALE
    if not settings.AI_ENABLED:
        turn.say("ระบบอ่านข้อความปิดอยู่ ขอถามทีละข้อ")
        _reprompt(turn, draft)
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
                            **({"size": i.size} if i.size else {}),
                        }
                        for i in p.items
                    ],
                    "fields": p.fields,
                    "field_sources": p.field_sources,
                    **({"sap_number": p.sap_number} if p.sap_number else {}),
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
        message = _message(run)
        source_text = message.text if message is not None else None
        if message is None or not source_text:
            status = R.FAILED
        if status:
            replies = _finish(run, status, [{"outcome": "blocked before provider"}], None, turn)
        context = ai.draft_context(_current_draft(turn)) if not status else None
    if status:
        conversation.send_reply(run.inbox_id, replies[:5])
        return status
    typed = run.result.get("typed")
    received = run.inbox.received_at
    budget = min(
        settings.AI_TOTAL_BUDGET_SECONDS,
        (
            received + timedelta(seconds=settings.AI_REPLY_FALLBACK_SECONDS - 3) - timezone.now()
        ).total_seconds(),
    )
    extraction, attempts = (None, [{"outcome": "skipped: no time left"}])
    started = time.monotonic()
    deadline = started + budget
    from operations import agent_chat

    agent_result, agent_attempts = (None, [])
    if run.result.get("agent_context") is not None:
        agent_result, agent_attempts = agent_chat.plan(run, source_text, budget)
        budget = max(0, budget - (time.monotonic() - started))
    if agent_result is not None:
        attempts = agent_attempts
    elif run.config_version.task == AIConfigVersion.Task.ANSWER:
        attempts = [{"outcome": "no extraction version: chat answer only"}]
    elif run.result.get("typed_as") == "question":
        attempts = [{"outcome": "a question at a typed step: chat answer only"}]
    elif message is not None and budget >= 2 and turn.requester is not None:
        extraction, attempts = ai.extract(source_text, context, run.config_version, budget)
    chat = None
    if (
        agent_result is None
        and turn.requester is not None
        and message is not None
        and not _proposals(extraction)
    ):
        # Not a request the guided flow can take: maybe a question (คลังคำตอบ ข/ค, ADR 0012).
        chat = _ask_answer(run, source_text, deadline - time.monotonic())
        if chat is not None:
            attempts = attempts + chat["attempts"]
    if agent_result is None:
        attempts = agent_attempts + attempts
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
        elif agent_result is not None:
            status = R.ANSWERED if agent_result["kind"] == "reply" else R.APPLIED
            agent_chat.present(turn, run, agent_result)
        elif _proposals(extraction):
            status = R.APPLIED
            apply(turn, draft, extraction, run)
        elif chat is not None and chat["key"] and not (typed and chat["key"] == "none"):
            status = R.ANSWERED
            run.result = {**run.result, "answer": _answer(turn, draft, chat)}
            if chat["key"] == "none" and draft is None:
                questions.record(turn, source_text)  # คำถามที่บอทตอบไม่ได้ (Q11)
        elif typed:
            # Neither a request nor an approved answer: the step takes the text, as without AI.
            status = R.TAKEN
            run.result = {**run.result, "answer": chat and {"key": chat["key"]}}
            conversation._draft_text(turn, draft, source_text)
        elif extraction is None:
            status = R.FAILED
            turn.say("ระบบอ่านข้อความไม่สำเร็จ ขอถามทีละข้อ")
            _reprompt(turn, draft)
        else:
            status = R.APPLIED
            if draft is None and not _proposals(extraction):
                questions.record(turn, source_text)  # neither a request nor an answer (Q11)
            apply(turn, draft, extraction, run)
        replies = _finish(run, status, attempts, extraction, turn)
        draft = _current_draft(turn) if status == R.APPLIED and _proposals(extraction) else None
    if draft is not None:
        replies = _rephrase(run, draft, replies, deadline - time.monotonic())
    conversation.send_reply(run.inbox_id, replies[:5])
    return status


def _message(run):
    """The requester's message the run reads: its own event's, or the one named at queueing."""
    rows = ConversationMessage.objects.filter(identity_id=run.identity_id)
    if run.result.get("message"):
        return rows.filter(pk=run.result["message"]).first()
    return rows.filter(inbox=run.inbox).first()


def _rephrase(run, draft, replies, budget):
    """The AI words the bot's next question with what the requester already said (ตัวเลือก
    ระบบ chat_followup, ADR 0012). The question itself, its buttons and the step stay the
    program's; a wording that fails the checks leaves the standard question."""
    from operations import ai_answer, options

    if options.get(options.CHAT_FOLLOWUP) != "ai" or not replies or budget < 2:
        return replies
    version = ai_answer.active_version()
    step = draft.step.split(":", 1)[1] if draft.step.startswith("field:") else draft.step
    last = replies[-1]
    if version is None or step not in ai_answer.FOLLOWUP_HINTS or last.get("type") != "text":
        return replies
    result = ai_answer.followup(
        last["text"], step, ai_answer.followup_context(draft), version, budget
    )
    record = {
        "step": draft.step,
        "question": result["question"] or "",
        "by": "model" if result["question"] else "template",
        "refused": result["refused"],
        "written": result.get("written", ""),
    }
    AIRun.objects.filter(pk=run.pk).update(
        result={**run.result, "followup": record}, attempts=run.attempts + result["attempts"]
    )
    if not result["question"]:
        return replies
    return [*replies[:-1], {**last, "text": result["question"]}]


def _proposals(extraction):
    if extraction is None:
        return []
    return [p for p in extraction.requests if p.intent in ai.KIND_FOR_INTENT]


def _ask_answer(run, text, budget):
    """The answer AI on this requester's message, when ตัวเลือกระบบ allows it (network; outside
    transactions). Only the requester's own open cases go with it, never การใช้ไฟฟ้า ones."""
    from operations import ai_answer, answers

    mode = answers.mode()
    version = ai_answer.active_version() if mode in ("choose", "write") else None
    if version is None or budget < 2:
        return None
    requester = Requester.objects.filter(pk=run.identity.requester_id).first()
    own = (
        list(Case.objects.filter(requester=requester).values_list("number", flat=True))
        if requester
        else []
    )
    return ai_answer.answer(
        text,
        list(answers.approved()),
        answers.facts(requester),
        own,
        version,
        mode,
        budget,
    )


def _answer(turn, draft, chat):
    """Say the chosen answer (the model's checked reply in write mode); "none" gets the
    approved out-of-scope answer. Returns what is kept on the run for review."""
    from operations import answers

    key = chat["key"]
    if key == "none":
        key = answers.OUT_OF_SCOPE
    answer = answers.approved().filter(key=key).first()
    if answer is None:
        turn.say("ยังไม่เข้าใจข้อความ กรุณาเลือกจากปุ่ม หรือพิมพ์รายละเอียดเพิ่ม")
        _reprompt(turn, draft)
        return {"key": chat["key"], "by": "none", "refused": chat["refused"]}
    written = chat["reply"] if key != answers.OUT_OF_SCOPE else None
    answers.respond(turn, draft, answer, written)
    return {
        "key": chat["key"],
        "by": "model" if written else "template",
        "reply": written or "",
        "refused": chat["refused"],
    }


def review(run, titles):
    """What the AI did on one chat message, in words for the AI page, so ฝอจ. can read the
    chat answers back (ADR 0012): the answer chosen, what the AI wrote or why it was not sent,
    the follow-up wording, and the step a typed message came at. `titles`: answer key → title.
    Returns (lines, refused)."""
    result = run.result or {}
    lines, refused = [], False
    if result.get("typed"):
        shape = {"question": "คำถาม", "request": "คำขอ"}.get(result.get("typed_as"), "")
        lines.append(f"พิมพ์ในขั้น {result['typed']}" + (f" ({shape})" if shape else ""))
    if result.get("requests"):
        lines.append(f"เติมร่าง {len(result['requests'])} เรื่อง")
    answer = result.get("answer") or {}
    if answer.get("key"):
        title = titles.get(answer["key"], answer["key"])
        how = {"model": "AI เขียน", "template": "ข้อความที่รับรอง"}.get(answer.get("by"), "")
        lines.append(f"คำตอบ: {title}" + (f" ({how})" if how else ""))
        if answer.get("reply"):
            lines.append(f"ข้อความที่ส่ง: {answer['reply']}")
    if answer.get("refused"):
        refused = True
        lines.append(f"ไม่ส่งถ้อยคำของ AI เพราะ{answer['refused']}")
    followup = result.get("followup") or {}
    if followup:
        if followup.get("by") == "model":
            lines.append(f"AI ถามต่อ: {followup.get('question', '')}")
        else:
            lines.append("ถามต่อด้วยคำถามมาตรฐาน")
        if followup.get("refused"):
            refused = True
            written = followup.get("written") or ""
            lines.append(
                f"ไม่ใช้ถ้อยคำของ AI เพราะ{followup['refused']}" + (f": {written}" if written else "")
            )
    return lines, refused


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
            draft = _current_draft(turn) if usable else None
            typed = run.result.get("typed")
            message = _message(run) if typed else None
            if (
                typed
                and draft is not None
                and draft.pk == run.draft_id
                and draft.revision == run.draft_revision
                and message is not None
            ):
                # Typed at a step that takes typed answers: the step takes it, as without AI.
                conversation._draft_text(turn, draft, message.text)
            elif usable:
                turn.say("ระบบอ่านข้อความไม่ทันเวลา ขอถามทีละข้อ")
                _reprompt(turn, draft)
            replies = _finish(run, R.FALLBACK, run.attempts, None, turn)
        conversation.send_reply(run.inbox_id, replies[:5])
        handled += 1
    return handled


# --- Filling drafts from a validated proposal -------------------------------------------------


def apply(turn, draft, extraction, run):
    proposals = [p for p in extraction.requests if p.intent in ai.KIND_FOR_INTENT]
    if not proposals:
        turn.say("ยังไม่เข้าใจข้อความ กรุณาเลือกจากปุ่ม หรือพิมพ์รายละเอียดเพิ่ม")
        return _reprompt(turn, draft)
    first, extra = proposals[0], proposals[1:]
    kind = ai.KIND_FOR_INTENT[first.intent]
    has_content = draft is not None and (draft.data.get("lines") or draft.data.get("fields"))
    if draft is None or (draft.kind and draft.kind != kind and has_content):
        draft = conversation._new_draft(turn)
    typed = (getattr(run, "result", None) or {}).get("typed")  # the evaluation passes no run
    notes = _merge(turn, draft, first, run, extraction.screened_draft_fields, typed)
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


def _merge(turn, draft, proposal, run, screened=None, typed=None):
    """Merge into the draft; anything missing or unknown is asked by the guided flow next.

    `screened`: the draft's text fields with directive segments removed by the harness screen
    ({name: text or None}); the requester's own words in a field survive an appended attack.
    `typed`: the step that took typed answers when the message came (ADR 0012). A message there
    that adds items was a new request, not that step's answer, so that field is asked again
    (the live model once took "เพิ่มอีก" from "ขอเบิกมิเตอร์เพิ่มอีก 2 เครื่อง" as the purpose).
    """
    kind = ai.KIND_FOR_INTENT[proposal.intent]
    if draft.kind and draft.kind != kind:
        kind = draft.kind
    path = _menu_path(proposal) if kind == Case.Kind.PROBLEM or proposal.division else {}
    if path.get("topic") in (Case.Topic.USAGE, Case.Topic.SYSTEM):
        proposal.items = []  # these topics list no items; a usage report names meters instead
    allowed = {name for name, _, _ in FIELDS[kind]}
    stored = dict(draft.data.get("fields", {}))
    for name, value in (screened or {}).items():
        if name in stored:
            stored[name] = value
    # A draft of typed rows (only a version that reads them gets here) keeps what makes it one.
    kept = draft.data if draft.data.get("typed") else {}
    data = {
        **{k: kept[k] for k in TYPED_KEEP if k in kept},
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
    for name in ("division", "topic", "menu_label"):
        if path.get(name):
            data[name] = path[name]
    items = {
        i.code: i
        for i in EquipmentType.objects.filter(code__in=[p.code for p in proposal.items if p.code])
    }
    lines = {line["item_code"]: dict(line) for line in data["lines"]}
    pending, unknown, rows = None, [], []
    recognised = any(items.get(p.code) for p in proposal.items)
    for proposed in proposal.items:
        equipment = items.get(proposed.code)
        if equipment is None:
            # Descriptive text the model listed as an item is ignored; an unrecognised thing
            # the user asked a number of (or the only thing mentioned) is asked about.
            if proposed.text and (proposed.stated_quantity or not recognised):
                unknown.append(proposed.text)
            continue
        if equipment.entry_group:
            rows.append((equipment, proposed))  # a typed row, joined below
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
    added = set(lines) - {line["item_code"] for line in draft.data.get("lines", [])}
    asked = typed.split(":", 1)[1] if typed and typed.startswith("field:") else None
    data["lines"] = list(lines.values())
    notes = []
    entry = _typed_rows(turn, data, rows, kind, notes) if rows else None
    if data.get("menu_label"):
        data["menu_label"] = conversation.typed_topic(data)
    added = added or (rows and data["lines"] != list(lines.values()))
    for name, value in proposal.fields.items():
        if name in allowed and value and not (name == asked and (added or pending or entry)):
            data["fields"][name] = value
    if proposal.sap_number and typed_lines.sap_label(kind, data.get("topic")):
        problem = typed_lines.sap_in_use(proposal.sap_number, turn.requester)
        if problem:
            notes.append(problem)
        else:
            data["sap_number"] = proposal.sap_number
    if data.get("topic") == Case.Topic.REPAIR and not data.get("sap_number"):
        data["sap_skipped"] = True  # the rows may name the piece instead (Q7); else asked
    if data.get("topic") == Case.Topic.USAGE:
        data["meters"] = [m for m in proposal.meters if m["district"]]
        data["meters_pending"] = [m["number"] for m in proposal.meters if not m["district"]]
    ambiguous, unplaced = _unplaced_meter(proposal, data, kind)
    if proposal.rejected_fields or any(i.rejected for i in proposal.items):
        notes.insert(0, "บางช่องยังตรวจสอบกับข้อความไม่ได้ ขอถามเฉพาะข้อมูลที่ขาด")
    if unknown:
        notes.append(f"ไม่พบ '{', '.join(unknown[:3])}' ในทะเบียน กรุณาเลือกจากหมวด")
    if ambiguous:
        data["meters_pending"] = unplaced
        step = "which_division"
    elif data.get("topic") == Case.Topic.USAGE:
        data["line"] = {}
        if not path.get("label"):
            step = "usage_topic"
        elif data["meters_pending"]:
            data["meter"], data["meters_pending"] = (
                data["meters_pending"][0],
                data["meters_pending"][1:],
            )
            step = "district"
        else:
            draft.kind, draft.data = kind, data
            step = conversation._next_field(draft)
    elif pending is not None:
        data["line"] = pending
        step = "serials" if "quantity" in pending else "quantity"
    elif entry is not None:
        data["line"], data["entry"] = {}, entry
        step = "entry"
    elif unknown or (kind != "problem" and not data["lines"]):
        data["line"] = {}
        step = "typed_group" if data.get("typed") else "category"
    else:
        data["line"] = {}
        draft.kind = kind
        draft.data = data
        step = conversation._next_field(draft)
    data["ai_run_id"] = run.pk
    data["ai_proposal"] = {
        "kind": kind,
        "items": {line["item_code"]: line.get("quantity") for line in data["lines"]},
    }
    conversation._touch(draft, turn.now, kind=kind, step=step, data=data)
    return notes


def _menu_path(proposal):
    """Where a menu version placed this request on the LINE menu (25 Oct 2569 Q9, Q10): the
    division, the case topic and, for a report topic, the menu label; {} when it said nothing.
    Older versions never set these, so their drafts are unchanged."""
    from operations import line_menu_catalog  # local: the owner's fixed menu

    tiles = {code: label for label, code, _, _ in line_menu_catalog.TOPICS}
    if proposal.intent == "repair":
        division = proposal.division if proposal.division in EQUIPMENT_DIVISIONS else ""
        path = {"topic": Case.Topic.REPAIR, "division": division}
        if division:
            path["menu_label"] = f"{tiles[division]} · ติดตามสถานะการซ่อม"
        return path
    if proposal.topic:
        division, topic = ai.menu_topics()[proposal.topic]
        return {
            "division": division,
            "topic": topic,
            "label": proposal.topic,
            "menu_label": f"{tiles[division]} · {proposal.topic}",
        }
    if proposal.division == "กตฟ.":
        return {"division": "กตฟ.", "topic": Case.Topic.USAGE}  # the topic is asked
    return {"division": proposal.division} if proposal.division else {}


EQUIPMENT_DIVISIONS = ("กคว.", "กอจ.", "กคอ.")


def _unplaced_meter(proposal, data, kind):
    """(ask, meter numbers): a menu version's problem about a meter that names neither a กตฟ.
    topic nor a division. Q10 asks whose meter it is instead of guessing; the typed meter
    rows' own division (กคว.) is not the model's word on it."""
    if (
        not proposal.menu_read
        or kind != Case.Kind.PROBLEM
        or proposal.intent == "repair"
        or proposal.topic
        or proposal.division
    ):
        return False, []
    rows = [
        row
        for line in data["lines"]
        for row in line.get("details", [])
        if row.get("group") == "meter"
    ]
    numbers = [m["number"] for m in proposal.meters] + [
        n for row in rows for n in row.get("numbers", [])
    ]
    return bool(proposal.meters or rows), list(dict.fromkeys(numbers))


# What a typed draft keeps when the AI fills it (typed_lines, 23 Oct 2569).
TYPED_KEEP = ("typed", "division", "menu_label", "sap_number", "topic")


def _typed_rows(turn, data, rows, kind, notes):
    """Join the typed rows the AI read to data["lines"] (typed_lines.add) the way the chat
    does when they are typed by hand. Returns the first row that still lacks a field, as the
    chat's typed-row step (data["entry"]) asks it, or None. A draft of typed rows only is
    filed with its group's tile and goes to that tile's division."""
    entry = None
    for equipment, proposed in rows:
        group = equipment.entry_group
        fields = typed_lines.fields(group, kind)
        values = {
            "numbers": " ".join(proposed.serials),
            "size": proposed.size,
            "quantity": _format(proposed.quantity) if proposed.quantity is not None else "",
            "name": proposed.text if group == "other" and not _says_no_name(proposed.text) else "",
            "equipment_number": getattr(proposed, "equipment_number", ""),
        }
        row, missing = {}, []
        for f in fields:
            value = values.get(f.key, "")
            if not value or typed_lines.take(row, f, value, kind, equipment.unit):
                missing.append(f.key)  # asked one at a time, as without AI
        if not missing:
            problem = typed_lines.check(row, kind, group, data["lines"], turn.requester)
            if not problem:
                data["lines"] = typed_lines.add(data["lines"], group, row, kind)
                continue
            notes.append(f"{problem} กรุณาพิมพ์รายการนี้ใหม่")
            row, missing = {}, [f.key for f in fields]
        if entry is None:
            entry = {"group": group, "line": None, "row": row, "keys": missing, "at": 0}
        else:
            notes.append(f"{typed_lines.label_of(group)} อีกรายการ เพิ่มได้หลังรายการนี้")
    shared = {i.code for i in EquipmentType.objects.exclude(entry_group="")}
    if all(line["item_code"] in shared for line in data["lines"]):
        first = entry["group"] if entry else rows[0][0].entry_group
        data.setdefault("typed", first)
        groups = {first} | {
            row["group"] for line in data["lines"] for row in line.get("details", [])
        }
        tiles = list(
            LineMenuItem.objects.filter(
                is_active=True, action=LineMenuItem.Action.EQUIPMENT, topic__in=groups
            ).exclude(division=None)
        )
        owners = {group: {t.division_id for t in tiles if t.topic == group} for group in groups}
        # อุปกรณ์อื่น is on several tiles: only groups one and the same division owns name it.
        # Rows of several divisions (meters with a transformer) name none, so the case splits
        # per division at submission (25 Oct 2569 Q19) instead of all going to the first.
        divisions = set().union(*owners.values())
        if all(len(found) == 1 for found in owners.values()) and len(divisions) == 1:
            tile = next(t for t in tiles if t.topic == first)
            data.setdefault("division", tile.division.code)
            data.setdefault("groups", conversation._tile_groups(tile))
    return entry


def _says_no_name(text):
    """A name that only repeats the group ("อุปกรณ์อื่น") is asked again, never stored."""
    words = {"อุปกรณ์", "อุปกรณ์อื่น", "อุปกรณ์อื่นๆ", "อื่นๆ", "อื่น ๆ", "อุปกรณ์อื่น ๆ"}
    return " ".join(str(text or "").split()) in words | {typed_lines.label_of("other")}


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
