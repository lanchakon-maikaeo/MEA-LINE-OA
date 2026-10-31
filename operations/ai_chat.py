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
    if version is None and answers.mode() in ("choose", "write", "agent"):
        version = ai_answer.active_version()
    return version


def wants_ai(turn, draft, text):
    """Free text that the guided flow cannot use as an answer, when AI is configured. While the
    AI talks (chat_mode, ADR 0022 Q8) every typed message goes to it."""
    from operations import agent_intake, free_chat

    talking = agent_intake.active()
    if not settings.AI_ENABLED or version_for_chat() is None:
        return False
    if len(text.strip()) < 4 and not (talking and draft is not None):
        return False
    if private_text(draft, text):
        return False
    if getattr(turn, "anonymous", False):
        # ผู้แจ้งไม่ระบุตัวตน: keyword answers only, except a question for ตอบอิสระ (ADR 0037
        # Q8), which never sees cases; a draft's own typed answers stay the draft's.
        return free_chat.wanted(turn, draft, text)
    if draft is None:
        # Several open drafts and none chosen: the user must pick one first (no guessing).
        return not conversation._open_drafts(turn).exists()
    if talking:
        return True
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

        if free_chat.wanted(turn, draft, text):
            return True
        return answers.mode() in ("choose", "write") and ai_answer.active_version() is not None
    return shape == "request"


def queue(turn, draft, text, message=None):
    """`message`: the requester's message to read, when it is not this event's own (the
    requester said 'ไม่ใช่ข้อมูลเพิ่ม' to a message first offered to staff)."""
    version = version_for_chat()
    # Messages already prepared this turn (e.g. "มีอัปเดตใหม่") go out with the AI answer.
    from operations import agent_chat, agent_intake

    result = {"prefix": turn.replies[:2]}
    if agent_chat.enabled():
        result["agent_context"] = agent_chat.state(turn)
    if message is not None:
        result["message"] = message.pk
    shape = conversation.off_step(draft, text)
    from operations import free_chat

    if free_chat.wanted(turn, draft, text):
        # ADR 0037: a question for ตอบอิสระ; the mark also counts it against the limits.
        result["free_chat"] = {"anonymous": bool(getattr(turn, "anonymous", False))}
    if agent_intake.active(version) and not getattr(turn, "anonymous", False):
        # ADR 0022: the AI decides what the text is. A step that takes typed answers is kept
        # so that, if the AI fails or is late, the step takes the text as before (Q10).
        result["intake"] = {"draft": draft is not None}
        shape = ""
        if draft is not None and conversation._expects_text(draft):
            result["typed"] = draft.step
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

    if (run.result.get("free_chat") or {}).get("anonymous"):
        # ADR 0037 Q8: an anonymous reporter's question; the tools never show it any case.
        anonymous = Requester.objects.filter(anonymous_identity=identity).first()
        draft = conv.active_draft
        if (
            anonymous is not None
            and run.inbox.source_user_id == identity.external_id
            and run.inbox.channel == identity.channel
            and (
                draft is None
                or (draft.identity_id == identity.pk and draft.requester_id == anonymous.pk)
            )
        ):
            turn.requester, turn.anonymous = anonymous, True
            return turn, True
        turn.say("บัญชีผู้แจ้งของคุณใช้งานไม่ได้ในขณะนี้ กรุณาติดต่อ ฝอจ.")
        return turn, False

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
        seen = None
        if not status and run.result.get("intake") is not None and _current_draft(turn):
            from operations import agent_intake

            seen = agent_intake.view(turn, _current_draft(turn))
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
    from operations import agent_chat, agent_intake

    # ADR 0022: with an open draft the intake agent decides first what the message is.
    decided, act = None, None
    if seen is not None and budget >= 2:
        decided = agent_intake.decide(run, source_text, seen, budget)
        act = decided["act"]
        budget = max(0, budget - (time.monotonic() - started))
    calls = decided["calls"] if decided else 0
    from operations import free_chat

    free = None
    if run.result.get("free_chat") is not None and (
        act is None or (act == "reply" and free_chat.asks(source_text))
    ):
        # ADR 0037: ตอบอิสระ answers the question (Q33: with a draft open, then the step again).
        tools = free_chat.Tools(run.identity_id, run.result["free_chat"].get("anonymous", False))
        calls_left = free_chat.MAX_TOOL_CALLS + 1 if act is None else agent_intake.MAX_CALLS - calls
        free = free_chat.run(
            source_text,
            tools,
            free_chat.version(),
            budget,
            free_chat.history(run),
            max_calls=max(2, calls_left),
        )
        calls += len({a["step"] for a in free["attempts"]})
        budget = max(0, budget - (time.monotonic() - started))
    agent_result, agent_attempts = (None, [])
    if free is None and run.result.get("agent_context") is not None and act in (None, "lookup"):
        steps = agent_chat.MAX_STEPS if act is None else agent_intake.MAX_CALLS - calls
        agent_result, agent_attempts = agent_chat.plan(run, source_text, budget, max_steps=steps)
        calls += len({a["step"] for a in agent_attempts if "step" in a})
        budget = max(0, budget - (time.monotonic() - started))
    # Q10: no usable decision at a step that takes typed answers: the step takes the text.
    fallback = seen is not None and act is None and bool(run.result.get("typed"))
    numeric = act == "answer" and _number_as_text(seen["step"], decided["value"])
    reads = (act in (None, "fill", "new") and not fallback) or (
        act == "answer" and (not seen["typed"] or numeric)
    )
    if free is not None:
        attempts = free["attempts"]
    elif agent_result is not None:
        attempts = agent_attempts
    elif not reads:
        attempts = []
    elif run.config_version.task == AIConfigVersion.Task.ANSWER:
        attempts = [{"outcome": "no extraction version: chat answer only"}]
    elif run.result.get("typed_as") == "question":
        attempts = [{"outcome": "a question at a typed step: chat answer only"}]
    elif message is not None and budget >= 2 and turn.requester is not None:
        extraction, attempts = ai.extract(source_text, context, run.config_version, budget)
        calls += 1
    if decided:
        attempts = decided["attempts"] + attempts
    chat = None
    if (
        free is None
        and agent_result is None
        and act is None
        and not fallback
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
        wording = False
        if status:
            extraction = None
        elif free is not None and (free["sent"] or draft is None or not (act or typed)):
            status = R.ANSWERED
            run.result = {
                **run.result,
                "free_chat": {**run.result["free_chat"], **free_chat.respond(turn, draft, free)},
            }
            if free["sent"]:
                run.result["free_chat_reply"] = free["reply"]  # what the next question sees
            elif draft is None:
                questions.record(turn, source_text)  # คำถามที่บอทตอบไม่ได้ (Q18)
        elif free is not None and act is None:
            # No answer at a step that takes typed answers: the step takes the text (ADR 0012).
            status = R.TAKEN
            run.result = {**run.result, "free_chat": {**run.result["free_chat"], "by": "step"}}
            conversation._draft_text(turn, draft, source_text)
        elif act is not None and agent_result is None and draft is not None:
            status, wording = _intake(turn, draft, decided, extraction, run, source_text)
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
        talking = run.result.get("intake") is not None
        if talking and status == R.APPLIED and _proposals(extraction):
            wording = True  # the first reply says what was read (ADR 0022)
        replies = _finish(run, status, attempts, extraction, turn)
        draft = _current_draft(turn) if status == R.APPLIED and _proposals(extraction) else None
    if wording and calls < agent_intake.MAX_CALLS:
        before = seen["draft"] if seen else ""
        replies = _worded(run, source_text, replies, deadline - time.monotonic(), before)
    elif draft is not None and not talking:
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


# --- The intake agent's decision, carried out (ADR 0022) ------------------------------------


def _record(run, **values):
    run.result = {**run.result, "intake": {**run.result.get("intake", {}), **values}}


def _with_step(turn, draft, text):
    """The agent's text with the current step's buttons; at the summary the card follows
    unchanged, so a typed confirmation still comes right after the latest summary (Q13)."""
    before = len(turn.replies)
    conversation._ask(turn, draft)
    asked = turn.replies[before:]
    if draft.step == "summary" or not asked:
        turn.replies.insert(before, conversation.text_message(text))
        return
    quick = asked[-1].get("quickReply")
    del turn.replies[before:]
    turn.replies.append(conversation.text_message(text) | ({"quickReply": quick} if quick else {}))


def _advance(turn, draft):
    """Pass the steps a requester who says "send it" may leave, with the steps' own buttons
    (no more items or meters, optional fields and row fields left empty); their questions are
    not sent. Stops at the first thing the draft still needs, or at the summary."""
    from operations import typed_lines

    mark = len(turn.replies)
    for _ in range(30):
        step, data = draft.step, draft.data
        if step == "more" and (data.get("lines") or draft.kind == Case.Kind.PROBLEM):
            action = "lines_done"
        elif step == "meters_more" and data.get("meters"):
            action = "meters_done"
        elif step.startswith("field:") and not next(
            (r for n, _, r in conversation._fields(draft) if n == step.split(":", 1)[1]), True
        ):
            action = "skip_field"
        elif step == "entry":
            fields, at = conversation._row_fields(draft), data["entry"]["at"]
            if at >= len(fields) or typed_lines.required(fields[at], draft.kind):
                break
            action = "entry_skip"
        else:
            break
        revision = draft.revision
        conversation._draft_button(turn, draft, action, {})
        if draft.revision == revision:
            break
    del turn.replies[mark:]


def _intake(turn, draft, decided, extraction, run, text):
    """Carry out the agent's decision on a message typed with this draft open: (status,
    whether the program's next message is then worded by the agent)."""
    from operations import agent_intake

    act = decided["act"]
    _record(run, act=act, refused=decided.get("refused", ""))
    if act == "reply":
        need, _ = agent_intake.needed(draft)
        said = decided["reply"] or f"ตอนนี้ร่างนี้ต้องการ {need} พิมพ์ได้เลยเมื่อพร้อม หรือเก็บร่างไว้ทำต่อภายหลัง"
        _record(run, said=said, by="model" if decided["reply"] else "template")
        _with_step(turn, draft, said)
        return R.ANSWERED, False
    if act == "cancel":
        # The agent never drops a draft: the requester taps to confirm.
        turn.say(
            "ต้องการยกเลิกร่างนี้ใช่ไหม กดยกเลิกร่างเพื่อยืนยัน หรือกดทำต่อ",
            [
                turn.button("ยกเลิกร่าง", "discard", draft=draft),
                turn.button("ทำต่อ", "select_draft", {"id": str(draft.pk)}),
            ],
        )
        _record(run, said="ต้องการยกเลิกร่างนี้ใช่ไหม")
        return R.ANSWERED, False
    if act == "confirm":
        # "ส่งเลย" passes what may be skipped and shows the summary; only a tap, or a
        # confirming word right after that summary, files (Q13).
        _advance(turn, draft)
        if draft.step != "summary":
            need, _ = agent_intake.needed(draft)
            turn.say(f"ยังส่งเรื่องไม่ได้ เพราะยังขาด {need}")
        conversation._ask(turn, draft)
        return R.ANSWERED, True
    if act == "lookup":
        conversation._my_cases(turn)
        turn.say("ร่างที่ทำอยู่ยังเก็บไว้ พิมพ์ต่อได้เลย")
        _record(run, said="ร่างที่ทำอยู่ยังเก็บไว้ พิมพ์ต่อได้เลย")
        return R.ANSWERED, False
    numeric = act == "answer" and _number_as_text(draft.step, decided["value"])
    if act == "answer" and conversation._expects_text(draft) and not numeric:
        conversation._draft_text(turn, draft, decided["value"])
        return R.TAKEN, True
    if act == "fill" and draft.kind and _proposals(extraction):
        same = [p for p in _proposals(extraction) if ai.KIND_FOR_INTENT[p.intent] == draft.kind]
        if not same:
            # Information for this draft read as another kind of request (dialogue-v1 D15:
            # a purpose "ใช้ติดตั้งบ้านลูกค้า" read as a กตฟ. report): the steps take it. A new
            # request is the decision "new".
            if draft.step == "more" and draft.data.get("lines"):
                mark = len(turn.replies)
                conversation._draft_button(turn, draft, "lines_done", {})
                del turn.replies[mark:]
            if conversation._expects_text(draft):
                conversation._draft_text(turn, draft, text)
                return R.TAKEN, True
            conversation._ask(turn, draft)
            return R.APPLIED, True
    if _proposals(extraction):
        step, held, mark = draft.step, _core(draft.data), len(turn.replies)
        apply(turn, draft, extraction, run)
        current = _current_draft(turn)
        if (
            step.startswith("field:")
            and current is not None
            and current.pk == draft.pk
            and (current.step, _core(current.data)) == (step, held)
        ):
            # Read as other information but nothing changed: the text answers the step
            # (dialogue-v1 D03: a purpose typed at the purpose step was lost).
            del turn.replies[mark:]
            conversation._draft_text(turn, current, text)
            return R.TAKEN, True
        return R.APPLIED, True
    if numeric:
        need, _ = agent_intake.needed(draft)
        turn.say(f"ข้อความนี้เป็นตัวเลข ช่องนี้ต้องการ {need}")
        conversation._ask(turn, draft)
        return R.ANSWERED, True
    if conversation._expects_text(draft):
        conversation._draft_text(turn, draft, text)
        return R.TAKEN, True
    turn.say("ยังไม่เข้าใจข้อความ กรุณาเลือกจากปุ่ม หรือพิมพ์รายละเอียดเพิ่ม")
    conversation._reprompt(turn, draft)
    return R.APPLIED, True


def _core(data):
    """A draft's data without the AI's bookkeeping, to tell whether a reading changed it."""
    return {k: v for k, v in data.items() if k not in ("ai_run_id", "ai_proposal", "line")}


def _number_as_text(step, value):
    """A bare number or code given as the answer of a text field (a purpose of "4500100013")
    is read by the extraction instead (dialogue-v1 D13)."""
    from operations import typed_lines

    value = " ".join(str(value or "").split())
    return step.startswith("field:") and bool(typed_lines.SAP_NUMBER.fullmatch(value))


def _worded(run, text, replies, budget, before=""):
    """The agent words the program's next message (what was read, any problem, the next
    question); the buttons and the summary card stay the program's. A wording that fails the
    checks leaves the program's own messages (Q17)."""
    from operations import agent_intake

    lead = len(run.result.get("prefix", []))
    own = replies[lead:]
    draft = Conversation.objects.get(identity_id=run.identity_id).active_draft
    if draft is not None and draft.status != D.OPEN:
        draft = None
    if not own or draft is None or budget < 2 or any(m.get("type") != "text" for m in own):
        return replies
    card = (
        own[-1]
        if draft.step == "summary" and own[-1]["text"].startswith(conversation.SUMMARY_HEAD)
        else None
    )
    standard = "\n".join(m["text"] for m in (own[:-1] if card else own))
    _, facts = agent_intake.needed(draft)
    result = agent_intake.word(
        run,
        text,
        standard,
        before,
        conversation._summary(draft).rsplit("\n", 1)[0],
        facts,
        agent_intake.menus(),
        budget,
        draft.step,
        agent_intake.hints_for(draft),
    )
    record = {
        "worded": result["reply"] or "",
        "word_refused": result["refused"],
        "word_written": result.get("written", ""),
    }
    said = result["reply"] or standard
    current = AIRun.objects.get(pk=run.pk)
    AIRun.objects.filter(pk=run.pk).update(
        result={
            **current.result,
            "intake": {**current.result.get("intake", {}), **record, "said": said},
        },
        attempts=current.attempts + result["attempts"],
    )
    if not result["reply"]:
        return replies
    if card is not None:
        return [*replies[:lead], conversation.text_message(result["reply"]), card]
    message = conversation.text_message(result["reply"])
    if own[-1].get("quickReply"):
        message["quickReply"] = own[-1]["quickReply"]
    return [*replies[:lead], message]


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
    # ADR 0032: a เบิก or ส่งคืน read from typed text only fills the slip draft already open
    # (from its equipment's tile); it never opens one, whatever the AI decided.
    slips = (Case.Kind.WITHDRAWAL, Case.Kind.RETURN)
    refused = [
        p
        for p in proposals
        if not settings.LINE_TYPED_SLIPS
        and ai.KIND_FOR_INTENT[p.intent] in slips
        and (draft is None or draft.kind != ai.KIND_FOR_INTENT[p.intent])
    ]
    proposals = [p for p in proposals if p not in refused]
    if refused and not proposals:
        source = _message(run) if isinstance(run, AIRun) else None  # evaluation: no run
        return conversation.slip_refusal(turn, source.text if source else "")
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
    if (getattr(run, "result", None) or {}).get("intake") is not None:
        # ADR 0022: the reply says what was read, also when the agent's wording is not used.
        read = conversation._summary(draft).split("\n")[1:-1]
        lines = [f"อ่านได้ว่า{KIND_LABELS[draft.kind]}", *read] if read else lines
    lines += notes
    if extra:
        lines.append(f"พบอีก {len(extra)} เรื่องในข้อความ บันทึกเป็นร่างไว้แล้ว ส่งเรื่องนี้ก่อน แล้วทำเรื่องถัดไปต่อ")
    if refused:
        lines.append(f"{conversation.SLIP_REFUSAL} ส่วนที่ขอเบิกหรือคืนจึงไม่ได้บันทึก")
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
    if kind != Case.Kind.PROBLEM:
        # A withdrawal or return goes to an equipment division only, never to a report topic
        # (dialogue-v1 D15: "ใช้ติดตั้งบ้านลูกค้า" made a meter withdrawal a กตฟ. report).
        division = path.get("division")
        path = {"division": division} if division in EQUIPMENT_DIVISIONS else {}
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
    # The row still being typed stays unless the message corrects it (dialogue-v1 D15).
    typing = draft.data.get("entry") or {}
    typing = (
        typing if getattr(draft, "step", "") == "entry" and typing.get("line", 0) is None else None
    )
    entry = _typed_rows(turn, data, rows, kind, notes, typing) if rows else typing
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
    ambiguous, unplaced = _unplaced_meter(proposal, data, kind, entry)
    if proposal.rejected_fields or any(i.rejected for i in proposal.items):
        notes.insert(0, "บางช่องยังตรวจสอบกับข้อความไม่ได้ ขอถามเฉพาะข้อมูลที่ขาด")
    if unknown:
        notes.append(f"ไม่พบ '{', '.join(unknown[:3])}' ในทะเบียน กรุณาเลือกจากหมวด")
    if ambiguous:
        data["meters_pending"] = unplaced
        if entry is not None:
            data["line"], data["entry"] = {}, entry  # kept for กคว. (found by typed-v1 T06)
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


def _unplaced_meter(proposal, data, kind, entry=None):
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
    if entry and entry.get("group") == "meter":
        rows.append(entry.get("row", {}))  # a meter row still asking a field
    numbers = [m["number"] for m in proposal.meters] + [
        n for row in rows for n in row.get("numbers", [])
    ]
    return bool(proposal.meters or rows), list(dict.fromkeys(numbers))


# What a typed draft keeps when the AI fills it (typed_lines, 23 Oct 2569).
TYPED_KEEP = ("typed", "division", "menu_label", "sap_number", "topic")


def _typed_rows(turn, data, rows, kind, notes, typing=None):
    """Join the typed rows the AI read to data["lines"] (typed_lines.add) the way the chat
    does when they are typed by hand. Returns the first row that still lacks a field, as the
    chat's typed-row step (data["entry"]) asks it, or None. A draft of typed rows only is
    filed with its group's tile and goes to that tile's division."""
    entry, keep = (
        None,
        typing is not None
        and all(equipment.entry_group != typing["group"] for equipment, _ in rows),
    )
    typed_row = {"group": typing["group"], **typing["row"]} if typing else None
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
        # Read again with the draft as context (ADR 0022 Q14), a row the draft already holds
        # is not a new one, and a new count for a row of the same size corrects that row.
        held = [r for line in data["lines"] for r in line.get("details", [])]
        if any(_covers(old, group, values) for old in held):
            continue
        if typed_row is not None and _covers(typed_row, group, values):
            keep = True  # the row being typed, read again
            continue
        held += [typed_row] if typed_row is not None else []
        corrected = [
            old
            for old in held
            if old["group"] == group
            and (not values["size"] or old.get("size") == values["size"])
            and values["quantity"]
            and old.get("quantity") != values["quantity"]
        ]
        if len(corrected) == 1:
            old = corrected[0]
            if old is not typed_row:
                data["lines"] = _without_row(data["lines"], old)
            for key in ("size", "name", "equipment_number"):
                values[key] = values[key] or old.get(key, "")
            if not values["numbers"] and len(old.get("numbers", [])) == int(values["quantity"]):
                values["numbers"] = " ".join(old["numbers"])
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
    if keep and entry is None:
        entry = typing
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
        elif len({next(iter(found)) for found in owners.values() if len(found) == 1}) > 1:
            # Groups that each belong to a different division (a meter and a transformer):
            # a division the model named does not hold them together (dialogue-v1 D11).
            # อุปกรณ์อื่น, on several tiles, keeps the division the message named.
            data.pop("division", None)
    return entry


def _covers(old, group, values):
    """Whether a row the draft holds already says everything these read values say."""
    numbers = set(values["numbers"].split())
    return (
        old.get("group") == group
        and all(not values[k] or old.get(k) == values[k] for k in ("size", "quantity", "name"))
        and (
            not values["equipment_number"]
            or old.get("equipment_number") == values["equipment_number"]
        )
        and numbers <= set(old.get("numbers", []))
    )


def _without_row(lines, row):
    """The draft lines without one typed row (its line's quantity follows the rest)."""
    kept = []
    for line in lines:
        details = [r for r in line.get("details", []) if r is not row]
        if len(details) == len(line.get("details", [])):
            kept.append(line)
            continue
        if details:
            line = {**line, "details": details}
            if "quantity" in line:
                line["quantity"] = str(sum(int(r.get("quantity", 0) or 0) for r in details))
            kept.append(line)
    return kept


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
