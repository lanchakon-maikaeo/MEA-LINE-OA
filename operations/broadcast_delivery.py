"""Durable campaign sending. Network calls run outside the reservation transaction."""

import uuid
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max, Sum
from django.utils import timezone

from operations import broadcasts, identity, line_api, notifications
from operations.models import (
    BroadcastBatch,
    BroadcastCampaign,
    ChannelIdentity,
    NotificationDelivery,
    OutboxEvent,
)

C = BroadcastCampaign.Status
B = BroadcastBatch.Status
PREPARE = "broadcast.prepare"
SEND = "broadcast.send_batch"


class RetryBroadcast(Exception):
    def __init__(self, retry_after=30):
        self.retry_after = retry_after
        super().__init__("Broadcast is awaiting LINE")


def enqueue(campaign):
    now = timezone.now()
    OutboxEvent.objects.get_or_create(
        event_id=uuid.uuid5(uuid.NAMESPACE_URL, f"broadcast:{campaign.pk}:{campaign.revision}"),
        defaults={
            "event_type": PREPARE,
            "subject_type": "broadcast",
            "subject_id": str(campaign.pk),
            "correlation_id": uuid.uuid4(),
            "payload": {"campaign": campaign.pk, "revision": campaign.revision},
            "occurred_at": now,
            "next_attempt_at": campaign.scheduled_at or now,
        },
    )


def _notice(campaign, event):
    sender = ChannelIdentity.objects.filter(channel="line", staff_user=campaign.created_by).first()
    notifications._create(
        event,
        "broadcast_status",
        NotificationDelivery.Level.CRITICAL,
        sender,
        f"ประกาศ {campaign.title}: {campaign.get_status_display()}\n{campaign.detail}\n"
        f"{settings.STAFF_WEB_BASE_URL.rstrip('/')}/broadcasts/{campaign.pk}/",
    )


def _held(campaign, detail, event):
    campaign.status, campaign.detail = C.HELD, detail
    campaign.save(update_fields=["status", "detail", "updated_at"])
    identity.audit(None, "broadcast.held", campaign, detail, actor_label="system:broadcast")
    _notice(campaign, event)


def _outbound_problem(campaign, recipients):
    if not notifications.push_enabled():
        return "ระบบยังไม่เปิดการส่งข้อความ LINE"
    if settings.APP_ENV != "production":
        if recipients is None:
            return "การส่งถึงเพื่อนทั้งหมดเปิดใช้เฉพาะระบบจริง"
        if set(recipients) - settings.LINE_ALLOWED_RECIPIENTS:
            return "ผู้รับบางรายไม่ได้อยู่ในรายชื่อที่อนุญาตสำหรับระบบทดสอบ"
    return ""


def prepare(event):
    """Resolve dynamic recipients, then reserve the whole campaign before creating batches."""
    row = BroadcastCampaign.objects.get(pk=event.payload["campaign"])
    if row.revision != event.payload["revision"] or row.status not in {C.QUEUED, C.SCHEDULED}:
        return
    now = timezone.now()
    if row.scheduled_at and row.scheduled_at > now:
        raise RetryBroadcast(int((row.scheduled_at - now).total_seconds()))
    try:
        broadcasts.require_supervisor(row.created_by)
        broadcasts.require_editor(row.confirmed_by, row)
        people = broadcasts.recipients(row)
        problem = _outbound_problem(row, people)
    except (PermissionDenied, ValidationError):
        problem = "สิทธิ์ผู้ส่งหรือบัญชี LINE เปลี่ยนแปลง กรุณาตรวจสอบก่อนยืนยันใหม่"
    if not problem:
        # Manual OA Manager traffic is part of this same quota.
        notifications.reconcile(now)
    with transaction.atomic():
        row = BroadcastCampaign.objects.select_for_update().get(pk=row.pk)
        if row.revision != event.payload["revision"] or row.status not in {C.QUEUED, C.SCHEDULED}:
            return
        if problem:
            _held(row, problem, event)
            return
        try:
            broadcasts.require_supervisor(row.created_by)
            broadcasts.require_editor(row.confirmed_by, row)
            people = broadcasts.recipients(row)
            problem = _outbound_problem(row, people)
        except (PermissionDenied, ValidationError):
            problem = "สิทธิ์ผู้ส่งหรือบัญชี LINE เปลี่ยนแปลง กรุณาตรวจสอบก่อนยืนยันใหม่"
        if problem:
            _held(row, problem, event)
            return
        notifications.lock_budget(now)
        state = notifications.budget_state(now)
        available = max(0, state.budget - state.used)
        if state.limit_reached or not available or (people is not None and len(people) > available):
            _held(row, "โควตาไม่เพียงพอสำหรับผู้รับทั้งกลุ่ม ยังไม่มีการส่งประกาศนี้", event)
            return
        if people is None:
            # No recipient/filter means all friends. LINE cancels the entire delivery if the
            # real audience exceeds this cap (Messaging API limit.forbidPartialDelivery).
            batches = [
                (
                    "narrowcast",
                    {
                        "messages": row.messages,
                        "limit": {
                            "max": available,
                            "upToRemainingQuota": True,
                            "forbidPartialDelivery": True,
                        },
                    },
                    available,
                )
            ]
            row.recipient_count = None
        else:
            batches = _multicast(row.messages, people)
            row.recipient_count = len(people)
        _queue(row, batches, now, event.correlation_id)
        row.status, row.detail = C.SENDING, ""
        row.save(update_fields=["status", "detail", "recipient_count", "updated_at"])


def _multicast(messages, people):
    return [
        ("multicast", {"messages": messages, "to": people[i : i + 500]}, len(people[i : i + 500]))
        for i in range(0, len(people), 500)
    ]


def _queue(campaign, batches, now, correlation_id, first=0):
    for index, (endpoint, payload, count) in enumerate(batches, first):
        batch = BroadcastBatch.objects.create(
            campaign=campaign,
            sequence=index,
            endpoint=endpoint,
            payload=payload,
            recipient_count=count,
        )
        OutboxEvent.objects.create(
            event_type=SEND,
            subject_type="broadcast_batch",
            subject_id=str(batch.pk),
            correlation_id=correlation_id,
            payload={"batch": batch.pk},
            occurred_at=now,
        )


def send_more(campaign, people):
    """ADR 0059 Q5, under the campaign lock: reserve the quota for these people, or refuse, and
    queue multicast batches after the campaign's last one. The campaign keeps its status."""
    problem = _outbound_problem(campaign, people)
    if problem:
        raise ValidationError(problem)
    now = timezone.now()
    notifications.lock_budget(now)
    state = notifications.budget_state(now)
    if state.limit_reached or len(people) > max(0, state.budget - state.used):
        raise ValidationError("โควตาข้อความเดือนนี้ไม่พอสำหรับผู้ที่ยังไม่ได้รับทั้งหมด ยังไม่มีการส่ง")
    first = campaign.batches.aggregate(n=Max("sequence"))["n"] + 1
    _queue(campaign, _multicast(campaign.messages, people), now, uuid.uuid4(), first)


def _refresh(campaign, event):
    """Called under the campaign lock; pending/unknown work never masquerades as delivered."""
    before = campaign.status
    states = set(campaign.batches.values_list("status", flat=True))
    partial_result = campaign.batches.filter(result__failureCount__gt=0).exists()
    if states == {B.ACCEPTED} and not partial_result:
        campaign.status = C.ACCEPTED
        campaign.detail = "LINE รับคำขอแล้ว สถานะนี้ไม่ยืนยันว่าผู้รับเปิดอ่านข้อความ"
        campaign.recipient_count = campaign.batches.aggregate(n=Sum("recipient_count"))["n"]
    elif B.FAILED in states or partial_result:
        campaign.status = C.PARTIAL if B.ACCEPTED in states or B.UNKNOWN in states else C.HELD
        campaign.detail = "มีส่วนที่ส่งไม่สำเร็จ กรุณาตรวจผลแต่ละชุดก่อนดำเนินการต่อ"
    else:
        return
    campaign.save(update_fields=["status", "detail", "recipient_count", "updated_at"])
    # A batch sent later to who it missed (ADR 0059) must not repeat a ส่งไม่ครบ notice.
    if campaign.status not in (C.ACCEPTED, before):
        _notice(campaign, event)


def _settle(
    batch_id, attempt, event, *, status, detail="", request_id=None, result=None, count=None
):
    with transaction.atomic():
        batch_info = BroadcastBatch.objects.get(pk=batch_id)
        campaign = BroadcastCampaign.objects.select_for_update().get(pk=batch_info.campaign_id)
        batch = BroadcastBatch.objects.select_for_update().get(pk=batch_id)
        if batch.attempts != attempt or batch.status != B.UNKNOWN:
            return
        batch.status, batch.detail, batch.lease_until = status, detail[:500], None
        if request_id is not None:
            batch.request_id = request_id
        if result is not None:
            batch.result = result
        if count is not None:
            batch.recipient_count = count
        if status == B.ACCEPTED:
            batch.accepted_at = timezone.now()
        batch.save()
        _refresh(campaign, event)


def send_batch(event):
    info = BroadcastBatch.objects.get(pk=event.payload["batch"])
    now = timezone.now()
    with transaction.atomic():
        campaign = BroadcastCampaign.objects.select_for_update().get(pk=info.campaign_id)
        batch = BroadcastBatch.objects.select_for_update().get(pk=info.pk)
        if batch.status in {B.ACCEPTED, B.FAILED}:
            return
        if batch.lease_until and batch.lease_until > now:
            raise RetryBroadcast(120)
        if not notifications.push_enabled():
            raise RetryBroadcast(60)
        if (
            not batch.request_id
            and batch.first_attempt_at
            and now - batch.first_attempt_at >= timedelta(hours=23)
        ):
            campaign.status, campaign.detail = (
                C.PARTIAL,
                "ผลบางชุดยังไม่ทราบ และพ้นเวลาที่ลองส่งซ้ำได้อย่างปลอดภัย กรุณาตรวจสอบกับ LINE",
            )
            campaign.save(update_fields=["status", "detail", "updated_at"])
            _notice(campaign, event)
            return
        if not batch.request_id:
            try:
                broadcasts.require_supervisor(campaign.created_by)
                broadcasts.require_editor(campaign.confirmed_by, campaign)
                problem = _outbound_problem(campaign, batch.payload.get("to"))
            except PermissionDenied:
                problem = "สิทธิ์ผู้ส่งเปลี่ยนแปลง"
            if problem:
                if batch.status == B.RESERVED:
                    batch.status, batch.detail = B.FAILED, problem
                    batch.save(update_fields=["status", "detail"])
                    _refresh(campaign, event)
                else:
                    campaign.status, campaign.detail = C.PARTIAL, problem + " ผลการส่งก่อนหน้ายังไม่ทราบ"
                    campaign.save(update_fields=["status", "detail", "updated_at"])
                    _notice(campaign, event)
                return
        batch.status = B.UNKNOWN
        batch.first_attempt_at = batch.first_attempt_at or now
        batch.attempts += 1
        batch.lease_until = now + timedelta(seconds=120)
        batch.save()
        attempt = batch.attempts
    try:
        if batch.endpoint == "narrowcast" and batch.request_id:
            result = line_api.call(
                "/v2/bot/message/progress/narrowcast?" + urlencode({"requestId": batch.request_id})
            )
            phase = result.get("phase")
            if phase in {"waiting", "sending"}:
                _settle(batch.pk, attempt, event, status=B.UNKNOWN, result=result)
                raise RetryBroadcast(60)
            if phase not in {"succeeded", "failed"}:
                raise OSError("Unknown LINE progress")
            success = result.get("successCount", 0 if phase == "failed" else None)
            failure = result.get("failureCount", 0 if phase == "failed" else None)
            if type(success) is not int or type(failure) is not int or min(success, failure) < 0:
                raise OSError("LINE progress counts missing")
            if phase == "succeeded" and failure == 0:
                _settle(batch.pk, attempt, event, status=B.ACCEPTED, count=success, result=result)
            else:
                # Known successful messages remain counted even when other recipients failed.
                _settle(
                    batch.pk,
                    attempt,
                    event,
                    status=B.ACCEPTED if success else B.FAILED,
                    count=success,
                    result=result,
                    detail="LINE รายงานการส่งไม่ครบ" if success else "LINE ยกเลิกการส่งทั้งกลุ่ม",
                )
            return
        request_id = line_api.campaign_send(batch.endpoint, batch.payload, batch.retry_key)
        if batch.endpoint == "narrowcast":
            if not request_id:
                raise OSError("LINE request ID missing")
            _settle(batch.pk, attempt, event, status=B.UNKNOWN, request_id=request_id)
            raise RetryBroadcast(30)
        _settle(batch.pk, attempt, event, status=B.ACCEPTED, request_id=request_id)
    except line_api.LineApiError as exc:
        if exc.status == 429 and "monthly limit" in exc.detail.lower() and not batch.request_id:
            _settle(
                batch.pk,
                attempt,
                event,
                status=B.FAILED if attempt == 1 else B.UNKNOWN,
                detail="LINE แจ้งว่าโควตารายเดือนไม่เพียงพอ",
                count=0 if attempt == 1 else None,
            )
            with transaction.atomic():
                campaign = BroadcastCampaign.objects.select_for_update().get(pk=campaign.pk)
                campaign.batches.filter(status=B.RESERVED).update(
                    status=B.FAILED, detail="หยุดก่อนส่ง เนื่องจากโควตาไม่เพียงพอ"
                )
                _refresh(campaign, event)
            if attempt > 1:
                raise RetryBroadcast(300) from exc
            return
        if exc.status == 429 or exc.status >= 500:
            _settle(
                batch.pk, attempt, event, status=B.UNKNOWN, detail=f"LINE {exc.status}: รอลองใหม่"
            )
            raise RetryBroadcast(exc.retry_after or 60) from exc
        # A failed progress query cannot prove that an already accepted request failed.
        if batch.request_id:
            _settle(batch.pk, attempt, event, status=B.UNKNOWN, detail="ยังตรวจสอบผลจาก LINE ไม่ได้")
            raise RetryBroadcast(300) from exc
        _settle(
            batch.pk,
            attempt,
            event,
            status=B.FAILED,
            detail=f"LINE ปฏิเสธคำขอ ({exc.status})",
            count=0,
        )
    except OSError as exc:
        _settle(
            batch.pk, attempt, event, status=B.UNKNOWN, detail="ยังไม่ทราบผลจาก LINE ระบบจะตรวจสอบซ้ำ"
        )
        raise RetryBroadcast(60) from exc


def exhausted(event):
    """Dead letters remain visible to the author without discarding unknown delivery counts."""
    with transaction.atomic():
        pk = event.payload.get("campaign")
        if event.event_type == SEND:
            pk = BroadcastBatch.objects.get(pk=event.payload["batch"]).campaign_id
        row = BroadcastCampaign.objects.select_for_update().get(pk=pk)
        if row.status in {C.CANCELLED, C.PAUSED, C.DRAFT}:
            return
        # Sent already, unless a batch to who it missed (ADR 0059) is still waiting.
        if row.status == C.ACCEPTED and not row.batches.exclude(status=B.ACCEPTED).exists():
            return
        row.batches.filter(status=B.RESERVED).update(
            status=B.FAILED, detail="ไม่ได้ส่งก่อนสิ้นสุดการลองดำเนินการ"
        )
        row.status = (
            C.PARTIAL if row.batches.filter(status__in=[B.ACCEPTED, B.UNKNOWN]).exists() else C.HELD
        )
        row.detail = "ระบบลองดำเนินการครบแล้ว กรุณาตรวจสอบสถานะก่อนส่งใหม่"
        row.save(update_fields=["status", "detail", "updated_at"])
        _notice(row, event)
