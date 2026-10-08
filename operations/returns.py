"""M08 returns: instructions, arrivals, inspection, discrepancies and closing (M08 Q1–Q15).

Handlers run inside services.perform_action (case locked, matrix and revision checked; Decision,
audit, outbox and receipt recorded there). What physically arrives is always recorded (Q3); what
does not match the request is kept apart as ส่วนต่างการรับคืน until a person says what it is.
Arrived is never ready: only an inspection result "ready" with condition certification moves a
unit to ready stock (Q5/Q6), and a second, different result is a conflict for a third person
(Q9). Lock order: case → its lines → items ascending (inventory.lock_items).
"""

from datetime import date
from decimal import Decimal

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from operations import coordination, fulfilment, inventory, registry
from operations.contracts import (
    MAX_LINES,
    ErrorCode,
    check_quantity_for_item,
    clean_serials,
    clean_text,
    fail,
    parse_quantity,
)
from operations.models import (
    AuthorityGrant,
    Case,
    CaseLine,
    Decision,
    EquipmentType,
    FakeSerial,
    FulfilmentEntry,
    Inspection,
    IssuedSerial,
    ReturnArrival,
    ReturnUnit,
    Role,
)

A = AuthorityGrant.Activity
S = Case.Status
K = Case.Kind
U = ReturnUnit
L = FulfilmentEntry.Kind
ZERO = Decimal("0")
TERMINAL = frozenset({S.CLOSED, S.REJECTED, S.CANCELLED})
READY, REPAIR, OTHER, PENDING = "ready", "repair", "other", "pending"
OUTCOMES = {READY: "พร้อมใช้", REPAIR: "ต้องซ่อม", OTHER: "ดำเนินการอื่นตามระเบียบ"}
TARGET = {READY: inventory.AVAILABLE, REPAIR: inventory.AWAITING_REPAIR, OTHER: None}
STOP_CATEGORIES = {"not_returned": "เขตไม่คืน", "lost": "สูญหาย", "other": "อื่น ๆ"}

_lines = fulfilment._lines
_ledger = fulfilment._ledger
_member = fulfilment._member
_number = fulfilment._number


def _require_role(ctx, case, role, message, lines=None):
    """The role in the division handling the lines (M09 Q11), else in the owner division."""
    divisions = {coordination.division_of(line) for line in lines or [] if line is not None}
    if lines is None or not divisions or any(line is None for line in lines):
        divisions.add(case.owner_division_id)
    if ctx.requester is not None or not all(_member(ctx, d, [role]) for d in divisions):
        fail(ErrorCode.FORBIDDEN, message)


def _require_member(ctx, case, message="สมาชิกกองเจ้าภาพเท่านั้น"):
    if ctx.requester is not None or not _member(ctx, case.owner_division_id):
        fail(ErrorCode.FORBIDDEN, message)


def _manual():
    return inventory.mode() == inventory.MANUAL


def _unit(case, payload):
    seq = payload.get("unit")
    if isinstance(seq, bool) or not isinstance(seq, int):
        fail(ErrorCode.VALIDATION_ERROR, "unit (its number in this case) is required.")
    unit = (
        U.objects.select_for_update(of=("self",))
        .select_related("item", "arrival", "line")
        .filter(case=case, seq=seq)
        .first()
    )
    if unit is None:
        fail(ErrorCode.VALIDATION_ERROR, "Unknown unit for this case.")
    return unit


def certification_grant(user, case, item, division_id=None, now=None):
    """An active "รับรองสภาพ" grant whose division/category/item scope covers the item."""
    from operations import policy

    division_id = division_id or case.owner_division_id
    for grant in policy.active_grants(user, A.CONDITION_CERTIFICATION, now):
        if grant.division_id not in (None, division_id):
            continue
        if grant.category and grant.category != item.category:
            continue
        if grant.item_id and grant.item_id != item.pk:
            continue
        return grant
    return None


def _unit_division(case, unit):
    return coordination.division_of(unit.line) if unit.line_id else case.owner_division_id


def _require_certification(ctx, case, item, unit=None):
    division = _unit_division(case, unit) if unit is not None else None
    grant = (
        None if ctx.requester is not None else certification_grant(ctx.user, case, item, division)
    )
    if grant is None:
        fail(ErrorCode.FORBIDDEN, f"ผลพร้อมใช้และการตัดสินผลขัดกันต้องมีอำนาจรับรองสภาพ {item.code} (Q5)")
    return grant


def settle(case, lines):
    """Q11: in progress while anything is outstanding, unexplained or not finally judged."""
    units = list(U.objects.filter(case=case))
    if any(line.return_outstanding > 0 for line in lines):
        return S.IN_PROGRESS if units else S.AWAITING_EXECUTION
    for unit in units:
        if unit.status == U.Status.SENT_BACK:
            continue
        if unit.open_discrepancy or unit.status != U.Status.FINAL:
            return S.IN_PROGRESS
    if coordination.blocks_closing(case):  # open child work keeps it open (M09 Q6)
        return S.IN_PROGRESS if units else S.AWAITING_EXECUTION
    return S.CLOSED


def _next_seq(model, case):
    return model.objects.filter(case=case).count() + 1


def _count(ctx, case, unit, reason):
    """The unit now counts as returned for its line."""
    line = CaseLine.objects.select_for_update().get(pk=unit.line_id)
    line.returned_quantity += unit.quantity
    line.save(update_fields=["returned_quantity"])
    _ledger(
        ctx, case, line, L.RETURNED, unit.quantity, reason, [unit.serial] if unit.serial else []
    )


# --- Instructions (Q7) -------------------------------------------------------------------------


def _date(payload, name):
    raw = payload.get(name)
    if raw in (None, ""):
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        fail(ErrorCode.VALIDATION_ERROR, f"{name} must be YYYY-MM-DD.")


def return_instructions(ctx, case, payload, reason, target):
    """แจ้งวิธีส่งคืน: where and when to bring the goods; pushed once (Q7/Q12)."""
    _require_member(ctx, case)
    place = clean_text(payload, "place", True, 200)
    start, end = _date(payload, "from_date"), _date(payload, "to_date")
    if start and end and end < start:
        fail(ErrorCode.VALIDATION_ERROR, "to_date is before from_date.")
    first = not Decision.objects.filter(case=case, action="return_instructions").exists()
    return {
        "place": place,
        "from_date": start.isoformat() if start else None,
        "to_date": end.isoformat() if end else None,
        "note": clean_text(payload, "note", False, 500),
        "first": first,
    }


def latest_instructions(case):
    decision = case.decisions.filter(action="return_instructions").order_by("-id").first()
    return decision.data if decision else None


# --- Arrival (Q2, Q3, Q4, Q6) ------------------------------------------------------------------


def _arrival_rows(payload):
    raw = payload.get("items")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_LINES:
        fail(ErrorCode.VALIDATION_ERROR, "items lists what arrived: item_code, quantity, serials.")
    codes = [row.get("item_code") if isinstance(row, dict) else None for row in raw]
    items = {i.code: i for i in EquipmentType.objects.filter(code__in=codes, is_active=True)}
    rows, seen = [], set()
    for row, code in zip(raw, codes, strict=True):
        if not isinstance(row, dict) or set(row) - {"item_code", "quantity", "serials"}:
            fail(ErrorCode.VALIDATION_ERROR, "Each item has item_code, quantity and serials.")
        item = items.get(code)
        if item is None:
            fail(ErrorCode.VALIDATION_ERROR, f"ไม่มีรายการของรหัส {code}")
        if item.pk in seen:
            fail(ErrorCode.VALIDATION_ERROR, "รวมรายการเดียวกันไว้ในแถวเดียว")
        seen.add(item.pk)
        serials = clean_serials(row.get("serials"))  # refuses a serial twice in one arrival
        if item.is_serialized:
            if not serials:
                fail(ErrorCode.VALIDATION_ERROR, f"{item.code} ต้องระบุเลขเครื่องที่มาถึง")
            quantity = Decimal(len(serials))
            if (
                row.get("quantity") not in (None, "")
                and parse_quantity(row["quantity"]) != quantity
            ):
                fail(ErrorCode.VALIDATION_ERROR, f"{item.code} จำนวนต้องเท่ากับจำนวนเลขเครื่อง")
        else:
            if serials:
                fail(ErrorCode.VALIDATION_ERROR, f"{item.code} ไม่ได้ควบคุมรายชิ้น")
            quantity = check_quantity_for_item(parse_quantity(row.get("quantity")), item)
        rows.append((item, quantity, serials))
    return rows


def _expected_serials(line):
    """Requested serials still expected: not arrived, not replaced, not stopped."""
    if line is None or line.return_outstanding <= 0:
        return set()
    done = set(
        U.objects.filter(line=line)
        .exclude(status=U.Status.SENT_BACK)
        .exclude(Q(discrepancy__gt="") & Q(resolution=""))
        .values_list("serial", flat=True)
    )
    done |= set(
        U.objects.filter(line=line)
        .exclude(replaces_serial="")
        .values_list("replaces_serial", flat=True)
    )
    return set(line.serials or []) - done


def _held_elsewhere(case, item, serial):
    """Another unit of an open return already holds this serial (not sent back)."""
    return (
        U.objects.filter(item=item, serial=serial)
        .exclude(status=U.Status.SENT_BACK)
        .exclude(discrepancy__in=[U.Discrepancy.DUPLICATE_CASE, U.Discrepancy.DUPLICATE_STORE])
        .exclude(case__status__in=TERMINAL)
        .exists()
    )


def _serial_discrepancy(case, line, item, serial, issued):
    status = inventory.serial_status(item, serial)
    if status not in (None, FakeSerial.Status.ISSUED, FakeSerial.Status.DISPOSED):
        return (
            U.Discrepancy.DUPLICATE_STORE,
            f"คลังจำลองมีเลขนี้สถานะ {FakeSerial.Status(status).label}",
        )
    if _held_elsewhere(case, item, serial):
        return U.Discrepancy.DUPLICATE_CASE, "เลขนี้ถูกรับไว้ในเรื่องคืนที่ยังเปิดอยู่"
    if issued is not None and issued.delivery_line.delivery.case.district_id != case.district_id:
        source = issued.delivery_line.delivery.case
        return U.Discrepancy.OTHER_DISTRICT, f"จ่ายให้ {source.district.code} ในเรื่อง {source.number}"
    if serial not in _expected_serials(line):
        return U.Discrepancy.OUTSIDE_REQUEST, "เลขนี้ไม่อยู่ในคำขอ"
    return "", ""


def receive(ctx, case, payload, reason, target):
    """การรับของคืน: record what physically arrived; arrived is not ready (Q2/Q3/Q6)."""
    if ctx.requester is not None:
        fail(ErrorCode.FORBIDDEN, "เฉพาะผู้รับคืนของกองเจ้าภาพบันทึกการรับ (Q2)")
    carrier = clean_text(payload, "carrier_name", True, 200)
    document = clean_text(payload, "document_number", False, 64)
    if _manual() and not document:
        fail(ErrorCode.VALIDATION_ERROR, "บันทึกเลขเอกสารรับจากระบบทะเบียนเดิม (manual_reference)")
    rows = _arrival_rows(payload)
    lines = _lines(case)
    by_item = {line.item_id: line for line in lines}
    _require_role(
        ctx,
        case,
        Role.RECEIVER,
        "เฉพาะผู้รับคืนของกองที่รับผิดชอบรายการบันทึกการรับ (Q2)",
        [by_item.get(item.pk) for item, _, _ in rows],
    )
    inventory.lock_items(item.pk for item, _, _ in rows)
    now = timezone.now()
    arrival = ReturnArrival.objects.create(
        case=case,
        seq=_next_seq(ReturnArrival, case),
        mode=inventory.mode(),
        document_number=document,
        carrier_name=carrier,
        note=reason,
        received_by=ctx.user,
        received_at=now,
    )
    seq = _next_seq(U, case)
    recorded = []

    def unit(item, line, quantity, serial="", discrepancy="", note="", issued=None):
        nonlocal seq
        made = U.objects.create(
            case=case,
            seq=seq,
            arrival=arrival,
            line=line,
            item=item,
            serial=serial,
            quantity=quantity,
            discrepancy=discrepancy,
            discrepancy_note=note,
            issued_serial=issued,
        )
        seq += 1
        # A duplicate is a data conflict: the fake store is left alone until it is settled.
        if discrepancy not in (U.Discrepancy.DUPLICATE_STORE, U.Discrepancy.DUPLICATE_CASE):
            moved = inventory.move_fake(
                item,
                quantity,
                serial,
                None,
                inventory.AWAITING_INSPECTION,
                case,
                ctx.user,
                ctx.label,
                f"รับคืน {case.number}",
            )
            if moved and inventory.mode() == inventory.FAKE:
                made.placed = {inventory.AWAITING_INSPECTION: _number(moved)}
                made.save(update_fields=["placed"])
        if made.counted:
            _count(ctx, case, made, reason)
        recorded.append(
            {
                "unit": made.seq,
                "item": item.code,
                "serial": serial,
                "quantity": _number(quantity),
                "discrepancy": discrepancy,
            }
        )
        return made

    for item, quantity, serials in rows:
        line = by_item.get(item.pk)
        if item.is_serialized:
            for serial in serials:
                issued = (
                    IssuedSerial.objects.select_for_update()
                    .select_related("delivery_line__delivery__case__district")
                    .filter(item=item, serial=serial, active=True)
                    .first()
                )
                discrepancy, note = _serial_discrepancy(case, line, item, serial, issued)
                if issued is not None:  # Q4: the goods are back, whatever else is wrong
                    issued.active, issued.ended_at = False, now
                    issued.end_reason = f"รับคืน {case.number}"[:100]
                    issued.save(update_fields=["active", "ended_at", "end_reason"])
                unit(item, line, Decimal("1"), serial, discrepancy, note, issued)
                if line is not None:
                    line.refresh_from_db()
            continue
        if line is None:
            unit(item, None, quantity, "", U.Discrepancy.OUTSIDE_REQUEST, "รายการนี้ไม่อยู่ในคำขอ")
            continue
        matched = min(quantity, line.return_outstanding)
        if matched > 0:
            unit(item, line, matched)
            line.refresh_from_db()
        if quantity > matched:
            unit(item, line, quantity - matched, "", U.Discrepancy.EXCESS, "มาเกินจำนวนที่ขอคืน")
    arrived = list(U.objects.filter(arrival=arrival).select_related("item", "line"))
    registry.note(  # M11: the SAP receipt document of what physically arrived
        ctx,
        case,
        registry.R.RETURN,
        "receive",
        f"arrival:{arrival.pk}",
        document,
        [(u.item, u.quantity, [u.serial] if u.serial else []) for u in arrived],
        _unit_division(case, arrived[0]) if arrived else None,
        payload,
    )
    lines = _lines(case)
    case.status = settle(case, lines)
    return {"arrival": arrival.seq, "document_number": document, "units": recorded}


# --- Inspection (Q5, Q6, Q9) -------------------------------------------------------------------


def _split(payload, unit, allow_pending=True):
    raw = payload.get("split")
    if not isinstance(raw, dict) or not raw:
        fail(ErrorCode.VALIDATION_ERROR, "split maps outcomes (ready/repair/other) to quantities.")
    allowed = set(OUTCOMES) | ({PENDING} if allow_pending else set())
    if set(raw) - allowed:
        fail(ErrorCode.VALIDATION_ERROR, f"Outcomes are {', '.join(sorted(allowed))}.")
    if PENDING in raw and len(raw) > 1:
        fail(ErrorCode.VALIDATION_ERROR, "ยังรอตรวจใช้กับทั้งหน่วย ไม่แบ่งกับผลอื่น")
    split = {}
    for outcome, value in raw.items():
        quantity = check_quantity_for_item(parse_quantity(value), unit.item)
        split[outcome] = quantity
    if sum(split.values(), ZERO) != unit.quantity:
        fail(ErrorCode.VALIDATION_ERROR, f"ผลตรวจต้องรวมได้ {_number(unit.quantity)} เท่ากับหน่วยนี้")
    return split


def _text_split(split):
    return {k: _number(v) for k, v in sorted(split.items())}


def _check_inspectable(ctx, unit):
    if unit.status == U.Status.SENT_BACK:
        fail(ErrorCode.VALIDATION_ERROR, "หน่วยนี้ส่งกลับเขตแล้ว")
    if unit.open_discrepancy:
        fail(ErrorCode.VALIDATION_ERROR, "บันทึกผลส่วนต่างการรับคืนก่อนตรวจสภาพ (Q3)")
    if unit.arrival.received_by_id == ctx.user.pk:
        fail(ErrorCode.FORBIDDEN, "ผู้รับของไม่ตรวจสภาพของชิ้นที่ตนรับ (Q5)")


def _place(ctx, case, unit, split, reason):
    """Move the unit from awaiting inspection to the figures its result names (Q6)."""
    placed = {}
    for outcome, quantity in split.items():
        moved = inventory.move_fake(
            unit.item,
            quantity,
            unit.serial,
            inventory.AWAITING_INSPECTION,
            TARGET[outcome],
            case,
            ctx.user,
            ctx.label,
            reason or f"ผลตรวจ {OUTCOMES[outcome]} {case.number}",
        )
        if moved and TARGET[outcome]:
            placed[TARGET[outcome]] = _number(Decimal(placed.get(TARGET[outcome], "0")) + moved)
    unit.placed = placed if inventory.mode() == inventory.FAKE else {}


def _withdraw(ctx, case, unit):
    """Q9: a conflicting result takes the unit back to awaiting inspection while it is still
    in the store; goods already issued again stay out and are flagged."""
    back, flags = {}, []
    for bucket, value in (unit.placed or {}).items():
        quantity = Decimal(value)
        if bucket == inventory.AWAITING_INSPECTION:
            back[bucket] = value
            continue
        moved = inventory.move_fake(
            unit.item,
            quantity,
            unit.serial,
            bucket,
            inventory.AWAITING_INSPECTION,
            case,
            ctx.user,
            ctx.label,
            f"ผลตรวจขัดกัน {case.number}",
        )
        if moved:
            total = Decimal(back.get(inventory.AWAITING_INSPECTION, "0")) + moved
            back[inventory.AWAITING_INSPECTION] = _number(total)
        else:
            flags.append(f"{_number(quantity)} จาก{bucket} ออกจากคลังไปแล้ว")
    unit.placed = back
    unit.conflict_note = "; ".join(flags)[:200]


def inspect(ctx, case, payload, reason, target):
    """การตรวจสภาพ: a result per unit; "ready" needs certification; a different second result
    is a conflict (Q9)."""
    unit = _unit(case, payload)
    _require_role(ctx, case, Role.INSPECTOR, "เฉพาะผู้ตรวจสภาพของกองเจ้าภาพ (Q5)", [unit.line])
    _check_inspectable(ctx, unit)
    if unit.status == U.Status.CONFLICT:
        fail(ErrorCode.VALIDATION_ERROR, "ผลตรวจขัดกัน รอผู้มีอำนาจรับรองสภาพตัดสิน (Q9)")
    split = _split(payload, unit)
    reference = clean_text(payload, "reference", False, 100)
    pending = PENDING in split
    if _manual() and not pending and not reference:
        fail(ErrorCode.VALIDATION_ERROR, "บันทึกเลขเอกสารผลตรวจจากระบบทะเบียนเดิม (manual_reference)")
    if OTHER in split and not reason:
        fail(ErrorCode.VALIDATION_ERROR, "ผลดำเนินการอื่นตามระเบียบต้องมีเหตุผล")
    grant = _require_certification(ctx, case, unit.item, unit) if split.get(READY) else None
    inventory.lock_items([unit.item_id])
    text = _text_split(split)
    if pending and unit.status == U.Status.FINAL:
        fail(ErrorCode.VALIDATION_ERROR, "หน่วยนี้มีผลตรวจแล้ว")
    if unit.status == U.Status.FINAL and text == unit.final_split:
        fail(ErrorCode.VALIDATION_ERROR, "ผลเดียวกับที่บันทึกไว้แล้ว")
    inspection = Inspection.objects.create(
        unit=unit,
        kind=Inspection.Kind.INSPECT,
        split=text,
        reason=reason,
        reference=reference,
        inspector=ctx.user,
        grant=grant,
    )
    if not pending:
        _note_status(ctx, case, unit, split, "inspect", inspection, reference, payload)
    outcome = "pending"
    if unit.status == U.Status.FINAL:
        _withdraw(ctx, case, unit)
        unit.status, outcome = U.Status.CONFLICT, "conflict"
    elif not pending:
        _place(ctx, case, unit, split, reason)
        unit.status, unit.final_split, outcome = U.Status.FINAL, text, "final"
    unit.save()
    _after_stock_change(unit)
    case.status = settle(case, _lines(case))
    return {"unit": unit.seq, "split": text, "outcome": outcome, "flag": unit.conflict_note}


def decide_inspection(ctx, case, payload, reason, target):
    """Q9: a certifier who inspected neither result chooses the final one."""
    unit = _unit(case, payload)
    if unit.status != U.Status.CONFLICT:
        fail(ErrorCode.VALIDATION_ERROR, "หน่วยนี้ไม่มีผลตรวจขัดกัน")
    grant = _require_certification(ctx, case, unit.item, unit)
    inspectors = set(
        unit.inspections.exclude(split__has_key=PENDING).values_list("inspector_id", flat=True)
    )
    if ctx.user.pk in inspectors or unit.arrival.received_by_id == ctx.user.pk:
        fail(ErrorCode.FORBIDDEN, "ผู้ตัดสินต้องไม่ใช่ผู้ตรวจหรือผู้รับของหน่วยนี้ (Q9)")
    split = _split(payload, unit, allow_pending=False)
    reference = clean_text(payload, "reference", False, 100)
    if _manual() and not reference:
        fail(ErrorCode.VALIDATION_ERROR, "บันทึกเลขเอกสารผลตรวจจากระบบทะเบียนเดิม (manual_reference)")
    inventory.lock_items([unit.item_id])
    text = _text_split(split)
    inspection = Inspection.objects.create(
        unit=unit,
        kind=Inspection.Kind.DECIDE,
        split=text,
        reason=reason,
        reference=reference,
        inspector=ctx.user,
        grant=grant,
    )
    _note_status(ctx, case, unit, split, "decide_inspection", inspection, reference, payload)
    _place(ctx, case, unit, split, reason)
    unit.status, unit.final_split = U.Status.FINAL, text
    unit.save()
    _after_stock_change(unit)
    case.status = settle(case, _lines(case))
    return {"unit": unit.seq, "split": text, "grant_id": grant.pk}


def _note_status(ctx, case, unit, split, action, inspection, reference, payload):
    """M11: the SAP stock-status document behind an inspection result (with its evidence)."""
    judged = sum((q for outcome, q in split.items() if outcome != PENDING), Decimal(0))
    registry.note(
        ctx,
        case,
        registry.R.STATUS,
        action,
        f"inspection:{inspection.pk}",
        reference,
        [(unit.item, judged, [unit.serial] if unit.serial else [])],
        _unit_division(case, unit),
        payload,
    )


def _after_stock_change(unit):
    inventory.check_allocations(unit.item)
    inventory.refresh_cases_later([unit.item_id])


# --- Discrepancy (Q10) ---------------------------------------------------------------------------


def resolve_return_discrepancy(ctx, case, payload, reason, target):
    """ส่วนต่างการรับคืน: count it for this case, send it back, or count it after a registry fix."""
    unit = _unit(case, payload)
    if ctx.requester is not None or not _member(ctx, _unit_division(case, unit)):
        fail(ErrorCode.FORBIDDEN, "สมาชิกกองที่รับผิดชอบรายการเท่านั้น")
    if not unit.open_discrepancy:
        fail(ErrorCode.VALIDATION_ERROR, "หน่วยนี้ไม่มีส่วนต่างที่รอผล")
    if unit.arrival.received_by_id == ctx.user.pk:
        fail(ErrorCode.FORBIDDEN, "ผู้รับครั้งนั้นไม่ปิดส่วนต่างของตนเอง (Q10)")
    kind = payload.get("resolution")
    if kind not in U.Resolution.values:
        fail(ErrorCode.VALIDATION_ERROR, "resolution is accepted, sent_back or registry_adjusted.")
    reference = clean_text(payload, "reference", False, 100)
    if kind != U.Resolution.ACCEPTED and not reference:
        fail(ErrorCode.VALIDATION_ERROR, "ระบุเลขเอกสารอ้างอิง")
    duplicate = unit.discrepancy in (U.Discrepancy.DUPLICATE_STORE, U.Discrepancy.DUPLICATE_CASE)
    replaces = clean_text(payload, "replaces", False, 64)
    inventory.lock_items([unit.item_id])
    if kind == U.Resolution.SENT_BACK:
        for bucket, value in (unit.placed or {}).items():
            inventory.move_fake(
                unit.item,
                Decimal(value),
                unit.serial,
                bucket,
                None,
                case,
                ctx.user,
                ctx.label,
                f"ส่งกลับเขต {case.number}",
                out=FakeSerial.Status.ISSUED,
            )
        unit.placed, unit.status = {}, U.Status.SENT_BACK
    else:
        if unit.line_id is None:
            fail(ErrorCode.VALIDATION_ERROR, "รายการนี้ไม่อยู่ในคำขอ ให้ส่งกลับเขตหรือยื่นเรื่องคืนใหม่")
        if duplicate and kind == U.Resolution.ACCEPTED:
            fail(ErrorCode.VALIDATION_ERROR, "เลขซ้ำต้องปรับในทะเบียนก่อน (registry_adjusted)")
        if kind == U.Resolution.REGISTRY_ADJUSTED and duplicate:
            if unit.serial and _still_conflicting(case, unit):
                fail(ErrorCode.VALIDATION_ERROR, "ยังพบเลขนี้ในคลังหรือเรื่องอื่น ปรับข้อมูลก่อน")
            moved = inventory.move_fake(
                unit.item,
                unit.quantity,
                unit.serial,
                None,
                inventory.AWAITING_INSPECTION,
                case,
                ctx.user,
                ctx.label,
                f"รับคืน {case.number} หลังปรับทะเบียน",
            )
            fake = moved and inventory.mode() == inventory.FAKE
            unit.placed = {inventory.AWAITING_INSPECTION: _number(moved)} if fake else {}
        if replaces:
            if unit.serial == "" or replaces not in _expected_serials(unit.line):
                fail(ErrorCode.VALIDATION_ERROR, "เลขที่ถูกแทนต้องเป็นเลขในคำขอที่ยังรออยู่")
            unit.replaces_serial = replaces
    unit.resolution, unit.resolution_reason, unit.resolution_reference = kind, reason, reference
    unit.resolved_by, unit.resolved_at = ctx.user, timezone.now()
    unit.save()
    if reference:  # M11: the registry document that settles this ส่วนต่างการรับคืน
        registry.note(
            ctx,
            case,
            registry.R.ADJUSTMENT,
            "resolve_return_discrepancy",
            f"unit:{unit.pk}",
            reference,
            [(unit.item, unit.quantity, [unit.serial] if unit.serial else [])],
            _unit_division(case, unit),
            payload,
        )
    if unit.counted:
        _count(ctx, case, unit, reason)
    case.status = settle(case, _lines(case))
    return {"unit": unit.seq, "resolution": kind, "reference": reference, "replaces": replaces}


def _still_conflicting(case, unit):
    status = inventory.serial_status(unit.item, unit.serial)
    if status not in (None, FakeSerial.Status.ISSUED, FakeSerial.Status.DISPOSED):
        return True
    return (
        U.objects.filter(item=unit.item, serial=unit.serial)
        .exclude(pk=unit.pk)
        .exclude(status=U.Status.SENT_BACK)
        .exclude(discrepancy__in=[U.Discrepancy.DUPLICATE_CASE, U.Discrepancy.DUPLICATE_STORE])
        .exclude(case__status__in=TERMINAL)
        .exists()
    )


# --- Ending (Q8, Q11, Q13) ----------------------------------------------------------------------


def terminate_return(ctx, case, payload, reason, target):
    """ยุติส่วนค้างส่งคืน by the owner division's supervisor; lost needs a registry document."""
    if ctx.requester is not None:
        fail(ErrorCode.FORBIDDEN, "เฉพาะผู้กำกับงานของกองเจ้าภาพยุติส่วนค้างส่งคืน (Q11)")
    lines = _lines(case)
    raw = payload.get("lines")
    if not isinstance(raw, list) or not raw:
        fail(ErrorCode.VALIDATION_ERROR, "lines lists the line numbers to stop.")
    chosen = fulfilment._rows({"lines": [{"line_no": n} for n in raw]}, lines, ())
    _require_role(
        ctx,
        case,
        Role.SUPERVISOR,
        "เฉพาะผู้กำกับงานของกองเจ้าภาพยุติส่วนค้างส่งคืน (Q11)",
        [line for line, _ in chosen],
    )
    category = payload.get("category")
    if category not in STOP_CATEGORIES:
        fail(ErrorCode.VALIDATION_ERROR, "category is not_returned, lost or other.")
    reference = clean_text(payload, "reference", False, 100)
    if category == "lost" and not reference:
        fail(ErrorCode.VALIDATION_ERROR, "สูญหายต้องมีเลขเอกสารทะเบียน")
    stopped = []
    for line, _ in chosen:
        quantity = line.return_outstanding
        if quantity <= 0:
            fail(ErrorCode.VALIDATION_ERROR, f"รายการ {line.line_no} ไม่มีส่วนค้างส่งคืน")
        line.return_terminated_quantity += quantity
        line.save(update_fields=["return_terminated_quantity"])
        _ledger(ctx, case, line, L.RETURN_TERMINATED, quantity, reason)
        stopped.append({"line_no": line.line_no, "quantity": _number(quantity)})
    case.status = settle(case, lines)
    return {"lines": stopped, "category": category, "reference": reference}


def close(ctx, case, payload, reason, target):
    fail(ErrorCode.VALIDATION_ERROR, "เรื่องคืนปิดอัตโนมัติเมื่อรับคืนครบ (Q11)")


def cancel_guard(case):
    """Q8: once goods arrived they must be inspected; stop the outstanding part instead."""
    if ReturnArrival.objects.filter(case=case).exists():
        fail(ErrorCode.VALIDATION_ERROR, "รับของแล้วยกเลิกไม่ได้ ใช้การยุติส่วนค้างส่งคืน (Q8)")


HANDLERS = {
    "return_instructions": return_instructions,
    "receive": receive,
    "inspect": inspect,
    "decide_inspection": decide_inspection,
    "resolve_return_discrepancy": resolve_return_discrepancy,
    "terminate_return": terminate_return,
}


# --- Visibility, facts and display ---------------------------------------------------------------


def certification_condition(user, now=None):
    """Certifiers see returns with a conflict in their scope (they decide it, Q9)."""
    from operations import policy

    condition = Q(pk__in=[])
    for grant in policy.active_grants(user, A.CONDITION_CERTIFICATION, now):
        units = U.objects.filter(case=OuterRef("pk"), status=U.Status.CONFLICT)
        if grant.category:
            units = units.filter(item__category=grant.category)
        if grant.item_id:
            units = units.filter(item_id=grant.item_id)
        scope = Q(kind=K.RETURN) & ~Q(status__in=TERMINAL) & Q(Exists(units))
        if grant.division_id:
            scope &= Q(owner_division_id=grant.division_id)
        condition |= scope
    return condition


def case_facts(case):
    """เลขเครื่อง/จำนวนไม่ตรง from open return discrepancies (Q3 → R07), as case data."""
    open_units = [u for u in U.objects.filter(case=case) if u.open_discrepancy]
    if not open_units:
        return {}
    evidence = "; ".join(
        f"หน่วย {u.seq} {u.serial or u.item.code}: {u.get_discrepancy_display()}" for u in open_units
    )
    return {"serial_mismatch": {"value": True, "source": "return", "evidence": evidence[:500]}}


def summary(case):
    """Per line: asked / arrived / ready / repair / other / awaiting / stopped / outstanding."""
    units = list(U.objects.filter(case=case).select_related("item", "line"))
    rows, arrived_any, outstanding_any = [], False, False
    for line in case.lines.select_related("item").order_by("line_no"):
        mine = [u for u in units if u.line_id == line.pk and u.counted]
        totals = {READY: ZERO, REPAIR: ZERO, OTHER: ZERO}
        awaiting = ZERO
        for u in mine:
            if u.status == U.Status.FINAL:
                for outcome, value in u.final_split.items():
                    totals[outcome] += Decimal(value)
            else:
                awaiting += u.quantity
        arrived_any |= line.returned_quantity > 0
        outstanding_any |= line.return_outstanding > 0
        rows.append(
            {
                "line_no": line.line_no,
                "item": line.item.code,
                "name": line.item.name,
                "unit": line.item.unit,
                "requested": _number(line.requested_quantity),
                "requested_serials": line.serials,
                "arrived": _number(line.returned_quantity),
                "ready": _number(totals[READY]),
                "repair": _number(totals[REPAIR]),
                "other": _number(totals[OTHER]),
                "awaiting": _number(awaiting),
                "terminated": _number(line.return_terminated_quantity),
                "outstanding": _number(line.return_outstanding),
            }
        )
    return {
        "lines": rows,
        "units": [
            {
                "unit": u.seq,
                "arrival": u.arrival.seq,
                "item": u.item.code,
                "serial": u.serial,
                "quantity": _number(u.quantity),
                "status": u.status,
                "status_label": u.get_status_display(),
                "discrepancy": u.get_discrepancy_display() if u.discrepancy else "",
                "resolution": u.get_resolution_display() if u.resolution else "",
                "source": _source(u),
                "result": u.final_split,
                "flag": u.conflict_note,
            }
            for u in units
        ],
        "instructions": latest_instructions(case),
        "no_source_document": not case.source_reference,
        "partially_returned": arrived_any and outstanding_any,
    }


def _source(unit):
    """Q4: where this serial came from when this system issued it (never guessed)."""
    if unit.issued_serial_id is None:
        return ""
    delivery = unit.issued_serial.delivery_line.delivery
    return f"{delivery.case.number} ส่งมอบครั้งที่ {delivery.seq}"


def status_text(case):
    data = summary(case)
    rows = []
    instructions = data["instructions"]
    if instructions and case.status not in TERMINAL:
        when = " ".join(x for x in (instructions["from_date"], instructions["to_date"]) if x)
        rows.append(f"ส่งคืนที่: {instructions['place']}" + (f" ({when})" if when else ""))
    for line in data["lines"]:
        rows.append(
            f"{line['line_no']}. {line['name']}: ขอคืน {line['requested']} มาถึง {line['arrived']}"
            f" พร้อมใช้ {line['ready']} ซ่อม {line['repair']} อื่น {line['other']}"
            f" รอตรวจ {line['awaiting']} ยุติ {line['terminated']} ค้างส่ง {line['outstanding']}"
            f" {line['unit']}"
        )
    pending = [u for u in data["units"] if u["discrepancy"] and not u["resolution"]]
    if pending:
        rows.append(f"ของที่ไม่ตรงคำขอ {len(pending)} หน่วย เจ้าหน้าที่กำลังตรวจสอบ")
    if data["partially_returned"]:
        rows.insert(0, "รับคืนบางส่วน ยังมีส่วนค้างส่งคืน")
    return "\n".join(rows)
