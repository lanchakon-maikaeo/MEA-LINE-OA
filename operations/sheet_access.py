"""ผู้ดูแลชีท (ADR 0033): who may change which sheet the system reads.

An admin names each person for a กอง, แผนก or กลุ่มงาน. That person changes every sheet of the
unit and of the units below it, both kinds (ค้นในชีท and งานติดตาม กอจ.); a sheet of no unit is the
admins' only. Which LINE flow box reads a sheet stays the admins' (flow LINE).
"""

import re

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from operations import identity, units
from operations.models import SheetEditor, WorkGroup

LINK = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")


def spreadsheet_id(text):
    """The ID out of a pasted Google Sheets link, or the text itself when it is the ID."""
    text = (text or "").strip()
    found = LINK.search(text)
    return found.group(1) if found else text


def unit_of(source):
    return units.Unit.of(source.division, source.section, source.work_group)


def covers(unit, target):
    """Whether `unit` is `target` or a unit above it."""
    return (
        unit is not None
        and target is not None
        and unit.division == target.division
        and (unit.section is None or unit.section == target.section)
        and (unit.group is None or unit.group == target.group)
    )


def editor_units(user):
    """The units this person looks after; None for an admin, who looks after every sheet."""
    if identity.staff_scope(user).is_admin:
        return None
    return [
        units.Unit.of(e.division, e.section, e.work_group)
        for e in SheetEditor.objects.filter(user=user, user__is_active=True).select_related(
            "division", "section", "work_group"
        )
    ]


def may_open(user):
    found = editor_units(user)
    return found is None or bool(found)


def can_edit(user, source):
    found = editor_units(user)
    if found is None:
        return True
    target = unit_of(source)
    return any(covers(u, target) for u in found)


def require(user, source):
    if not can_edit(user, source):
        raise PermissionDenied


def unit_options(user):
    """(key, label) of the units a sheet of this person may belong to; "" (no unit) for admins."""
    found = editor_units(user)
    rows = [("", "ไม่ระบุหน่วย (แอดมินแก้ได้คนเดียว)")] if found is None else []
    if found is None:
        from operations.models import Division

        found = [units.Unit.of(d) for d in Division.objects.filter(is_active=True)]
    seen = set()
    for top in found:
        for unit in _below(top):
            if unit.key not in seen:
                seen.add(unit.key)
                rows.append((unit.key, unit.path))
    return rows


def _below(unit):
    yield unit
    if unit.group is not None:
        return
    sections = (
        [unit.section]
        if unit.section
        else unit.division.sections.filter(is_active=True).order_by("order", "name")
    )
    for section in sections:
        if unit.section is None:
            yield units.Unit.of(section=section)
        for group in WorkGroup.objects.filter(section=section, is_active=True).order_by(
            "order", "name"
        ):
            yield units.Unit.of(group=group)


def place(actor, source, key):
    """Put a sheet in the unit `key` ("" for none), which must be one the actor may choose."""
    if key not in dict(unit_options(actor)):
        raise ValidationError("เลือกหน่วยที่คุณดูแล")
    unit = units.unit_from_key(key) if key else None
    source.division = unit.division if unit else None
    source.section = unit.section if unit else None
    source.work_group = unit.group if unit else None


def _reason(reason):
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("ใส่เหตุผลก่อนบันทึก")
    return reason[:300]


@transaction.atomic
def grant(actor, user_id, key, reason):
    identity.require_admin(actor)
    reason = _reason(reason)
    user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
    if user is None or not hasattr(user, "staff_profile"):
        raise ValidationError("เลือกบัญชีเจ้าหน้าที่")
    unit = units.unit_from_key(key)
    if not unit.rows(SheetEditor).filter(user=user).exists():
        SheetEditor.objects.create(
            user=user,
            division=unit.division,
            section=unit.section,
            work_group=unit.group,
            added_by=actor,
        )
    identity.audit(
        actor, "sheet_editor.granted", user, reason, unit=unit.path, person=units.display(user)
    )


@transaction.atomic
def revoke(actor, editor_id, reason):
    identity.require_admin(actor)
    reason = _reason(reason)
    editor = SheetEditor.objects.select_related("user").filter(pk=editor_id).first()
    if editor is None:
        raise ValidationError("ไม่พบผู้ดูแลชีทนี้")
    unit = units.Unit.of(editor.division, editor.section, editor.work_group)
    editor.delete()
    identity.audit(
        actor,
        "sheet_editor.revoked",
        editor.user,
        reason,
        unit=unit.path,
        person=units.display(editor.user),
    )
