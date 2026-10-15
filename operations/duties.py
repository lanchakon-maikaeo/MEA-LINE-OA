"""หน้าที่ของกอง (ADR 0013; routing grill 14 Oct 2569 Q14, Q17-Q21, Q25).

An admin writes what each division does: an overview and a list of งานที่ดูแล, each with two or
more example messages. The routing AI reads the overviews and the entries, never the examples:
those are its test (Q25). Every change needs a reason and is audited. A change to what the AI
reads stops the routing AI until it is evaluated again (Q17); a change to the examples only does
not. Admins and ส่วนกลาง see every division's duties; other staff see their own division's
(Q21 ข and ส่วนกลาง). Only admins edit.
"""

import hashlib
import json

from django.core.exceptions import ValidationError
from django.db import transaction

from operations import identity, routing_words
from operations.models import Division, DutyEntry

MIN_EXAMPLES = 2  # per entry (Q25)
MAX_EXAMPLES = 20
EXAMPLE_LENGTH = (4, 300)


def divisions():
    """The divisions the AI chooses from: active กอง, not a coordination desk."""
    return Division.objects.filter(is_active=True, kind=Division.Kind.DIVISION).order_by("code")


def entries():
    return DutyEntry.objects.filter(is_active=True, division__in=divisions()).select_related(
        "division"
    )


def digest():
    """What the routing AI reads, as one hash; its evaluation is bound to it (Q17)."""
    rows = []
    for division in divisions():
        duties = sorted(
            [e.pk, e.title, e.description] for e in division.duties.filter(is_active=True)
        )
        rows.append([division.code, division.name, division.duty_overview.strip(), duties])
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _examples(value, required):
    lines = value.splitlines() if isinstance(value, str) else list(value or [])
    found = []
    for line in lines:
        text = " ".join(str(line).split())
        if text and text not in found:
            found.append(text)
    if len(found) > MAX_EXAMPLES:
        raise ValidationError(f"ตัวอย่างได้ไม่เกิน {MAX_EXAMPLES} ข้อ")
    low, high = EXAMPLE_LENGTH
    for text in found:
        if not low <= len(text) <= high:
            raise ValidationError(f"ตัวอย่างแต่ละข้อยาว {low} ถึง {high} ตัวอักษร")
    if len(found) < required:
        raise ValidationError(f"ใส่ตัวอย่างข้อความอย่างน้อย {required} ข้อ บรรทัดละข้อ")
    return found


def _text(value, name, low, high):
    text = " ".join(str(value or "").split()) if name == "หัวข้องาน" else str(value or "").strip()
    if not low <= len(text) <= high:
        raise ValidationError(f"{name}ยาว {low} ถึง {high} ตัวอักษร")
    return text


@transaction.atomic
def set_overview(actor, division, overview, examples, reason):
    identity.require_admin(actor)
    reason = identity._require_reason(reason)
    division = Division.objects.select_for_update().get(pk=division.pk)
    overview = str(overview or "").strip()
    if len(overview) > 2000:
        raise ValidationError("ภาพรวมยาวได้ไม่เกิน 2000 ตัวอักษร")
    examples = _examples(examples, 0)
    if (overview, examples) == (division.duty_overview, division.duty_examples):
        raise ValidationError("ไม่มีอะไรเปลี่ยน")
    reads = overview != division.duty_overview
    division.duty_overview, division.duty_examples = overview, examples
    division.save(update_fields=["duty_overview", "duty_examples"])
    identity.audit(
        actor, "duty.overview_changed", division, reason, division=division.code, ai_reads=reads
    )
    return division


@transaction.atomic
def add_entry(actor, division, title, description, examples, reason):
    identity.require_admin(actor)
    reason = identity._require_reason(reason)
    if not division.is_active or division.kind != Division.Kind.DIVISION:
        raise ValidationError("เลือกกองที่ใช้งานอยู่")
    entry = DutyEntry.objects.create(
        division=division,
        title=_text(title, "หัวข้องาน", 2, 100),
        description=_text(description, "คำอธิบาย", 2, 1000),
        examples=_examples(examples, MIN_EXAMPLES),
        updated_by=actor,
    )
    identity.audit(
        actor, "duty.entry_added", entry, reason, division=division.code, title=entry.title
    )
    return entry


@transaction.atomic
def change_entry(actor, entry, title, description, examples, reason):
    identity.require_admin(actor)
    reason = identity._require_reason(reason)
    entry = DutyEntry.objects.select_for_update().get(pk=entry.pk)
    if not entry.is_active:
        raise ValidationError("รายการนี้เลิกใช้แล้ว")
    wanted = (
        _text(title, "หัวข้องาน", 2, 100),
        _text(description, "คำอธิบาย", 2, 1000),
        _examples(examples, MIN_EXAMPLES),
    )
    if wanted == (entry.title, entry.description, entry.examples):
        raise ValidationError("ไม่มีอะไรเปลี่ยน")
    reads = wanted[:2] != (entry.title, entry.description)
    entry.title, entry.description, entry.examples = wanted
    entry.updated_by = actor
    entry.save()
    identity.audit(
        actor,
        "duty.entry_changed",
        entry,
        reason,
        division=entry.division.code,
        title=entry.title,
        ai_reads=reads,
    )
    return entry


@transaction.atomic
def retire_entry(actor, entry, reason):
    identity.require_admin(actor)
    reason = identity._require_reason(reason)
    entry = DutyEntry.objects.select_for_update().get(pk=entry.pk)
    if not entry.is_active:
        raise ValidationError("รายการนี้เลิกใช้แล้ว")
    entry.is_active, entry.updated_by = False, actor
    entry.save(update_fields=["is_active", "updated_by", "updated_at"])
    identity.audit(
        actor, "duty.entry_retired", entry, reason, division=entry.division.code, title=entry.title
    )
    return entry


def may_see_all(user):
    """Admins and ส่วนกลาง (Q21)."""
    return routing_words.may_manage(user)


def visible_divisions(user):
    if may_see_all(user):
        return divisions()
    scope = identity.staff_scope(user)
    return divisions().filter(pk__in=list(scope.division_roles))
