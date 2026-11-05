"""Display data of the case page (M10 Q8; round 2 Q2, Q8).

The proposal (policy.card, AI and rules) and the people's decisions are built apart so the
page can never present a proposal as a decision. Everything here is read-only; actions go
through services.perform_action.
"""

from django.utils import timezone

from operations import (
    coordination,
    flow_runner,
    fulfilment,
    options,
    policy,
    problems,
    queues,
    registry,
    returns,
    typed_lines,
    usage_meters,
    workflow,
)
from operations.models import (
    AckClock,
    AuditEvent,
    Case,
    CaseTriage,
    ChildWork,
    EquipmentType,
    Handoff,
    RoutingReview,
)

K = Case.Kind
S = Case.Status

# The main path of each kind, for the step tracker at the top of the case page. Every case starts
# at รับเรื่อง (ADR 0044); a case from before still in screening shows there too.
RECEIVE = ("รับเรื่อง", {S.SUBMITTED, S.TRIAGE, S.AWAITING_APPROVAL})
JOURNEY = {
    K.PROBLEM: [
        RECEIVE,
        ("แก้ปัญหา", {S.IN_PROGRESS}),
        ("รอหน่วยงาน (เขต/ฝ่าย)ยืนยันผล", {S.AWAITING_CONFIRMATION}),
        ("ปิดเรื่อง", {S.CLOSED}),
    ],
    K.WITHDRAWAL: [
        RECEIVE,
        ("จ่ายของ", {S.AWAITING_EXECUTION, S.IN_PROGRESS}),
        ("รอหน่วยงาน (เขต/ฝ่าย)ยืนยันรับ", {S.AWAITING_CONFIRMATION}),
        ("ปิดเรื่อง", {S.CLOSED}),
    ],
    K.RETURN: [
        RECEIVE,
        ("รอของคืน", {S.AWAITING_EXECUTION}),
        ("รับคืนและตรวจสภาพ", {S.IN_PROGRESS}),
        ("ปิดเรื่อง", {S.CLOSED}),
    ],
}
ASIDE = {
    S.AWAITING_INFO: "รอข้อมูลเพิ่มจากหน่วยงาน (เขต/ฝ่าย)",
    S.ON_HOLD: "พักเรื่องไว้",
    S.REJECTED: "ไม่รับเรื่อง",
    S.CANCELLED: "ยกเลิก",
}

FACT_LABELS = {
    "kind": "ชนิดเรื่อง",
    "equipment": "อุปกรณ์",
    "hazard_reported": "แจ้งเหตุอันตราย",
    "severe_impact": "ผลกระทบรุนแรง",
    "registry_only": "ขอแก้ทะเบียนอย่างเดียว",
    "serial_mismatch": "เลขเครื่อง/จำนวนไม่ตรง",
    "shortage": "ของไม่พอ",
}

# Short titles of the synthetic routing rules (PROJECT.md B.3); the policy file holds the logic.
RULE_TITLES = {
    "R01": "เบิกมิเตอร์",
    "R02": "เบิกหม้อแปลง",
    "R03": "อุปกรณ์อื่นในขอบเขตชัด",
    "R04": "คืนมิเตอร์หรือหม้อแปลง",
    "R05": "ปัญหาทางเทคนิคของอุปกรณ์ที่ระบุได้",
    "R06": "ขอแก้ข้อมูลทะเบียนล้วน",
    "R07": "เลขเครื่อง/จำนวนไม่ตรง",
    "R08": "ของขาดหรือหลายหน่วยงาน (เขต/ฝ่าย)แย่งของ",
    "R09": "หลายอุปกรณ์หรือหลายกองในเรื่องเดียว",
    "R10": "ไม่พบรายการหรือขอบเขตไม่ชัด",
}

EXTRA_ACTIONS = {
    "close_on_delivery": "ปิดเรื่องเมื่อเขตได้รับของ",
    "submit": "ยื่นเรื่อง",
    "route_by_ai": "AI ส่งกองตามหน้าที่",
}

SYSTEM_EVENTS = {
    "case.ack_overdue": "เกินเวลาตอบรับ",
    "case.deadline_overdue": "เกินกรอบเวลา",
    "case.ack_escalated": "ยกระดับการตอบรับ",
    "case.deadline_escalated": "ยกระดับกรอบเวลา",
    "case.escalation_held": "ระบบมีปัญหา จึงยังไม่ยกระดับ",
    "case.repeat_dispute": "ผู้แจ้งโต้แย้งผลซ้ำ",
}

# ADR 0047: staff add and remove เลขติดตาม; the timeline names who did it.
NUMBER_EVENTS = {"case.number_added": "เพิ่มเลข", "case.number_removed": "ลบเลข"}

TEXT_FIELDS = [
    ("purpose", "จุดประสงค์"),
    ("description", "รายละเอียด"),
    ("impact", "ผลกระทบ"),
    ("condition", "สภาพของ"),
    ("return_reason", "เหตุผลที่คืน"),
    ("source_reference", "เอกสารอ้างอิง"),
    ("intake_note", "บันทึกการรับเรื่อง"),
]


def _value_text(name, value):
    if value is None:
        return "ไม่ทราบ"
    if isinstance(value, bool):
        return "ใช่" if value else "ไม่"
    if name == "kind":
        return Case.Kind(value).label if value in K.values else str(value)
    if name == "equipment" and isinstance(value, list):
        categories = dict(EquipmentType.Category.choices)
        parts = []
        for entry in value:
            if isinstance(entry, dict):
                label = categories.get(entry.get("category"), entry.get("category") or "")
                parts.append(
                    f"{entry.get('item', '')} ({label})" if label else entry.get("item", "")
                )
            else:
                parts.append(str(entry))
        return ", ".join(p for p in parts if p) or "-"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "-"
    return str(value)


def proposal(case):
    """B.7 card as display rows: what the rules and AI propose, never a decision."""
    card = policy.card(case)
    if card is None:
        return None
    facts = [
        {
            "label": FACT_LABELS.get(name, name),
            "value": _value_text(name, fact.get("value")),
            "source": fact.get("source", ""),
            "source_label": fact.get("source_label", ""),
            "evidence": fact.get("evidence", ""),
            "confirmed": fact.get("source") in {"staff", "case", "stock", "delivery", "return"},
        }
        for name, fact in card["facts"].items()
    ]
    return {
        **card,
        "fact_rows": facts,
        "missing_labels": [FACT_LABELS.get(m, m) for m in card["missing"]],
        "rule_rows": [(rule, RULE_TITLES.get(rule, "")) for rule in card["rules"]],
        "owner_changes": card["proposed_owner"] != card["current_owner"],
    }


def action_label(action):
    spec = workflow.ACTIONS.get(action)
    return spec.label if spec else EXTRA_ACTIONS.get(action, action)


def _who(user, label, case):
    if user is not None:
        profile = getattr(user, "staff_profile", None)
        return profile.display_name if profile else user.username
    if label.startswith("requester"):
        return f"ผู้แจ้ง ({case.requester.display_name})"
    if label in {"policy", "system", "worker"}:
        return "ระบบ"
    return label or "ระบบ"


def timeline(case):
    """Decisions and the policy's own events (overdue, escalation), oldest first."""
    status_labels = dict(Case.Status.choices)
    entries = []
    for decision in case.decisions.select_related("actor__staff_profile"):
        label = action_label(decision.action)
        data = decision.data if isinstance(decision.data, dict) else {}
        if decision.action == "set_sap_number" and data.get("sap_number"):
            label += f" {data.get('previous') or 'ไม่มี'} → {data['sap_number']}"
        if decision.action == "assign" and data.get("auto"):
            # ADR 0031: the system handed the case on; the reason says why.
            label = (
                f"ระบบมอบหมายให้ {data['assignee_name']}"
                if data.get("assignee_name")
                else "ระบบเอาผู้รับผิดชอบออก"
            )
        moved = decision.from_status and decision.from_status != decision.to_status
        entries.append(
            {
                "at": decision.created_at,
                "what": label,
                "who": _who(decision.actor, decision.actor_label, case),
                "why": decision.reason,
                # ADR 0007: การทำแทนกอง is shown where the division reads its case.
                "support": (decision.data or {}).get("support", {}).get("division", "")
                if isinstance(decision.data, dict)
                else "",
                "status": (
                    f"{status_labels.get(decision.from_status, '')} → "
                    f"{status_labels.get(decision.to_status, '')}"
                )
                if moved
                else "",
                # ADR 0028: what the requester read, and the note only staff read.
                "notice": data.get("notice", "") if data.get("notice") != decision.reason else "",
                "note": data.get("internal_note", ""),
                "system": False,
            }
        )
    clock_labels = dict(AckClock.Kind.choices)
    for event in AuditEvent.objects.filter(
        target_type="Case", target_id=str(case.pk), action__in=list(SYSTEM_EVENTS)
    ):
        detail = clock_labels.get(event.data.get("clock"), "")
        division = event.data.get("division", "")
        target = event.data.get("to", "")
        if target.startswith("supervisors:"):
            target = f"แอดมินกอง {target.split(':', 1)[1]}"
        parts = [
            p for p in (detail, division and f"กอง {division}", target and f"ไป {target}") if p
        ]
        entries.append(
            {
                "at": event.created_at,
                "what": SYSTEM_EVENTS[event.action],
                "who": "ระบบ",
                "why": event.reason,
                "status": " · ".join(parts),
                "system": True,
            }
        )
    for event in AuditEvent.objects.filter(
        target_type="Case", target_id=str(case.pk), action__in=list(NUMBER_EVENTS)
    ).select_related("actor__staff_profile"):
        entries.append(
            {
                "at": event.created_at,
                "what": f"{NUMBER_EVENTS[event.action]} {event.data.get('number', '')}",
                "who": _who(event.actor, event.actor_label, case),
                "why": event.reason,
                "status": "",
                "system": False,
            }
        )
    for reply in case.info_replies.order_by("created_at"):
        entries.append(
            {
                "at": reply.created_at,
                "what": "ผู้แจ้งตอบข้อมูลเพิ่มทาง LINE",
                "who": f"ผู้แจ้ง ({case.requester.display_name})",
                "why": reply.text[:300],
                "status": "",
                "system": False,
            }
        )
    return sorted(entries, key=lambda e: e["at"])


def clocks(case, now):
    calendar = queues.calendar_now()
    rows = []
    for clock in AckClock.objects.filter(case=case, stopped_at__isnull=True).select_related(
        "division", "child_work"
    ):
        paused = clock.paused_at is not None
        rows.append(
            {
                "label": clock.get_kind_display(),
                "division": clock.division.code,
                "due_at": clock.due_at,
                "remaining": None if paused else queues.remaining_minutes(clock, now, calendar),
                "paused": paused,
                "paused_minutes": clock.paused_minutes,
                "overdue": not paused and clock.due_at <= now,
                "reminded": clock.reminded_at is not None,
                "escalated": clock.escalated_at is not None,
                "held": clock.escalation_held_at is not None and clock.escalated_at is None,
                "continuous": calendar.counts_continuously(clock.priority),
            }
        )
    return sorted(rows, key=lambda r: r["due_at"])


def decisions_made(case, triage):
    """The people's decisions on route and level, with who and when (shown on white, ✓)."""
    rows = []
    if triage and triage.confirmed_priority:
        last = (
            case.decisions.filter(action="set_priority")
            .select_related("actor__staff_profile")
            .order_by("-id")
            .first()
        )
        rows.append(
            {
                "what": f"ระดับ{queues.level_name(triage.confirmed_priority)}",
                "by": _who(last.actor, last.actor_label, case) if last else "",
                "division": triage.confirmed_division.code if triage.confirmed_division else "",
                "at": last.created_at if last else None,
            }
        )
    owner = (
        case.decisions.filter(action__in=["accept_handoff", "decide_routing", "complete_triage"])
        .select_related("actor__staff_profile")
        .order_by("-id")
        .first()
    )
    if owner is not None:
        rows.append(
            {
                "what": f"กองเจ้าภาพ {case.owner_division.code}"
                + (" · ส่งเข้าขั้นดำเนินงาน" if owner.action == "complete_triage" else ""),
                "by": _who(owner.actor, owner.actor_label, case),
                "division": "",
                "at": owner.created_at,
            }
        )
    return rows


def line_owners(case):
    """R09 (ADR 0004): which division acts on each line once its child work was accepted."""
    owners = {}
    for line in case.lines.select_related("case"):
        division_id = coordination.division_of(line)
        if division_id != case.owner_division_id:
            owners[line.line_no] = division_id
    if not owners:
        return {}
    from operations.models import Division

    codes = dict(Division.objects.filter(pk__in=owners.values()).values_list("pk", "code"))
    return {line_no: codes[division_id] for line_no, division_id in owners.items()}


def lines(case):
    """The line table, with what the requester typed for a shared item (typed_lines). While the
    stock work is hidden (ADR 0032) a slip shows only รายละเอียดตามใบ, as a problem report does."""
    stock = options.old_stock()
    if case.kind == K.WITHDRAWAL and stock:
        page = fulfilment.summary(case)
    elif case.kind == K.RETURN and stock:
        page = returns.summary(case)
    else:
        page = _problem_lines(case)
        if case.kind == K.RETURN:
            page["instructions"] = returns.latest_instructions(case)  # แจ้งวิธีส่งคืน stays
    page["stock"] = stock
    typed = {}
    for line in case.lines.select_related("item"):
        unit = line.item.unit if typed_lines.is_shared(line.item) else ""
        rows = [
            " · ".join(
                part
                for part in (typed_lines.label_of(row["group"]), typed_lines.describe(row, unit))
                if part
            )
            for row in line.details
        ]
        if rows:
            typed[line.line_no] = rows
    for row in page.get("lines", []):
        row["typed"] = typed.get(row["line_no"], [])
    return page


def _problem_lines(case):
    return {
        "lines": [
            {
                "line_no": line.line_no,
                "item": line.item.code,
                "name": line.item.name,
                "unit": line.item.unit,
                "requested": f"{line.requested_quantity.normalize():f}"
                if line.requested_quantity is not None
                else "-",
                "serials": line.serials,
            }
            for line in case.lines.select_related("item").order_by("line_no")
        ]
    }


REGISTRY_STATES = {
    "matched": ("ตรงกับ SAP", "tag--closed"),
    "waiting": ("รอไฟล์ SAP", "tag--muted"),
    "discrepancy": ("มีส่วนต่างทะเบียน", "tag--outline"),
}


def registry_facts(case):
    """M11: the SAP documents this case's facts cite, their evidence and reconciliation state.
    With the registry menu off (23 Oct 2569) there is nothing to compare with: the typed
    numbers and their files only."""
    records = list(
        case.registry_records.select_related("division", "recorded_by__staff_profile")
        .prefetch_related("evidence", "discrepancies")
        .order_by("recorded_at")
    )
    if not records:
        return []
    compared = registry.enabled()
    docs = registry.documents([r.document_number for r in records]) if compared else {}
    spans = registry.coverage() if compared else []
    facts = []
    for record in records:
        label, tone = (
            REGISTRY_STATES[registry.record_state(record, docs, spans)] if compared else ("", "")
        )
        facts.append(
            {
                "record": record,
                "label": label,
                "tone": tone,
                "evidence": list(record.evidence.all()),
                "open": [d for d in record.discrepancies.all() if d.status != "resolved"]
                if compared
                else [],
            }
        )
    return facts


def _handoff(case):
    handoff = (
        Handoff.objects.filter(case=case, status=Handoff.Status.PENDING)
        .select_related(
            "to_division",
            "from_division",
            "requested_by__staff_profile",
            "recommendation__proposed_owner",
            "recommendation__policy_version",
        )
        .first()
    )
    if handoff is None:
        return None
    info = queues.handoff_info(handoff)
    info["by"] = _who(handoff.requested_by, "", case)
    return info


def journey(case):
    """Steps done, the current one and those to come. A paused case, or one that ended early,
    stays at the step it left, with the reason beside it."""
    steps = JOURNEY.get(case.kind, [])
    status = case.status
    if status in ASIDE:
        left = case.decisions.filter(to_status=status).order_by("-id").first()
        status = left.from_status if left else S.TRIAGE
    at = next((i for i, (_, statuses) in enumerate(steps) if status in statuses), 0)
    stopped = case.status in (S.REJECTED, S.CANCELLED)
    closed = case.status == S.CLOSED
    return [
        {
            "label": label,
            "state": (
                "done"
                if closed or i < at
                else ("stopped" if stopped else "current")
                if i == at
                else "todo"
            ),
            "note": ASIDE.get(case.status, "") if i == at else "",
        }
        for i, (label, _) in enumerate(steps)
    ]


def line_request(case, attachments):
    """ADR 0036: what the requester picked and typed in a LINE เบิก/คืน flow, built from what
    the case already keeps, so older cases show it too; None for other cases."""
    if case.channel != Case.Channel.LINE or case.kind not in (K.WITHDRAWAL, K.RETURN):
        return None
    data = case.intake_data or {}
    no_number = bool(data.get("no_number")) and not case.sap_number
    label = typed_lines.sap_label(case.kind, case.topic, case.sap_kind)
    picks = [
        flow_runner.SET_LABELS.get(data.get("set_kind"), data.get("set_kind"))
        or data.get("equipment_name")
        or flow_runner.GROUP_LABELS.get(data.get("group"), ""),
        "ติดตามใบเบิก" if case.kind == K.WITHDRAWAL else "ติดตามใบส่งคืน",
        "ไม่มีหมายเลข" if no_number else label if case.sap_number else "",
    ]
    typed = [t for t in (case.purpose, case.condition, case.return_reason) if t and t != "-"]
    return {
        "tile": (case.menu_label or "").split(" · ")[0],
        "picks": [p for p in picks if p],
        "number": case.sap_number,
        "number_label": label,
        "no_number": no_number,
        "details": "\n".join(dict.fromkeys(typed)),  # a return keeps one text as both fields
        "photos": [
            a
            for a in attachments
            if a.identity_id
            and a.scan_status == a.ScanStatus.CLEAN
            and a.content_type.startswith("image/")
        ],
    }


def build(case, now=None):
    now = now or timezone.now()
    triage = CaseTriage.objects.filter(case=case).select_related("confirmed_division").first()
    children = list(
        ChildWork.objects.filter(case=case)
        .select_related("division", "answered_by__staff_profile", "finished_by__staff_profile")
        .prefetch_related("lines")
    )
    for work in children:
        work.line_numbers = sorted(line.line_no for line in work.lines.all())
    related = []
    if case.related_case_id:
        related.append(case.related_case)
    related += list(Case.objects.filter(related_case=case).order_by("number"))
    attachments = list(case.attachments.order_by("created_at"))
    request = line_request(case, attachments)
    sap_label = typed_lines.sap_label(case.kind, case.topic, case.sap_kind)
    return {
        "level": queues.level_of(triage),
        "triage": triage,
        "line_request": request,
        "texts": []  # the LINE section says all of it (ADR 0036)
        if request
        else [
            *([(sap_label, case.sap_number)] if case.sap_number and sap_label else []),
            *([("เครื่องวัดฯ", usage_meters.describe(case.meters))] if case.meters else []),
            *((label, getattr(case, name)) for name, label in TEXT_FIELDS if getattr(case, name)),
        ],
        "hazard_guidance": policy.hazard_guidance(case),
        # ADR 0021: an การใช้ไฟฟ้า case reaches the AI unless ตัวเลือกระบบ usage_ai keeps it away.
        "usage_private": case.topic == Case.Topic.USAGE and policy.usage_kept_from_ai(),
        "hazard": bool(triage and triage.priority_floor == "P0"),
        "proposal": proposal(case),
        "decisions": decisions_made(case, triage),
        "clocks": clocks(case, now),
        "lines": lines(case),
        "line_owners": line_owners(case),
        "children": children,
        "problem": problems.summary(case) if case.kind == K.PROBLEM else None,
        "handoff": _handoff(case),
        "review": RoutingReview.objects.filter(case=case, status=RoutingReview.Status.OPEN).first(),
        "attachments": attachments,
        "registry": registry_facts(case),
        "timeline": timeline(case),
        "related": related,
        "update_due": policy.update_due(case),
        "journey": journey(case),
    }
