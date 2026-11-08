"""Actions on the case page (M10 round 1 Q9, round 2 Q6/Q8).

`available` decides which buttons a viewer sees, mirroring the handlers' own checks so the page
does not offer what will be refused; services.perform_action still decides every request.
Each action has a small form (fields) and a parser that turns the posted form into the action
payload of contract v1. Nothing here changes a case by itself.
"""

import re
from dataclasses import dataclass, field

from django.conf import settings
from django.utils import timezone

from operations import (
    ai_keywords,
    ai_routing,
    approvals,
    coordination,
    identity,
    inventory,
    options,
    policy,
    problems,
    queues,
    registry,
    requester_steps,
    returns,
    routing_words,
    typed_lines,
    units,
    workflow,
)
from operations.fulfilment import CANCEL_RESOLUTIONS
from operations.models import (
    VIEWER_ROLES,
    AuthorityGrant,
    Case,
    CaseTriage,
    ChannelIdentity,
    ChildWork,
    Delivery,
    Division,
    ProblemResolution,
    ReturnUnit,
    Role,
    RoleGrant,
    Section,
    WorkGroup,
)

S = Case.Status
K = Case.Kind
A = AuthorityGrant.Activity
U = ReturnUnit

# Button wording on the page (the timeline keeps the matrix labels).
BUTTON_LABELS = {
    "start_triage": "เริ่มคัดกรอง",
    "claim": "รับเป็นผู้รับผิดชอบ",
    "assign": "มอบหมายผู้รับผิดชอบ",
    "accept_assignment": "รับงาน",
    "return_assignment": "ขอคืนงาน",
    "request_info": "ขอข้อมูลเพิ่มจากหน่วยงาน (เขต/ฝ่าย)",
    "info_received": "ได้ข้อมูลครบแล้ว",
    "hold": "พักเรื่องไว้ก่อน",
    "resume": "ทำต่อ",
    "cancel": "ยกเลิกเรื่อง",
    "decline": "ไม่รับเรื่อง",
    "complete_triage": "คัดกรองเสร็จ ส่งดำเนินการ",
    "request_handoff": "ส่งต่อให้กองอื่น",
    "accept_handoff": "รับงานนี้",
    "decline_handoff": "ไม่รับงานนี้",
    "request_routing_decision": "ขอให้ผู้ประสานตัดสิน",
    "decide_routing": "ตัดสินว่ากองใดรับ",
    "set_priority": "ตั้งระดับความเร่งด่วน",
    "set_section": "ระบุแผนก",
    "set_work_group": "ระบุกลุ่มงาน",
    "set_sap_number": "แก้เลข SAP ของคำขอ",
    "record_facts": "แก้ข้อเท็จจริง",
    "approve": "ดำเนินการ",
    "start_work": "เริ่มงานทันที (เรื่องฉุกเฉิน)",
    "skip_screening": "ข้ามขั้นคัดกรอง เข้ารอรับเรื่อง",
    "deny_approval": "ไม่รับเรื่อง",
    "allocate": "จัดสรรของที่มีไม่พอ",
    "issue": "บันทึกการจ่ายของ",
    "terminate_outstanding": "ยุติส่วนที่ค้างจ่าย",
    "resolve_discrepancy": "ปิดส่วนต่างการส่งมอบ",
    "confirm": "บันทึกคำตอบแทนหน่วยงาน (เขต/ฝ่าย)",
    "dispute_resolution": "บันทึกการโต้แย้งแทนหน่วยงาน (เขต/ฝ่าย)",
    "close": "ปิดเรื่อง",
    "reopen": "เปิดเรื่องใหม่",
    "return_instructions": "แจ้งวิธีส่งคืน",
    "receive": "บันทึกรับของคืน",
    "inspect": "บันทึกผลตรวจสภาพ",
    "decide_inspection": "ตัดสินผลตรวจที่ขัดกัน",
    "resolve_return_discrepancy": "ปิดส่วนต่างการรับคืน",
    "terminate_return": "ยุติส่วนที่ค้างส่งคืน",
    "resolve": "บันทึกผลการแก้ปัญหา",
    "admin_send": "ส่งให้กอง",
    "admin_answer": "ตอบเรื่อง",
    "request_child_work": "สร้างงานย่อย",
    "accept_child_work": "รับงานย่อย",
    "decline_child_work": "ไม่รับงานย่อย",
    "decide_child_work": "ตัดสินงานย่อย",
    "finish_child_work": "บันทึกผลงานย่อย",
    "cancel_child_work": "ยกเลิกงานย่อย",
    "pause_deadlines": "พักกรอบเวลา",
}

# ADR 0032: the stock work of M07, M08 and M11, offered only when ตัวเลือกระบบ old_stock_work is on.
STOCK = frozenset(
    {
        "allocate",
        "issue",
        "terminate_outstanding",
        "resolve_discrepancy",
        "receive",
        "inspect",
        "decide_inspection",
        "resolve_return_discrepancy",
        "terminate_return",
    }
)

# Hard to undo: the form asks for an explicit tick before saving (round 1 Q9).
IRREVERSIBLE = frozenset(
    {
        "cancel",
        "decline",
        "deny_approval",
        "terminate_outstanding",
        "terminate_return",
        "cancel_child_work",
        "close",
        "decline_handoff",
        "decline_child_work",
    }
)

# Round 2 Q6: the one black button, first match wins.
PRIMARY_ORDER = [
    "accept_assignment",
    "start_triage",
    "accept_handoff",
    "accept_child_work",
    "decide_routing",
    "admin_answer",
    "decide_child_work",
    "decide_inspection",
    "approve",
    "issue",
    "receive",
    "inspect",
    "resolve",
    "finish_child_work",
    "info_received",
    "resume",
    "confirm",
    "request_handoff",
    "complete_triage",
]

STAFF_CHANNELS = [
    ("phone", "โทรศัพท์"),
    ("letter", "หนังสือ"),
    ("counter", "หน้าเคาน์เตอร์"),
    ("fallback_form", "แบบฟอร์มสำรอง"),
]


# --- What the viewer may do ------------------------------------------------------------------


@dataclass
class Situation:
    """Everything the availability rules look at, read once per page."""

    user: object
    case: Case
    roles: dict
    lines: list
    line_division: dict
    handoff: object
    review_open: bool
    routing: object
    children: list
    deliveries: list
    units: list
    intake_id: int | None
    now: object = field(default_factory=timezone.now)
    support: bool = False  # ผู้ช่วยเหลือระบบ (ADR 0007)
    separation: bool = True  # ตัวเลือกระบบ: consecutive steps by different people (ADR 0009)
    # What a step needs before it can work, so a button is offered only when it can succeed.
    priority_confirmed: bool = False
    r09: bool = False
    close_from: object = None  # when ปิดเรื่อง becomes allowed (no answer for 7 days)
    line: bool = False  # the requester has LINE, so answers there by themselves
    resolver_id: int | None = None  # who recorded the latest ผลการแก้ปัญหา
    sections: list = field(default_factory=list)  # the owner division's active แผนก
    groups: list = field(default_factory=list)  # ADR 0052: the case's แผนก's active กลุ่มงาน
    scope: object = None  # the viewer's own scope, before for_case (ADR 0052)
    overseen: frozenset = frozenset()  # divisions this viewer supervises (16 Oct 2569 Q5)
    admin: bool = False  # ADR 0030: admins send cases to divisions and answer what they hold
    department: bool = False  # ADR 0039: a แอดมินฝ่าย sends the admins' cases too

    def member(self, division_id, roles=None):
        if self.support:
            return True
        if roles is not None and Role.SUPERVISOR in roles and division_id in self.overseen:
            return True
        held = self.roles.get(division_id, frozenset())
        return bool(held if roles is None else held & set(roles))

    def other(self, user_id):
        """Someone else did the earlier step, or the separation of duties does not bind."""
        return self.support or not self.separation or user_id != self.user.pk

    @property
    def owner(self):
        return self.case.owner_division_id

    @property
    def desk(self):
        """Who records a district's answer: the intake desk, or (ADR 0008) the owner."""
        return self.intake_id if self.intake_id is not None else self.owner

    def answers_for_district(self):
        """Mirrors problems/fulfilment: without intake, not the person who resolved it."""
        if not self.member(self.desk):
            return False
        if self.intake_id is None and self.case.kind == K.PROBLEM and self.resolver_id:
            return self.other(self.resolver_id)
        return True

    @property
    def with_intake(self):
        """Still at the intake desk: triage ends in the division that takes it (unless R09)."""
        return self.intake_id is not None and self.owner == self.intake_id and not self.r09

    def grant(self, activity):
        return any(
            g.division_id in (None, self.owner)
            for g in policy.active_grants(self.user, activity, self.now)
        )

    @property
    def routes(self):
        return self.routing == "all" or bool(self.routing and self.owner in self.routing)


def situation(user, case):
    from operations.models import Handoff, RoutingReview

    base = identity.staff_scope(user)
    scope = identity.for_case(base, case)  # ADR 0052: a unit admin's rights in this case
    lines = list(case.lines.select_related("item", "case").order_by("line_no"))
    intake = Division.objects.filter(is_intake=True, is_active=True).first()
    return Situation(
        user=user,
        case=case,
        scope=base,
        roles=dict(scope.division_roles),
        lines=lines,
        line_division={line.line_no: coordination.division_of(line) for line in lines},
        handoff=Handoff.objects.filter(case=case, status=Handoff.Status.PENDING).first(),
        review_open=RoutingReview.objects.filter(
            case=case, status=RoutingReview.Status.OPEN
        ).exists(),
        routing=policy.routing_scope(user),
        children=list(ChildWork.objects.filter(case=case).select_related("division")),
        deliveries=list(
            Delivery.objects.filter(case=case).prefetch_related("lines__line").order_by("seq")
        ),
        units=list(
            U.objects.filter(case=case).select_related("item", "arrival", "line").order_by("seq")
        ),
        intake_id=intake.pk if intake else None,
        support=scope.is_support,
        separation=identity.separated(scope),
        overseen=scope.overseen,
        admin=scope.is_admin or scope.is_support,
        department=scope.is_department_supervisor,
        **_readiness(case, lines),
    )


def _readiness(case, lines):
    """Read-only (never policy.state_for, which locks): the level, R09 and the close date."""
    triage = CaseTriage.objects.filter(case=case).select_related("current_recommendation").first()
    resolved = (
        case.decisions.filter(action__in=problems.RESULT_ACTIONS).order_by("-id").first()
        if case.kind == K.PROBLEM
        else None
    )
    recommendation = triage.current_recommendation if triage else None
    wait = timezone.timedelta(days=settings.RECEIPT_NO_RESPONSE_DAYS)
    close_from = None
    if case.status == Case.Status.AWAITING_CONFIRMATION and case.kind == K.PROBLEM:
        close_from = resolved.created_at + wait if resolved else None
    elif case.kind == K.WITHDRAWAL and not any(line.outstanding > 0 for line in lines):
        deliveries = list(Delivery.objects.filter(case=case))
        waiting = [d.issued_at for d in deliveries if d.status == Delivery.Status.AWAITING]
        if not any(d.status == Delivery.Status.MISMATCH for d in deliveries):
            close_from = max(waiting) + wait if waiting else timezone.now()
    return {
        "priority_confirmed": bool(
            triage
            and triage.confirmed_priority
            and triage.confirmed_division_id == case.owner_division_id
        ),
        "r09": bool(recommendation and "R09" in (recommendation.rules or [])),
        "close_from": close_from,
        "resolver_id": resolved.actor_id if resolved else None,
        "sections": list(
            Section.objects.filter(division_id=case.owner_division_id, is_active=True)
        ),
        "groups": list(WorkGroup.objects.filter(section_id=case.section_id, is_active=True))
        if case.section_id
        else [],
        "line": ChannelIdentity.objects.filter(
            requester_id=case.requester_id,
            channel=ChannelIdentity.Channel.LINE,
            status="active",
        ).exists(),
    }


def _delivery_divisions(delivery):
    return {coordination.division_of(dl.line) for dl in delivery.lines.all()}


def _unit_division(st, unit):
    return st.line_division.get(unit.line.line_no, st.owner) if unit.line_id else st.owner


RULES = {
    "start_triage": lambda st: st.member(st.owner),
    # ADR 0050 Q20: a แอดมินฝ่าย may take a case of a กอง they oversee.
    "claim": lambda st: (
        (st.member(st.owner) or st.member(st.owner, [Role.SUPERVISOR]))
        and st.case.assignee_id != st.user.pk
    ),
    "assign": lambda st: st.member(st.owner, [Role.SUPERVISOR]),
    "accept_assignment": lambda st: _awaits_me(st),
    "return_assignment": lambda st: _awaits_me(st),
    "request_info": lambda st: st.member(st.owner),
    "hold": lambda st: st.member(st.owner),
    "info_received": lambda st: st.member(st.owner),
    "resume": lambda st: st.member(st.owner),
    "cancel": lambda st: (
        st.member(st.owner)
        and (
            not any(line.approved_quantity is not None for line in st.lines)
            or st.grant(A.WITHDRAWAL_APPROVAL)
        )
    ),
    "decline": lambda st: st.member(st.owner),
    "complete_triage": lambda st: (
        st.member(st.owner) and st.handoff is None and not st.review_open and not st.with_intake
    ),
    "request_handoff": lambda st: st.member(st.owner) and st.handoff is None and not st.review_open,
    "accept_handoff": lambda st: st.handoff is not None and st.member(st.handoff.to_division_id),
    "decline_handoff": lambda st: st.handoff is not None and st.member(st.handoff.to_division_id),
    "request_routing_decision": lambda st: (
        not st.review_open
        and (
            st.member(st.owner) or (st.handoff is not None and st.member(st.handoff.to_division_id))
        )
    ),
    "decide_routing": lambda st: st.routes and (st.review_open or st.handoff is not None),
    "set_priority": lambda st: st.member(st.owner),
    "record_facts": lambda st: st.member(st.owner),
    # ADR 0025: every kind; a case without item rows (or not a withdrawal) is approved whole,
    # and so is every case while the stock work is hidden (ADR 0032).
    # ADR 0044: ดำเนินการ is for the owner's แอดมินกอง, a แอดมินฝ่าย, or a granted authority.
    "approve": lambda st: (
        st.other(st.case.submitted_by_id)
        and approvals.may_approve(st.user, st.case)
        and (approvals.whole(st.case) or any(line.approved_quantity is None for line in st.lines))
    ),
    "deny_approval": lambda st: RULES["approve"](st) and st.grant(A.WITHDRAWAL_APPROVAL),
    "start_work": lambda st: False,  # the system starts an emergency itself (ADR 0025)
    "skip_screening": lambda st: False,
    "allocate": lambda st: (
        st.grant(A.ALLOCATION) and any(line.outstanding > 0 for line in st.lines)
    ),
    "issue": lambda st: any(
        line.outstanding > 0 and st.member(st.line_division[line.line_no], [Role.ISSUER])
        for line in st.lines
    ),
    "terminate_outstanding": lambda st: (
        st.grant(A.WITHDRAWAL_APPROVAL) and any(line.outstanding > 0 for line in st.lines)
    ),
    "resolve_discrepancy": lambda st: any(
        d.status == Delivery.Status.MISMATCH
        and st.other(d.issued_by_id)
        and all(st.member(div) for div in _delivery_divisions(d))
        for d in st.deliveries
    ),
    "confirm": lambda st: (
        st.answers_for_district()
        and (
            st.case.kind == K.PROBLEM
            or any(
                d.status == Delivery.Status.AWAITING and st.other(d.issued_by_id)
                for d in st.deliveries
            )
        )
    ),
    "dispute_resolution": lambda st: st.case.kind == K.PROBLEM and st.answers_for_district(),
    # ADR 0052 Q13: a แอดมินแผนก sends their own cases on; a แอดมินกลุ่มงาน keeps the แผนก.
    "set_section": lambda st: (
        bool(st.sections)
        and st.member(st.owner)
        and (not st.scope.unit_only or st.case.section_id in st.scope.sections)
    ),
    "set_work_group": lambda st: (
        bool(st.groups)
        and st.member(st.owner)
        and (not st.scope.unit_only or identity.in_unit(st.scope, st.case))
    ),
    "set_sap_number": lambda st: (
        st.member(st.owner) and bool(typed_lines.sap_label(st.case.kind, st.case.topic))
    ),
    "close": lambda st: (
        st.case.kind in (K.WITHDRAWAL, K.PROBLEM)
        and st.member(st.owner)
        and st.close_from is not None
        and st.close_from <= st.now
    ),
    "reopen": lambda st: (
        st.case.kind == K.PROBLEM
        and st.member(st.owner)
        and st.case.ended_at is not None
        and (st.now - st.case.ended_at).days < workflow.REOPEN_WINDOW_DAYS
    ),
    "return_instructions": lambda st: st.member(st.owner),
    "receive": lambda st: any(
        line.return_outstanding > 0 and st.member(st.line_division[line.line_no], [Role.RECEIVER])
        for line in st.lines
    ),
    "inspect": lambda st: any(
        u.status in (U.Status.AWAITING, U.Status.FINAL)
        and u.line_id
        and not u.open_discrepancy
        and st.other(u.arrival.received_by_id)
        and st.member(_unit_division(st, u), [Role.INSPECTOR])
        for u in st.units
    ),
    "decide_inspection": lambda st: (
        any(u.status == U.Status.CONFLICT for u in st.units) and st.grant(A.CONDITION_CERTIFICATION)
    ),
    "resolve_return_discrepancy": lambda st: any(
        u.open_discrepancy
        and st.other(u.arrival.received_by_id)
        and st.member(_unit_division(st, u))
        for u in st.units
    ),
    "terminate_return": lambda st: (
        st.member(st.owner, [Role.SUPERVISOR])
        and any(line.return_outstanding > 0 for line in st.lines)
    ),
    # The case page offers it for the admins' own cases; any case moves from the tab (Q11).
    "admin_send": lambda st: (
        (st.department or (st.admin and st.case.held_by_admin))
        and st.handoff is None
        and not st.review_open
    ),
    "admin_answer": lambda st: st.admin and st.case.held_by_admin and st.case.kind == K.PROBLEM,
    "resolve": lambda st: (
        st.case.kind == K.PROBLEM
        and not st.case.held_by_admin
        and st.member(st.owner, [Role.RESOLVER])
        and not coordination.blocks_closing(st.case)
    ),
    "request_child_work": lambda st: st.member(st.owner),
    "accept_child_work": lambda st: any(
        c.status == ChildWork.Status.PENDING and st.member(c.division_id) for c in st.children
    ),
    "decline_child_work": lambda st: RULES["accept_child_work"](st),
    "decide_child_work": lambda st: (
        st.routes and any(c.status == ChildWork.Status.DISPUTED for c in st.children)
    ),
    "finish_child_work": lambda st: any(
        c.status == ChildWork.Status.ACCEPTED
        and st.member(c.division_id)
        and not coordination.closes_itself(c)
        for c in st.children
    ),
    "cancel_child_work": lambda st: (
        st.member(st.owner) and any(c.status in ChildWork.OPEN for c in st.children)
    ),
    "pause_deadlines": lambda st: st.member(st.owner, [Role.SUPERVISOR]),
}


@dataclass
class Offer:
    name: str
    label: str
    primary: bool = False
    irreversible: bool = False


def _awaits_me(st):
    return st.case.awaits_acceptance and st.case.assignee_id == st.user.pk


def available(user, case, st=None):
    """Actions this viewer may start now, the primary one first (empty for read-only viewers).
    ADR 0049 Q9: a ผู้รับผิดชอบ who has not pressed รับงาน may only take it or give it back."""
    st = st or situation(user, case)
    names = []
    hidden = set() if options.old_stock() else STOCK
    waiting = _awaits_me(st)
    for name, rule in RULES.items():
        if name in hidden or (waiting and name not in workflow.ACCEPTANCE_STEPS):
            continue
        spec = workflow.ACTIONS.get(name)
        if (
            spec is None
            or not spec.enabled_for(case.kind)
            or not spec.targets(case.kind, case.status)
        ):
            continue
        if rule(st):
            names.append(name)
    primary = _primary(case, names)
    offers = [
        Offer(
            name,
            BUTTON_LABELS.get(name, workflow.ACTIONS[name].label),
            name == primary,
            name in IRREVERSIBLE,
        )
        for name in names
    ]
    return sorted(offers, key=lambda o: (not o.primary, o.irreversible, names.index(o.name)))


@dataclass
class Hint:
    text: str
    at: object = None  # a date the step becomes possible, shown after the text


def hints(st, offers):
    """Why a step the viewer expects is not offered yet, and when it will be."""
    case, names = st.case, {o.name for o in offers}
    out = []
    if case.status == S.TRIAGE and st.with_intake and st.member(st.owner):
        out.append(Hint("เรื่องยังอยู่ที่กองรับเรื่อง ส่งต่อให้กองที่รับผิดชอบก่อน แล้วกองนั้นจะกด “คัดกรองเสร็จ”"))
    waiting = (case.kind == K.PROBLEM and case.status == S.AWAITING_CONFIRMATION) or (
        case.kind == K.WITHDRAWAL
        and any(d.status == Delivery.Status.AWAITING for d in st.deliveries)
    )
    if waiting and (st.member(st.owner) or "confirm" in names):
        # Who answers: the requester in LINE, or staff on their behalf when there is no LINE.
        who = _who(st, names)
        answer = "ตอบผล" if case.kind == K.PROBLEM else "ยืนยันรับของ"
        if st.line:
            out.append(
                Hint(
                    f"ผู้แจ้ง{answer}ใน LINE ได้เอง"
                    + (" กด “ยืนยัน” แล้วเรื่องจะปิดเอง" if case.kind == K.PROBLEM else "")
                )
            )
        else:
            out.append(
                Hint(
                    f"ผู้แจ้งไม่ได้ผูก LINE ไว้ จึง{answer}ใน LINE ไม่ได้ "
                    f"ติดต่อหน่วยงาน (เขต/ฝ่าย)ทางอื่น แล้ว{who}กด “บันทึกคำตอบแทนหน่วยงาน (เขต/ฝ่าย)”"
                )
            )
    close = workflow.ACTIONS["close"]
    if (
        "close" not in names
        and close.enabled_for(case.kind)
        and close.targets(case.kind, case.status)
        and st.member(st.owner)
        and st.close_from is not None
    ):
        if case.kind == K.PROBLEM:
            out.append(Hint("ถ้าหน่วยงาน (เขต/ฝ่าย)ยังไม่ตอบผล ปิดเรื่องเองได้ตั้งแต่", st.close_from))
            if st.line:
                who = _who(st, names)
                out.append(
                    Hint(
                        f"ถ้าหน่วยงาน (เขต/ฝ่าย)แจ้งทางอื่นว่าเรียบร้อยแล้ว {who}กด "
                        "“บันทึกคำตอบแทนหน่วยงาน (เขต/ฝ่าย)” เรื่องจะปิดทันที"
                    )
                )
        else:
            out.append(Hint("ถ้าหน่วยงาน (เขต/ฝ่าย)ยังไม่ยืนยันรับ ปิดเรื่องเองได้ตั้งแต่", st.close_from))
    return out


def _who(st, names):
    """Who should press “บันทึกคำตอบแทนหน่วยงาน (เขต/ฝ่าย)” when this viewer cannot."""
    if "confirm" in names:
        return ""
    return f"ให้ {_intake_code(st)} " if st.intake_id is not None else "ให้เจ้าหน้าที่อีกคนในกอง "


def _intake_code(st):
    return Division.objects.values_list("code", flat=True).get(pk=st.intake_id)


def _primary(case, names):
    if "request_handoff" in names and "complete_triage" in names:
        # A read: never policy.state_for, which locks the row for writers.
        state = (
            CaseTriage.objects.filter(case=case).select_related("current_recommendation").first()
        )
        recommendation = state.current_recommendation if state else None
        elsewhere = recommendation and recommendation.proposed_owner_id != case.owner_division_id
        names = [n for n in names if n != ("complete_triage" if elsewhere else "request_handoff")]
    return next((name for name in PRIMARY_ORDER if name in names), None)


# --- Forms ----------------------------------------------------------------------------------------


@dataclass
class Field:
    name: str
    label: str
    kind: str = "text"  # text, textarea, select, radio, date, number, checks, lines, split, hidden
    required: bool = False
    choices: list = field(default_factory=list)
    initial: object = ""
    help: str = ""
    rows: list = field(default_factory=list)  # for kind "lines"
    columns: list = field(default_factory=list)
    ui_only: bool = False  # helps the page, never part of the payload
    value: object = None  # what the form shows: the posted value, else the initial one


@dataclass
class ActionForm:
    action: str
    label: str
    fields: list
    intro: str = ""
    irreversible: bool = False
    picker: list = field(default_factory=list)  # (value, label, url-query) to switch the subject


def _priority_field(case, start=False):
    """start: ดำเนินการ begins at ปกติ, or at the rule floor (ADR 0044), not at the proposal."""
    proposal = policy.card(case)
    suggested = proposal["priority"]["suggested"] if proposal else ""
    floor = proposal["priority"]["floor"] if proposal else ""
    return Field(
        "priority",
        "ระดับความเร่งด่วน",
        "select",
        True,
        [(p, label) for p, label in _levels()],
        (floor or approvals.NORMAL) if start else suggested,
        help=(
            f"ระบบเสนอ: {queues.level_name(suggested)}"
            if suggested
            else "ระบบยังไม่ได้เสนอระดับ เลือกเองและบอกเหตุผล"
        )
        + (f" · ห้ามต่ำกว่า {queues.level_name(floor)}" if floor else ""),
    )


def _reason(required=False, label="เหตุผล", help_text=""):
    return Field("reason", label + " (ไม่บังคับ)", "textarea", False, help=help_text)


def _members(st):
    """Who may take the case: everyone with a working role in its กอง, the แอดมินแผนก/กลุ่มงาน
    included (ADR 0052 Q16); a unit admin picks among the people of their own unit (Q6)."""
    case = st.case
    grants = (
        RoleGrant.objects.filter(
            division_id=case.owner_division_id, revoked_at__isnull=True, user__is_active=True
        )
        .exclude(role__in=VIEWER_ROLES)  # ADR 0039: nobody assigns work to a viewer
        .select_related("user__staff_profile")
        .order_by("user__username")
    )
    seen, choices = set(), []
    for g in grants:
        if st.scope is not None and not units.in_reach(st.scope, g.user):
            continue
        if g.user_id not in seen:
            seen.add(g.user_id)
            profile = getattr(g.user, "staff_profile", None)
            choices.append((str(g.user_id), profile.display_name if profile else g.user.username))
    return choices


def _divisions(exclude=None, real=False):
    divisions = Division.objects.filter(is_active=True).exclude(pk=exclude)
    if real:  # ADR 0030: the real divisions only, not the demo ones
        divisions = divisions.filter(is_synthetic=False)
    return [(d.code, f"{d.code} {d.name}") for d in divisions.order_by("code")]


def _number(value):
    return f"{value.normalize():f}" if value is not None else ""


def _line_label(line):
    return f"#{line.line_no} {line.item.code} {line.item.name}"


def _pick(items, wanted, key):
    """The subject named in the query (?delivery= / ?unit= / ?child=), else the first one."""
    for item in items:
        if str(getattr(item, key)) == str(wanted):
            return item
    return items[0] if items else None


def build_form(action, st, params, demo=False):
    """The form of one action; `demo` (สาธิตการใช้งาน) lists every line whatever its state or
    the viewer's duty, as an example that is never saved."""
    case, label = st.case, BUTTON_LABELS.get(action, workflow.ACTIONS[action].label)
    spec = workflow.ACTIONS[action]
    form = ActionForm(action, label, [], irreversible=action in IRREVERSIBLE)
    add = form.fields.append

    if action == "assign":
        add(Field("assignee_id", "ผู้รับผิดชอบ", "select", True, _members(st)))
        add(_reason())
    elif action == "request_info":
        add(
            Field(
                "reason",
                "ขอข้อมูลอะไรจากหน่วยงาน (เขต/ฝ่าย)",
                "textarea",
                True,
                help="ข้อความนี้ส่งถึงผู้แจ้งทาง LINE",
            )
        )
    elif action in (
        "hold",
        "decline",
        "deny_approval",
        "decline_handoff",
        "pause_deadlines",
        "request_routing_decision",
        "reopen",
    ):
        add(_reason(True))
    elif action == "return_assignment":
        add(Field("reason", "เหตุผลที่ขอคืนงาน", "textarea", True, help="แอดมินกองเห็นเหตุผลนี้"))
    elif action == "cancel":
        if case.kind == K.WITHDRAWAL and st.deliveries:
            add(
                Field("resolution", "ของที่จ่ายไปแล้ว", "radio", True, list(CANCEL_RESOLUTIONS.items()))
            )
            add(Field("reference", "เลขเอกสารการปรับใน SAP", help="จำเป็นเมื่อปรับใน SAP แล้ว"))
        add(_reason(True))
    elif action == "request_handoff":
        proposal = policy.card(case)
        initial = proposal["proposed_owner"] if proposal else ""
        reasons = (
            policy.current_policy().doc.get("handoff_reasons", {})
            if policy.current_policy()
            else {}
        )
        add(
            Field(
                "to_division",
                "ส่งให้กอง",
                "select",
                True,
                _divisions(case.owner_division_id),
                initial if initial != case.owner_division.code else "",
                help="ดูว่าแต่ละกองดูแลอะไรได้ที่เมนู 'หน้าที่ของกอง'"
                if routing_words.teachable(case)
                else "",
            )
        )
        add(
            Field(
                "reason_category",
                "เหตุผลที่เลือกต่างจากข้อเสนอ",
                "select",
                False,
                [("", "ตรงกับข้อเสนอ ไม่ต้องเลือก"), *reasons.items()],
            )
        )
        add(_reason(False, help_text="จำเป็นเมื่อเลือกกองต่างจากข้อเสนอ"))
        if routing_words.teachable(case):
            add(
                Field(
                    "keywords",
                    "คำสำคัญให้ระบบจำ",
                    initial=ai_routing.keyword_for(case) or ai_keywords.keywords_for(case),
                    help=routing_words.KEYWORDS_HELP,
                )
            )
    elif action == "decide_routing":
        add(Field("division", "กองที่รับเรื่อง", "select", True, _divisions()))
        add(_reason(True))
        if routing_words.teachable(case):
            add(Field("keywords", "คำสำคัญให้ระบบจำ", help=routing_words.KEYWORDS_HELP))
    elif action == "set_sap_number":
        add(
            Field(
                "sap_number",
                typed_lines.sap_label(case.kind, case.topic) or "เลข SAP",
                required=True,
                initial=case.sap_number,
                help="ตัวอักษร ตัวเลข หรือขีด 4 ถึง 20 ตัว ประวัติเรื่องเก็บทั้งเลขเดิมและเลขใหม่",
            )
        )
        add(_reason())
    elif action == "set_work_group":
        add(
            Field(
                "work_group",
                "กลุ่มงานที่ดูแล",
                "select",
                False,
                [("", "ไม่ระบุกลุ่มงาน"), *[(str(x.pk), x.name) for x in st.groups]],
                str(case.work_group_id or ""),
                help="กลุ่มงานในแผนกของเรื่อง แอดมินกลุ่มงานเห็นเฉพาะเรื่องของกลุ่มตน",
            )
        )
        add(_reason())
    elif action == "set_section":
        add(
            Field(
                "section",
                "แผนกที่ดูแล",
                "select",
                False,
                [("", "ไม่ระบุแผนก"), *[(str(x.pk), x.name) for x in st.sections]],
                str(case.section_id or ""),
                help="ใช้แยกคิว รายงาน และงานของแอดมินแผนก เจ้าภาพและกรอบเวลายังเป็นของกอง",
            )
        )
        add(_reason())
    elif action == "set_priority":
        add(_priority_field(case))
        add(_reason(help_text="จำเป็นเมื่อต่างจากที่ระบบเสนอ"))
    elif action == "complete_triage" and not st.priority_confirmed:
        # One step instead of two: the level is confirmed with the end of triage.
        level = _priority_field(case)
        form.intro = "ขั้นนี้ยืนยันระดับความเร่งด่วนไปพร้อมกัน " + (
            "ระบบเลือกระดับที่เสนอไว้ให้แล้ว" if level.initial else "เลือกระดับแล้วบอกเหตุผล"
        )
        add(level)
        add(_reason(help_text="จำเป็นเมื่อระดับต่างจากที่ระบบเสนอ"))
    elif action == "record_facts":
        for name, title in FACT_FIELDS:
            add(Field(f"fact-{name}", title, "select", False, FACT_CHOICES))
        add(_reason(True, help_text="บอกว่ารู้ข้อเท็จจริงนี้จากไหน เช่น หน่วยงาน (เขต/ฝ่าย)แจ้งทางโทรศัพท์"))
    elif action == "approve" and approvals.whole(case):
        # ADR 0025 Q25: one button for the whole case, the level in the same form (ADR 0044).
        if not st.priority_confirmed:
            form.intro = "เริ่มดำเนินการและตั้งระดับความเร่งด่วนไปพร้อมกัน"
            add(_priority_field(case, start=True))
        add(_reason(label="หมายเหตุ"))
    elif action == "approve":
        rows = [
            {
                "no": line.line_no,
                "label": _line_label(line),
                "hint": f"ขอ {_number(line.requested_quantity)} {line.item.unit}",
                "values": {"quantity": _number(line.requested_quantity), "reason": ""},
            }
            for line in st.lines
            if demo or line.approved_quantity is None
        ]
        add(
            Field(
                "lines",
                "จำนวนที่อนุมัติ",
                "lines",
                rows=rows,
                columns=[("quantity", "อนุมัติ", "number"), ("reason", "เหตุผล (ถ้าน้อยกว่าที่ขอ)", "text")],
            )
        )
        add(_reason(label="หมายเหตุรวม"))
    elif action == "allocate":
        rows = [
            {
                "no": line.line_no,
                "label": _line_label(line),
                "hint": f"ค้างจ่าย {_number(line.outstanding)}",
                "values": {"quantity": ""},
            }
            for line in st.lines
            if demo or line.outstanding > 0
        ]
        add(
            Field(
                "lines",
                "จำนวนที่จัดสรรให้เรื่องนี้",
                "lines",
                rows=rows,
                columns=[("quantity", "จัดสรร", "number")],
            )
        )
        add(_reason(True))
    elif action == "issue":
        rows = []
        for line in st.lines:
            if not demo and (
                line.outstanding <= 0
                or not st.member(st.line_division[line.line_no], [Role.ISSUER])
            ):
                continue
            if line.approved_quantity is None:  # demo before approval
                hint = f"ขอ {_number(line.requested_quantity)} {line.item.unit} (ยังไม่อนุมัติ)"
            else:
                hint = f"ค้างจ่าย {_number(line.outstanding)} {line.item.unit}"
            if line.item.is_serialized and inventory.mode() == inventory.FAKE:
                ready = inventory.available_serials(line.item)[:8]
                if ready:
                    hint += " · เลขพร้อมจ่าย: " + ", ".join(ready)
            rows.append(
                {
                    "no": line.line_no,
                    "label": _line_label(line),
                    "hint": hint,
                    "values": {
                        "quantity": _number(line.outstanding or line.requested_quantity),
                        "serials": "",
                    },
                }
            )
        add(Field("recipient_name", "ชื่อผู้รับของ ณ จุดส่งมอบ", required=True))
        if inventory.mode() == inventory.MANUAL:
            add(Field("document_number", "เลขเอกสารจ่าย SAP", required=True))
        add(
            Field(
                "lines",
                "ของที่จ่ายครั้งนี้",
                "lines",
                rows=rows,
                columns=[
                    ("quantity", "จำนวน", "number"),
                    ("serials", "เลขเครื่อง (คั่นด้วยเว้นวรรค)", "text"),
                ],
                help="รายการที่ควบคุมรายชิ้นต้องระบุเลขเครื่องครบตามจำนวน",
            )
        )
        add(_reason(label="หมายเหตุ"))
    elif action == "terminate_outstanding":
        add(
            Field(
                "lines",
                "รายการที่ยุติส่วนค้างจ่าย",
                "checks",
                True,
                [
                    (str(line.line_no), f"{_line_label(line)} · ค้าง {_number(line.outstanding)}")
                    for line in st.lines
                    if demo or line.outstanding > 0
                ],
            )
        )
        add(_reason(True))
    elif action == "resolve_discrepancy":
        mismatched = [d for d in st.deliveries if d.status == Delivery.Status.MISMATCH]
        delivery = _pick(mismatched, params.get("delivery"), "seq")
        form.picker = [(d.seq, f"ส่งมอบครั้งที่ {d.seq}", f"delivery={d.seq}") for d in mismatched]
        add(Field("delivery", "", "hidden", initial=delivery.seq if delivery else ""))
        add(Field("resolution", "ผลการตรวจส่วนต่าง", "radio", True, Delivery.Resolution.choices))
        add(Field("reference", "เลขเอกสารอ้างอิง", help="จำเป็นเมื่อของหายหรือใช้เลขเอกสาร SAP"))
        if delivery:
            rows = [
                {
                    "no": dl.line.line_no,
                    "label": _line_label(dl.line),
                    "hint": f"บันทึกจ่าย {_number(dl.quantity)}",
                    "values": {
                        "quantity": _number(dl.quantity),
                        "serials": " ".join(dl.serials or []),
                    },
                }
                for dl in delivery.lines.select_related("line__item")
            ]
            add(
                Field(
                    "lines",
                    "จำนวนที่จ่ายจริง (เมื่อบันทึกจ่ายผิด)",
                    "lines",
                    rows=rows,
                    columns=[("quantity", "จำนวน", "number"), ("serials", "เลขเครื่อง", "text")],
                )
            )
        add(_reason(True))
    elif action == "confirm":
        if case.kind == K.WITHDRAWAL:
            waiting = [d for d in st.deliveries if d.status == Delivery.Status.AWAITING]
            delivery = _pick(waiting, params.get("delivery"), "seq")
            form.picker = [(d.seq, f"ส่งมอบครั้งที่ {d.seq}", f"delivery={d.seq}") for d in waiting]
            add(Field("delivery", "", "hidden", initial=delivery.seq if delivery else ""))
            add(
                Field(
                    "result",
                    "หน่วยงาน (เขต/ฝ่าย)ได้รับของ",
                    "radio",
                    True,
                    [("full", "ครบตามที่บันทึกจ่าย"), ("mismatch", "ไม่ตรง (กรอกจำนวนที่ได้จริง)")],
                    "full",
                )
            )
            if delivery:
                rows = [
                    {
                        "no": dl.line.line_no,
                        "label": _line_label(dl.line),
                        "hint": f"บันทึกจ่าย {_number(dl.quantity)}",
                        "values": {
                            "quantity": _number(dl.quantity),
                            "serials": " ".join(dl.serials or []),
                        },
                    }
                    for dl in delivery.lines.select_related("line__item")
                ]
                add(
                    Field(
                        "lines",
                        "ที่ได้รับจริง",
                        "lines",
                        rows=rows,
                        columns=[("quantity", "จำนวน", "number"), ("serials", "เลขเครื่อง", "text")],
                    )
                )
            form.intro = "บันทึกคำตอบที่หน่วยงาน (เขต/ฝ่าย)แจ้งทางช่องทางอื่น (ไม่ใช่ LINE)"
        else:
            form.intro = "หน่วยงาน (เขต/ฝ่าย)ยอมรับผลการแก้ปัญหาแล้ว บันทึกแทนหน่วยงาน (เขต/ฝ่าย)"
        add(Field("channel", "หน่วยงาน (เขต/ฝ่าย)แจ้งทาง", "select", True, STAFF_CHANNELS))
        add(Field("intake_note", "บันทึกการรับแจ้ง", "textarea", True, help="เช่น ใครโทรมา เวลาไหน"))
    elif action == "dispute_resolution":
        form.intro = "หน่วยงาน (เขต/ฝ่าย)แจ้งว่ายังไม่เรียบร้อย เรื่องจะกลับไปที่กองเจ้าภาพ"
        add(Field("channel", "หน่วยงาน (เขต/ฝ่าย)แจ้งทาง", "select", True, STAFF_CHANNELS))
        add(Field("intake_note", "บันทึกการรับแจ้ง", "textarea", True))
        add(_reason(True, "หน่วยงาน (เขต/ฝ่าย)แจ้งว่ายังไม่เรียบร้อยอย่างไร"))
    elif action == "close":
        if case.kind == K.WITHDRAWAL:
            form.intro = (
                f"ปิดได้เมื่อหน่วยงาน (เขต/ฝ่าย)ไม่ยืนยันรับเกิน {settings.RECEIPT_NO_RESPONSE_DAYS} วัน"
            )
            add(Field("reference", "เลขเอกสารอ้างอิง", required=True))
        else:
            form.intro = (
                f"ปิดได้เมื่อหน่วยงาน (เขต/ฝ่าย)ไม่ตอบผลเกิน {settings.RECEIPT_NO_RESPONSE_DAYS} วัน"
            )
        add(_reason(True))
    elif action == "return_instructions":
        add(Field("place", "สถานที่ส่งคืน", required=True))
        add(Field("from_date", "ตั้งแต่วันที่", "date"))
        add(Field("to_date", "ถึงวันที่", "date"))
        add(Field("note", "ข้อความเพิ่มเติมถึงหน่วยงาน (เขต/ฝ่าย)", "textarea"))
    elif action == "receive":
        rows = []
        for line in st.lines:
            if not demo and (
                line.return_outstanding <= 0
                or not st.member(st.line_division[line.line_no], [Role.RECEIVER])
            ):
                continue
            hint = f"ค้างส่งคืน {_number(line.return_outstanding)}"
            if line.serials:
                hint += " · เลขที่ขอคืน: " + ", ".join(line.serials)
            rows.append(
                {
                    "no": line.line_no,
                    "label": _line_label(line),
                    "hint": hint,
                    "values": {"quantity": "", "serials": ""},
                    "item": line.item.code,
                }
            )
        add(
            Field(
                "lines",
                "ของที่มาถึงจริง",
                "lines",
                rows=rows,
                columns=[("quantity", "จำนวน", "number"), ("serials", "เลขเครื่องที่มาถึง", "text")],
                help="เว้นว่างรายการที่ยังไม่มา ของนอกคำขอให้นับรวมในรายการที่ตรงรหัส",
            )
        )
        add(Field("carrier_name", "ผู้นำของมาส่ง", required=True))
        if inventory.mode() == inventory.MANUAL:
            add(Field("document_number", "เลขเอกสารรับคืน SAP", required=True))
        add(_reason(label="หมายเหตุ"))
    elif action in ("inspect", "decide_inspection"):
        if action == "inspect":
            units = [
                u
                for u in st.units
                if u.status in (U.Status.AWAITING, U.Status.FINAL)
                and u.line_id
                and not u.open_discrepancy
                and st.other(u.arrival.received_by_id)
            ]
        else:
            units = [u for u in st.units if u.status == U.Status.CONFLICT]
        unit = _pick(units, params.get("unit"), "seq")
        form.picker = [
            (
                u.seq,
                f"หน่วย {u.seq} · {u.item.code} {u.serial or _number(u.quantity)}",
                f"unit={u.seq}",
            )
            for u in units
        ]
        add(Field("unit", "", "hidden", initial=unit.seq if unit else ""))
        if unit:
            form.intro = (
                f"หน่วย {unit.seq}: {unit.item.code} "
                f"{unit.serial or ''} จำนวน {_number(unit.quantity)} · {unit.get_status_display()}"
            )
        add(
            Field(
                "split",
                "ผลตรวจ (รวมต้องเท่ากับจำนวนของหน่วย)",
                "split",
                initial={"ready": "", "repair": "", "other": ""},
                choices=list(returns.OUTCOMES.items()),
            )
        )
        if action == "inspect":
            add(Field("pending", "ยังตรวจไม่เสร็จ", "checks", False, [("1", "บันทึกว่ายังรอตรวจทั้งหน่วย")]))
            add(
                Field(
                    "pending-quantity",
                    "",
                    "hidden",
                    initial=_number(unit.quantity) if unit else "",
                    ui_only=True,
                )
            )
        if inventory.mode() == inventory.MANUAL:
            add(Field("reference", "เลขเอกสารผลตรวจ SAP", required=True))
        add(_reason(action == "decide_inspection", help_text="จำเป็นเมื่อผลเป็น ดำเนินการอื่นตามระเบียบ"))
    elif action == "resolve_return_discrepancy":
        units = [u for u in st.units if u.open_discrepancy and st.other(u.arrival.received_by_id)]
        unit = _pick(units, params.get("unit"), "seq")
        form.picker = [
            (u.seq, f"หน่วย {u.seq} · {u.get_discrepancy_display()}", f"unit={u.seq}") for u in units
        ]
        add(Field("unit", "", "hidden", initial=unit.seq if unit else ""))
        if unit:
            what = unit.serial or _number(unit.quantity)
            form.intro = (
                f"หน่วย {unit.seq}: {unit.item.code} {what} · {unit.get_discrepancy_display()}"
            )
        add(Field("resolution", "ผล", "radio", True, U.Resolution.choices))
        add(Field("reference", "เลขเอกสารอ้างอิง", help="จำเป็นเมื่อไม่รับหรือปรับในทะเบียน"))
        add(Field("replaces", "แทนเลขเครื่อง (ถ้ามี)"))
        add(_reason())
    elif action == "terminate_return":
        add(
            Field(
                "lines",
                "รายการที่ยุติส่วนค้างส่งคืน",
                "checks",
                True,
                [
                    (
                        str(line.line_no),
                        f"{_line_label(line)} · ค้าง {_number(line.return_outstanding)}",
                    )
                    for line in st.lines
                    if demo or line.return_outstanding > 0
                ],
            )
        )
        add(Field("category", "สาเหตุ", "radio", True, list(returns.STOP_CATEGORIES.items())))
        add(Field("reference", "เลขเอกสาร SAP", help="จำเป็นเมื่อสูญหาย"))
        add(_reason(True))
    elif action == "admin_send":
        here = None if case.held_by_admin else case.owner_division_id
        add(Field("division", "ส่งให้กอง", "select", True, _divisions(here, real=True)))
        add(Field("reason", "เหตุผล", "textarea", True))
    elif action in ("resolve", "admin_answer"):
        suggestion = case.topic == Case.Topic.SUGGESTION
        results = dict(ProblemResolution.Category.choices)
        if case.topic == Case.Topic.GENERAL:
            # ADR 0030 Q13: staff say which it is; each kind keeps its own results.
            form.intro = "เลือกว่าเรื่องนี้เป็นปัญหาหรือข้อเสนอแนะ แล้วเลือกผลในกลุ่มนั้น"
            add(
                Field(
                    "as",
                    "เรื่องนี้เป็น",
                    "radio",
                    True,
                    [("problem", "ปัญหา"), ("suggestion", "ข้อเสนอแนะ")],
                )
            )
            choices = [
                (c, f"{group}: {results[c]}")
                for kind, group in (("problem", "ปัญหา"), ("suggestion", "ข้อเสนอแนะ"))
                for c in problems.categories_for(case, kind)
            ]
        else:
            if suggestion:
                form.intro = "ตอบข้อเสนอแนะ ผู้แจ้งจะได้ผลทาง LINE และกดรับทราบได้"
            choices = [(c, results[c]) for c in problems.categories_for(case)]
        add(
            Field(
                "category",
                "ผลการพิจารณา" if suggestion else "ผลที่ได้",
                "radio",
                True,
                choices,
            )
        )
        add(Field("message", "ข้อความถึงผู้แจ้ง", "textarea", True, help="ผู้แจ้งเห็นข้อความนี้ใน LINE"))
        add(Field("internal_note", "บันทึกภายใน (ผู้แจ้งไม่เห็น)", "textarea"))
        add(Field("files", "แนบไฟล์ (JPG, PNG, PDF)", "file", help="ไม่เกิน 5 ไฟล์ ไฟล์ละไม่เกิน 10 MB"))
    elif action == "request_child_work":
        add(Field("division", "กองที่ทำงานย่อย", "select", True, _divisions(case.owner_division_id)))
        add(Field("task", "งานที่ขอ", "textarea", True))
        add(Field("deliverable", "ผลที่ต้องส่งกลับ"))
        add(
            Field(
                "rule",
                "กติกา",
                "select",
                False,
                [
                    ("", "ไม่ระบุ"),
                    ("R07", "ตรวจทะเบียน"),
                    ("R08", "ของขาดหรือหลายเรื่องแย่งของกัน"),
                    ("R09", "หลายกองในเรื่องเดียว"),
                ],
            )
        )
        add(
            Field(
                "lines",
                "รายการที่งานย่อยรับผิดชอบ (เมื่อหลายกองอยู่ในเรื่องเดียว)",
                "checks",
                False,
                [(str(line.line_no), _line_label(line)) for line in st.lines],
            )
        )
        add(_reason())
    elif action in (
        "accept_child_work",
        "decline_child_work",
        "finish_child_work",
        "cancel_child_work",
        "decide_child_work",
    ):
        if action in ("accept_child_work", "decline_child_work"):
            works = [
                c
                for c in st.children
                if c.status == ChildWork.Status.PENDING and st.member(c.division_id)
            ]
        elif action == "finish_child_work":
            works = [
                c
                for c in st.children
                if c.status == ChildWork.Status.ACCEPTED
                and st.member(c.division_id)
                and not coordination.closes_itself(c)
            ]
        elif action == "decide_child_work":
            works = [c for c in st.children if c.status == ChildWork.Status.DISPUTED]
        else:
            works = [c for c in st.children if c.status in ChildWork.OPEN]
        work = _pick(works, params.get("child"), "seq")
        form.picker = [
            (c.seq, f"งานย่อย {c.seq} · {c.division.code}", f"child={c.seq}") for c in works
        ]
        add(Field("child", "", "hidden", initial=work.seq if work else ""))
        if work:
            form.intro = f"งานย่อย {work.seq} ({work.division.code}): {work.task}"
        if action == "decide_child_work":
            add(Field("division", "กองที่ทำงานย่อยนี้", "select", True, _divisions()))
        if action == "finish_child_work":
            add(
                Field(
                    "result",
                    "ผล",
                    "radio",
                    True,
                    [(ChildWork.Status.DONE, "ทำเสร็จ"), (ChildWork.Status.FAILED, "ทำไม่ได้")],
                )
            )
            add(Field("summary", "สรุปผล", "textarea", True))
        add(_reason(action in ("decline_child_work", "cancel_child_work", "decide_child_work")))
    else:
        add(_reason(spec.reason_required, "หมายเหตุ" if not spec.reason_required else "เหตุผล"))
    if action in registry.EVIDENCE_ACTIONS and inventory.mode() == inventory.MANUAL:
        # M11 Q3/Q13: the SAP document this fact cites; the file is optional since 23 Oct 2569
        # (the system is for typing the SAP number).
        add(
            Field(
                "evidence",
                "ไฟล์เอกสาร SAP",
                "file",
                False,
                help="ไม่บังคับ สแกนหรือรูปเอกสาร SAP ที่ตรงกับเลขเอกสาร ได้ถึง 5 ไฟล์ JPG, PNG หรือ PDF",
            )
        )
    _status_notes(form, action, case, spec)
    if st.support and not demo:
        # ADR 0007: every การทำแทนกอง by ผู้ช่วยเหลือระบบ carries a reason.
        reason = next((f for f in form.fields if f.name == "reason"), None)
        if reason is None:
            add(_reason(True, "เหตุผลที่ทำแทนกอง", "เหตุผลไม่บังคับ"))
    if form.irreversible:
        form.fields.append(
            Field(
                "confirm_irreversible", "ยืนยัน", "checks", True, [("1", "ตรวจแล้ว เข้าใจว่าย้อนกลับไม่ได้")]
            )
        )
    return form


def _status_notes(form, action, case, spec):
    """ADR 0028: a status change carries a notice for the requester and an internal note. An
    action whose own reason or message reaches the requester uses that field instead."""
    targets = spec.targets(case.kind, case.status)
    if not any(t != case.status for t in targets):
        return
    own = requester_steps.REASON_IS_NOTICE | requester_steps.MESSAGE_IS_NOTICE
    if action in own:
        for f in form.fields:
            if f.name == "reason" and not f.help:
                f.help = "ผู้แจ้งเห็นข้อความนี้ใน LINE"
    else:
        required = all(requester_steps.notice_required(action, case.status, t) for t in targets)
        form.fields.append(
            Field(
                "notice",
                "ข้อความแจ้งสถานะ",
                "textarea",
                required,
                help="ผู้แจ้งเห็นในรายละเอียดเรื่องใน LINE" + ("" if required else " ไม่บังคับ"),
            )
        )
    if not any(f.name == "internal_note" for f in form.fields):
        form.fields.append(Field("internal_note", "บันทึกภายใน (ผู้แจ้งไม่เห็น)", "textarea"))


def _levels():
    from operations.queues import LEVEL_TITLES

    return list(LEVEL_TITLES.items())


FACT_FIELDS = [
    ("hazard_reported", "แจ้งเหตุอันตราย"),
    ("severe_impact", "ผลกระทบรุนแรง"),
    ("registry_only", "ขอแก้ทะเบียนอย่างเดียว"),
    ("serial_mismatch", "เลขเครื่องหรือจำนวนไม่ตรง"),
    ("shortage", "ของไม่พอ"),
]
FACT_CHOICES = [("", "ไม่เปลี่ยน"), ("true", "ใช่"), ("false", "ไม่ใช่"), ("clear", "ล้างค่าที่ยืนยันไว้")]


# --- Parsing the posted form ------------------------------------------------------------------


SERIAL_SPLIT = re.compile(r"[\s,]+")


def _serials(text):
    return [s for s in SERIAL_SPLIT.split((text or "").strip()) if s]


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


class MissingConfirmation(Exception):
    pass


def payload_from(form, post):
    """Contract-v1 payload from the posted form; empty optional fields are left out."""
    payload = {}
    action = form.action
    for f in form.fields:
        if f.kind == "lines":
            rows = []
            for row in f.rows:
                values = {
                    col: (post.get(f"line-{row['no']}-{col}") or "").strip()
                    for col, _, _ in f.columns
                }
                if not any(values.values()):
                    continue
                entry = {"line_no": row["no"]}
                for col, value in values.items():
                    if col == "serials":
                        entry["serials"] = _serials(value)
                    elif value:
                        entry[col] = value
                if action == "receive":
                    entry = {
                        "item_code": row["item"],
                        **{k: v for k, v in entry.items() if k != "line_no"},
                    }
                rows.append(entry)
            payload["items" if action == "receive" else "lines"] = rows
        elif f.kind == "checks" and f.name == "confirm_irreversible":
            if post.get(f.name) != "1":
                raise MissingConfirmation
        elif f.kind == "checks" and f.name == "pending":
            continue
        elif f.kind == "checks":
            values = [_int(v) for v in post.getlist(f.name) if v]
            if values:
                payload[f.name] = values
        elif f.kind == "split":
            if action == "inspect" and post.get("pending") == "1":
                payload["split"] = {"pending": post.get("pending-quantity") or ""}
                continue
            split = {key: (post.get(f"split-{key}") or "").strip() for key, _ in f.choices}
            payload["split"] = {k: v for k, v in split.items() if v}
        elif f.kind == "file" or f.ui_only:
            continue
        elif f.name.startswith("fact-"):
            value = post.get(f.name, "")
            name = f.name[5:]
            if value == "clear":
                payload.setdefault("clear", []).append(name)
            elif value in ("true", "false"):
                payload.setdefault("facts", {})[name] = value == "true"
        else:
            value = (post.get(f.name) or "").strip()
            if not value:
                continue
            payload[f.name] = (
                _int(value) if f.name in ("delivery", "unit", "child", "assignee_id") else value
            )
    return payload


def bind(form, data=None):
    """Fill each field with the posted value (kept after an error or a conflict) or its initial."""
    for f in form.fields:
        if f.kind == "lines":
            for row in f.rows:
                row["current"] = {
                    col: (
                        data.get(f"line-{row['no']}-{col}", "")
                        if data
                        else row["values"].get(col, "")
                    )
                    for col, _, _ in f.columns
                }
        elif f.kind == "checks":
            f.value = data.getlist(f.name) if data else []
        elif f.kind == "split":
            f.value = (
                {key: data.get(f"split-{key}", "") for key, _ in f.choices}
                if data
                else dict(f.initial or {})
            )
        elif f.kind == "hidden" and f.initial not in ("", None):
            f.value = f.initial  # the subject chosen when the form opened
        else:
            f.value = data.get(f.name, f.initial) if data else f.initial
    return form
