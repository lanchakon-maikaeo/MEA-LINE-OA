"""Executable case transition matrix (M02).

Every action lists, per case kind, the statuses it may start from and the statuses it may lead
to. Actions owned by later modules are part of the matrix but disabled until that module
implements their guards; services refuse them instead of pretending success. M06 enables
routing, handoff and priority decisions (operations/routing.py); M07 enables the withdrawal
fulfilment actions (operations/fulfilment.py); M08 the return actions (operations/returns.py);
M09 problems, child work and deadline pauses (operations/problems.py, coordination.py).
Shared lifecycle actions ("M07-M09") arrive per kind: withdrawal with M07, return with M08,
problem with M09.

An action may lead to several statuses when the outcome depends on the line counters (e.g. an
issue leaves the case in progress or waiting for receipt); the handler chooses one of them.
"""

from dataclasses import dataclass

from operations.models import Case

S = Case.Status
K = Case.Kind

# The working lifecycle of each kind (D/E.5). Paused and terminal statuses are shared.
KIND_STATUSES = {
    K.WITHDRAWAL: (
        S.SUBMITTED,
        S.TRIAGE,
        S.AWAITING_APPROVAL,
        S.AWAITING_EXECUTION,
        S.IN_PROGRESS,
        S.AWAITING_CONFIRMATION,
    ),
    K.RETURN: (S.SUBMITTED, S.TRIAGE, S.AWAITING_EXECUTION, S.IN_PROGRESS),
    K.PROBLEM: (S.SUBMITTED, S.TRIAGE, S.IN_PROGRESS, S.AWAITING_CONFIRMATION),
}
PAUSED = frozenset({S.AWAITING_INFO, S.ON_HOLD})
TERMINAL = frozenset({S.CLOSED, S.REJECTED, S.CANCELLED})
OPEN = frozenset(set(S) - TERMINAL)

# Placeholder target: return to the status recorded when the case was paused.
RESUME = "resume"
ENABLED_MODULES = frozenset({"M02", "M06", "M07", "M08", "M09"})
KIND_MODULE = {K.WITHDRAWAL: "M07", K.RETURN: "M08", K.PROBLEM: "M09"}


@dataclass(frozen=True)
class ActionSpec:
    name: str
    label: str
    module: str
    transitions: dict  # kind -> {from_status: frozenset(to_status)}
    reason_required: bool = False

    def enabled_for(self, kind):
        if "-" in self.module:  # shared lifecycle action: each kind arrives with its module
            return KIND_MODULE[kind] in ENABLED_MODULES
        return self.module in ENABLED_MODULES

    @property
    def enabled(self):
        """Usable for at least one kind (the contract also lists the kinds)."""
        return bool(self.enabled_kinds)

    @property
    def enabled_kinds(self):
        return [str(k) for k in K if self.enabled_for(k) and self.transitions.get(k)]

    def targets(self, kind, status):
        return self.transitions.get(kind, {}).get(status, frozenset())


def _per_kind(build):
    return {
        kind: {s: frozenset(t) for s, t in build(kind, KIND_STATUSES[kind]).items()} for kind in K
    }


def _working(statuses):
    """Statuses where someone is responsible for moving the case (not waiting for intake)."""
    return [s for s in statuses if s != S.SUBMITTED] + sorted(PAUSED)


def _same(statuses):
    return {s: {s} for s in statuses}


def _only(kind, mapping):
    return lambda k, _: mapping if k == kind else {}


def _child(closing):
    """Child work happens in any working or paused status; finishing or dropping it may let a
    withdrawal or return close (M09 Q6)."""

    def build(kind, statuses):
        mapping = _same(_working(statuses))
        if closing and kind == K.WITHDRAWAL:
            mapping[S.IN_PROGRESS] = {S.IN_PROGRESS, S.AWAITING_CONFIRMATION, S.CLOSED}
            mapping[S.AWAITING_CONFIRMATION] = {S.AWAITING_CONFIRMATION, S.CLOSED}
        if closing and kind == K.RETURN:
            mapping[S.IN_PROGRESS] = {S.IN_PROGRESS, S.CLOSED}
        return mapping

    return build


ACTIONS = {
    spec.name: spec
    for spec in [
        # --- M02: lifecycle actions that need no authority, stock or routing policy ---------
        ActionSpec(
            "start_triage", "เริ่มคัดกรอง", "M02", _per_kind(lambda k, _: {S.SUBMITTED: {S.TRIAGE}})
        ),
        ActionSpec(
            "claim", "รับเป็นผู้รับผิดชอบเรื่อง", "M02", _per_kind(lambda k, st: _same(_working(st)))
        ),
        ActionSpec(
            "assign", "มอบหมายผู้รับผิดชอบเรื่อง", "M02", _per_kind(lambda k, st: _same(_working(st)))
        ),
        ActionSpec(
            "request_info",
            "ขอข้อมูลเพิ่ม",
            "M02",
            _per_kind(
                lambda k, st: {
                    s: {S.AWAITING_INFO}
                    for s in st
                    if s in {S.TRIAGE, S.AWAITING_APPROVAL, S.AWAITING_EXECUTION, S.IN_PROGRESS}
                }
            ),
            reason_required=True,
        ),
        ActionSpec(
            "info_received", "ได้ข้อมูลแล้ว", "M02", _per_kind(lambda k, _: {S.AWAITING_INFO: {RESUME}})
        ),
        ActionSpec(
            "hold",
            "พักเรื่อง",
            "M02",
            _per_kind(lambda k, st: {s: {S.ON_HOLD} for s in st if s != S.SUBMITTED}),
            reason_required=True,
        ),
        ActionSpec("resume", "ทำต่อ", "M02", _per_kind(lambda k, _: {S.ON_HOLD: {RESUME}})),
        # Requesters may cancel without a reason while submitted/triage; staff always give one.
        ActionSpec(
            "cancel",
            "ยกเลิกเรื่อง",
            "M02",
            _per_kind(lambda k, st: {s: {S.CANCELLED} for s in list(st) + sorted(PAUSED)}),
        ),
        ActionSpec(
            "decline",
            "ไม่รับเรื่อง",
            "M02",
            _per_kind(lambda k, _: {S.TRIAGE: {S.REJECTED}}),
            reason_required=True,
        ),
        # --- Later modules: present in the matrix, refused until their guards exist ----------
        ActionSpec(
            "complete_triage",
            "ส่งเข้าขั้นดำเนินงาน",
            "M06",
            {
                K.WITHDRAWAL: {S.TRIAGE: frozenset({S.AWAITING_APPROVAL})},
                K.RETURN: {S.TRIAGE: frozenset({S.AWAITING_EXECUTION})},
                K.PROBLEM: {S.TRIAGE: frozenset({S.IN_PROGRESS})},
            },
        ),
        ActionSpec(
            "request_handoff", "ส่งต่อเรื่อง", "M06", _per_kind(lambda k, st: _same(_working(st)))
        ),
        ActionSpec(
            "accept_handoff", "ตอบรับงาน", "M06", _per_kind(lambda k, st: _same(_working(st)))
        ),
        ActionSpec(
            "decline_handoff",
            "ไม่ตอบรับงาน",
            "M06",
            _per_kind(lambda k, st: _same(_working(st))),
            reason_required=True,
        ),
        ActionSpec(
            "request_routing_decision",
            "ขอผู้ประสานตัดสิน",
            "M06",
            _per_kind(lambda k, st: _same(_working(st))),
            reason_required=True,
        ),
        ActionSpec(
            "decide_routing",
            "ตัดสินเส้นทาง",
            "M06",
            _per_kind(lambda k, st: _same(_working(st))),
            reason_required=True,
        ),
        ActionSpec(
            "set_priority",
            "ยืนยันระดับความเร่งด่วน",
            "M06",
            _per_kind(lambda k, st: _same(_working(st))),
        ),
        ActionSpec(
            "record_facts",
            "ยืนยัน/แก้ข้อเท็จจริง",
            "M06",
            _per_kind(lambda k, st: _same(_working(st))),
            reason_required=True,
        ),
        # --- M07: withdrawal fulfilment (approval authority, allocation, issue, receipt) ----
        # Lines are decided one by one; the case waits until every line is decided (Q3).
        ActionSpec(
            "approve",
            "อนุมัติ",
            "M07",
            _per_kind(
                _only(
                    K.WITHDRAWAL,
                    {S.AWAITING_APPROVAL: {S.AWAITING_APPROVAL, S.AWAITING_EXECUTION, S.REJECTED}},
                )
            ),
        ),
        ActionSpec(
            "deny_approval",
            "ไม่อนุมัติ",
            "M07",
            _per_kind(_only(K.WITHDRAWAL, {S.AWAITING_APPROVAL: {S.REJECTED}})),
            reason_required=True,
        ),
        ActionSpec(
            "allocate",
            "จัดสรรของขาด",
            "M07",
            _per_kind(_only(K.WITHDRAWAL, _same([S.AWAITING_EXECUTION, S.IN_PROGRESS]))),
            reason_required=True,
        ),
        ActionSpec(
            "issue",
            "จ่ายของ",
            "M07",
            _per_kind(
                _only(
                    K.WITHDRAWAL,
                    {
                        S.AWAITING_EXECUTION: {S.IN_PROGRESS, S.AWAITING_CONFIRMATION},
                        S.IN_PROGRESS: {S.IN_PROGRESS, S.AWAITING_CONFIRMATION},
                    },
                )
            ),
        ),
        ActionSpec(
            "terminate_outstanding",
            "ยุติส่วนค้าง",
            "M07",
            _per_kind(
                _only(
                    K.WITHDRAWAL,
                    {
                        S.AWAITING_EXECUTION: {S.AWAITING_EXECUTION, S.CLOSED},
                        S.IN_PROGRESS: {S.IN_PROGRESS, S.AWAITING_CONFIRMATION, S.CLOSED},
                    },
                )
            ),
            reason_required=True,
        ),
        # A delivery the district reported as not matching (Q18); deliveries made before a
        # cancellation can still be confirmed or settled (Q7).
        ActionSpec(
            "resolve_discrepancy",
            "บันทึกผลส่วนต่างการส่งมอบ",
            "M07",
            _per_kind(
                _only(
                    K.WITHDRAWAL,
                    {
                        S.IN_PROGRESS: {S.IN_PROGRESS, S.AWAITING_CONFIRMATION, S.CLOSED},
                        S.AWAITING_CONFIRMATION: {
                            S.AWAITING_CONFIRMATION,
                            S.IN_PROGRESS,
                            S.CLOSED,
                        },
                        S.CANCELLED: {S.CANCELLED},
                    },
                )
            ),
            reason_required=True,
        ),
        ActionSpec(
            "receive",
            "รับของคืน",
            "M08",
            _per_kind(
                _only(
                    K.RETURN,
                    {S.AWAITING_EXECUTION: {S.IN_PROGRESS}, S.IN_PROGRESS: {S.IN_PROGRESS}},
                )
            ),
        ),
        # --- M08: returns (arrivals, inspection, discrepancies); returns close by themselves --
        ActionSpec(
            "return_instructions",
            "แจ้งวิธีส่งคืน",
            "M08",
            _per_kind(_only(K.RETURN, _same([S.AWAITING_EXECUTION, S.IN_PROGRESS]))),
        ),
        ActionSpec(
            "inspect",
            "ตรวจสภาพ",
            "M08",
            _per_kind(_only(K.RETURN, {S.IN_PROGRESS: {S.IN_PROGRESS, S.CLOSED}})),
        ),
        ActionSpec(
            "decide_inspection",
            "ตัดสินผลตรวจขัดกัน",
            "M08",
            _per_kind(_only(K.RETURN, {S.IN_PROGRESS: {S.IN_PROGRESS, S.CLOSED}})),
            reason_required=True,
        ),
        ActionSpec(
            "resolve_return_discrepancy",
            "บันทึกผลส่วนต่างการรับคืน",
            "M08",
            _per_kind(_only(K.RETURN, {S.IN_PROGRESS: {S.IN_PROGRESS, S.CLOSED}})),
            reason_required=True,
        ),
        ActionSpec(
            "terminate_return",
            "ยุติส่วนค้างส่งคืน",
            "M08",
            _per_kind(
                _only(
                    K.RETURN,
                    {
                        S.AWAITING_EXECUTION: {S.AWAITING_EXECUTION, S.CLOSED},
                        S.IN_PROGRESS: {S.IN_PROGRESS, S.CLOSED},
                    },
                )
            ),
            reason_required=True,
        ),
        # --- M09: problems, child work, deadline pauses -----------------------------------
        ActionSpec(
            "resolve",
            "บันทึกผลแก้ปัญหา",
            "M09",
            _per_kind(_only(K.PROBLEM, {S.IN_PROGRESS: {S.AWAITING_CONFIRMATION}})),
        ),
        ActionSpec(
            "dispute_resolution",
            "โต้แย้งผล",
            "M09",
            _per_kind(_only(K.PROBLEM, {S.AWAITING_CONFIRMATION: {S.IN_PROGRESS}})),
            reason_required=True,
        ),
        ActionSpec("request_child_work", "สร้างงานย่อย", "M09", _per_kind(_child(False))),
        ActionSpec("accept_child_work", "ตอบรับงานย่อย", "M09", _per_kind(_child(False))),
        ActionSpec(
            "decline_child_work",
            "ไม่รับงานย่อย",
            "M09",
            _per_kind(_child(True)),
            reason_required=True,
        ),
        ActionSpec(
            "decide_child_work",
            "ตัดสินงานย่อย",
            "M09",
            _per_kind(_child(False)),
            reason_required=True,
        ),
        ActionSpec("finish_child_work", "บันทึกผลงานย่อย", "M09", _per_kind(_child(True))),
        ActionSpec(
            "cancel_child_work",
            "ยกเลิกงานย่อย",
            "M09",
            _per_kind(_child(True)),
            reason_required=True,
        ),
        ActionSpec(
            "pause_deadlines",
            "พักกรอบเวลา",
            "M09",
            _per_kind(lambda k, _: {S.ON_HOLD: {S.ON_HOLD}}),
            reason_required=True,
        ),
        # Withdrawal: การยืนยันรับของ per delivery, also while more is still to come (Q6).
        ActionSpec(
            "confirm",
            "เขตยืนยันผล",
            "M07-M09",
            {
                K.WITHDRAWAL: {
                    S.IN_PROGRESS: frozenset({S.IN_PROGRESS}),
                    S.AWAITING_CONFIRMATION: frozenset({S.AWAITING_CONFIRMATION, S.CLOSED}),
                    S.CANCELLED: frozenset({S.CANCELLED}),
                },
                K.RETURN: {},
                K.PROBLEM: {S.AWAITING_CONFIRMATION: frozenset({S.CLOSED})},
            },
        ),
        ActionSpec(
            "close",
            "ปิดเรื่อง",
            "M07-M09",
            _per_kind(
                lambda k, st: (
                    {S.IN_PROGRESS: {S.CLOSED}}
                    if k == K.RETURN
                    else {S.AWAITING_CONFIRMATION: {S.CLOSED}}
                )
            ),
        ),
        # Problem cases only (M07 Q16, M08 Q13): later trouble with withdrawn or returned goods
        # is a new problem case, a late return a new return case.
        ActionSpec(
            "reopen",
            "เปิดเรื่องใหม่",
            "M07-M09",
            _per_kind(lambda k, _: {S.CLOSED: {S.IN_PROGRESS}} if k == K.PROBLEM else {}),
            reason_required=True,
        ),
    ]
}

REOPEN_WINDOW_DAYS = 30


def matrix_rows():
    """Flattened matrix for contract export and tests: (action, kind, from, to, module)."""
    rows = []
    for spec in ACTIONS.values():
        for kind, mapping in spec.transitions.items():
            for source, targets in mapping.items():
                for target in sorted(targets):
                    rows.append((spec.name, str(kind), str(source), str(target), spec.module))
    return sorted(rows)
