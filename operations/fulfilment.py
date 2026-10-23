"""M07 withdrawal fulfilment: approval, allocation, issue, receipt and closing (M07 Q1–Q23).

Handlers run inside services.perform_action: the case row is locked, the matrix and revision are
checked, and the Decision, audit, outbox event and receipt are recorded there. Counters live on
CaseLine with one ledger row per change; the database refuses issuing more than was approved.
Lock order: case → its lines → items ascending (inventory.lock_items). Authority comes from
AuthorityGrant only (Q2): a role is a duty, never an approval. Partial states ("จ่ายบางส่วน")
are labels derived from the counters, not statuses (E.5).
"""

import re
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from operations import coordination, identity, inventory, policy, registry
from operations.contracts import (
    MAX_LINES,
    STAFF_CHANNELS,
    ErrorCode,
    check_quantity_for_item,
    clean_serials,
    clean_text,
    fail,
    parse_quantity,
)
from operations.models import (
    Allocation,
    AuthorityGrant,
    Case,
    CaseLine,
    Decision,
    Delivery,
    DeliveryLine,
    FulfilmentEntry,
    IssuedSerial,
    Receipt,
    Role,
)

A = AuthorityGrant.Activity
S = Case.Status
K = Case.Kind
L = FulfilmentEntry.Kind
ZERO = Decimal("0")
ZERO_TEXT = re.compile(r"^0+(\.0+)?$")
TERMINAL = frozenset({S.CLOSED, S.REJECTED, S.CANCELLED})
# Decisions that keep a case readable by the authority holder who made them (Q12).
AUTHORITY_ACTIONS = (
    "approve",
    "deny_approval",
    "allocate",
    "terminate_outstanding",
    "cancel",
)
CANCEL_RESOLUTIONS = {
    "keep": "หน่วยงาน (เขต/ฝ่าย)เก็บไว้ใช้",
    "will_return": "หน่วยงาน (เขต/ฝ่าย)จะส่งคืน (ยื่นเรื่องคืนอ้างเรื่องนี้เอง)",
    "adjusted": "ปรับใน SAP แล้ว",
}


# --- Shared helpers --------------------------------------------------------------------------


def _lines(case):
    """The case lines, locked after the case row (already locked by perform_action)."""
    return list(
        CaseLine.objects.select_for_update(of=("self",))
        .select_related("item")
        .filter(case=case)
        .order_by("line_no")
    )


def _rows(payload, lines, fields, name="lines"):
    """[{line_no, ...}] referring to lines of this case, each line at most once."""
    raw = payload.get(name)
    if not isinstance(raw, list) or not raw or len(raw) > MAX_LINES:
        fail(ErrorCode.VALIDATION_ERROR, f"{name} must list the lines (line_no) to act on.")
    by_no = {line.line_no: line for line in lines}
    seen, rows = set(), []
    for row in raw:
        if not isinstance(row, dict) or set(row) - ({"line_no"} | set(fields)):
            fail(ErrorCode.VALIDATION_ERROR, f"Each line has line_no and {', '.join(fields)}.")
        number = row.get("line_no")
        if isinstance(number, bool) or not isinstance(number, int) or number not in by_no:
            fail(ErrorCode.VALIDATION_ERROR, "Unknown line_no for this case.")
        if number in seen:
            fail(ErrorCode.VALIDATION_ERROR, "List each line once.")
        seen.add(number)
        rows.append((by_no[number], row))
    return rows


def _quantity(raw, item, allow_zero=False):
    if allow_zero and (raw == 0 or (isinstance(raw, str) and ZERO_TEXT.match(raw.strip()))):
        return ZERO
    return check_quantity_for_item(parse_quantity(raw), item)


def _ledger(ctx, case, line, kind, quantity, reason="", serials=(), delivery=None):
    FulfilmentEntry.objects.create(
        case=case,
        line=line,
        kind=kind,
        quantity=quantity,
        serials=list(serials),
        delivery=delivery,
        actor=ctx.user,
        actor_label=ctx.label,
        case_revision=case.revision + 1,  # the revision of the Decision recorded with it
        reason=reason,
    )


def _member(ctx, division_id, roles=None):
    return identity.holds(ctx.scope, division_id, roles)


def covering_grant(user, activity, case, line, quantity=None, now=None):
    """An active grant whose division/category/item scope (and quantity cap) covers the line."""
    for grant in policy.active_grants(user, activity, now).select_related("item"):
        # M09 Q11: R09 lines are judged in the division of their accepted child work.
        if grant.division_id not in (None, coordination.division_of(line)):
            continue
        if grant.category and grant.category != line.item.category:
            continue
        if grant.item_id and grant.item_id != line.item_id:
            continue
        if (
            quantity is not None
            and grant.max_quantity is not None
            and quantity > grant.max_quantity
        ):
            continue
        return grant
    return None


def _require_grant(ctx, activity, case, line, quantity=None):
    if ctx.requester is not None:
        fail(ErrorCode.FORBIDDEN, "Requesters do not decide withdrawals.")
    grant = covering_grant(ctx.user, activity, case, line, quantity)
    if grant is None:
        label = A(activity).label
        extra = f" สำหรับจำนวน {quantity}" if quantity is not None else ""
        fail(ErrorCode.FORBIDDEN, f"ไม่มีอำนาจ{label}ที่ครอบรายการ {line.line_no}{extra}")
    return grant


def _not_filer(ctx, case):
    # Separation of duties binds everyone but ผู้ช่วยเหลือระบบ (ADR 0007).
    if ctx.user is not None and identity.separated(ctx) and case.submitted_by_id == ctx.user.pk:
        fail(ErrorCode.FORBIDDEN, "ผู้บันทึกเรื่องแทนผู้แจ้งไม่อนุมัติเรื่องนั้นเอง (Q4)")


def approved(case):
    """After approval (Q7): at least one line was approved above zero."""
    return CaseLine.objects.filter(case=case, approved_quantity__gt=0).exists()


def settle(case, lines):
    """The lifecycle status the counters lead to (Q19): outstanding work, waiting receipts, or
    closed when nothing is outstanding and every delivery is settled."""
    deliveries = list(Delivery.objects.filter(case=case))
    if any(line.outstanding > 0 for line in lines):
        return S.IN_PROGRESS if deliveries else S.AWAITING_EXECUTION
    if all(d.settled for d in deliveries) and not coordination.blocks_closing(case):
        return S.CLOSED
    # Open child work (M09 Q6) keeps it open; nothing is left for the district if all settled.
    return S.AWAITING_CONFIRMATION if deliveries else S.AWAITING_EXECUTION


def _delivery(case, payload):
    seq = payload.get("delivery")
    if isinstance(seq, bool) or not isinstance(seq, int):
        fail(ErrorCode.VALIDATION_ERROR, "delivery (its number in this case) is required.")
    delivery = Delivery.objects.select_for_update().filter(case=case, seq=seq).first()
    if delivery is None:
        fail(ErrorCode.VALIDATION_ERROR, "Unknown delivery for this case.")
    return delivery


def _end_allocations(lines, reason):
    Allocation.objects.filter(line__in=lines, ended_at__isnull=True).update(
        ended_at=timezone.now(), end_reason=reason
    )


# --- Approval (Q2, Q3, Q4, Q13) --------------------------------------------------------------


def approve(ctx, case, payload, reason, target):
    """Decide lines: 0 ≤ approved ≤ requested, final once given; less than asked needs a reason."""
    _not_filer(ctx, case)
    lines = _lines(case)
    now = timezone.now()
    decided = []
    for line, row in _rows(payload, lines, ("quantity", "reason")):
        if line.approved_quantity is not None:
            fail(
                ErrorCode.VALIDATION_ERROR, f"รายการ {line.line_no} ตัดสินแล้ว การอนุมัติเป็นค่าสุดท้าย (Q13)"
            )
        quantity = _quantity(row.get("quantity"), line.item, allow_zero=True)
        if quantity > line.requested_quantity:
            fail(ErrorCode.VALIDATION_ERROR, "อนุมัติเกินจำนวนที่ขอไม่ได้ ต้องการเพิ่มให้ยื่นเรื่องใหม่ (Q13)")
        line_reason = clean_text(row, "reason", False) or reason
        grant = _require_grant(ctx, A.WITHDRAWAL_APPROVAL, case, line, quantity)
        line.approved_quantity, line.approval_reason = quantity, line_reason
        line.approved_by, line.approved_at = ctx.user, now
        line.save(
            update_fields=["approved_quantity", "approval_reason", "approved_by", "approved_at"]
        )
        _ledger(ctx, case, line, L.APPROVED, quantity, line_reason)
        decided.append(
            {
                "line_no": line.line_no,
                "requested": str(line.requested_quantity),
                "approved": str(quantity),
                "grant_id": grant.pk,
            }
        )
    undecided = [line.line_no for line in lines if line.approved_quantity is None]
    if not undecided:
        case.status = (
            S.AWAITING_EXECUTION if any(x.approved_quantity > 0 for x in lines) else S.REJECTED
        )
    return {"lines": decided, "undecided": undecided}


def deny_approval(ctx, case, payload, reason, target):
    """Not approved at all: every undecided line becomes 0 with the reason (Q3)."""
    _not_filer(ctx, case)
    lines = _lines(case)
    if any((line.approved_quantity or ZERO) > 0 for line in lines):
        fail(ErrorCode.VALIDATION_ERROR, "มีรายการที่อนุมัติแล้ว ให้ตัดสินรายการที่เหลือด้วยจำนวน 0 แทน")
    now = timezone.now()
    denied = []
    for line in lines:
        if line.approved_quantity is not None:
            continue
        _require_grant(ctx, A.WITHDRAWAL_APPROVAL, case, line, ZERO)
        line.approved_quantity, line.approval_reason = ZERO, reason
        line.approved_by, line.approved_at = ctx.user, now
        line.save(
            update_fields=["approved_quantity", "approval_reason", "approved_by", "approved_at"]
        )
        _ledger(ctx, case, line, L.APPROVED, ZERO, reason)
        denied.append(line.line_no)
    case.status = target
    return {"lines": denied}


# --- Allocation (Q8, Q20) ---------------------------------------------------------------------


def allocate(ctx, case, payload, reason, target):
    """การจัดสรร: how much of short stock this case may get; replaces its earlier allocation."""
    lines = _lines(case)
    rows = _rows(payload, lines, ("quantity",))
    inventory.lock_items(line.item_id for line, _ in rows)
    result = []
    for line, row in rows:
        quantity = _quantity(row.get("quantity"), line.item, allow_zero=True)
        grant = _require_grant(ctx, A.ALLOCATION, case, line)
        if quantity > line.outstanding:
            fail(ErrorCode.VALIDATION_ERROR, f"จัดสรรรายการ {line.line_no} ได้ไม่เกินส่วนค้างจ่าย")
        if inventory.contended(line.item) is not True:
            fail(
                ErrorCode.VALIDATION_ERROR,
                f"{line.item.code} ไม่ได้แย่งกันหรือไม่ทราบยอด การจัดสรรใช้เฉพาะเมื่อของไม่พอ (Q8/Q20)",
            )
        available = inventory.availability(line.item).available
        if inventory.active_allocated(line.item, exclude_line=line) + quantity > available:
            fail(
                ErrorCode.VALIDATION_ERROR,
                f"การจัดสรร {line.item.code} รวมเกินยอดพร้อมจ่าย {available}",
            )
        _end_allocations([line], Allocation.End.REPLACED)
        if quantity > 0:
            Allocation.objects.create(
                case=case,
                line=line,
                item=line.item,
                quantity=quantity,
                reason=reason,
                allocated_by=ctx.user,
                grant=grant,
            )
        result.append(
            {
                "line_no": line.line_no,
                "quantity": str(quantity),
                "available": str(available),
                "grant_id": grant.pk,
            }
        )
    return {"allocations": result}


# --- Issue (Q5, Q10, Q11) ---------------------------------------------------------------------


def _issue_plan(ctx, lines, payload):
    plan = []
    for line, row in _rows(payload, lines, ("quantity", "serials")):
        quantity = _quantity(row.get("quantity"), line.item)
        if line.approved_quantity is None or quantity > line.outstanding:
            fail(ErrorCode.VALIDATION_ERROR, f"รายการ {line.line_no} จ่ายเกินส่วนค้างจ่ายตามอนุมัติไม่ได้")
        if identity.separated(ctx) and line.approved_by_id == ctx.user.pk:
            fail(ErrorCode.FORBIDDEN, "ผู้อนุมัติรายการไม่บันทึกจ่ายรายการนั้นเอง (Q4)")
        serials = clean_serials(row.get("serials"))
        if line.item.is_serialized:
            if Decimal(len(serials)) != quantity:
                fail(ErrorCode.VALIDATION_ERROR, f"{line.item.code} ต้องระบุเลขเครื่องครบตามจำนวน")
        elif serials:
            fail(ErrorCode.VALIDATION_ERROR, f"{line.item.code} ไม่ได้ควบคุมรายชิ้น")
        plan.append((line, quantity, serials))
    return plan


def _serials_out(item, serials):
    out = list(
        IssuedSerial.objects.filter(item=item, serial__in=serials, active=True).values_list(
            "serial", flat=True
        )
    )
    if out:
        fail(ErrorCode.VALIDATION_ERROR, f"เลขเครื่อง {', '.join(out)} ถูกจ่ายออกไปแล้วและยังไม่มีบันทึกรับคืน")


def _record_serials(item, serials, delivery_line):
    _serials_out(item, serials)
    try:
        with transaction.atomic():
            IssuedSerial.objects.bulk_create(
                IssuedSerial(item=item, serial=s, delivery_line=delivery_line) for s in serials
            )
    except IntegrityError:
        fail(ErrorCode.VALIDATION_ERROR, "เลขเครื่องถูกจ่ายออกไปแล้ว")


def issue(ctx, case, payload, reason, target):
    """การส่งมอบ: goods that left the store; within outstanding approval, allocation and stock."""
    if ctx.requester is not None:
        fail(ErrorCode.FORBIDDEN, "เฉพาะผู้จ่ายของกองเจ้าภาพบันทึกการจ่าย (Q5)")
    recipient = clean_text(payload, "recipient_name", True, 200)
    document = clean_text(payload, "document_number", False, 64)
    mode = inventory.mode()
    if mode == inventory.MANUAL and not document:
        fail(ErrorCode.VALIDATION_ERROR, "ระบุเลขเอกสารจ่าย SAP")
    if mode == inventory.FAKE and document:
        fail(ErrorCode.VALIDATION_ERROR, "คลังจำลองออกเลขเอกสารจ่ายเอง")
    lines = _lines(case)
    plan = _issue_plan(ctx, lines, payload)
    for line, _, _ in plan:  # Q5; M09 Q11: an R09 line's issuer is in its child division
        if not _member(ctx, coordination.division_of(line), [Role.ISSUER]):
            fail(ErrorCode.FORBIDDEN, "เฉพาะผู้จ่ายของกองที่รับผิดชอบรายการบันทึกการจ่าย (Q5)")
    inventory.lock_items(line.item_id for line, _, _ in plan)
    now = timezone.now()
    allocations = {}
    for line, quantity, _ in plan:
        if inventory.contended(line.item, now):
            allocation = (
                Allocation.objects.select_for_update()
                .filter(line=line, ended_at__isnull=True)
                .first()
            )
            if allocation is None or allocation.remaining < quantity:
                fail(
                    ErrorCode.VALIDATION_ERROR,
                    f"{line.item.code} มีไม่พอสำหรับทุกเรื่อง จ่ายได้ตามที่ผู้มีอำนาจจัดสรรให้เรื่องนี้เท่านั้น (Q8)",
                )
            if identity.separated(ctx) and allocation.allocated_by_id == ctx.user.pk:
                fail(ErrorCode.FORBIDDEN, "ผู้จัดสรรไม่บันทึกจ่ายจากการจัดสรรของตนเอง (Q20)")
            allocations[line.pk] = allocation
    if mode == inventory.FAKE:
        document = inventory.next_document_number(now)
    seq = Delivery.objects.filter(case=case).count() + 1
    delivery = Delivery.objects.create(
        case=case,
        seq=seq,
        mode=mode,
        document_number=document,
        recipient_name=recipient,
        note=reason,
        issued_by=ctx.user,
        issued_at=now,
    )
    flags, issued = [], []
    for line, quantity, serials in plan:
        if mode == inventory.FAKE:
            try:
                inventory.take_fake(
                    line.item, quantity, serials, case, ctx.user, ctx.label, document
                )
            except inventory.InventoryError as exc:
                fail(ErrorCode.VALIDATION_ERROR, str(exc))
        else:
            figures = inventory.availability(line.item, now)
            if figures.available is not None and quantity > figures.available:
                flags.append(f"{line.item.code} ยอดนำเข้า {figures.available} < จ่าย {quantity}")
        delivery_line = DeliveryLine.objects.create(
            delivery=delivery, line=line, quantity=quantity, serials=serials
        )
        if serials:
            _record_serials(line.item, serials, delivery_line)
        line.issued_quantity += quantity
        line.save(update_fields=["issued_quantity"])
        allocation = allocations.get(line.pk)
        if allocation is not None:
            allocation.used_quantity += quantity
            if allocation.remaining == 0:
                allocation.ended_at, allocation.end_reason = now, Allocation.End.USED
            allocation.save(update_fields=["used_quantity", "ended_at", "end_reason"])
        _ledger(ctx, case, line, L.ISSUED, quantity, reason, serials, delivery)
        issued.append({"line_no": line.line_no, "quantity": str(quantity), "serials": serials})
    if flags:
        delivery.stock_flag = "; ".join(flags)[:200]
        delivery.save(update_fields=["stock_flag"])
    registry.note(  # M11: the SAP issue document behind this delivery, with its evidence
        ctx,
        case,
        registry.R.ISSUE,
        "issue",
        f"delivery:{delivery.pk}",
        document,
        [(line.item, quantity, serials) for line, quantity, serials in plan],
        coordination.division_of(plan[0][0]),
        payload,
    )
    items = {line.item for line, _, _ in plan}
    for item in items:
        inventory.check_allocations(item, now)
    _end_allocations([line for line in lines if line.outstanding <= 0], Allocation.End.CASE_ENDED)
    inventory.refresh_cases_later(item.pk for item in items)
    case.status = settle(case, lines)
    return {
        "delivery": seq,
        "document_number": document,
        "mode": mode,
        "lines": issued,
        "stock_flag": delivery.stock_flag,
    }


# --- Receipt (Q4, Q6, Q15, Q18) ---------------------------------------------------------------


def _effective(delivery):
    """What each line of the delivery stands at now: recorded figures plus corrections."""
    figures = {}
    for dl in delivery.lines.select_related("line__item"):
        corrected = sum(
            (
                e.quantity
                for e in FulfilmentEntry.objects.filter(
                    delivery=delivery, line=dl.line, kind=L.CORRECTED
                )
            ),
            ZERO,
        )
        serials = sorted(
            IssuedSerial.objects.filter(delivery_line=dl, active=True).values_list(
                "serial", flat=True
            )
        )
        figures[dl.line_id] = (dl, dl.quantity + corrected, serials)
    return figures


def _received(delivery):
    """{line_id: quantity} of the latest receipt for the delivery (empty before any)."""
    latest = delivery.receipts.order_by("-id").first()
    if latest is None:
        return {}
    by_no = {dl.line.line_no: dl.line_id for dl in delivery.lines.select_related("line")}
    return {by_no[row["line_no"]]: Decimal(row["quantity"]) for row in latest.lines}


def _receipt_lines(payload, delivery, figures, full):
    if full:
        return [
            {"line_no": dl.line.line_no, "quantity": _number(quantity), "serials": serials}
            for dl, quantity, serials in figures.values()
        ]
    raw = payload.get("lines")
    by_no = {dl.line.line_no: (dl, q, s) for dl, q, s in figures.values()}
    if not isinstance(raw, list) or {r.get("line_no") for r in raw if isinstance(r, dict)} != set(
        by_no
    ):
        fail(ErrorCode.VALIDATION_ERROR, "ระบุจำนวนที่ได้รับจริงของทุกรายการในการส่งมอบนี้")
    rows = []
    for row in raw:
        if set(row) - {"line_no", "quantity", "serials"}:
            fail(ErrorCode.VALIDATION_ERROR, "Each line has line_no, quantity and serials.")
        dl, _, _ = by_no[row["line_no"]]
        item = dl.line.item
        serials = clean_serials(row.get("serials"))
        if item.is_serialized:
            quantity = Decimal(len(serials))
        else:
            if serials:
                fail(ErrorCode.VALIDATION_ERROR, f"{item.code} ไม่ได้ควบคุมรายชิ้น")
            quantity = _quantity(row.get("quantity"), item, allow_zero=True)
        rows.append({"line_no": row["line_no"], "quantity": _number(quantity), "serials": serials})
    return sorted(rows, key=lambda r: r["line_no"])


def confirm(ctx, case, payload, reason, target):
    """การยืนยันรับของ for one delivery: "full" or what really arrived ("mismatch")."""
    if case.kind != K.WITHDRAWAL:
        fail(ErrorCode.ACTION_NOT_ENABLED, "confirm for this kind arrives with M09.")
    delivery = _delivery(case, payload)
    result = payload.get("result")
    if result not in {Receipt.Result.FULL, Receipt.Result.MISMATCH}:
        fail(ErrorCode.VALIDATION_ERROR, "result is full or mismatch.")
    channel, note, requester, recorded_by = Case.Channel.LINE, "", ctx.requester, None
    if ctx.requester is None:
        # Intake staff, or (ADR 0008, no intake) the owner division's staff.
        intake = identity.intake_division()
        desk = intake.pk if intake else case.owner_division_id
        if not _member(ctx, desk):
            fail(
                ErrorCode.FORBIDDEN,
                "เจ้าหน้าที่กองรับเรื่องกลางบันทึกการยืนยันรับแทนผู้แจ้งได้ (Q4)"
                if intake
                else "เจ้าหน้าที่กองเจ้าภาพบันทึกการยืนยันรับแทนผู้แจ้งได้",
            )
        channel = payload.get("channel")
        if channel not in STAFF_CHANNELS:
            fail(ErrorCode.VALIDATION_ERROR, "channel must be phone, letter or counter.")
        note = clean_text(payload, "intake_note", True)
        recorded_by = ctx.user
        if identity.separated(ctx) and delivery.issued_by_id == ctx.user.pk:
            fail(ErrorCode.FORBIDDEN, "ผู้จ่ายไม่ยืนยันรับการส่งมอบของตนเอง (Q4)")
    if delivery.status not in {Delivery.Status.AWAITING, Delivery.Status.MISMATCH}:
        fail(ErrorCode.VALIDATION_ERROR, "การส่งมอบนี้ยืนยันหรือปิดแล้ว")
    figures = _effective(delivery)
    rows = _receipt_lines(payload, delivery, figures, result == Receipt.Result.FULL)
    expected = {
        dl.line.line_no: (quantity, sorted(serials)) for dl, quantity, serials in figures.values()
    }
    matches = all(
        (Decimal(r["quantity"]), sorted(r["serials"])) == expected[r["line_no"]] for r in rows
    )
    result = Receipt.Result.FULL if matches else Receipt.Result.MISMATCH
    previous = _received(delivery)
    Receipt.objects.create(
        delivery=delivery,
        result=result,
        lines=rows,
        requester=requester,
        recorded_by=recorded_by,
        channel=channel,
        note=note,
    )
    lines = _lines(case)
    by_no = {line.line_no: line for line in lines}
    for row in rows:
        line = by_no[row["line_no"]]
        delta = Decimal(row["quantity"]) - previous.get(line.pk, ZERO)
        if delta:
            line.received_quantity += delta
            line.save(update_fields=["received_quantity"])
            _ledger(ctx, case, line, L.RECEIVED, delta, note, row["serials"], delivery)
    delivery.status = (
        Delivery.Status.CONFIRMED if result == Receipt.Result.FULL else Delivery.Status.MISMATCH
    )
    delivery.save(update_fields=["status"])
    if case.status not in TERMINAL:
        case.status = settle(case, lines)
    return {"delivery": delivery.seq, "result": result, "lines": rows}


# --- Discrepancy (Q18) -------------------------------------------------------------------------


def resolve_discrepancy(ctx, case, payload, reason, target):
    """ส่วนต่างการส่งมอบ: an issue recording error (append-only correction) or goods lost."""
    if ctx.requester is not None:
        fail(ErrorCode.FORBIDDEN, "สมาชิกกองเจ้าภาพเป็นผู้บันทึกผลส่วนต่าง")
    delivery = _delivery(case, payload)
    for dl in delivery.lines.select_related("line__case"):
        if not _member(ctx, coordination.division_of(dl.line)):
            fail(ErrorCode.FORBIDDEN, "สมาชิกกองที่รับผิดชอบรายการเป็นผู้บันทึกผลส่วนต่าง")
    if delivery.status != Delivery.Status.MISMATCH:
        fail(ErrorCode.VALIDATION_ERROR, "การส่งมอบนี้ไม่มีส่วนต่างที่รอผล")
    if identity.separated(ctx) and delivery.issued_by_id == ctx.user.pk:
        fail(ErrorCode.FORBIDDEN, "ผู้จ่ายของการส่งมอบนี้ไม่ปิดส่วนต่างของตนเอง (Q18)")
    kind = payload.get("resolution")
    if kind not in Delivery.Resolution.values:
        fail(ErrorCode.VALIDATION_ERROR, "resolution is issue_error or lost.")
    reference = clean_text(payload, "reference", False, 100)
    needs_reference = kind == Delivery.Resolution.LOST or inventory.mode() == inventory.MANUAL
    if needs_reference and not reference:
        fail(ErrorCode.VALIDATION_ERROR, "ระบุเลขเอกสาร SAP")
    lines = _lines(case)
    corrections = []
    if kind == Delivery.Resolution.ISSUE_ERROR:
        figures = _effective(delivery)
        by_no = {dl.line.line_no: (dl, q, s) for dl, q, s in figures.values()}
        for line, row in _rows(payload, lines, ("quantity", "serials")):
            if line.line_no not in by_no:
                fail(ErrorCode.VALIDATION_ERROR, "แก้ได้เฉพาะรายการในการส่งมอบนี้")
            dl, current, current_serials = by_no[line.line_no]
            serials = clean_serials(row.get("serials"))
            if line.item.is_serialized:
                quantity = Decimal(len(serials))
            else:
                if serials:
                    fail(ErrorCode.VALIDATION_ERROR, f"{line.item.code} ไม่ได้ควบคุมรายชิ้น")
                quantity = _quantity(row.get("quantity"), line.item, allow_zero=True)
            delta = quantity - current
            if delta > line.outstanding:
                fail(ErrorCode.VALIDATION_ERROR, "แก้แล้วเกินจำนวนอนุมัติ")
            removed = sorted(set(current_serials) - set(serials))
            added = sorted(set(serials) - set(current_serials))
            IssuedSerial.objects.filter(delivery_line=dl, serial__in=removed, active=True).update(
                active=False, ended_at=timezone.now(), end_reason="แก้รายการจ่าย"
            )
            if added:
                _record_serials(line.item, added, dl)
            if delta == 0 and not removed and not added:
                continue
            line.issued_quantity += delta
            line.save(update_fields=["issued_quantity"])
            _ledger(ctx, case, line, L.CORRECTED, delta, reason, serials, delivery)
            corrections.append(
                {
                    "line_no": line.line_no,
                    "from": str(current),
                    "to": str(quantity),
                    "serials": serials,
                }
            )
        if not corrections:
            fail(ErrorCode.VALIDATION_ERROR, "ไม่มีตัวเลขที่เปลี่ยน")
    delivery.status, delivery.resolution = Delivery.Status.RESOLVED, kind
    delivery.resolution_reason, delivery.resolution_reference = reason, reference
    delivery.resolved_by, delivery.resolved_at = ctx.user, timezone.now()
    delivery.save()
    if reference:  # M11: the registry document that settles this ส่วนต่างการส่งมอบ
        by_no = {line.line_no: line for line in lines}
        changed = corrections or [
            {"line_no": dl.line.line_no, "to": str(dl.quantity), "serials": dl.serials}
            for dl in delivery.lines.select_related("line__item")
        ]
        registry.note(
            ctx,
            case,
            registry.R.ADJUSTMENT,
            "resolve_discrepancy",
            f"delivery_resolution:{delivery.pk}",
            reference,
            [(by_no[c["line_no"]].item, Decimal(c["to"]), c.get("serials")) for c in changed],
            coordination.division_of(by_no[changed[0]["line_no"]]) if changed else None,
            payload,
        )
    if case.status not in TERMINAL:
        case.status = settle(case, lines)
    return {
        "delivery": delivery.seq,
        "resolution": kind,
        "reference": reference,
        "corrections": corrections,
    }


# --- Ending outstanding work (Q7, Q19) --------------------------------------------------------


def _terminate(ctx, case, lines, reason):
    terminated = []
    for line in lines:
        quantity = line.outstanding
        if quantity <= 0:
            continue
        line.terminated_quantity += quantity
        line.save(update_fields=["terminated_quantity"])
        _ledger(ctx, case, line, L.TERMINATED, quantity, reason)
        terminated.append({"line_no": line.line_no, "quantity": str(quantity)})
    _end_allocations(lines, Allocation.End.CASE_ENDED)
    return terminated


def terminate_outstanding(ctx, case, payload, reason, target):
    """การยุติส่วนค้าง by approval authority; with nothing ever issued the case closes (Q19)."""
    lines = _lines(case)
    raw = payload.get("lines")
    if not isinstance(raw, list) or not raw:
        fail(ErrorCode.VALIDATION_ERROR, "lines lists the line numbers to stop.")
    chosen = _rows({"lines": [{"line_no": n} for n in raw]}, lines, ())
    for line, _ in chosen:
        if line.outstanding <= 0:
            fail(ErrorCode.VALIDATION_ERROR, f"รายการ {line.line_no} ไม่มีส่วนค้างจ่าย")
        _require_grant(ctx, A.WITHDRAWAL_APPROVAL, case, line)
    terminated = _terminate(ctx, case, [line for line, _ in chosen], reason)
    case.status = settle(case, lines)
    data = {"lines": terminated}
    if case.status == S.CLOSED and not Delivery.objects.filter(case=case).exists():
        data["outcome"] = "ยุติโดยไม่มีการจ่าย"
    return data


def cancel_after_approval(ctx, case, payload, reason):
    """Q7: only approval authority covering every line; goods already out need a resolution;
    stock is never put back here (ADR 0003)."""
    lines = _lines(case)
    for line in lines:
        _require_grant(ctx, A.WITHDRAWAL_APPROVAL, case, line)
    resolution = payload.get("resolution") or ""
    reference = clean_text(payload, "reference", False, 100)
    if Delivery.objects.filter(case=case).exists():
        if resolution not in CANCEL_RESOLUTIONS:
            fail(
                ErrorCode.VALIDATION_ERROR, "เลือกการจัดการของที่จ่ายแล้ว: keep, will_return หรือ adjusted"
            )
        if resolution == "adjusted" and not reference:
            fail(ErrorCode.VALIDATION_ERROR, "ระบุเลขเอกสารการปรับใน SAP")
    elif resolution:
        fail(ErrorCode.VALIDATION_ERROR, "ยังไม่มีการจ่าย ไม่ต้องระบุการจัดการของ")
    terminated = _terminate(ctx, case, lines, reason)
    return {
        "resolution": resolution,
        "resolution_label": CANCEL_RESOLUTIONS.get(resolution, ""),
        "reference": reference,
        "terminated": terminated,
    }


def close(ctx, case, payload, reason, target):
    """Owner division closes when the requester has not answered for 7 days (Q6)."""
    if case.kind != K.WITHDRAWAL:
        fail(ErrorCode.ACTION_NOT_ENABLED, "close for this kind arrives with M08/M09.")
    if ctx.requester is not None or not _member(ctx, case.owner_division_id):
        fail(ErrorCode.FORBIDDEN, "สมาชิกกองเจ้าภาพเป็นผู้ปิดเรื่อง")
    reference = clean_text(payload, "reference", True, 100)
    lines = _lines(case)
    if any(line.outstanding > 0 for line in lines):
        fail(ErrorCode.VALIDATION_ERROR, "ยังมีส่วนค้างจ่าย")
    deliveries = list(Delivery.objects.select_for_update().filter(case=case))
    if any(d.status == Delivery.Status.MISMATCH for d in deliveries):
        fail(ErrorCode.VALIDATION_ERROR, "มีการส่งมอบที่แจ้งไม่ตรง บันทึกผลส่วนต่างก่อน (Q18)")
    waiting = [d for d in deliveries if d.status == Delivery.Status.AWAITING]
    cutoff = timezone.now() - timedelta(days=settings.RECEIPT_NO_RESPONSE_DAYS)
    if any(d.issued_at > cutoff for d in waiting):
        fail(
            ErrorCode.VALIDATION_ERROR,
            f"รอผู้แจ้งยืนยันรับครบ {settings.RECEIPT_NO_RESPONSE_DAYS} วันก่อนปิดโดยไม่มีการยืนยัน",
        )
    for d in waiting:
        d.status = Delivery.Status.UNCONFIRMED
        d.save(update_fields=["status"])
    case.status = target
    return {"unconfirmed_deliveries": [d.seq for d in waiting], "reference": reference}


HANDLERS = {
    "approve": approve,
    "deny_approval": deny_approval,
    "allocate": allocate,
    "issue": issue,
    "confirm": confirm,
    "resolve_discrepancy": resolve_discrepancy,
    "terminate_outstanding": terminate_outstanding,
    "close": close,
}


# --- Visibility (Q12) and facts for the rules (Q6, Q9) -----------------------------------------


def authority_condition(user, now=None):
    """Approval and allocation authority holders see the withdrawal cases waiting for them in
    their scope, and keep read access to the cases they decided."""
    decided = Q(
        Exists(
            Decision.objects.filter(case=OuterRef("pk"), actor=user, action__in=AUTHORITY_ACTIONS)
        )
    )
    return decided | authority_waiting_condition(user, now)


def authority_waiting_condition(user, now=None, strict=False):
    """Withdrawals in this user's approval/allocation scope. `strict` (the M10 "รอฉันตัดสิน"
    tab) keeps only what waits for a decision: undecided lines for approval holders, outstanding
    lines for allocation holders; otherwise approvers also see outstanding lines (to stop them)."""
    condition = Q(pk__in=[])
    grants = list(policy.active_grants(user, A.WITHDRAWAL_APPROVAL, now)) + list(
        policy.active_grants(user, A.ALLOCATION, now)
    )
    for grant in grants:
        lines = CaseLine.objects.filter(case=OuterRef("pk"))
        if grant.category:
            lines = lines.filter(item__category=grant.category)
        if grant.item_id:
            lines = lines.filter(item_id=grant.item_id)
        outstanding = Exists(
            lines.filter(approved_quantity__gt=F("issued_quantity") + F("terminated_quantity"))
        )
        waiting = Q(pk__in=[])
        if not strict or grant.activity == A.ALLOCATION:
            waiting |= Q(outstanding)
        if grant.activity == A.WITHDRAWAL_APPROVAL:
            waiting |= Q(status=S.AWAITING_APPROVAL) & Q(
                Exists(lines.filter(approved_quantity__isnull=True))
            )
        scope = Q(kind=K.WITHDRAWAL) & ~Q(status__in=TERMINAL) & waiting
        if grant.division_id:
            scope &= Q(owner_division_id=grant.division_id)
        condition |= scope
    return condition


def receipt_condition():
    """Q4: intake staff record a district's receipt on its behalf, so they see withdrawals with
    a delivery waiting for an answer (nothing else of other divisions)."""
    return Q(
        Exists(
            Delivery.objects.filter(
                case=OuterRef("pk"), status__in=[Delivery.Status.AWAITING, Delivery.Status.MISMATCH]
            )
        )
    ) | Q(kind=K.PROBLEM, status=S.AWAITING_CONFIRMATION)  # M09: accept/dispute for them


def _fact(value, source, evidence=""):
    return {"value": value, "source": source, "evidence": evidence}


def case_facts(case):
    """ของไม่พอ from the inventory (Q9) and a reported receipt mismatch (Q6), as case data."""
    short, unknown, evidence = False, False, []
    for line in case.lines.select_related("item"):
        need = (
            line.outstanding
            if line.approved_quantity is not None
            else (line.requested_quantity or ZERO)
        )
        if need <= 0:
            continue
        figures = inventory.availability(line.item)
        if not figures.known:
            unknown = True
            continue
        competing = inventory.demand(line.item) > figures.available
        if need > figures.available or competing:
            short = True
            note = f"{line.item.code} ต้องใช้ {_number(need)} พร้อมจ่าย {_number(figures.available)}"
            evidence.append(note + (" (หลายเรื่องรอของเดียวกัน)" if competing else ""))
    if short:
        shortage = _fact(True, "stock", "; ".join(evidence))
    elif unknown:
        shortage = _fact(None, "stock", "ไม่มียอดหรือยอดเก่าเกินเกณฑ์")
    else:
        shortage = _fact(False, "stock")
    facts = {"shortage": shortage}
    mismatched = list(
        Delivery.objects.filter(case=case, status=Delivery.Status.MISMATCH).values_list(
            "seq", flat=True
        )
    )
    if mismatched:
        facts["serial_mismatch"] = _fact(
            True,
            "delivery",
            "ผู้แจ้งแจ้งไม่ตรงในการส่งมอบครั้งที่ " + ", ".join(str(s) for s in mismatched),
        )
    return facts


# --- Display (LINE status and the M10 card) ----------------------------------------------------


def _number(value):
    return f"{value.normalize():f}" if value is not None else "-"


def summary(case):
    """Per line: requested / approved / issued / received / outstanding, plus deliveries."""
    rows, issued_any, outstanding_any = [], False, False
    for line in case.lines.select_related("item").order_by("line_no"):
        issued_any |= line.issued_quantity > 0
        outstanding_any |= line.outstanding > 0
        rows.append(
            {
                "line_no": line.line_no,
                "item": line.item.code,
                "name": line.item.name,
                "unit": line.item.unit,
                "requested": _number(line.requested_quantity),
                "approved": _number(line.approved_quantity),
                "not_approved": _number(line.requested_quantity - line.approved_quantity)
                if line.approved_quantity is not None
                else "-",
                "approval_reason": line.approval_reason,
                "issued": _number(line.issued_quantity),
                "received": _number(line.received_quantity),
                "terminated": _number(line.terminated_quantity),
                "outstanding": _number(line.outstanding),
            }
        )
    deliveries = [
        {
            "seq": d.seq,
            "document_number": d.document_number,
            "status": d.status,
            "status_label": d.get_status_display(),
            "issued_at": d.issued_at.isoformat(),
            "lines": [
                {
                    "line_no": dl.line.line_no,
                    "item": dl.line.item.code,
                    "quantity": _number(quantity),
                    "unit": dl.line.item.unit,
                    "serials": serials,
                }
                for dl, quantity, serials in _effective(d).values()
            ],
        }
        for d in Delivery.objects.filter(case=case).order_by("seq")
    ]
    return {
        "lines": rows,
        "deliveries": deliveries,
        "partially_issued": issued_any and outstanding_any,
    }
