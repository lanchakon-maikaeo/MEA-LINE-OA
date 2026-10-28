"""กอจ. Q1–Q20 (27 Oct 2569): read external work, subscribe, and notify through the outbox.

Only mapped columns are retained. Private sheets are read by the application, never by AI.
"""

import hashlib
import json
import re
import secrets
import unicodedata
import uuid
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from operations import identity as identities
from operations import orchestration
from operations.models import (
    ChannelIdentity,
    District,
    Division,
    KojHistory,
    KojNotice,
    KojRecipient,
    KojRecord,
    KojSource,
    KojWatch,
    NotificationDelivery,
    OutboxEvent,
    RoleGrant,
)
from operations.templatetags.ui import thai_datetime

EQUIPMENT = {
    "transformer": "หม้อแปลง",
    "lbs": "LBS",
    "rmu": "RMU",
    "metering": "ตู้ metering",
    "frtu": "LBS FRTU",
    "bushing": "bushing box",
    "vt_lbs": "VT ประกอบ LBS",
    "lv_cap": "LV Cap.",
    "other": "อุปกรณ์อื่น ๆ",
}
SERVICES = {"withdrawal": "ติดตามใบเบิก", "return": "ติดตามใบส่งคืน", "repair": "ติดตามสถานะการซ่อม"}
GROUPS = {
    "mk": "มค. · เบิก/คืนหม้อแปลงและคาปาซิเตอร์",
    "sj": "ซจ. · ซ่อม LBS/RMU",
    "other": "กอจ. · เบิก/คืนอุปกรณ์อื่น",
}
COLUMNS = {
    "row_key": "รหัสแถวถาวร (ไม่ซ้ำ)",
    "reference": "เลขเอกสาร",
    "status": "สถานะงาน",
    "district": "เขต",
    "equipment": "ชนิดอุปกรณ์",
    "name": "ชื่ออุปกรณ์",
    "size": "ขนาด",
    "quantity": "จำนวน",
    "appointment": "วันนัดรับ",
    "updated": "เวลาปรับปรุงจากต้นทาง",
    "description": "รายละเอียดรายการ",
}
NAMESPACE = uuid.UUID("5e3da671-c81c-4fd4-b9e6-9419cf397220")
REQUIRED = {"row_key", "reference", "status", "district"}
DIVISION_CODES = ("กอจ.", "กองอุปกรณ์ฯ")


def division():
    return Division.objects.filter(code__in=DIVISION_CODES, is_active=True).first()


def reference_key(value):
    value = "".join(unicodedata.normalize("NFKC", str(value)).split())
    value = value.translate(str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789"))
    if not re.fullmatch(r"[A-Za-z0-9./_-]{1,100}", value):
        raise ValidationError("เลขเอกสารใช้ตัวอักษร ตัวเลข จุด / - หรือ _ ได้ ไม่เกิน 100 ตัว")
    return value.upper()


def group_for(equipment, service):
    if equipment not in EQUIPMENT or service not in SERVICES:
        raise ValidationError("ไม่พบบริการหรืออุปกรณ์นี้")
    if service == "repair":
        if equipment not in {"lbs", "rmu"}:
            raise ValidationError("เฟสแรกติดตามซ่อมเฉพาะ LBS และ RMU")
        return "sj"
    return "mk" if equipment in {"transformer", "lv_cap"} else "other"


def staff_can_read(user):
    if user is None or not user.is_active:
        return False
    return (
        identities.staff_scope(user).is_admin
        or RoleGrant.objects.filter(
            user=user,
            revoked_at__isnull=True,
            division__code__in=DIVISION_CODES,
            division__is_active=True,
        ).exists()
    )


def visible(identity):
    rows = KojRecord.objects.filter(present=True, source__enabled=True)
    if staff_can_read(identity.staff_user):
        return rows
    requester = identity.requester
    if (
        identity.status != "active"
        or requester is None
        or requester.status != "active"
        or requester.is_anonymous
        or requester.is_external
        or requester.is_internal
    ):
        return rows.none()
    return rows.filter(district_id=requester.district_id)


def watch_rows(watch):
    rows = visible(watch.identity).filter(
        source=watch.source,
        reference_key=watch.reference_key,
        equipment=watch.equipment,
    )
    if watch.equipment_name:
        rows = rows.filter(equipment_name__iexact=watch.equipment_name)
    return rows


def find(identity, equipment, service, number, name=""):
    group_for(equipment, service)
    key = reference_key(number)
    rows = visible(identity).filter(equipment=equipment, source__service=service)
    if name:
        rows = rows.filter(equipment_name__iexact=name)
    exact = rows.filter(reference_key=key)
    return exact if exact.exists() else rows.filter(reference_key__startswith=key)


def check_source(source):
    group_for(source.default_equipment, source.service)
    if source.enabled and not source.contact.strip():
        raise ValidationError("กรอกช่องทางติดต่อเมื่อค้นไม่พบก่อนเปิดแหล่งข้อมูล")
    if not isinstance(source.columns, dict) or not REQUIRED <= source.columns.keys():
        raise ValidationError("ต้องจับคู่คอลัมน์รหัสแถว เลขเอกสาร สถานะ และเขต")
    if any(not isinstance(v, str) or not v.strip() for v in source.columns.values()):
        raise ValidationError("ชื่อคอลัมน์ต้องเป็นข้อความที่ไม่ว่าง")
    if not source.columns.keys() <= COLUMNS.keys():
        raise ValidationError("ความหมายคอลัมน์ไม่ถูกต้อง")
    if not isinstance(source.district_map, dict) or not isinstance(source.equipment_map, dict):
        raise ValidationError("ตารางจับคู่ต้องเป็น JSON object")
    if any(not isinstance(value, str) for value in source.district_map.values()):
        raise ValidationError("ค่าเขตในตารางจับคู่ต้องเป็นข้อความ")
    if "equipment" in source.columns and not source.equipment_map:
        raise ValidationError("เลือกคอลัมน์อุปกรณ์แล้ว ต้องจับคู่ชนิดอุปกรณ์ด้วย")
    for code in source.equipment_map.values():
        group_for(code, source.service)
    if not isinstance(source.completed_statuses, list) or any(
        not isinstance(v, str) or not v.strip() for v in source.completed_statuses
    ):
        raise ValidationError("สถานะจบงานต้องเป็นรายการข้อความ")
    if source.provider == "google" and (
        not re.fullmatch(r"[A-Za-z0-9_-]{20,200}", source.spreadsheet_id)
        or not source.sheet_name.strip()
    ):
        raise ValidationError("กรอก Spreadsheet ID และชื่อแท็บ")


def _config(source):
    return json.dumps(
        [
            source.provider,
            source.spreadsheet_id,
            source.sheet_name,
            source.service,
            source.default_equipment,
            source.columns,
            source.equipment_map,
            source.district_map,
            source.completed_statuses,
        ],
        sort_keys=True,
    )


def read_rows(source, matrix):
    """Validate the entire snapshot before changing records. Document numbers may repeat."""
    check_source(source)
    if not matrix or len(matrix) > 20001:
        raise ValidationError("ข้อมูลต้องมีหัวคอลัมน์และไม่เกิน 20000 แถว")
    header = [str(v).strip() for v in matrix[0]]
    if any(header.count(column) != 1 for column in source.columns.values()):
        raise ValidationError("หัวคอลัมน์ที่เลือกว่าง ซ้ำ หรือไม่ตรงการตั้งค่า ข้อมูลเดิมยังอยู่ครบ")
    positions = {key: header.index(column) for key, column in source.columns.items()}
    districts = {d.code: d.pk for d in District.objects.filter(is_active=True)}
    rows, keys = [], set()
    for index, values in enumerate(matrix[1:], 2):
        if not any(str(v).strip() for v in values):
            continue

        def cell(key, values=values):
            pos = positions.get(key)
            return str(values[pos]).strip() if pos is not None and pos < len(values) else ""

        key, reference, status = cell("row_key"), cell("reference"), cell("status")
        if not key or len(key) > 150 or key in keys:
            raise ValidationError(f"แถว {index}: รหัสแถวว่าง ซ้ำ หรือยาวเกิน 150 ตัว")
        if not reference or not status or len(status) > 300:
            raise ValidationError(f"แถว {index}: ต้องมีเลขเอกสารและสถานะงาน")
        keys.add(key)
        equipment = source.default_equipment
        if "equipment" in positions:
            equipment = source.equipment_map.get(cell("equipment"), "")
        group_for(equipment, source.service)
        raw_district = cell("district")
        district = districts.get(source.district_map.get(raw_district, raw_district))
        name = cell("name") or EQUIPMENT[equipment]
        if len(name) > 150:
            raise ValidationError(f"แถว {index}: ชื่ออุปกรณ์ยาวเกิน 150 ตัว")
        details = {
            k: cell(k)[:1000]
            for k in ("size", "quantity", "appointment", "updated", "description")
            if cell(k)
        }
        if len(reference) > 100:
            raise ValidationError(f"แถว {index}: เลขเอกสารยาวเกิน 100 ตัว")
        rows.append(
            {
                "row_key": key,
                "reference": reference,
                "reference_key": reference_key(reference),
                "equipment": equipment,
                "equipment_name": name,
                "district_id": district,
                "status": status,
                "details": details,
            }
        )
    return rows


@transaction.atomic
def apply_snapshot(source_id, matrix, *, expected_config=None, token=None, now=None):
    now = now or timezone.now()
    source = KojSource.objects.select_for_update().get(pk=source_id)
    if (expected_config is not None and _config(source) != expected_config) or (
        token is not None and source.sync_token != token
    ):
        raise ValidationError("การตั้งค่าเปลี่ยนระหว่างดึงข้อมูล กรุณา refresh อีกครั้ง")
    rows = read_rows(source, matrix)
    existing = {r.row_key: r for r in KojRecord.objects.select_for_update().filter(source=source)}
    changed, restored = set(), set()
    for values in rows:
        record = existing.get(values["row_key"])
        if record is None:
            record = KojRecord.objects.create(source=source, observed_at=now, **values)
            KojHistory.objects.create(record=record, status=record.status, observed_at=now)
            changed.add(record.reference_key)
            continue
        old_status = record.status
        old_reference = record.reference_key
        if not record.present:
            restored.add(values["reference_key"])
        # A row's authority or identity can change too; later notification checks use live scope.
        different = not record.present or any(getattr(record, k) != v for k, v in values.items())
        for key, value in values.items():
            setattr(record, key, value)
        record.present, record.observed_at = True, now
        if different:
            record.revision += 1
        if different:
            record.save()
        if old_status != record.status:
            KojHistory.objects.create(record=record, status=record.status, observed_at=now)
            changed.update([old_reference, record.reference_key])
    absent = set(existing) - {r["row_key"] for r in rows}
    KojRecord.objects.filter(source=source, row_key__in=absent).update(present=False)
    source.last_synced_at, source.last_error = now, ""
    source.last_attempt_at = now
    source.next_sync_at = now + timedelta(hours=1)
    source.save(update_fields=["last_synced_at", "last_attempt_at", "next_sync_at", "last_error"])
    for watch in (
        source.watches.select_for_update(of=("self",))
        .filter(status="active", reference_key__in=changed | restored)
        .select_related("identity__requester", "source")
    ):
        update_watch(watch, now, notify_change=watch.reference_key in changed)
    return len(rows)


def request_refresh(actor, source):
    # 28 ต.ค. 2569: only admins pull a source between the hourly rounds (ADR 0026).
    if not identities.staff_scope(actor).is_admin:
        raise PermissionDenied
    if source.provider != "google":
        raise ValidationError("แหล่ง CSV ใช้ปุ่มนำเข้าไฟล์ใหม่")
    check_source(source)
    KojSource.objects.filter(pk=source.pk).update(refresh_requested=True)
    identities.audit(actor, "koj.refresh_requested", source)


def sync_due(now=None):
    """Claim each due source once; preserve its last good snapshot on any failure."""
    now = now or timezone.now()
    with transaction.atomic():
        source = (
            KojSource.objects.select_for_update(skip_locked=True)
            .filter(provider="google")
            .filter(Q(enabled=True, next_sync_at__lte=now) | Q(refresh_requested=True))
            .filter(Q(syncing_until__isnull=True) | Q(syncing_until__lt=now))
            .order_by("next_sync_at")
            .first()
        )
        if source is None:
            return False
        token = secrets.token_hex(16)
        source.sync_token, source.syncing_until = token, now + timedelta(minutes=2)
        source.refresh_requested, source.last_attempt_at = False, now
        source.next_sync_at = now + timedelta(hours=1)
        source.save()
        config = _config(source)
    try:
        from operations.koj_sheets import fetch

        matrix = fetch(source)
        apply_snapshot(source.pk, matrix, expected_config=config, token=token, now=now)
    except Exception as exc:  # one failed/private source must not stop chat or expose its data
        error = (
            "; ".join(exc.messages)[:200]
            if isinstance(exc, ValidationError)
            else "ดึงข้อมูลไม่สำเร็จ ตรวจการตั้งค่าและสิทธิ์อ่านชีท"
        )
        KojSource.objects.filter(pk=source.pk, sync_token=token).update(last_error=error)
    finally:
        KojSource.objects.filter(pk=source.pk, sync_token=token).update(
            sync_token="", syncing_until=None
        )
    return True


def sync_loop():
    import logging
    import time

    from django.db import close_old_connections

    while True:
        try:
            close_old_connections()
            handled = sync_due()
        except Exception:  # maintenance failure is retried, no sheet contents in logs
            logging.getLogger(__name__).warning("กอจ. source maintenance failed")
            handled = False
        finally:
            close_old_connections()
        time.sleep(1 if handled else 10)


def describe(record):
    parts = [f"{record.reference} · {record.equipment_name}", f"สถานะงาน: {record.status}"]
    if record.district_id:
        parts.append(f"เขต: {record.district.name}")
    for key, label in [
        ("size", "ขนาด"),
        ("quantity", "จำนวน"),
        ("appointment", "วันนัดรับ"),
        ("updated", "ปรับปรุงต้นทาง"),
        ("description", "รายละเอียด"),
    ]:
        if record.details.get(key):
            parts.append(f"{label}: {record.details[key]}")
    parts.append(f"แหล่งข้อมูล: {record.source.name}")
    if record.source.last_synced_at:
        parts.append(f"ข้อมูล ณ {thai_datetime(record.source.last_synced_at)}")
    if record.source.last_error:
        parts.append("ดึงข้อมูลรอบล่าสุดไม่สำเร็จ กำลังแสดงข้อมูลที่บันทึกล่าสุด")
    return "\n".join(parts)


def _snapshot(watch):
    return [(r.pk, r.revision, r.status) for r in watch_rows(watch).order_by("pk")]


def stamp(watch):
    return hashlib.sha256(json.dumps(_snapshot(watch)).encode()).hexdigest()


@transaction.atomic
def subscribe(identity, record):
    record = visible(identity).select_related("source").filter(pk=record.pk).first()
    if record is None:
        raise PermissionDenied
    watch, created = KojWatch.objects.select_for_update().get_or_create(
        identity=identity,
        source=record.source,
        reference_key=record.reference_key,
        equipment=record.equipment,
        equipment_name=record.equipment_name.casefold() if record.equipment == "other" else "",
    )
    if created or watch.status != "active":
        if not created:
            watch.cycle += 1
        watch.status = "active"
        watch.seen_stamp = stamp(watch)  # the chat shows the rows as it starts following
        watch.save()
        _plan(watch, "started", timezone.now())
        update_watch(watch, timezone.now(), notify_change=False)
    return watch


@transaction.atomic
def unsubscribe(identity, watch_id):
    watch = KojWatch.objects.select_for_update().filter(pk=watch_id, identity=identity).first()
    if watch is None:
        raise PermissionDenied
    watch.status = "stopped"
    watch.save(update_fields=["status", "updated_at"])
    return watch


def update_watch(watch, now, notify_change=True):
    rows = list(watch_rows(watch).select_related("source"))
    if not rows:
        return
    absent = KojRecord.objects.filter(
        source=watch.source,
        reference_key=watch.reference_key,
        equipment=watch.equipment,
        present=False,
    )
    if not staff_can_read(watch.identity.staff_user) and watch.identity.requester:
        absent = absent.filter(district_id=watch.identity.requester.district_id)
    complete = not absent.exists() and all(
        r.status in watch.source.completed_statuses for r in rows
    )
    if complete:
        watch.status = "completed"
        watch.save(update_fields=["status", "updated_at"])
    if notify_change or complete:
        _plan(watch, "completed" if complete else "changed", now)


def _plan(watch, event, now):
    rows = list(watch_rows(watch).select_related("source"))
    if not rows:
        return
    group = group_for(watch.equipment, watch.source.service)
    # Owner, 28 Oct 2569 (Q2): the follower gets no push; updates() shows a change at their
    # next message. Staff recipients still get a push within the staff message budget.
    people = []
    for recipient in KojRecipient.objects.filter(group=group).select_related("user"):
        if not staff_can_read(recipient.user):
            continue
        line = ChannelIdentity.objects.filter(staff_user=recipient.user, is_following=True).first()
        if line and line.pk != watch.identity_id:
            people.append((line, recipient.user, group))
    current = stamp(watch)
    title = {"started": "เริ่มติดตามงาน", "changed": "สถานะงานเปลี่ยน", "completed": "ติดตามจบแล้ว"}[event]
    text = (
        f"กอจ. · {title}\n{SERVICES[watch.source.service]} · {EQUIPMENT[watch.equipment]}\n"
        + "\n\n".join(describe(r) for r in rows)
    )
    # Full results remain in the private chat; the delivery links back to all matching rows.
    text = text[:4500]
    for line, user, recipient_group in people:
        subject = (
            f"{watch.source_id}:{watch.reference_key}:{watch.equipment}:{watch.equipment_name}"
            if recipient_group
            else f"{watch.pk}:{watch.cycle}"
        )
        key = uuid.uuid5(NAMESPACE, f"{subject}:{event}:{current}:{line.pk}")
        delivery, created = NotificationDelivery.objects.get_or_create(
            source_event=key,
            kind=f"koj_{event}",
            defaults={
                "level": "normal",
                "identity": line,
                "recipient_user": user,
                "text": text,
                "send_after": now,
                "message": {
                    "type": "text",
                    "text": text,
                    "quickReply": {
                        "items": [
                            {
                                "type": "action",
                                "action": {
                                    "type": "postback",
                                    "label": "ดูสถานะงาน",
                                    "data": f"m=koj&do=view&id={rows[0].pk}",
                                },
                            }
                        ]
                    },
                },
            },
        )
        if created:
            KojNotice.objects.create(
                delivery=delivery,
                watch=watch,
                group=recipient_group,
                record_ids=[r.pk for r in rows],
            )
            OutboxEvent.objects.create(
                event_type=orchestration.EVENT_NOTIFICATION,
                subject_type="delivery",
                subject_id=str(delivery.pk),
                correlation_id=key,
                payload={"delivery_id": delivery.pk},
                occurred_at=now,
                next_attempt_at=now,
            )


def updates(identity, limit=3):
    """[(watch, title, rows)] of followed jobs whose rows changed since the follower last saw
    them; marks them seen. Rows are read with the follower's own scope at this moment."""
    found = []
    watches = KojWatch.objects.filter(identity=identity, status__in=["active", "completed"])
    for watch in watches.select_related("source").order_by("updated_at", "pk"):
        current = stamp(watch)
        if current == watch.seen_stamp:
            continue
        rows = list(watch_rows(watch).select_related("source"))
        watch.seen_stamp = current
        watch.save(update_fields=["seen_stamp", "updated_at"])
        if rows:
            title = "ติดตามจบแล้ว" if watch.status == "completed" else "สถานะงานเปลี่ยน"
            found.append((watch, title, rows))
        if len(found) >= limit:
            break
    return found


def recipient_problem(delivery):
    notice = (
        KojNotice.objects.select_related("watch__identity__requester", "watch__source")
        .filter(delivery=delivery)
        .first()
    )
    if notice is None:
        return "tracking stopped"
    if notice.group:
        user = delivery.recipient_user
        if (
            not staff_can_read(user)
            or delivery.identity.staff_user_id != user.pk
            or not KojRecipient.objects.filter(user=user, group=notice.group).exists()
            or notice.group != group_for(notice.watch.equipment, notice.watch.source.service)
        ):
            return "tracking recipient no longer allowed"
        if not KojWatch.objects.filter(
            source=notice.watch.source,
            reference_key=notice.watch.reference_key,
            equipment=notice.watch.equipment,
            status__in=["active", "completed"],
        ).exists():
            return "tracking stopped"
        available = set(
            visible(delivery.identity)
            .filter(
                source=notice.watch.source,
                reference_key=notice.watch.reference_key,
                equipment=notice.watch.equipment,
            )
            .values_list("pk", flat=True)
        )
    elif delivery.identity_id != notice.watch.identity_id:
        return "tracking belongs to another identity"
    else:
        if notice.watch.status == "stopped":
            return "tracking stopped"
        available = set(watch_rows(notice.watch).values_list("pk", flat=True))
    if not notice.record_ids or not set(notice.record_ids) <= available:
        return "tracking no longer in scope"
    return ""


@transaction.atomic
def set_recipients(actor, user, groups):
    if not identities.staff_scope(actor).is_admin:
        raise PermissionDenied
    groups = set(groups)
    if not groups <= GROUPS.keys():
        raise ValidationError("ชุดรับแจ้งไม่ถูกต้อง")
    if groups and (
        not staff_can_read(user)
        or not ChannelIdentity.objects.filter(staff_user=user, is_following=True).exists()
    ):
        raise ValidationError("ผู้รับต้องมีสิทธิ์ กอจ. และเชื่อมบัญชี LINE แล้ว")
    KojRecipient.objects.filter(user=user).exclude(group__in=groups).delete()
    for group in groups:
        KojRecipient.objects.get_or_create(user=user, group=group)
    identities.audit(actor, "koj.recipients_changed", user, groups=sorted(groups))
