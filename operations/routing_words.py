"""คำที่ระบบจำ: words that send a case to a division (ADR 0013; routing grill 14 Oct 2569).

When the rules cannot tell which division a case belongs to (R10), the case goes to ฝอจ., the
coordinator (ADR 0008). ฝอจ. hands it to the right division and may pick up to three words from
the case's own text (Q4, Q8). When that division accepts, or a coordinator with authority
decides the route, the system remembers them at once (Q3, Q6). The next case of the same kind
that the rules cannot place and that holds one of the words goes to that division:
- ตัวเลือกระบบ learned_routes (Q5, Q12): straight there (the default), or to ฝอจ. with the
  division proposed so one press hands it on, or not at all;
- the rules always win: words count only when the rules cannot tell (Q7);
- words pointing to different divisions in one case leave it with ฝอจ. (Q10);
- when a case the words sent is handed on as "ส่งผิดกอง" and the new division accepts, every
  word that sent it moves to the new division (Q9 ข, Q13); a word that moved twice shows
  "ย้ายบ่อย";
- admins and ฝอจ. see every word and cancel or move one with a reason; other divisions see the
  words that point to them (Q36).
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from operations import identity, options, pii
from operations.models import Division, RoutingWord

MIN_LENGTH = 3
MAX_LENGTH = 40
MAX_WORDS = 3
OFTEN_MOVED = 2  # moves before a word shows "ย้ายบ่อย"
WRONG_DIVISION = "wrong_division"  # the handoff reason that moves words (Q13)
TEXT_FIELDS = ("description", "purpose", "condition", "return_reason", "impact")


def case_text(case):
    """What the requester wrote in the case, as one lower-case line."""
    parts = [getattr(case, name, "") or "" for name in TEXT_FIELDS]
    return " ".join(" ".join(parts).lower().split())


def mode():
    return options.get(options.LEARNED_ROUTES)


def matches(case):
    """Active words of the case's kind found in its text, longest first."""
    text = case_text(case)
    if not text:
        return []
    rows = (
        RoutingWord.objects.filter(kind=case.kind, cancelled_at__isnull=True)
        .select_related("division", "learned_from")
        .order_by("pk")
    )
    found = [row for row in rows if row.division.is_active and row.word.lower() in text]
    return sorted(found, key=lambda row: -len(row.word))


def apply(case, result):
    """The rules' proposal with the words applied: an R10 case whose text holds words that all
    point to one division is proposed to that division. Anything else is left as it was."""
    if result.get("rules") != ["R10"] or mode() == "off":
        return result
    found = matches(case)
    if not found:
        return result
    listed = ", ".join(f"'{row.word}' → {row.division.code}" for row in found)
    divisions = {row.division.code for row in found}
    if len(divisions) > 1:
        note = f"คำที่ระบบจำชี้ไปหลายกอง ({listed}) ฝอจ. เป็นผู้เลือก"
        return {**result, "notes": [*result.get("notes", []), note]}
    first = found[0]
    note = (
        f"ส่งตามคำที่ระบบจำ {', '.join(repr(row.word) for row in found)} "
        f"(สอนจากเรื่อง {first.learned_from.number})"
    )
    return {
        **result,
        "proposed_owner": divisions.pop(),
        "notes": [*result.get("notes", []), note],
        "next_action": "กองตรวจรับเรื่อง ถ้าไม่ใช่งานของกองให้โอนต่อด้วยเหตุผล 'ส่งผิดกอง' "
        "คำที่ระบบจำจะย้ายตามไปเอง",
    }


def place(case, result, coordinator):
    """At submission: the division the words send an R10 case to when learned_routes is
    'direct' (Q5), counting a use for each word; None leaves the case with the coordinator."""
    proposed = apply(case, result)
    if proposed is result or proposed["proposed_owner"] == result["proposed_owner"]:
        return None
    found = [row for row in matches(case) if row.division.code == proposed["proposed_owner"]]
    RoutingWord.objects.filter(pk__in=[row.pk for row in found]).update(uses=F("uses") + 1)
    if mode() != "direct":
        return None
    return Division.objects.filter(code=proposed["proposed_owner"], is_active=True).first()


# --- Choosing and learning words ---------------------------------------------------------------


def policy_terms():
    from operations import policy  # local: policy reads this module for its proposals

    current = policy.current_policy()
    if current is None:
        return set()
    return {e["term"].lower() for e in current.doc.get("equipment_terms", [])}


def clean(case, values):
    """Up to three words from the case's own text (Q8): at least three letters, not a word the
    rules already know, and never personal data (Q31). Raises ValidationError."""
    raw = values if isinstance(values, list) else str(values or "").replace("\n", ",").split(",")
    words, seen = [], set()
    for value in raw:
        word = " ".join(str(value).split())
        if word and word.lower() not in seen:
            seen.add(word.lower())
            words.append(word)
    if len(words) > MAX_WORDS:
        raise ValidationError(f"ใส่คำสำคัญได้ไม่เกิน {MAX_WORDS} คำ")
    text, terms = case_text(case), policy_terms()
    masker = pii.Masker.for_ai() if words else None
    for word in words:
        problem = word_problem(word, text, terms, masker)
        if problem == "length":
            raise ValidationError(f"คำสำคัญ '{word}' ต้องยาว {MIN_LENGTH} ถึง {MAX_LENGTH} ตัวอักษร")
        if problem == "not_in_text":
            raise ValidationError(f"'{word}' ไม่อยู่ในข้อความของเรื่อง")
        if problem == "rule_word":
            raise ValidationError(f"'{word}' มีคำในกติกาอยู่แล้ว กติกาจะเป็นผู้เลือกกอง")
        if problem == "personal":
            raise ValidationError(f"'{word}' เป็นข้อมูลส่วนบุคคล ระบบไม่จำ")
    return words


def word_problem(word, text, terms, masker):
    """Why a word cannot be remembered, or "": its length, not in the (lower-case) text, a word
    the rules already know, or personal data. Shared by people and the keyword AI (Q34)."""
    lowered = word.lower()
    if not MIN_LENGTH <= len(word) <= MAX_LENGTH:
        return "length"
    if lowered not in text:
        return "not_in_text"
    if any(term == lowered or term in lowered for term in terms):
        return "rule_word"
    if masker.mask(word) != word:
        return "personal"
    return ""


def learn(case, words, division, source, taught_by=None, accepted_by=None):
    """Remember the words for the case's kind and this division, at once (Q3, Q6). A word that
    already points elsewhere moves here: two people just decided so (Q9 ข)."""
    learned = []
    for word in words:
        row = (
            RoutingWord.objects.select_for_update()
            .filter(kind=case.kind, cancelled_at__isnull=True, word__iexact=word)
            .first()
        )
        actor = taught_by or accepted_by
        if row is None:
            row = RoutingWord.objects.create(
                word=word,
                kind=case.kind,
                division=division,
                source=source,
                learned_from=case,
                taught_by=taught_by,
                accepted_by=accepted_by,
            )
            identity.audit(
                actor,
                "routing_word.learned",
                row,
                actor_label="" if actor else "ระบบ",
                word=word,
                kind=case.kind,
                division=division.code,
                case=case.number,
                source=source,
            )
        elif row.division_id != division.pk:
            _move(row, division, case, actor)
        learned.append(row)
    return learned


def _move(row, division, case, actor):
    previous = row.division.code
    row.division, row.learned_from = division, case
    row.moves = F("moves") + 1
    row.save(update_fields=["division", "learned_from", "moves", "updated_at"])
    row.refresh_from_db()
    identity.audit(
        actor,
        "routing_word.moved",
        row,
        actor_label="" if actor else "ระบบ",
        word=row.word,
        previous=previous,
        division=division.code,
        case=case.number,
    )


def may_teach(case, recommendation):
    """ส่วนกลาง hands on a case the rules could not place (Q2), while the system learns."""
    coordinator = coordinator_division()
    return (
        mode() != "off"
        and recommendation is not None
        and recommendation.rules == ["R10"]
        and coordinator is not None
        and case.owner_division_id == coordinator.pk
    )


def teachable(case):
    """may_teach for a page: reads the current proposal without locking the case."""
    from operations.models import CaseTriage

    state = CaseTriage.objects.filter(case=case).select_related("current_recommendation").first()
    return may_teach(case, state.current_recommendation if state else None)


KEYWORDS_HELP = (
    "ไม่บังคับ คำจากข้อความของเรื่องนี้ ยาว 3 ตัวอักษรขึ้นไป ไม่เกิน 3 คำ คั่นด้วยจุลภาค "
    "เมื่อกองปลายทางรับเรื่อง เรื่องครั้งหน้าที่มีคำนี้จะไปกองนั้นเอง"
)


def after_handoff(case, handoff, actor):
    """A handoff the receiving division accepted (routing._answer): learn the words ฝอจ. picked
    for an R10 case, or move the words that sent the case when it was the wrong division."""
    rules = handoff.recommendation.rules if handoff.recommendation else []
    if rules != ["R10"] or mode() == "off":
        return
    if handoff.keywords:
        learn(
            case,
            handoff.keywords,
            handoff.to_division,
            RoutingWord.Source.HANDOFF,
            taught_by=handoff.requested_by,
            accepted_by=actor,
        )
    if handoff.reason_category == WRONG_DIVISION:
        for row in matches(case):
            if row.division_id == handoff.from_division_id:
                _move(row, handoff.to_division, case, actor)


def after_decision(case, recommendation, previous, division, actor, keywords):
    """A coordinator with authority placed an R10 case (routing.decide_routing): learn the words
    picked with the decision at once (Q3), and move the words that had sent it elsewhere."""
    if recommendation is None or recommendation.rules != ["R10"] or mode() == "off":
        return
    if keywords:
        learn(case, keywords, division, RoutingWord.Source.HANDOFF, actor, actor)
    for row in matches(case):
        if row.division_id == previous.pk:
            _move(row, division, case, actor)


# --- The list page (Q36) -----------------------------------------------------------------------


def may_manage(user):
    """Admins and the coordinator's staff see and change every word."""
    scope = identity.staff_scope(user)
    if scope.is_admin:
        return True
    coordinator = coordinator_division()
    return coordinator is not None and identity.holds(scope, coordinator.pk)


def coordinator_division():
    from operations import policy  # local: policy reads this module for its proposals

    current = policy.current_policy()
    code = current.units.get("coordinator") if current else None
    return Division.objects.filter(code=code, is_active=True).first() if code else None


def visible(user):
    """The words this person may see: all of them, or those pointing to their divisions."""
    rows = RoutingWord.objects.filter(cancelled_at__isnull=True).select_related(
        "division", "learned_from", "taught_by__staff_profile"
    )
    if may_manage(user):
        return rows
    scope = identity.staff_scope(user)
    return rows.filter(division_id__in=list(scope.division_roles))


def _managed(actor, row, reason):
    if not may_manage(actor):
        raise ValidationError("เฉพาะแอดมินและ ฝอจ.")
    reason = str(reason or "").strip()
    if row.cancelled_at is not None:
        raise ValidationError("คำนี้ถูกยกเลิกไปแล้ว")
    return reason


@transaction.atomic
def cancel(actor, row, reason):
    row = RoutingWord.objects.select_for_update().get(pk=row.pk)
    reason = _managed(actor, row, reason)
    row.cancelled_by, row.cancelled_at, row.cancel_reason = actor, timezone.now(), reason
    row.save(update_fields=["cancelled_by", "cancelled_at", "cancel_reason", "updated_at"])
    identity.audit(actor, "routing_word.cancelled", row, reason, word=row.word)
    return row


@transaction.atomic
def change_division(actor, row, division, reason):
    row = RoutingWord.objects.select_for_update().get(pk=row.pk)
    reason = _managed(actor, row, reason)
    if division is None or not division.is_active:
        raise ValidationError("เลือกกองที่ใช้งานอยู่")
    if division.pk == row.division_id:
        raise ValidationError("คำนี้ชี้ไปกองนี้อยู่แล้ว")
    previous = row.division.code
    row.division = division
    row.save(update_fields=["division", "updated_at"])
    identity.audit(
        actor,
        "routing_word.changed",
        row,
        reason,
        word=row.word,
        previous=previous,
        division=division.code,
    )
    return row
