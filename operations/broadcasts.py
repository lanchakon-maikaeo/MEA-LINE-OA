"""Shared draft, audience and confirmation rules for web and Mini App (ADR 0040)."""

import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.db.models import Count, Min, Q
from django.utils import timezone

from operations import identity
from operations.models import (
    BroadcastBatch,
    BroadcastCampaign,
    BroadcastSegment,
    ChannelIdentity,
    District,
    Division,
    Role,
    RoleGrant,
)

S = BroadcastCampaign.Status
EDITABLE = {S.DRAFT, S.PAUSED, S.SCHEDULED, S.QUEUED, S.HELD}
SENT = {S.ACCEPTED, S.PARTIAL}


def require_supervisor(user):
    scope = identity.staff_scope(user)
    if not scope.overseen and not scope.is_department_supervisor:
        raise PermissionDenied("การประกาศสงวนสิทธิ์สำหรับแอดมินกองและแอดมินฝ่ายเท่านั้น")
    return scope


def may_edit(user, row):
    scope = require_supervisor(user)
    return row.created_by_id == user.pk or scope.is_department_supervisor


def require_editor(user, row):
    if not may_edit(user, row):
        raise PermissionDenied("ท่านสามารถดูประวัติได้ แต่ไม่มีสิทธิ์แก้ไขรายการนี้")


def _revision(row, revision):
    if row.revision != revision:
        raise ValidationError("ข้อมูลมีการเปลี่ยนแปลง กรุณาเปิดรายการล่าสุดอีกครั้ง")


def validate_criteria(criteria):
    if (
        not isinstance(criteria, dict)
        or not criteria
        or set(criteria) - {"districts", "divisions", "roles", "people"}
    ):
        raise ValidationError("กรุณาระบุเงื่อนไขกลุ่มผู้รับ")
    clean = {}
    for key, values in criteria.items():
        if not isinstance(values, list) or not values or len(values) > 1000:
            raise ValidationError("เงื่อนไขกลุ่มผู้รับไม่ถูกต้อง")
        if key == "roles":
            if any(v not in Role.values for v in values):
                raise ValidationError("บทบาทผู้รับไม่ถูกต้อง")
        else:
            if any(type(v) is not int or v < 1 for v in values):
                raise ValidationError("รายการผู้รับไม่ถูกต้อง")
            model = {"districts": District, "divisions": Division, "people": ChannelIdentity}[key]
            if model.objects.filter(pk__in=values).count() != len(set(values)):
                raise ValidationError("ไม่พบรายการผู้รับที่เลือก")
        clean[key] = sorted(set(values))
    return clean


@transaction.atomic
def save_segment(user, name, criteria, *, segment_id=None, revision=None):
    require_supervisor(user)
    name = str(name).strip()
    if not name or len(name) > 100:
        raise ValidationError("กรุณาระบุชื่อกลุ่มไม่เกิน 100 ตัวอักษร")
    criteria = validate_criteria(criteria)
    if segment_id:
        row = BroadcastSegment.objects.select_for_update().get(pk=segment_id)
        require_editor(user, row)
        _revision(row, revision)
        row.revision += 1
    else:
        row = BroadcastSegment(created_by=user)
    row.name, row.criteria = name, criteria
    row.save()
    identity.audit(user, "broadcast.segment_saved", row, revision=row.revision)
    return row


def segment_members(segment):
    """Owner, 19 Nov 2569: units mix. Everyone in any chosen หน่วยงาน (a District row: a เขต,
    a ฟข. or another ฝ่าย such as ฝบก., the unit a registered person files for) or กอง of ฝอจ.
    (or picked by name) is in; roles only narrow the กอง staff."""
    criteria = validate_criteria(segment.criteria)
    chosen = Q(pk__in=criteria.get("people", []))
    if "districts" in criteria:
        chosen |= Q(requester__district_id__in=criteria["districts"], requester__status="active")
    if "divisions" in criteria or "roles" in criteria:
        grants = RoleGrant.objects.filter(revoked_at__isnull=True)
        if "divisions" in criteria:
            # A closed กอง reaches nobody, as before.
            ids = Division.objects.filter(pk__in=criteria["divisions"], is_active=True).values("pk")
            grants = grants.filter(division_id__in=ids)
        if "roles" in criteria:
            grants = grants.filter(role__in=criteria["roles"])  # held in a chosen กอง, as before
        staff = Q(staff_user_id__in=grants.values("user_id"))
        if "divisions" in criteria and "roles" not in criteria:
            # Or the สังกัด the roster gives: a เจ้าหน้าที่ holds no role.
            staff |= Q(staff_user__staff_profile__division_id__in=ids)
        chosen |= staff & Q(staff_user__is_active=True)
    return _friends().filter(chosen).order_by("external_id")


def _friends():
    return ChannelIdentity.objects.filter(channel="line", is_following=True).exclude(
        status=ChannelIdentity.Status.REVOKED
    )


def units():
    """หน่วยงาน of the segment picker (ADR 0060 Q5): the หน่วยตามทะเบียน, named as the roster
    names them (ฟขป.), with the registered people (still friends) each reaches now."""
    from operations import employees

    reached = dict(
        _friends()
        .filter(requester__status="active")
        .values_list("requester__district")
        .annotate(n=Count("pk"))
    )
    return [
        {"id": row.pk, "name": row.name, "members": reached.get(row.pk, 0)}
        for row in employees.units()
    ]


def missed(campaign):
    """Who this sent campaign has not reached (ADR 0059 Q5, Q6): the segment's members now, or
    for เพื่อนทั้งหมด the friends the system first saw after LINE took the send (LINE keeps
    no list; one who blocked and came back is missed); less everyone a batch went or may still
    go to. A failed batch reached nobody."""
    if campaign.test_source_id or campaign.status not in SENT:
        return ChannelIdentity.objects.none()
    batches = campaign.batches.exclude(status=BroadcastBatch.Status.FAILED)
    if campaign.audience == "all":
        start = batches.filter(endpoint="narrowcast").aggregate(at=Min("first_attempt_at"))["at"]
        if start is None:
            return ChannelIdentity.objects.none()
        rows = _friends().filter(created_at__gt=start)
    else:
        rows = segment_members(campaign.segment)
    payloads = batches.values_list("payload", flat=True)
    return rows.exclude(external_id__in={i for p in payloads for i in p.get("to", ())})


def missed_counts(campaign):
    """(not reached, of whom not registered: neither an approved requester nor staff)."""
    rows = missed(campaign)
    strangers = rows.exclude(status=ChannelIdentity.Status.ACTIVE).filter(staff_user__isnull=True)
    return rows.count(), strangers.count()


@transaction.atomic
def send_missed(user, campaign_id):
    """ส่งให้คนที่ยังไม่ได้รับ (ADR 0059 Q5): more batches of the same campaign, to who it has not
    reached, under the same quota rules; the editors of the campaign press it."""
    row = BroadcastCampaign.objects.select_for_update().get(pk=campaign_id)
    require_editor(user, row)
    people = sorted(missed(row).values_list("external_id", flat=True))
    if not people:
        raise ValidationError("ไม่มีผู้ที่ยังไม่ได้รับประกาศนี้")
    from operations import broadcast_delivery

    broadcast_delivery.send_more(row, people)
    identity.audit(user, "broadcast.missed_sent", row, recipients=len(people))
    return row


def sender_identity(user):
    row = (
        ChannelIdentity.objects.filter(channel="line", staff_user=user, is_following=True)
        .exclude(status=ChannelIdentity.Status.REVOKED)
        .first()
    )
    if row is None:
        raise ValidationError("กรุณาเชื่อมบัญชี LINE และเพิ่มบัญชีทางการเป็นเพื่อนก่อนส่งประกาศ")
    return row


def recipients(campaign):
    """Resolve at send time. The creator receives one copy even outside the segment."""
    sender = sender_identity(campaign.created_by)
    if campaign.test_source_id:
        return [sender.external_id]
    if campaign.audience == "all":
        return None  # Only the LINE broadcast endpoint knows every OA friend.
    return sorted(
        set(segment_members(campaign.segment).values_list("external_id", flat=True))
        | {sender.external_id}
    )


def _url(value):
    if not isinstance(value, str) or len(value) > 2000:
        raise ValidationError("ลิงก์ไม่ถูกต้อง")
    URLValidator(schemes=["https"])(value)
    return value


def _ratio(content):
    """The picture's own width:height, so LINE shows all of it (LINE: height ≤ 3 × width)."""
    width, height = content.get("width"), content.get("height")
    if type(width) is int and type(height) is int and 0 < width <= 100000 and 0 < height <= 100000:
        return f"{width}:{min(height, 3 * width)}"
    return "20:13"


def build_messages(content):
    if not isinstance(content, dict):
        raise ValidationError("เนื้อหาประกาศไม่ถูกต้อง")
    from operations import broadcast_media

    content = broadcast_media.resolve_content(content)
    kind = content.get("kind")
    text = content.get("text", "")
    if not isinstance(text, str) or len(text) > 5000:
        raise ValidationError("ข้อความต้องไม่เกิน 5,000 ตัวอักษร")
    if kind == "text":
        if not text.strip():
            raise ValidationError("กรุณาระบุข้อความประกาศ")
        return [{"type": "text", "text": text}]
    if kind in {"image", "video"}:
        media = _url(content.get("url"))
        preview = _url(content.get("preview") or (media if kind == "image" else None))
        messages = [{"type": kind, "originalContentUrl": media, "previewImageUrl": preview}]
        return ([{"type": "text", "text": text}] if text.strip() else []) + messages
    if kind in {"click_image", "card"}:
        title = content.get("title", "")
        if not isinstance(title, str) or not title.strip() or len(title) > 100:
            raise ValidationError("กรุณาระบุหัวข้อการ์ดไม่เกิน 100 ตัวอักษร")
        bubble = {
            "type": "bubble",
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [{"type": "text", "text": title, "weight": "bold", "wrap": True}],
            },
        }
        if len(text) > 2000:
            raise ValidationError("ข้อความในการ์ดต้องไม่เกิน 2,000 ตัวอักษร")
        if text.strip():
            bubble["body"]["contents"].append({"type": "text", "text": text, "wrap": True})
        if content.get("url"):
            # "cover" at a fixed 20:13 cut the top and bottom off posters; "fit" never crops.
            bubble["hero"] = {
                "type": "image",
                "url": _url(content["url"]),
                "size": "full",
                "aspectMode": "fit",
                "aspectRatio": _ratio(content),
            }
        if kind == "click_image":
            if "hero" not in bubble:
                raise ValidationError("กรุณาเลือกรูปภาพ")
            link = _url(content.get("link"))
            bubble["hero"]["action"] = {"type": "uri", "uri": link}
            # The admin picks whether a button sits under the picture and what it says.
            # Drafts saved before that choice existed carry neither key: "ดูรายละเอียด".
            buttons = []
            if content.get("show_button", True) is not False:
                buttons = [{"label": content.get("button_label", "ดูรายละเอียด"), "url": link}]
        else:
            buttons = content.get("buttons", [])
            if not isinstance(buttons, list) or not 1 <= len(buttons) <= 3:
                raise ValidationError("กรุณาระบุปุ่มการ์ด 1 ถึง 3 ปุ่ม")
        actions = []
        for button in buttons:
            if (
                not isinstance(button, dict)
                or not isinstance(button.get("label"), str)
                or not 1 <= len(button["label"].strip()) <= 40
            ):
                raise ValidationError("ชื่อปุ่มต้องมี 1 ถึง 40 ตัวอักษร")
            actions.append(
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#16804A",
                    "action": {
                        "type": "uri",
                        "label": button["label"],
                        "uri": _url(button.get("url")),
                    },
                }
            )
        if actions:
            bubble["footer"] = {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": actions,
            }
        return [{"type": "flex", "altText": title, "contents": bubble}]
    raise ValidationError("กรุณาเลือกรูปแบบข้อความ")


@transaction.atomic
def save_draft(
    user, *, title, content, audience="all", segment_id=None, campaign_id=None, revision=None
):
    require_supervisor(user)
    if not isinstance(title, str) or not title.strip() or len(title) > 100:
        raise ValidationError("กรุณาระบุชื่อประกาศไม่เกิน 100 ตัวอักษร")
    if not isinstance(content, dict):
        raise ValidationError("เนื้อหาประกาศไม่ถูกต้อง")
    if audience not in {"all", "segment"}:
        raise ValidationError("กรุณาเลือกกลุ่มผู้รับ")
    segment = BroadcastSegment.objects.get(pk=segment_id) if audience == "segment" else None
    if campaign_id:
        row = BroadcastCampaign.objects.select_for_update().get(pk=campaign_id)
        require_editor(user, row)
        _revision(row, revision)
        if (
            row.test_source_id
            or row.status not in {S.DRAFT, S.PAUSED, S.HELD}
            or row.batches.exists()
        ):
            raise ValidationError("กรุณาเริ่มแก้ไขเพื่อพักการส่งก่อนบันทึก")
        row.revision += 1
    else:
        row = BroadcastCampaign(created_by=user)
    row.title, row.content, row.audience, row.segment = title.strip(), content, audience, segment
    row.messages = []
    row.save()
    identity.audit(user, "broadcast.draft_saved", row, revision=row.revision)
    return row


@transaction.atomic
def begin_edit(user, campaign_id, revision):
    row = BroadcastCampaign.objects.select_for_update().get(pk=campaign_id)
    require_editor(user, row)
    _revision(row, revision)
    if row.test_source_id or row.status not in EDITABLE or row.batches.exists():
        raise ValidationError("ประกาศนี้เริ่มส่งแล้ว ไม่สามารถแก้ไขได้")
    row.status = S.PAUSED
    row.revision += 1
    row.save(update_fields=["status", "revision", "updated_at"])
    identity.audit(user, "broadcast.paused_for_edit", row)
    return row


@transaction.atomic
def confirm(user, campaign_id, revision, scheduled_at=None):
    row = (
        BroadcastCampaign.objects.select_for_update(of=("self",))
        .select_related("created_by", "segment")
        .get(pk=campaign_id)
    )
    require_editor(user, row)
    if row.status in {S.QUEUED, S.SCHEDULED} and revision in {row.revision, row.revision - 1}:
        return row
    _revision(row, revision)
    if row.test_source_id or row.status not in {S.DRAFT, S.PAUSED, S.HELD} or row.batches.exists():
        raise ValidationError("ประกาศนี้ไม่อยู่ในสถานะที่ยืนยันส่งได้")
    if scheduled_at is not None and (
        timezone.is_naive(scheduled_at) or scheduled_at <= timezone.now()
    ):
        raise ValidationError("กรุณาระบุเวลาส่งในอนาคต")
    row.messages = build_messages(row.content)
    recipients(row)
    row.status = S.SCHEDULED if scheduled_at else S.QUEUED
    row.scheduled_at = scheduled_at
    row.confirmed_by, row.confirmed_at = user, timezone.now()
    row.revision += 1
    row.save()
    from operations import broadcast_delivery

    broadcast_delivery.enqueue(row)
    identity.audit(
        user,
        "broadcast.confirmed",
        row,
        revision=row.revision,
        scheduled_at=str(scheduled_at or ""),
    )
    return row


@transaction.atomic
def cancel(user, campaign_id, revision):
    row = BroadcastCampaign.objects.select_for_update().get(pk=campaign_id)
    require_editor(user, row)
    _revision(row, revision)
    if row.status not in EDITABLE or row.batches.exists():
        raise ValidationError("ประกาศนี้เริ่มส่งแล้ว ไม่สามารถยกเลิกได้")
    row.status = S.CANCELLED
    row.revision += 1
    row.save(update_fields=["status", "revision", "updated_at"])
    identity.audit(user, "broadcast.cancelled", row)
    return row


@transaction.atomic
def test_to_self(user, campaign_id, revision, request_key):
    """Snapshot a separate one-recipient job; retries never confirm the real draft."""
    try:
        key = uuid.UUID(str(request_key))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("รหัสการส่งทดสอบไม่ถูกต้อง") from exc
    row = BroadcastCampaign.objects.select_for_update().get(pk=campaign_id)
    require_editor(user, row)
    existing = BroadcastCampaign.objects.filter(test_key=key).first()
    if existing:
        if (
            existing.created_by_id != user.pk
            or existing.test_source_id != row.pk
            or existing.test_revision != revision
        ):
            raise ValidationError("รหัสการส่งทดสอบนี้ถูกใช้กับรายการอื่นแล้ว")
        return existing
    _revision(row, revision)
    if row.test_source_id or row.status not in {S.DRAFT, S.PAUSED, S.HELD} or row.batches.exists():
        raise ValidationError("กรุณาเปิดร่างเพื่อส่งทดสอบก่อนยืนยันประกาศจริง")
    messages = build_messages(row.content)
    sender_identity(user)
    test = BroadcastCampaign.objects.create(
        title=row.title,
        created_by=user,
        confirmed_by=user,
        confirmed_at=timezone.now(),
        content=row.content,
        messages=messages,
        status=S.QUEUED,
        audience=row.audience,
        segment=row.segment,
        test_source=row,
        test_key=key,
        test_revision=revision,
    )
    from operations import broadcast_delivery

    broadcast_delivery.enqueue(test)
    identity.audit(user, "broadcast.test_to_self", test, source=row.pk, revision=revision)
    return test


@transaction.atomic
def copy_draft(user, campaign_id):
    require_supervisor(user)
    row = BroadcastCampaign.objects.select_for_update().get(pk=campaign_id)
    if row.batches.filter(status="unknown").exists():
        raise ValidationError("กรุณารอผลที่ยังไม่ทราบแน่ชัดก่อนคัดลอก เพื่อป้องกันการส่งซ้ำ")
    return save_draft(
        user,
        title=("สำเนา " + row.title)[:100],
        content=row.content,
        audience=row.audience,
        segment_id=row.segment_id,
    )
