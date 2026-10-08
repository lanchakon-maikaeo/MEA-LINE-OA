"""Inventory seam (M07, ADR 0003): this system never owns stock figures.

`fake` stands in for the registry outside production: synthetic counts and serials, taken off
under a row lock when goods are issued, with a synthetic document number. `manual_reference`
records what staff already did in the registry: figures never change here; imported snapshots
(with as_of) are advisory, so they flag what looks short but never block recording a fact.

Lock order: callers hold the case row first, then `lock_items` (item ids ascending), so two
issues for different cases never wait on each other in opposite orders.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import F, Sum
from django.utils import timezone

from operations import identity
from operations.models import (
    Allocation,
    Case,
    CaseLine,
    CaseNumberCounter,
    EquipmentType,
    FakeSerial,
    FakeStock,
    FulfilmentEntry,
    IssuedSerial,
    StockMovement,
    StockSnapshot,
)

FAKE = "fake"
MANUAL = "manual_reference"
ZERO = Decimal("0")
OPEN_CASE_EXCLUDED = (Case.Status.CLOSED, Case.Status.REJECTED, Case.Status.CANCELLED)


class InventoryError(Exception):
    """The inventory refused (e.g. the fake store has fewer than asked)."""


def mode():
    return settings.INVENTORY_MODE


@dataclass(frozen=True)
class Availability:
    """Ready-to-issue figure for one item. `available` is None when there are no figures."""

    available: Decimal | None
    as_of: datetime | None
    stale: bool
    source: str

    @property
    def known(self):
        """Usable for decisions: figures exist and are not stale (Q9/Q10)."""
        return self.available is not None and not self.stale


def lock_items(item_ids):
    """Serialise issues and allocations per item (both modes), ascending ids."""
    ids = sorted({int(i) for i in item_ids})
    with connection.cursor() as cursor:
        for item_id in ids:
            digest = hashlib.sha256(f"inventory-item|{item_id}".encode()).digest()
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                [int.from_bytes(digest[:8], "big", signed=True)],
            )
    if mode() == FAKE:
        list(FakeStock.objects.select_for_update().filter(item_id__in=ids).order_by("item_id"))


def availability(item, now=None):
    now = now or timezone.now()
    if mode() == FAKE:
        stock = FakeStock.objects.filter(item=item).first()
        if stock is None:
            return Availability(None, None, False, "none")
        return Availability(stock.available, stock.updated_at, False, FAKE)
    snapshot = StockSnapshot.objects.filter(item=item).order_by("-as_of", "-id").first()
    if snapshot is None:
        return Availability(None, None, False, "none")
    # Issues recorded here after the registry figures were taken are not in them yet.
    since = (
        FulfilmentEntry.objects.filter(
            line__item=item,
            kind__in=[FulfilmentEntry.Kind.ISSUED, FulfilmentEntry.Kind.CORRECTED],
            created_at__gt=snapshot.as_of,
        ).aggregate(total=Sum("quantity"))["total"]
        or ZERO
    )
    stale = now - snapshot.as_of > timedelta(hours=settings.INVENTORY_STALE_HOURS)
    return Availability(snapshot.available - since, snapshot.as_of, stale, "snapshot")


def demand(item):
    """ส่วนค้างจ่าย of every open withdrawal case for this item."""
    return (
        CaseLine.objects.filter(
            item=item, case__kind=Case.Kind.WITHDRAWAL, approved_quantity__isnull=False
        )
        .exclude(case__status__in=OPEN_CASE_EXCLUDED)
        .aggregate(
            total=Sum(F("approved_quantity") - F("issued_quantity") - F("terminated_quantity"))
        )["total"]
        or ZERO
    )


def contended(item, now=None):
    """True when open cases need more than is ready (allocation required, Q8); None = unknown."""
    figures = availability(item, now)
    if not figures.known:
        return None
    return demand(item) > figures.available


def active_allocated(item, exclude_line=None):
    allocations = Allocation.objects.filter(item=item, ended_at__isnull=True)
    if exclude_line is not None:
        allocations = allocations.exclude(line=exclude_line)
    return allocations.aggregate(total=Sum(F("quantity") - F("used_quantity")))["total"] or ZERO


def check_allocations(item, now=None):
    """Q20: when ready stock falls below what is allocated but unused, all of it is void."""
    figures = availability(item, now)
    if figures.available is None:
        return 0
    if active_allocated(item) <= figures.available:
        return 0
    return Allocation.objects.filter(item=item, ended_at__isnull=True).update(
        ended_at=now or timezone.now(), end_reason=Allocation.End.VOID_STOCK
    )


def next_document_number(now):
    """Synthetic issue document numbers of the fake store (never confused with registry ones)."""
    year_be = timezone.localtime(now).year + 543
    CaseNumberCounter.objects.bulk_create(
        [CaseNumberCounter(prefix="I", year_be=year_be)], ignore_conflicts=True
    )
    counter = CaseNumberCounter.objects.select_for_update().get(prefix="I", year_be=year_be)
    counter.last_value += 1
    counter.save(update_fields=["last_value"])
    return f"SIM-ISS{year_be % 100:02d}-{counter.last_value:06d}"


def take_fake(item, quantity, serials, case, actor, actor_label, document_number):
    """Issue from the fake store: the caller holds `lock_items` for this item."""
    stock = FakeStock.objects.select_for_update().filter(item=item).first()
    if stock is None or stock.available < quantity:
        have = stock.available if stock else ZERO
        raise InventoryError(f"คลังจำลองมี {item.code} พร้อมจ่าย {have} ไม่พอสำหรับ {quantity}")
    if item.is_serialized:
        found = {
            s.serial: s
            for s in FakeSerial.objects.select_for_update().filter(item=item, serial__in=serials)
        }
        for serial in serials:
            row = found.get(serial)
            if row is None or row.status != FakeSerial.Status.AVAILABLE:
                raise InventoryError(f"เลขเครื่อง {serial} ไม่อยู่ในสถานะพร้อมจ่ายของ {item.code}")
        FakeSerial.objects.filter(item=item, serial__in=serials).update(
            status=FakeSerial.Status.ISSUED, case=case
        )
    stock.available -= quantity
    stock.save(update_fields=["available", "updated_at"])
    StockMovement.objects.create(
        item=item,
        kind=StockMovement.Kind.ISSUE,
        quantity=-quantity,
        serials=list(serials),
        document_number=document_number,
        case=case,
        actor=actor,
        actor_label=actor_label,
    )


# --- Returns (M08): where a returned unit sits in the fake store -----------------------------

AVAILABLE = "available"
AWAITING_INSPECTION = "awaiting_inspection"
AWAITING_REPAIR = "awaiting_repair"
BUCKETS = {
    AVAILABLE: FakeSerial.Status.AVAILABLE,
    AWAITING_INSPECTION: FakeSerial.Status.AWAITING_INSPECTION,
    AWAITING_REPAIR: FakeSerial.Status.AWAITING_REPAIR,
}


def _stock(item):
    stock = FakeStock.objects.select_for_update().filter(item=item).first()
    return stock or FakeStock.objects.create(item=item, available=ZERO)


def serial_status(item, serial):
    """The fake store's status of a serial, or None when it does not know it (or not fake)."""
    if mode() != FAKE:
        return None
    return (
        FakeSerial.objects.filter(item=item, serial=serial).values_list("status", flat=True).first()
    )


def move_fake(item, quantity, serial, source, target, case, actor, actor_label, reason, out=None):
    """Move a returned unit between figures (None = outside the store). The caller holds
    `lock_items`. Returns the quantity moved (less than asked when stock already left)."""
    if mode() != FAKE:
        return quantity
    stock = _stock(item)
    if source is not None and getattr(stock, source) < quantity:
        return ZERO
    if serial:
        row = FakeSerial.objects.filter(item=item, serial=serial).first()
        if source is not None and (row is None or row.status != BUCKETS[source]):
            return ZERO
        status = BUCKETS[target] if target else (out or FakeSerial.Status.DISPOSED)
        FakeSerial.objects.update_or_create(
            item=item,
            serial=serial,
            defaults={"status": status, "case": case if target else None},
        )
    changes = {}
    if source is not None:
        setattr(stock, source, getattr(stock, source) - quantity)
        changes[source] = str(-quantity)
    if target is not None:
        setattr(stock, target, getattr(stock, target) + quantity)
        changes[target] = str(quantity)
    stock.save()
    kind = (
        StockMovement.Kind.RECEIVE
        if source is None
        else StockMovement.Kind.RETURN_OUT
        if out == FakeSerial.Status.ISSUED
        else StockMovement.Kind.INSPECT
    )
    StockMovement.objects.create(
        item=item,
        kind=kind,
        quantity=Decimal(changes.get(AVAILABLE, "0")),
        serials=[serial] if serial else [],
        case=case,
        actor=actor,
        actor_label=actor_label,
        reason=reason,
        changes={k: v for k, v in changes.items() if k != AVAILABLE},
    )
    return quantity


def available_serials(item):
    if mode() != FAKE or not item.is_serialized:
        return []
    return list(
        FakeSerial.objects.filter(item=item, status=FakeSerial.Status.AVAILABLE).values_list(
            "serial", flat=True
        )
    )


def refresh_cases_later(item_ids):
    """Q9: open withdrawal cases of these items get a fresh proposal after this commit, one case
    per transaction (no other case is locked while the change that caused it holds locks)."""
    ids = sorted(set(item_ids))
    if not ids:
        return

    def run():
        from operations import policy

        case_ids = list(
            Case.objects.filter(kind=Case.Kind.WITHDRAWAL, lines__item_id__in=ids)
            .exclude(status__in=OPEN_CASE_EXCLUDED)
            .values_list("pk", flat=True)
            .distinct()
        )
        for case_id in case_ids:
            with transaction.atomic():
                case = Case.objects.select_for_update().filter(pk=case_id).first()
                if case is not None and case.status not in OPEN_CASE_EXCLUDED:
                    policy.refresh(case, "stock_changed")

    transaction.on_commit(run)


# --- Admin operations ------------------------------------------------------------------------


def _decimal(value, name):
    if isinstance(value, bool) or isinstance(value, float):
        raise ValidationError(f"{name} must be an integer or a decimal string.")
    try:
        number = Decimal(str(value))
    except ArithmeticError as exc:
        raise ValidationError(f"{name} is not a number.") from exc
    if number != number.quantize(Decimal("0.01")):
        raise ValidationError(f"{name} allows at most 2 decimal places.")
    return number


def seed_fake(rows, actor_label="seed"):
    """Reset the fake store from fixture rows (demo database only; the caller checks)."""
    if mode() != FAKE:
        raise ValidationError("The fake inventory is not in use (INVENTORY_MODE).")
    items = {i.code: i for i in EquipmentType.objects.filter(code__in=[r["item"] for r in rows])}
    for row in rows:
        item = items.get(row["item"])
        if item is None:
            raise ValidationError(f"Unknown item {row['item']}.")
        serials = row.get("serials") or {}
        if item.is_serialized and len(serials.get("available", [])) != int(row["available"]):
            raise ValidationError(f"{item.code}: one serial per available unit.")
        FakeSerial.objects.filter(item=item).delete()
        FakeSerial.objects.bulk_create(
            FakeSerial(item=item, serial=serial, status=status)
            for status, values in serials.items()
            for serial in values
        )
        # The reset store holds these serials again, so an earlier demo issue of them is over.
        on_hand = [serial for values in serials.values() for serial in values]
        IssuedSerial.objects.filter(item=item, serial__in=on_hand, active=True).update(
            active=False, ended_at=timezone.now(), end_reason="ตั้งคลังจำลองใหม่ (demo)"
        )
        FakeStock.objects.update_or_create(
            item=item,
            defaults={
                name: _decimal(row.get(name, 0), name)
                for name in ("available", "reserved", "awaiting_inspection", "awaiting_repair")
            },
        )
        StockMovement.objects.create(
            item=item,
            kind=StockMovement.Kind.SEED,
            quantity=_decimal(row["available"], "available"),
            serials=list(serials.get("available", [])),
            actor_label=actor_label,
            reason=row.get("source", "synthetic fixture"),
        )


@transaction.atomic
def adjust_fake(actor, item, delta, reason, add_serials=(), remove_serials=(), bucket=AVAILABLE):
    """An admin correction of the fake store, like a registry correction document (Q18).

    `bucket` is the figure changed: ready to issue (default), awaiting inspection or repair."""
    identity.require_admin(actor)
    if mode() != FAKE:
        raise ValidationError("ปรับได้เฉพาะคลังจำลอง (โหมดอ้างเลขเอกสารใช้การนำเข้ายอด)")
    if bucket not in BUCKETS:
        raise ValidationError("Unknown stock figure.")
    reason = identity._require_reason(reason)
    delta = _decimal(delta, "delta")
    if delta == 0 and not add_serials and not remove_serials:
        raise ValidationError("Nothing to change.")
    lock_items([item.pk])
    stock = _stock(item)
    if item.is_serialized:
        if Decimal(len(add_serials) - len(remove_serials)) != delta:
            raise ValidationError("Serialised items change by the serials added or removed.")
        for serial in add_serials:
            FakeSerial.objects.update_or_create(
                item=item, serial=serial, defaults={"status": BUCKETS[bucket], "case": None}
            )
        removed = FakeSerial.objects.filter(
            item=item, serial__in=list(remove_serials), status=BUCKETS[bucket]
        ).delete()[0]
        if removed != len(remove_serials):
            raise ValidationError("Only serials in the chosen figure can be removed.")
    elif add_serials or remove_serials:
        raise ValidationError(f"{item.code} is not tracked by serial.")
    if getattr(stock, bucket) + delta < 0:
        raise ValidationError("The fake store cannot go below zero.")
    setattr(stock, bucket, getattr(stock, bucket) + delta)
    stock.save(update_fields=[bucket, "updated_at"])
    StockMovement.objects.create(
        item=item,
        kind=StockMovement.Kind.ADJUST,
        quantity=delta if bucket == AVAILABLE else ZERO,
        serials=[*add_serials, *(f"-{s}" for s in remove_serials)],
        actor=actor,
        actor_label=f"user:{actor.pk}",
        reason=reason,
        changes={} if bucket == AVAILABLE else {bucket: str(delta)},
    )
    voided = check_allocations(item)
    identity.audit(
        actor, "inventory.adjusted", stock, reason, item=item.code, delta=str(delta), voided=voided
    )
    refresh_cases_later([item.pk])
    return stock


@transaction.atomic
def import_snapshot(actor, rows, as_of, source, is_synthetic=False, *, authorized=False):
    """Registry figures for manual_reference mode (Q10), appended with their as_of.
    `authorized`: the caller already checked the actor (M11: registry staff upload balances)."""
    if actor is not None and not authorized:
        identity.require_admin(actor)
    if not rows:
        raise ValidationError("No rows to import.")
    if not source or not str(source).strip():
        raise ValidationError("Name the source of the figures.")
    if as_of > timezone.now() + timedelta(minutes=5):
        raise ValidationError("as_of cannot be in the future.")
    codes = [row.get("item_id") or row.get("item") for row in rows]
    items = {i.code: i for i in EquipmentType.objects.filter(code__in=codes)}
    created = []
    for row, code in zip(rows, codes, strict=True):
        item = items.get(code)
        if item is None:
            raise ValidationError(f"Unknown item {code}.")
        available = _decimal(row["available"], "available")
        if available < 0:
            raise ValidationError("available cannot be negative.")
        total = row.get("total_on_hand")
        created.append(
            StockSnapshot.objects.create(
                item=item,
                available=available,
                reserved=_decimal(row.get("reserved", 0), "reserved"),
                awaiting_inspection=_decimal(
                    row.get("awaiting_inspection", 0), "awaiting_inspection"
                ),
                awaiting_repair=_decimal(row.get("awaiting_repair", 0), "awaiting_repair"),
                total_on_hand=None if total is None else _decimal(total, "total_on_hand"),
                as_of=as_of,
                source=str(source).strip()[:200],
                is_synthetic=is_synthetic,
                imported_by=actor,
            )
        )
    if mode() == MANUAL:
        for item in items.values():
            lock_items([item.pk])
            check_allocations(item)
    identity.audit(
        actor,
        "inventory.snapshot_imported",
        created[0],
        str(source),
        actor_label="" if actor else "system:import",
        items=len(created),
        as_of=as_of.isoformat(),
    )
    refresh_cases_later([i.pk for i in items.values()])
    return created
