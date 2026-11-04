"""เลขติดตามหลายเลขในเรื่องเดียว (ADR 0047): staff of the owner division add the numbers the
requester typed in the details, each with its kind. The first number stays Case.sap_number:
edited with set_sap_number, never removed (Q21). Removing an added number needs a reason."""

import re

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q

from operations import identity, typed_lines, workflow
from operations.contracts import TRACKING_PATTERNS
from operations.flow_runner import TRACKING_LABELS
from operations.models import Case, CaseMilestone, CaseNumber

K = Case.Kind
KINDS = {
    K.WITHDRAWAL: ["slip", "work_order", "memo", "meter", "project", "other_number"],
    K.RETURN: ["return_slip", "return_transfer", "work_order", "meter", "project", "other_number"],
}
OTHER_KINDS = ["work_order", "other_number"]
# ดึงเลขจากรายละเอียด: a run with six digits or more that is not a date.
FOUND = re.compile(r"[A-Za-z0-9][A-Za-z0-9./-]{4,29}")
DATE = re.compile(r"\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}")


def kinds(case):
    return [(k, TRACKING_LABELS[k]) for k in KINDS.get(case.kind, OTHER_KINDS)]


def may_edit(user, case):
    scope = identity.staff_scope(user)
    return (
        bool(typed_lines.sap_label(case.kind, case.topic))
        and case.status not in workflow.TERMINAL
        and (identity.holds(scope, case.owner_division_id) or scope.is_admin)
    )


def _have(case):
    return {case.sap_number.upper(), *(n.upper() for n in _numbers(case))}


def _numbers(case):
    return case.extra_numbers.values_list("number", flat=True)


def split(text, kind):
    """What staff typed, one number per comma or line; a kind without spaces splits on them too."""
    parts = re.split(r"[\n,，、;]+", str(text or "").translate(typed_lines.THAI_DIGITS))
    if " " not in TRACKING_PATTERNS.get(kind, ("",))[0]:
        parts = [p for part in parts for p in part.split()]
    return [" ".join(p.split()) for p in parts if p.strip()]


def found_in_text(case):
    texts = (case.purpose, case.description, case.condition, case.return_reason)
    text = " ".join(t for t in texts if t).translate(typed_lines.THAI_DIGITS)
    have, out = _have(case), []
    for number in FOUND.findall(text):
        number = number.strip("./-")
        digits = sum(c.isdigit() for c in number)
        if digits >= 6 and not DATE.fullmatch(number) and number.upper() not in have:
            have.add(number.upper())
            out.append(number)
    return out[:20]


def repeats(case, number):
    """Q20: other open cases of the same division and kind citing this number."""
    found = (
        Case.objects.filter(Q(sap_number__iexact=number) | Q(extra_numbers__number__iexact=number))
        .filter(owner_division_id=case.owner_division_id, kind=case.kind)
        .exclude(pk=case.pk)
        .exclude(status__in=workflow.TERMINAL)
    )
    return list(found.distinct().values_list("number", flat=True)[:5])


@transaction.atomic
def add(actor, case, kind, numbers):
    """Adds every number or none; returns [(number, other open cases citing it)]."""
    if not may_edit(actor, case):
        raise PermissionDenied
    if kind not in dict(kinds(case)):
        raise ValidationError("เลือกชนิดของเลข")
    numbers = list({n.upper(): n for n in numbers if n}.values())
    if not numbers:
        raise ValidationError("พิมพ์หรือติ๊กเลขที่จะเพิ่ม")
    pattern, problem = TRACKING_PATTERNS[kind]
    have = _have(case)
    for number in numbers:
        if not re.fullmatch(pattern, number):
            raise ValidationError(f"{number}: {problem}")
        if number.upper() in have:
            raise ValidationError(f"{number} อยู่ในเรื่องนี้แล้ว")
    added = []
    for number in numbers:
        CaseNumber.objects.create(case=case, kind=kind, number=number, added_by=actor)
        identity.audit(actor, "case.number_added", case, number=number, kind=kind)
        added.append((number, repeats(case, number)))
    return added


@transaction.atomic
def remove(actor, case, pk, reason):
    if not may_edit(actor, case):
        raise PermissionDenied
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("ใส่เหตุผลที่ลบเลข")
    row = case.extra_numbers.filter(pk=pk).first()
    if row is None:
        raise ValidationError("ไม่พบเลขนี้ในเรื่อง")
    if case.milestones.filter(number=row.number).exists():
        raise ValidationError(f"{row.number} บันทึกคีย์จ่ายแล้ว จึงลบไม่ได้")
    row.delete()
    identity.audit(
        actor, "case.number_removed", case, reason=reason[:300], number=row.number, kind=row.kind
    )


def panel(user, case):
    """What the case page shows of the added numbers; None for a case with no เลขติดตาม."""
    if not typed_lines.sap_label(case.kind, case.topic):
        return None
    keyed = {
        m.number: m for m in case.milestones.filter(stage=CaseMilestone.Stage.KEYED) if m.number
    }
    rows = [
        {
            "pk": n.pk,
            "number": n.number,
            "label": TRACKING_LABELS.get(n.kind, n.kind),
            "keyed": keyed.get(n.number),
            "repeats": repeats(case, n.number),
        }
        for n in case.extra_numbers.all()
    ]
    edit = may_edit(user, case)
    if not rows and not edit:
        return None
    options = kinds(case)
    return {
        "rows": rows,
        "may_edit": edit,
        "kinds": options,
        "kind": case.sap_kind if case.sap_kind in dict(options) else options[0][0],
        "found": found_in_text(case) if edit else [],
    }
