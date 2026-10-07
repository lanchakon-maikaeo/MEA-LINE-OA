"""Executable case transition matrix (M02).

Every action lists, per case kind, the statuses it may start from and the statuses it may lead
to. Actions owned by later modules are part of the matrix but disabled until that module
implements their guards; services refuse them instead of pretending success. M06 enables
routing, handoff and priority decisions (operations/routing.py).
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
ENABLED_MODULES = frozenset({"M02", "M06"})


@dataclass(frozen=True)
class ActionSpec:
    name: str
    label: str
    module: str
    transitions: dict  # kind -> {from_status: frozenset(to_status)}
    reason_required: bool = False

    @property
    def enabled(self):
        return self.module in ENABLED_MODULES

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
        ActionSpec(
            "approve",
            "อนุมัติ",
            "M07",
            _per_kind(_only(K.WITHDRAWAL, {S.AWAITING_APPROVAL: {S.AWAITING_EXECUTION}})),
        ),
        ActionSpec(
            "deny_approval",
            "ไม่อนุมัติ",
            "M07",
            _per_kind(_only(K.WITHDRAWAL, {S.AWAITING_APPROVAL: {S.REJECTED}})),
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
        ActionSpec(
            "inspect",
            "ตรวจสภาพ",
            "M08",
            _per_kind(_only(K.RETURN, {S.IN_PROGRESS: {S.IN_PROGRESS}})),
        ),
        ActionSpec(
            "resolve",
            "บันทึกผลแก้ปัญหา",
            "M09",
            _per_kind(_only(K.PROBLEM, {S.IN_PROGRESS: {S.AWAITING_CONFIRMATION}})),
        ),
        ActionSpec(
            "confirm",
            "เขตยืนยันผล",
            "M07-M09",
            _per_kind(lambda k, st: {S.AWAITING_CONFIRMATION: {S.CLOSED}} if k != K.RETURN else {}),
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
        ActionSpec(
            "reopen",
            "เปิดเรื่องใหม่",
            "M07-M09",
            _per_kind(lambda k, _: {S.CLOSED: {S.IN_PROGRESS}}),
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
