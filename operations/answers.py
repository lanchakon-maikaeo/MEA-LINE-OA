"""คลังคำตอบ: answering a requester's question in LINE (ADR 0012).

The user (10 Oct 2569) chose all three levels and approved AI for them:

- ก keyword answers: an approved answer whose keyword is in the message, no AI;
- ข the AI chooses one approved answer (ai_answer, mode "choose");
- ค the AI writes a short reply from the approved answers and the requester's own open cases,
  and the reply is checked before it is sent (ai_answer, mode "write").

Which levels run is ตัวเลือกระบบ `chat_answers` (ADR 0009; off by default). The text of every
answer is written and approved by people on the คลังคำตอบ page; an action adds what only the
system knows: the requester's own cases, the policy's emergency guidance, or service hours from
the policy calendar. An answer never touches a draft or a case: after it, the bot asks its
current question again, so the guided flow is never lost.

Hazard words are handled first and everywhere: the emergency guidance goes out at once, then
the message is handled as usual (it may be the description of the problem).
"""

import hashlib
import json

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from operations import identity, options, policy, requester_steps
from operations.models import Case, ChatAnswer

A = ChatAnswer.Action
OUT_OF_SCOPE = "out_of_scope"
# Steps where a typed message is not the answer to the bot's question (a choice is expected).
ANSWER_STEPS = {"kind", "topic", "category", "meter_group", "item", "more", "summary", "location"}
PARTICLES = ("ครับ", "ค่ะ", "คะ", "นะ", "จ้า", "จ้ะ", "ฮะ", "ค้าบ", "ๆ")
SHORT_REST = 6  # characters a greeting may carry besides its keyword (e.g. a name)
# Greetings and thanks when the bank has no short-only answer yet (they are never information
# for staff, nor a name).
GREETINGS = ("สวัสดี", "หวัดดี", "ขอบคุณ", "ขอบใจ", "hello", "thank")
DAYS = ["", "จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]


def mode():
    return options.get(options.CHAT_ANSWERS)


def approved():
    return ChatAnswer.objects.filter(status=ChatAnswer.Status.APPROVED)


def bank_digest(rows=None):
    """What the AI chooses from (keys, titles, keywords, actions). The AI evaluation is bound to
    it: a new or renamed answer needs a new evaluation; editing an answer's text does not."""
    rows = rows if rows is not None else approved()
    payload = sorted([r.key, r.title, list(r.keywords), r.action, r.short_only] for r in rows)
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def normalise(text):
    return " ".join(str(text or "").lower().split())


def may_answer(turn, draft, text=None):
    """A question is answered only where the message is not the answer to the bot's question:
    at a choice, with no draft, or a question typed at a step that takes typed answers when
    ตัวเลือกระบบ chat_typed_steps asks to check."""
    from operations import conversation  # local: conversation imports this module

    if mode() == "off" or turn.conversation.prompt:
        return False
    if draft is None or draft.step in ANSWER_STEPS:
        return True
    return text is not None and conversation.off_step(draft, text) == "question"


# --- ก: keywords ----------------------------------------------------------------------------------


def keyword_answer(turn, draft, text):
    """The approved answer for the message where the chat may answer (see `match`)."""
    if not may_answer(turn, draft, text):
        return None
    return match(text)


def match(text):
    """The approved answer with the most keywords in the message (ties answer nothing). A message
    that names equipment is left to the guided flow and the AI: it is usually a request."""
    current = policy.current_policy()
    message = normalise(text)
    if current is not None and any(
        e["term"].lower() in message for e in current.doc.get("equipment_terms", [])
    ):
        return None
    scored = []
    for answer in approved().exclude(action=A.EMERGENCY).exclude(key=OUT_OF_SCOPE):
        found = [normalise(w) for w in answer.keywords if w and normalise(w) in message]
        if found and answer.short_only and len(_rest(message, found)) > SHORT_REST:
            continue
        if found:
            scored.append((len(found), answer))
    scored.sort(key=lambda row: -row[0])
    if not scored or (len(scored) > 1 and scored[0][0] == scored[1][0]):
        return None
    return scored[0][1]


def _rest(message, words):
    """What is left of a message without the keywords, polite endings and punctuation."""
    for word in [*words, *PARTICLES]:
        message = message.replace(word, "")
    return "".join(ch for ch in message if ch.isalnum() or "\u0e00" <= ch <= "\u0e7f")


def rest(text):
    """The words of a message without polite endings and punctuation ("ครับ" leaves nothing)."""
    return _rest(normalise(text), [])


def _greeting_words():
    """The keywords of the approved short-only answers, or GREETINGS while the bank has none."""
    words = [normalise(w) for a in approved().filter(short_only=True) for w in a.keywords if w]
    return words or list(GREETINGS)


def small_talk(text):
    """True when the message is little more than a greeting or thanks."""
    message = normalise(text)
    found = [w for w in _greeting_words() if w in message]
    return bool(found) and len(_rest(message, found)) <= SHORT_REST


def greets(text):
    """True when the message opens with a greeting or thanks ("สวัสดีครับ ผมชื่อ...")."""
    message = normalise(text)
    return any(message.startswith(w) for w in _greeting_words())


def emergency(turn, text):
    """Hazard words (the policy's own list, negation respected) get the guidance at once."""
    if mode() == "off" or not text:
        return False
    answer = approved().filter(action=A.EMERGENCY).first()
    current = policy.current_policy()
    if answer is None or current is None or not policy.hazard_keywords({"t": text}, current):
        return False
    turn.say(compose(answer))
    return True


# --- Replies --------------------------------------------------------------------------------------


def compose(answer, written=None):
    """The text sent: the model's checked reply, or the approved text with its action's part."""
    if written:
        text = written
        if answer.action == A.EMERGENCY:
            text += "\n" + _guidance()
        return text
    extra = {
        A.EMERGENCY: _guidance,
        A.HOURS: _hours,
    }.get(answer.action)
    part = extra() if extra else ""
    return answer.text + (f"\n{part}" if part else "")


def respond(turn, draft, answer, written=None):
    """Say the answer, then carry on where the requester was."""
    from operations import conversation  # local: conversation imports this module

    text = compose(answer, written)
    if draft is not None:
        turn.say(text)
        if answer.action == A.MY_CASES and not written:
            conversation._my_cases(turn)
        return conversation._ask(turn, draft)
    if answer.action == A.MY_CASES and not written:
        turn.say(text)
        return conversation._my_cases(turn)
    if answer.action == A.MY_CASES:
        # A written status reply still offers the requester's own cases to open.
        return turn.say(text, _case_buttons(turn)[: conversation.MAX_QUICK])
    return conversation._menu(turn, text)


def _case_buttons(turn):
    from operations import services, workflow
    from operations.contracts import Actor

    if turn.requester is None:
        return []
    cases = (
        services.visible_cases(Actor.for_requester(turn.requester))
        .exclude(status__in=workflow.TERMINAL)
        .order_by("-submitted_at")[:5]
    )
    return [(c.number, f"c={c.number}") for c in cases]


def _guidance():
    current = policy.current_policy()
    return current.emergency_guidance if current else ""


def _hours():
    current = policy.current_policy()
    calendar = current.doc.get("calendar", {}) if current else {}
    days = calendar.get("weekdays") or []
    periods = calendar.get("periods") or []
    if not days or not periods:
        return ""
    if days == list(range(days[0], days[-1] + 1)) and len(days) > 2:
        day_text = f"วัน{DAYS[days[0]]}ถึงวัน{DAYS[days[-1]]}"
    else:
        day_text = "วัน" + " ".join(DAYS[d] for d in days)
    times = " และ ".join(f"{start}-{end}" for start, end in periods)
    text = f"เวลาทำการ: {day_text} {times} น."
    urgent = calendar.get("continuous_priorities") or []
    if urgent:
        text += f"\nเรื่องระดับ {', '.join(urgent)} ดูแลนอกเวลาด้วย"
    return text + "\nส่งเรื่องในแชตนี้ได้ตลอด 24 ชั่วโมง"


def facts(requester):
    """What the AI may say about this requester: their own open cases and never anyone else's;
    การใช้ไฟฟ้า ones only while they may reach the AI (Q14, lifted by ADR 0021)."""
    from operations import policy, services, workflow
    from operations.contracts import Actor

    if requester is None:
        return []
    cases = services.visible_cases(Actor.for_requester(requester)).exclude(
        status__in=workflow.TERMINAL
    )
    if policy.usage_kept_from_ai():
        cases = cases.exclude(topic=Case.Topic.USAGE)
    cases = cases.select_related("owner_division").order_by("-submitted_at")[:5]
    rows = []
    for case in cases:
        when = timezone.localtime(case.updated_at)
        rows.append(
            f"เรื่อง {case.number}: {case.get_kind_display()} "
            f"สถานะ {requester_steps.status_word(case)} กองที่ดูแล {case.owner_division.code} "
            f"อัปเดตล่าสุด {when.day:02d}/{when.month:02d}/{when.year + 543}"
        )
    return rows


# --- คลังคำตอบ page -------------------------------------------------------------------------------


@transaction.atomic
def save(actor, key, title, keywords, text, action, reason, approve=False, short_only=False):
    """Create or edit an answer (admin, with a reason, audited). Editing puts it back to ร่าง
    unless it is approved in the same step; text a person writes is no longer synthetic."""
    identity.require_admin(actor)
    reason = identity._require_reason(reason)
    title = " ".join((title or "").split())[:100]
    text = (text or "").strip()
    words = [" ".join(w.split()) for w in (keywords or []) if w and w.strip()][:30]
    if not title or not text or len(text) > 1000:
        raise ValidationError("ต้องมีหัวข้อ และข้อความไม่เกิน 1,000 ตัวอักษร")
    if action not in A.values:
        raise ValidationError("เลือกสิ่งที่ต่อท้ายคำตอบจากรายการ")
    answer = ChatAnswer.objects.select_for_update().filter(key=key).first()
    created = answer is None
    if created:
        answer = ChatAnswer(key=key)
        answer.full_clean(exclude=["title", "text", "keywords", "action", "status"])
    wanted = (title, text, words, action, bool(short_only))
    changed = (
        created
        or (
            answer.title,
            answer.text,
            answer.keywords,
            answer.action,
            answer.short_only,
        )
        != wanted
    )
    if changed and answer.text != text:
        answer.is_synthetic = False
    answer.title, answer.text, answer.keywords, answer.action, answer.short_only = wanted
    answer.updated_by = actor
    if changed:
        answer.status = ChatAnswer.Status.DRAFT
    if approve:
        _approve(answer, actor)
    answer.save()
    identity.audit(
        actor,
        "chat_answer.saved",
        answer,
        reason,
        key=answer.key,
        status=answer.status,
        created=created,
    )
    return answer


def _approve(answer, actor):
    if answer.is_synthetic and settings.APP_ENV == "production":
        raise ValidationError("คำตอบนี้ยังเป็นข้อความจำลอง แก้ข้อความจริงก่อนรับรอง")
    answer.status = ChatAnswer.Status.APPROVED
    answer.approved_by, answer.approved_at = actor, timezone.now()


@transaction.atomic
def retire(actor, key, reason):
    identity.require_admin(actor)
    reason = identity._require_reason(reason)
    answer = ChatAnswer.objects.select_for_update().filter(key=key).first()
    if answer is None:
        raise ValidationError("ไม่พบคำตอบนี้")
    answer.status = ChatAnswer.Status.RETIRED
    answer.updated_by = actor
    answer.save(update_fields=["status", "updated_by", "updated_at"])
    identity.audit(actor, "chat_answer.retired", answer, reason, key=key)
    return answer


DEFAULT = settings.BASE_DIR / "operations" / "fixtures" / "chat_answers_v1.json"


def load(document, approve_as=None):
    """Answers from a fixture; existing keys are never overwritten (people may have edited
    them). `approve_as` (local demo only) approves the new ones as that account."""
    made = 0
    for row in document["answers"]:
        if ChatAnswer.objects.filter(key=row["key"]).exists():
            continue
        answer = ChatAnswer(
            key=row["key"],
            title=row["title"],
            keywords=row.get("keywords", []),
            text=row["text"],
            action=row.get("action", A.TEXT),
            short_only=bool(row.get("short_only")),
            is_synthetic=bool(document.get("is_synthetic", True)),
        )
        if approve_as is not None:
            _approve(answer, approve_as)
        answer.save()
        made += 1
    return made
