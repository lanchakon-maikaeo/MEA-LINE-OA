"""Division queues of the staff web (M10 Q6, Q7; round 2 Q1).

Every tab, count, search result and export starts from services.visible_cases, so the web never
shows more than the service layer allows; a tab only narrows that set. All members of a division
see the same cases, and their roles only preset the filters they land on.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from operations import fulfilment, policy, returns, services, workflow
from operations.contracts import Actor
from operations.models import (
    AckClock,
    AuthorityGrant,
    Case,
    CaseTriage,
    ChildWork,
    Division,
    Handoff,
    Priority,
    Role,
    RoutingReview,
)

S = Case.Status
K = Case.Kind
WAITING_DISTRICT = frozenset({S.AWAITING_INFO, S.AWAITING_CONFIRMATION})
CLOSED_DAYS = 30
PAGE_SIZE = 50

TABS = [
    ("host", "งานของกอง"),
    ("incoming", "รอรับงาน"),
    ("child", "งานย่อย"),
    ("decide", "รอฉันตัดสิน"),
    ("mine", "งานของฉัน"),
    ("district", "รอเขตตอบ"),
    ("closed", "ปิดแล้ว"),
]
TAB_LABELS = dict(TABS)

# The staff web names levels in words (user, 8 Oct 2569); P0–P3 stay the stored values.
LEVEL_NAMES = {"P0": "ฉุกเฉิน", "P1": "เร่งด่วน", "P2": "ตามกำหนด", "P3": "ปกติ"}
LEVEL_TITLES = {
    "P0": "ฉุกเฉิน · ต้องคัดกรองทันที",
    "P1": "เร่งด่วน",
    "P2": "ตามกำหนดงาน",
    "P3": "งานปกติ",
}
UNTRIAGED = "รอคัดกรอง"


def level_name(level):
    return LEVEL_NAMES.get(level, UNTRIAGED)


# A role held alone in the division presets the filters of the host tab (round 2 Q1).
ROLE_PRESETS = {
    Role.APPROVER: {"status": S.AWAITING_APPROVAL},
    Role.ISSUER: {"kind": K.WITHDRAWAL, "status": S.AWAITING_EXECUTION},
    Role.RECEIVER: {"kind": K.RETURN, "status": S.AWAITING_EXECUTION},
    Role.INSPECTOR: {"kind": K.RETURN, "status": S.IN_PROGRESS},
    Role.RESOLVER: {"kind": K.PROBLEM, "status": S.IN_PROGRESS},
}

DECISION_ACTIVITIES = (
    AuthorityGrant.Activity.WITHDRAWAL_APPROVAL,
    AuthorityGrant.Activity.ALLOCATION,
    AuthorityGrant.Activity.CONDITION_CERTIFICATION,
)


def visible(user):
    return services.visible_cases(Actor.staff(user))


def decides(user, now=None):
    """Holds an authority that makes cases wait for this user (routing, approval, ...)."""
    if policy.routing_scope(user, now) is not None:
        return True
    return any(
        policy.active_grants(user, activity, now).exists() for activity in DECISION_ACTIVITIES
    )


def _decide_condition(user, scope_admin_all, now):
    routing = policy.routing_scope(user, now)
    condition = Q(pk__in=[])
    if routing is not None or scope_admin_all:
        waiting = services.coordinator_waiting_condition()
        if routing in (None, "all"):
            condition |= waiting
        else:
            condition |= waiting & Q(owner_division__in=routing)
    condition |= fulfilment.authority_waiting_condition(user, now, strict=True)
    condition |= returns.certification_condition(user, now)
    return condition


def tab_condition(tab, division, user, now, admin_all=False):
    """The cases of one tab; `division` None is the admin's every-division view."""
    owner = Q(owner_division=division) if division else Q()
    if tab == "host":
        return owner & Q(status__in=workflow.OPEN - WAITING_DISTRICT)
    if tab == "incoming":
        handoffs = Handoff.objects.filter(case=OuterRef("pk"), status=Handoff.Status.PENDING)
        if division:
            handoffs = handoffs.filter(to_division=division)
        return Q(Exists(handoffs))
    if tab == "child":
        works = ChildWork.objects.filter(case=OuterRef("pk"), status__in=ChildWork.OPEN)
        if division:
            works = works.filter(division=division)
        return Q(Exists(works))
    if tab == "decide":
        return _decide_condition(user, admin_all, now)
    if tab == "mine":
        return owner & Q(assignee=user, status__in=workflow.OPEN)
    if tab == "district":
        condition = owner & Q(status__in=WAITING_DISTRICT)
        if division is not None and division.is_intake:
            # Intake staff answer for a district (receipt, accept/dispute) whoever owns the case.
            condition |= fulfilment.receipt_condition()
        return condition
    if tab == "closed":
        return owner & Q(
            status__in=workflow.TERMINAL, ended_at__gte=now - timedelta(days=CLOSED_DAYS)
        )
    raise ValueError(tab)


def tabs_for(user, division, now, admin_all=False):
    """Tabs this viewer has, in order (the decision tab only for authority holders)."""
    keys = [key for key, _ in TABS]
    if not admin_all and not decides(user, now):
        keys.remove("decide")
    return keys


@dataclass
class Filters:
    kind: str = ""
    priority: str = ""
    status: str = ""
    district: str = ""
    section: str = ""  # a แผนก pk, or "none" for cases without one (real structure Q12)
    overdue: bool = False
    preset: bool = False

    @property
    def active(self):
        return bool(
            self.kind
            or self.priority
            or self.status
            or self.district
            or self.section
            or self.overdue
        )

    def query(self, **extra):
        params = {
            "kind": self.kind,
            "priority": self.priority,
            "status": self.status,
            "district": self.district,
            "section": self.section,
            "overdue": "1" if self.overdue else "",
            **extra,
        }
        return {k: v for k, v in params.items() if v}


def read_filters(params, roles):
    """Filters from the query string; a lone duty role presets them on a bare visit."""
    keys = ("kind", "priority", "status", "district", "section", "overdue", "tab", "page")
    if not any(params.get(k) for k in keys) and not params.get("clear"):
        duties = set(roles) - {Role.STAFF, Role.SUPERVISOR}
        if len(roles) == 1 and len(duties) == 1:
            preset = ROLE_PRESETS.get(next(iter(duties)))
            if preset:
                return Filters(**preset, preset=True)
    priority = params.get("priority", "")
    section = params.get("section") or ""
    return Filters(
        kind=params.get("kind", "") if params.get("kind") in K.values else "",
        priority=priority if priority in [*Priority.values, "none"] else "",
        status=params.get("status", "") if params.get("status") in S.values else "",
        district=params.get("district", "") if (params.get("district") or "").isdigit() else "",
        section=section if section == "none" or section.isdigit() else "",
        overdue=params.get("overdue") == "1",
    )


def apply_filters(cases, filters):
    if filters.kind:
        cases = cases.filter(kind=filters.kind)
    if filters.status:
        cases = cases.filter(status=filters.status)
    if filters.district:
        cases = cases.filter(district_id=int(filters.district))
    if filters.section == "none":
        cases = cases.filter(section__isnull=True)
    elif filters.section:
        cases = cases.filter(section_id=int(filters.section))
    if filters.priority == "none":
        cases = cases.filter(
            Q(triage__isnull=True) | Q(triage__effective_priority="", triage__suggested_priority="")
        )
    elif filters.priority:
        cases = cases.filter(
            Q(triage__effective_priority=filters.priority)
            | Q(triage__effective_priority="", triage__suggested_priority=filters.priority)
        )
    if filters.overdue:
        cases = cases.filter(
            Exists(
                AckClock.objects.filter(
                    case=OuterRef("pk"),
                    stopped_at__isnull=True,
                    paused_at__isnull=True,
                    due_at__lte=timezone.now(),
                )
            )
        )
    return cases


def counts(user, division, now, keys, admin_all=False):
    base = visible(user)
    return {
        key: base.filter(tab_condition(key, division, user, now, admin_all)).count() for key in keys
    }


def overdue_count(user, division, now, admin_all=False):
    cases = visible(user).filter(tab_condition("host", division, user, now, admin_all))
    return apply_filters(cases, Filters(overdue=True)).count()


# --- Rows ---------------------------------------------------------------------------------------


@dataclass
class Row:
    case: Case
    level: str
    summary: str
    clock: AckClock | None = None
    remaining: int | None = None
    overdue: bool = False
    escalated: bool = False
    flags: list = field(default_factory=list)
    handoff: dict | None = None


def level_of(triage):
    if triage is None:
        return ""
    return triage.effective_priority or triage.suggested_priority


def summary_text(case):
    text = case.description or case.purpose or case.return_reason or case.intake_note
    text = " ".join(text.split())
    lines = ", ".join(
        f"{line.item.code} ×{line.requested_quantity.normalize():f}"
        if line.requested_quantity is not None
        else line.item.code
        for line in case.lines.all()
    )
    if text and lines:
        return f"{lines} — {text}"
    return text or lines


def handoff_info(handoff):
    """A pending handoff as staff read it: the target, the typed reason and its category, and
    the division the rules proposed when the person chose another one (B.6)."""
    recommendation = handoff.recommendation
    proposed = recommendation.proposed_owner if recommendation else None
    if recommendation is not None:
        document = recommendation.policy_version.document
    else:
        current = policy.current_policy()
        document = current.doc if current else {}
    labels = document.get("handoff_reasons", {})
    return {
        "to": handoff.to_division.code,
        "from": handoff.from_division.code,
        "by": handoff.requested_by,
        "at": handoff.requested_at,
        "reason": handoff.reason,
        "category": labels.get(handoff.reason_category, handoff.reason_category),
        "proposed": proposed.code if proposed is not None else "",
        "against_proposal": proposed is not None and proposed.pk != handoff.to_division_id,
    }


def remaining_minutes(clock, now, calendar):
    """Service minutes left (negative when past due) on the policy's calendar."""
    if clock.due_at >= now:
        return calendar.minutes_between(now, clock.due_at, clock.priority)
    return -calendar.minutes_between(clock.due_at, now, clock.priority)


def calendar_now():
    current = policy.current_policy()
    from operations.servicetime import Calendar

    return current.calendar if current else Calendar()


def _answered_after_request(ids):
    """Cases waiting for information whose requester answered in LINE after the last request."""
    if not ids:
        return set()
    from operations.models import ConversationMessage, Decision

    asked = {}
    for case_id, at in Decision.objects.filter(case_id__in=ids, action="request_info").values_list(
        "case_id", "created_at"
    ):
        asked[case_id] = max(at, asked.get(case_id, at))
    answered = set()
    for case_id, at in ConversationMessage.objects.filter(case_id__in=ids).values_list(
        "case_id", "created_at"
    ):
        if case_id in asked and at > asked[case_id]:
            answered.add(case_id)
    return answered


def build_rows(cases, now):
    """Queue rows for a page of cases (a few queries for the whole page)."""
    cases = list(cases)
    ids = [c.pk for c in cases]
    triages = {
        t.case_id: t
        for t in CaseTriage.objects.filter(case_id__in=ids).select_related("current_recommendation")
    }
    clocks = {}
    for clock in AckClock.objects.filter(case_id__in=ids, stopped_at__isnull=True).select_related(
        "division"
    ):
        clocks.setdefault(clock.case_id, []).append(clock)
    handoffs = {
        h.case_id: handoff_info(h)
        for h in Handoff.objects.filter(
            case_id__in=ids, status=Handoff.Status.PENDING
        ).select_related(
            "to_division",
            "from_division",
            "requested_by__staff_profile",
            "recommendation__proposed_owner",
            "recommendation__policy_version",
        )
    }
    reviews = set(
        RoutingReview.objects.filter(case_id__in=ids, status=RoutingReview.Status.OPEN).values_list(
            "case_id", flat=True
        )
    )
    children = {}
    for work in ChildWork.objects.filter(case_id__in=ids, status__in=ChildWork.OPEN).select_related(
        "division"
    ):
        children.setdefault(work.case_id, []).append(work)
    answered = _answered_after_request(
        [c.pk for c in cases if c.status == Case.Status.AWAITING_INFO]
    )
    calendar = calendar_now()
    rows = []
    for case in cases:
        triage = triages.get(case.pk)
        open_clocks = clocks.get(case.pk, [])
        running = [c for c in open_clocks if c.paused_at is None]
        nearest = min(running or open_clocks, key=lambda c: c.due_at, default=None)
        row = Row(
            case=case,
            level=level_of(triage),
            summary=summary_text(case),
            clock=nearest,
            remaining=remaining_minutes(nearest, now, calendar) if nearest and running else None,
            # Past due now (a later policy may have moved a reminded deadline).
            overdue=any(c.due_at <= now for c in running),
            escalated=any(c.escalated_at for c in open_clocks),
        )
        recommendation = triage.current_recommendation if triage else None
        if triage and triage.priority_floor == "P0":
            row.flags.append(("alert", "แจ้งเหตุอันตราย"))
        if row.escalated:
            row.flags.append(("alert", "ยกระดับแล้ว"))
        elif row.overdue:
            row.flags.append(("alert", "เกินกรอบเวลา"))
        if case.pk in answered:
            row.flags.append(("info", "ผู้แจ้งตอบข้อมูลแล้ว"))
        if case.pk in handoffs:
            row.handoff = handoffs[case.pk]
            row.flags.append(("", f"รอ {row.handoff['to']} ตอบรับ"))
            if row.handoff["against_proposal"]:
                row.flags.append(("info", f"ส่งต่างจากที่ระบบเสนอ ({row.handoff['proposed']})"))
        if case.pk in reviews:
            row.flags.append(("", "รอผู้ประสานตัดสิน"))
        for work in children.get(case.pk, []):
            row.flags.append(("", f"งานย่อย {work.division.code}: {work.get_status_display()}"))
        if recommendation is not None:
            if recommendation.missing:
                row.flags.append(("info", "ข้อมูลยังไม่ครบ"))
            if recommendation.case_revision != case.revision:
                row.flags.append(("info", "ข้อเสนอยังไม่อัปเดต"))
        if triage and triage.policy_change_pending:
            row.flags.append(("info", "กติกาใหม่เสนอต่างจากเดิม"))
        rows.append(row)
    return rows


def ordered(cases, tab):
    """B.4 order (level → date needed → arrival); closed cases newest first."""
    cases = list(cases.select_related("triage"))
    if tab == "closed":
        return sorted(cases, key=lambda c: c.ended_at or c.updated_at, reverse=True)

    def key(case):
        triage = getattr(case, "triage", None)
        if triage is None:
            return (policy.rank(""), case.needed_by or date.max, case.submitted_at)
        return policy.queue_key(triage, case)

    return sorted(cases, key=key)


def page(cases, tab, number):
    items = ordered(cases, tab)
    total = len(items)
    pages = max(1, -(-total // PAGE_SIZE))
    number = min(max(1, number), pages)
    start = (number - 1) * PAGE_SIZE
    return items[start : start + PAGE_SIZE], {"number": number, "pages": pages, "total": total}


def prefetch(cases):
    return cases.select_related(
        "district", "owner_division", "section", "assignee__staff_profile"
    ).prefetch_related("lines__item")


def own_divisions(scope):
    """Divisions a user may open a queue for: their own, or every active one for an admin."""
    divisions = Division.objects.filter(is_active=True).order_by("code")
    if scope.is_admin or scope.is_support:
        return list(divisions)
    return list(divisions.filter(pk__in=list(scope.division_roles)))
