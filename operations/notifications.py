"""Notifications to requesters (M04): planning, the monthly message budget and push delivery.

Planning turns domain events into NotificationDelivery rows plus `notification.requested`
outbox events (sent later by n8n through the internal API). Sending re-checks the recipient,
applies the budget tiers of C.2.3, batches a recipient's waiting notifications into one push
(at most 5 messages) and records the real outcome. A push that may or may not have reached
LINE is `unknown`, counts against the budget and is retried with the same X-Line-Retry-Key.
"""

import calendar
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from operations import identity as identity_services
from operations import line_api, options, orchestration
from operations.models import (
    BroadcastBatch,
    ChannelIdentity,
    Decision,
    LineQuotaSnapshot,
    NotificationDelivery,
    OutboxEvent,
    RegistrationRequest,
    Requester,
)

logger = logging.getLogger(__name__)
N = NotificationDelivery
ALERT_LEVELS = (50, 80, 95)  # percent of budget; plus "forecast" when the month will overrun

# --- Budget ---------------------------------------------------------------------------------


def month_key(now):
    local = timezone.localtime(now)
    return f"{local.year:04d}-{local.month:02d}"


def _month_start(now):
    return timezone.localtime(now).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _snapshot(now):
    return LineQuotaSnapshot.objects.filter(month=month_key(now)).first()


@dataclass(frozen=True)
class BudgetState:
    budget: int
    used: int
    tier: int
    forecast: int
    limit_reached: bool

    @property
    def percent(self):
        return int(100 * self.used / self.budget) if self.budget else 100


def budget_state(now=None):
    now = now or timezone.now()
    snapshot = _snapshot(now)
    budget = settings.LINE_PUSH_MONTHLY_BUDGET
    if snapshot and snapshot.quota is not None:
        budget = min(budget, snapshot.quota)
    if settings.APP_ENV != "production":
        budget = min(budget, settings.LINE_PUSH_NONPRODUCTION_CAP)
    notices = N.objects.filter(channel=N.Channel.LINE_PUSH, sent_at__gte=_month_start(now))
    batches = BroadcastBatch.objects.all()

    def total(rows, field):
        return rows.aggregate(total=Sum(field))["total"] or 0

    sent = notices.filter(status=N.Status.SENT)
    accepted = batches.filter(
        status=BroadcastBatch.Status.ACCEPTED, accepted_at__gte=_month_start(now)
    )
    unknown = total(notices.filter(status=N.Status.UNKNOWN), "counted") + total(
        batches.filter(status=BroadcastBatch.Status.UNKNOWN), "recipient_count"
    )
    local = total(sent, "counted") + total(accepted, "recipient_count") + unknown
    remote = snapshot.usage if snapshot else 0
    if snapshot and snapshot.checked_at:
        remote += total(sent.filter(sent_at__gte=snapshot.checked_at), "counted")
        remote += total(accepted.filter(accepted_at__gte=snapshot.checked_at), "recipient_count")
        remote += unknown
    reserved = total(batches.filter(status=BroadcastBatch.Status.RESERVED), "recipient_count")
    used = max(local, remote) + reserved
    limit_reached = bool(snapshot and snapshot.limit_reached)
    ratio = used / budget if budget else 1
    if limit_reached or used >= budget:
        tier = 3
    elif ratio >= 0.8:
        tier = 2
    elif ratio >= 0.6:
        tier = 1
    else:
        tier = 0
    local_now = timezone.localtime(now)
    days = calendar.monthrange(local_now.year, local_now.month)[1]
    elapsed = max((now - _month_start(now)).total_seconds() / 86400, 1)
    return BudgetState(budget, used, tier, int(used / elapsed * days), limit_reached)


def lock_budget(now):
    """Serialize quota reservations across normal notifications and campaigns."""
    row, _ = LineQuotaSnapshot.objects.get_or_create(month=month_key(now))
    return LineQuotaSnapshot.objects.select_for_update().get(pk=row.pk)


def allows(level, state):
    """Normal notifications stop at the critical reserve (tier 2); nothing goes out at tier 3."""
    if state.tier >= 3:
        return False
    if level == N.Level.CRITICAL:
        return state.used + 1 <= state.budget
    reserve = state.budget * settings.LINE_PUSH_CRITICAL_RESERVE_PERCENT / 100
    return state.tier < 2 and state.used + 1 <= state.budget - reserve


def push_enabled():
    return bool(
        settings.OUTBOUND_ENABLED
        and settings.LINE_CHANNEL_ACCESS_TOKEN
        and settings.LINE_PUSH_ENABLED
    )


# --- Answers held up by an outage (ADR 0015 Q12) -------------------------------------------

KIND_CATCH_UP = "catch_up"
CATCH_UP_QUIET = timedelta(seconds=45)  # later answers for the same person replace this one
CATCH_UP_ONCE_PER = timedelta(minutes=30)
CATCH_UP_APOLOGY = "ขอโทษที่ตอบช้า ระบบเพิ่งกลับมาใช้งานได้ ข้อความที่ส่งไว้ยังอยู่ครบ"


def plan_catch_up(inbox, messages, now=None):
    """The reply to a message could not use its reply token (LINE redelivered it after an
    outage): push the newest answer instead, once per person per outage, inside the normal
    budget (normal level stops at the 20% reserve). Returns the planned delivery or None."""
    if not messages or options.get(options.CATCH_UP) != "on" or not inbox.source_user_id:
        return None
    identity = ChannelIdentity.objects.filter(
        channel=ChannelIdentity.Channel.LINE, external_id=inbox.source_user_id
    ).first()
    if identity is None:
        return None
    now = now or timezone.now()
    with transaction.atomic():
        recent = N.objects.filter(
            identity=identity,
            kind=KIND_CATCH_UP,
            status__in=[N.Status.SENT, N.Status.UNKNOWN],
            sent_at__gte=now - CATCH_UP_ONCE_PER,
        )
        if recent.exists():
            return None
        N.objects.filter(identity=identity, kind=KIND_CATCH_UP, status=N.Status.PLANNED).update(
            status=N.Status.SKIPPED, detail="superseded by a newer answer"
        )
        batch = list(messages[:4])
        minutes = options.get(options.CATCH_UP_APOLOGY)
        if minutes != "never" and now - inbox.occurred_at >= timedelta(minutes=int(minutes)):
            batch.insert(0, {"type": "text", "text": CATCH_UP_APOLOGY})
        first = None
        for message in batch:
            delivery = N.objects.create(
                kind=KIND_CATCH_UP,
                level=N.Level.NORMAL,
                identity=identity,
                text=str(message.get("text") or message.get("altText") or "")[:5000] or "-",
                message=message,
                send_after=now + CATCH_UP_QUIET,
            )
            first = first or delivery
        OutboxEvent.objects.create(
            event_type=orchestration.EVENT_NOTIFICATION,
            subject_type="delivery",
            subject_id=str(first.pk),
            correlation_id=uuid.uuid4(),
            payload={"delivery_id": first.pk, "kind": KIND_CATCH_UP, "level": N.Level.NORMAL},
            occurred_at=now,
            next_attempt_at=first.send_after,
        )
    return first


# --- Planning --------------------------------------------------------------------------------


def _case_identity(case):
    return ChannelIdentity.objects.filter(
        requester=case.requester, channel=ChannelIdentity.Channel.LINE, status="active"
    ).first()


def _create(event, kind, level, identity, text, case=None, registration=None, message=None):
    if identity is None:
        return None  # Requester without LINE: staff follow up; email arrives later.
    if case is not None and case.is_example:
        return None  # ตัวอย่าง (15 ต.ค. 2569 Q33): nobody gets a message about it
    now = timezone.now()
    delay = timedelta(0)
    if level == N.Level.NORMAL:
        minutes = 60 if budget_state(now).tier == 1 else settings.LINE_PUSH_DEBOUNCE_MINUTES
        delay = timedelta(minutes=minutes)
    try:
        with transaction.atomic():
            delivery = N.objects.create(
                kind=kind,
                level=level,
                identity=identity,
                text=text,
                message=message or {},
                case=case,
                registration=registration,
                source_event=event.event_id,
                send_after=now + delay,
            )
    except IntegrityError:
        return None  # Already planned for this event (worker retried after a crash).
    OutboxEvent.objects.create(
        event_type=orchestration.EVENT_NOTIFICATION,
        subject_type="delivery",
        subject_id=str(delivery.pk),
        case=case,
        case_revision=case.revision if case else None,
        correlation_id=event.correlation_id,
        payload={"delivery_id": delivery.pk, "kind": kind, "level": level},
        occurred_at=now,
        next_attempt_at=delivery.send_after,
    )
    return delivery


def plan_for_event(event):
    """Deliveries for the C.2.3 push table; every other change waits as 'มีอัปเดตใหม่'."""
    if event.event_type == orchestration.EVENT_DECISION:
        decision = (
            Decision.objects.select_related("case")
            .filter(action_id=event.payload.get("action_id"))
            .first()
        )
        if decision is None:
            return None
        case = decision.case
        if decision.action == "request_info":
            text = f"เรื่อง {case.number}: เจ้าหน้าที่ขอข้อมูลเพิ่ม\n{decision.reason}\nตอบกลับในแชตนี้ได้เลย"
            return _create(
                event, "request_info", N.Level.CRITICAL, _case_identity(case), text, case
            )
        if decision.action == "decline":
            text = f"เรื่อง {case.number} ไม่ได้รับเรื่อง\nเหตุผล: {decision.reason}"
            return _create(event, "case_declined", N.Level.NORMAL, _case_identity(case), text, case)
        if decision.action == "cancel" and decision.actor_id is not None:
            text = f"เรื่อง {case.number} ถูกยกเลิกโดยเจ้าหน้าที่\nเหตุผล: {decision.reason}"
            return _create(
                event, "case_cancelled", N.Level.NORMAL, _case_identity(case), text, case
            )
        return _plan_fulfilment(event, decision, case)
    if event.event_type == orchestration.EVENT_REGISTRATION:
        registration = (
            RegistrationRequest.objects.select_related("identity")
            .filter(pk=event.subject_id)
            .first()
        )
        if registration is None:
            return None
        if registration.status == RegistrationRequest.Status.APPROVED:
            text = "ลงทะเบียนเรียบร้อยแล้ว กด 'ส่งเรื่องใหม่' เพื่อแจ้งเรื่องได้เลย"
        elif registration.status == RegistrationRequest.Status.REJECTED:
            text = "คำขอลงทะเบียนไม่ได้รับการรับรอง"
            if registration.decision_reason:
                text += f"\nเหตุผล: {registration.decision_reason}"
            text += "\nพิมพ์ชื่อ-นามสกุลเพื่อส่งคำขอใหม่ได้"
        else:
            return None
        return _create(
            event,
            "registration_result",
            N.Level.NORMAL,
            registration.identity,
            text,
            registration=registration,
        )
    if event.event_type == orchestration.EVENT_CASE_SUBMITTED:
        return _plan_hazard_ack(event)
    return None


def _plan_hazard_ack(event):
    """M09 Q10 / push table P0: a case filed for the requester by staff with a reported hazard
    is acknowledged at once with the emergency guidance (LINE submissions got it by reply)."""
    from operations import policy
    from operations.models import Case

    case = Case.objects.filter(pk=event.subject_id).first()
    if case is None or case.channel == Case.Channel.LINE:
        return None
    guidance = policy.hazard_guidance(case)
    if not guidance:
        return None
    text = f"รับเรื่อง {case.number} แล้ว (เจ้าหน้าที่บันทึกแทน)\nคำแนะนำเร่งด่วน: {guidance}"
    return _create(event, "hazard_ack", N.Level.CRITICAL, _case_identity(case), text, case)


def _plan_fulfilment(event, decision, case):
    """M07 Q15: a delivery asks the district to confirm (critical, with buttons); not approved
    and closed by staff are normal; partial approval waits as 'มีอัปเดตใหม่'. M08 Q12: the first
    return instructions are critical (the district must act); a closed return is normal with
    the per-line result; arrivals and inspections wait as 'มีอัปเดตใหม่'."""
    identity = _case_identity(case)
    if decision.action in ("resolve", "admin_answer"):  # ADR 0030: an admin's answer too
        from operations import problem_chat  # local: builds LINE cards on services

        card = problem_chat.resolution_card(case, identity)
        if card is None:
            return None
        text, message = card
        return _create(
            event, "resolution_request", N.Level.CRITICAL, identity, text, case, message=message
        )
    if decision.action == "return_instructions":
        if not decision.data.get("first"):
            return None
        data = decision.data
        when = " ถึง ".join(x for x in (data.get("from_date"), data.get("to_date")) if x)
        text = f"เรื่อง {case.number}: ส่งของคืนได้ที่ {data['place']}"
        if when:
            text += f"\nช่วงวันที่ {when}"
        if data.get("note"):
            text += f"\n{data['note']}"
        return _create(event, "return_instructions", N.Level.CRITICAL, identity, text, case)
    if (
        case.kind == "return"
        and decision.to_status == "closed"
        and decision.from_status != "closed"
    ):
        from operations import returns

        text = f"เรื่อง {case.number} รับคืนครบแล้ว\n{returns.status_text(case)}"
        return _create(event, "case_closed", N.Level.NORMAL, identity, text, case)
    if decision.action == "issue":
        from operations import receipts  # local: receipts builds LINE cards on conversation

        card = receipts.delivery_card(case, decision.data.get("delivery"), identity)
        if card is None:
            return None
        text, message = card
        return _create(
            event, "receipt_request", N.Level.CRITICAL, identity, text, case, message=message
        )
    if decision.to_status == "rejected" and decision.action in {"approve", "deny_approval"}:
        text = f"กองไม่รับเรื่อง {case.number}"
        if decision.reason:
            text += f"\nเหตุผล: {decision.reason}"
        return _create(event, "case_not_approved", N.Level.NORMAL, identity, text, case)
    staff_closed = decision.actor_id is not None and decision.from_status != decision.to_status
    if staff_closed and decision.to_status == "closed":
        text = f"เรื่อง {case.number} ปิดแล้ว"
        if decision.data.get("outcome"):
            text += f" ({decision.data['outcome']})"
        if decision.reason:
            text += f"\nเหตุผล: {decision.reason}"
        return _create(event, "case_closed", N.Level.NORMAL, identity, text, case)
    return None


# --- Sending -------------------------------------------------------------------------------


@dataclass(frozen=True)
class SendResult:
    outcome: str
    final: bool = True
    retry_after: int | None = None


def _recipient_problem(delivery):
    """Re-checked at send time: ownership, membership, allowlist and follow state (E.6)."""
    if delivery.channel != N.Channel.LINE_PUSH:
        return "channel not enabled"
    if not push_enabled():
        return "push disabled"
    identity = delivery.identity
    if identity is None:
        return "no LINE identity"
    if (
        settings.APP_ENV != "production"
        and identity.external_id not in settings.LINE_ALLOWED_RECIPIENTS
        and options.get(options.LINE_AUDIENCE) != "everyone"
    ):
        return "not in allowlist"
    if not identity.is_following:
        return "unfollowed or blocked"
    if delivery.kind.startswith("koj_"):
        from operations import koj_tracking

        return koj_tracking.recipient_problem(delivery)
    from operations import staff_line  # local: staff_line plans through this module

    if staff_line.is_staff_delivery(delivery):
        return staff_line.recipient_problem(delivery)
    if delivery.case_id is not None:
        requester = delivery.case.requester
        if (
            identity.status != ChannelIdentity.Status.ACTIVE
            or identity.requester_id != requester.pk
            or requester.status != Requester.Status.ACTIVE
        ):
            return "recipient no longer owns the case"
    if delivery.registration_id is not None and delivery.registration.identity_id != identity.pk:
        return "registration belongs to another identity"
    return ""


def _finish(leader, status, detail="", counted=None):
    fields = {"status": status, "detail": detail[:200]}
    N.objects.filter(batch_leader=leader).exclude(pk=leader.pk).update(**fields)
    if counted is not None:
        fields["counted"] = counted
    N.objects.filter(pk=leader.pk).update(**fields)


def send(delivery_id, now=None):
    """Send one planned delivery (with its recipient's other waiting ones) or retry an unknown."""
    now = now or timezone.now()
    if N.objects.filter(pk=delivery_id, channel=N.Channel.EMAIL).exists():
        from operations import staff_email  # M10: staff email shares the outbox path

        return staff_email.send(delivery_id, now)
    with transaction.atomic():
        delivery = (
            N.objects.select_for_update(of=("self",))
            .select_related("identity", "case__requester", "registration")
            .get(pk=delivery_id)
        )
        if delivery.status == N.Status.PLANNED:
            problem = _recipient_problem(delivery)
            if problem:
                delivery.status, delivery.detail = N.Status.SKIPPED, problem
                delivery.save(update_fields=["status", "detail"])
                return SendResult("skipped")
            lock_budget(now)
            state = budget_state(now)
            from operations import staff_line

            over_staff = staff_line.is_staff_delivery(delivery) and not staff_line.budget_allows(
                delivery.level, now
            )
            if over_staff or not allows(delivery.level, state):
                delivery.status, delivery.tier = N.Status.DEFERRED, state.tier
                delivery.detail = f"budget tier {state.tier}"
                delivery.save(update_fields=["status", "tier", "detail"])
                return SendResult("deferred")
            # Debounce: everything else waiting for this person goes in the same request.
            others = list(
                N.objects.select_for_update(skip_locked=True)
                .filter(identity=delivery.identity, channel=N.Channel.LINE_PUSH)
                .filter(status=N.Status.PLANNED)
                .exclude(pk=delivery.pk)
                .order_by("created_at")[:4]
            )
            members = [delivery]
            for other in others:
                problem = _recipient_problem(other)
                if problem:
                    other.status, other.detail = N.Status.SKIPPED, problem
                    other.save(update_fields=["status", "detail"])
                elif not allows(other.level, state) or (
                    staff_line.is_staff_delivery(other)
                    and not staff_line.budget_allows(other.level, now)
                ):
                    other.status, other.tier = N.Status.DEFERRED, state.tier
                    other.detail = "budget limit for batch member"
                    other.save(update_fields=["status", "tier", "detail"])
                else:
                    members.append(other)
            delivery.messages = [
                m.message or {"type": "text", "text": m.text[:5000]} for m in members
            ]
            delivery.counted = 1
            for member in members:
                member.status = N.Status.UNKNOWN
                member.batch_leader = delivery
                member.retry_key = delivery.retry_key
                member.tier = state.tier
                member.attempts += 1
                member.sent_at = now
                member.save()
        elif delivery.status == N.Status.UNKNOWN and delivery.batch_leader_id == delivery.pk:
            # A retry must not disclose a tracking snapshot after access was revoked.
            for member in N.objects.filter(batch_leader=delivery, kind__startswith="koj_"):
                problem = _recipient_problem(member)
                if problem:
                    N.objects.filter(batch_leader=delivery).update(detail=problem)
                    return SendResult("skipped")
            delivery.attempts += 1  # Same request, same retry key: LINE counts it once.
            delivery.save(update_fields=["attempts"])
        else:
            return SendResult("already")
        to, messages, key = delivery.identity.external_id, delivery.messages, delivery.retry_key
    try:
        line_api.push(to, messages, key)
    except line_api.LineApiError as exc:
        if exc.status == 409:
            _finish(delivery, N.Status.SENT, "accepted earlier (retry key)")
            return SendResult("sent")
        if exc.status == 429 and "monthly limit" in exc.detail.lower():
            LineQuotaSnapshot.objects.update_or_create(
                month=month_key(now), defaults={"limit_reached": True}
            )
            _finish(delivery, N.Status.DEFERRED, "monthly limit reached", counted=0)
            return SendResult("deferred")
        if exc.status == 429 or exc.status >= 500:
            N.objects.filter(pk=delivery.pk).update(detail=f"LINE {exc.status}, will retry")
            return SendResult("retry", final=False, retry_after=exc.retry_after)
        _finish(delivery, N.Status.FAILED, f"LINE {exc.status}", counted=0)
        return SendResult("failed")
    except OSError as exc:
        # Timeout: LINE may have accepted it. Stays unknown (counted) and is retried.
        N.objects.filter(pk=delivery.pk).update(detail=type(exc).__name__)
        return SendResult("retry", final=False)
    N.objects.filter(batch_leader=delivery).update(status=N.Status.SENT, detail="")
    return SendResult("sent")


# --- Maintenance (n8n reminder-recovery, hourly) ---------------------------------------------


def reconcile(now=None):
    """Read LINE's own quota and usage so manual OA Manager messages are counted too."""
    now = now or timezone.now()
    quota = line_api.call("/v2/bot/message/quota")
    usage = line_api.call("/v2/bot/message/quota/consumption")
    snapshot, _ = LineQuotaSnapshot.objects.get_or_create(month=month_key(now))
    snapshot.quota = quota.get("value") if quota.get("type") == "limited" else None
    snapshot.usage = int(usage.get("totalUsage") or 0)
    snapshot.checked_at = now
    snapshot.limit_reached = snapshot.quota is not None and snapshot.usage >= snapshot.quota
    snapshot.save(update_fields=["quota", "usage", "checked_at", "limit_reached"])
    return snapshot


def maintenance(now=None):
    now = now or timezone.now()
    report = {"reconciled": False}
    snapshot = _snapshot(now)
    stale = (
        snapshot is None
        or snapshot.checked_at is None
        or now - snapshot.checked_at > timedelta(minutes=55)
    )
    if settings.LINE_CHANNEL_ACCESS_TOKEN and stale:
        try:
            snapshot = reconcile(now)
            report["reconciled"] = True
        except (line_api.LineApiError, OSError) as exc:
            report["reconcile_error"] = type(exc).__name__
    state = budget_state(now)
    snapshot, _ = LineQuotaSnapshot.objects.get_or_create(month=month_key(now))
    reached = [str(level) for level in ALERT_LEVELS if state.percent >= level]
    if state.forecast > state.budget:
        reached.append("forecast")
    new_alerts = [level for level in reached if level not in snapshot.alerted_levels]
    if new_alerts:
        snapshot.alerted_levels = sorted({*snapshot.alerted_levels, *new_alerts})
        snapshot.save(update_fields=["alerted_levels"])
        identity_services.audit(
            None,
            "line.budget_alert",
            snapshot,
            actor_label="system:budget",
            levels=new_alerts,
            used=state.used,
            budget=state.budget,
            forecast=state.forecast,
        )
        logger.warning("LINE push budget at %s%% (%s/%s)", state.percent, state.used, state.budget)
    report.update(budget=asdict(state), alerts=new_alerts, outbox=orchestration.health(now))
    return report
