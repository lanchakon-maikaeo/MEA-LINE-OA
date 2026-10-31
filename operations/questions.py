"""คำถามที่บอทตอบไม่ได้ (routing grill 14 Oct 2569, Q1 ค and Q11).

A question the bot could not answer does not become a case. It is kept for admins so they can
add an answer to คลังคำตอบ: text typed outside a draft that neither the keywords nor the AI
answered, apart from greetings, thanks and a few letters. The same words count together. The
text is stored with personal data masked (ADR 0014) and without who typed it. The bot still
answers with the menu and promises nobody will reply.
"""

import hashlib

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from operations import identity
from operations.models import UnansweredQuestion

MIN_LETTERS = 4  # letters left without polite endings and punctuation
MAX_TEXT = 500
MAX_ASKERS = 100


def record(turn, text):
    """Keep a question the bot answered with the menu or the out-of-scope answer."""
    from operations import answers, pii  # local: answers builds on the conversation

    if not text or turn.requester is None or answers.small_talk(text):
        return None
    if len(answers.rest(text)) < MIN_LETTERS:
        return None
    masked = pii.Masker.for_ai().mask(" ".join(text.split()))[:MAX_TEXT]
    key = hashlib.sha256(answers.rest(masked).encode()).hexdigest()
    rows = UnansweredQuestion.objects.filter(key=key)
    if rows.update(count=F("count") + 1, last_at=turn.now):
        return _asked_by(rows.first(), turn)
    try:
        with transaction.atomic():
            return _asked_by(
                UnansweredQuestion.objects.create(
                    key=key, text=masked, first_at=turn.now, last_at=turn.now
                ),
                turn,
            )
    except IntegrityError:
        rows.update(count=F("count") + 1, last_at=turn.now)
        return _asked_by(rows.first(), turn)


def _asked_by(question, turn):
    """ADR 0037 Q31: a keyed hash of a registered asker and the day, so the nightly tuning
    counts people; anonymous reporters are not counted. Who asked is never stored."""
    from operations import free_chat  # local: free_chat reads the conversation

    if question is None or getattr(turn, "anonymous", False) or turn.requester is None:
        return question
    entry = {"h": free_chat.asker_key(turn.identity.pk), "d": turn.now.date().isoformat()}
    with transaction.atomic():
        row = UnansweredQuestion.objects.select_for_update().get(pk=question.pk)
        if entry not in row.askers:
            row.askers = [*row.askers, entry][-MAX_ASKERS:]
            row.save(update_fields=["askers"])
    return row


@transaction.atomic
def handle(actor, question, status, reason="", answer_key=""):
    """An admin marks a question answered (added to คลังคำตอบ), not to be answered, or open."""
    identity.require_admin(actor)
    question = UnansweredQuestion.objects.select_for_update().get(pk=question.pk)
    if status not in UnansweredQuestion.Status.values:
        raise ValidationError("ไม่รู้จักสถานะนี้")
    if question.status == status:
        raise ValidationError("คำถามนี้อยู่ในสถานะนี้แล้ว")
    previous = question.status
    question.status, question.answer_key = status, answer_key
    question.handled_by, question.handled_at = actor, timezone.now()
    question.save(update_fields=["status", "answer_key", "handled_by", "handled_at"])
    identity.audit(
        actor,
        "question.handled",
        question,
        reason,
        previous=previous,
        status=status,
        answer=answer_key,
    )
    return question
