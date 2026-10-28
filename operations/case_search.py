"""ค้นเรื่องใน เรื่องของฉัน (grill กคว. 27 ต.ค. 2569 Q2, Q7 ถึง Q10, Q22).

Two buttons under เรื่องของฉัน: ค้นด้วยเลขที่ใบเบิก (a number finds every document it starts,
so 4500012345 finds 4500012345/10 and /20; SAP work orders too) and ค้นเลขเครื่องวัดฯ (the meter
number, also called UI or เลขตู้). Several numbers go in one message split by ",". People of the
same district see each other's withdrawals and returns: the status, the issue day, how the goods
come and the sender's employee ID, never the name.
"""

from django.db.models import Q

from operations import typed_input, typed_lines, workflow
from operations.models import Appointment, Case, CaseLine, CaseMilestone

PROMPTS = {"track_slip": "slip", "track_meter": "meter"}
LABELS = {"slip": "ค้นด้วยเลขที่ใบเบิก", "meter": "ค้นเลขเครื่องวัดฯ"}
ASK = {
    "slip": "พิมพ์เลขที่ใบเบิก ใบส่งคืน หรือใบสั่งงาน SAP พิมพ์แค่ต้นเลขก็ได้ หลายเลขคั่นด้วย ,",
    "meter": "พิมพ์หมายเลขเครื่องวัดฯ (UI หรือเลขตู้) หลายเลขคั่นด้วย ,",
}
MAX_NUMBERS = 10
MAX_SHOWN = 8


def ask(turn, by):
    turn.set_prompt({v: k for k, v in PROMPTS.items()}[by])
    turn.say(ASK[by], [("ยกเลิกการค้น", "m=mine")])


def _scope(requester):
    """The requester's own cases, and the district's withdrawals and returns (Q10)."""
    own = Q(requester=requester)
    if requester.is_anonymous or requester.is_external:
        return Case.objects.filter(own)
    district = Q(district=requester.district, kind__in=[Case.Kind.WITHDRAWAL, Case.Kind.RETURN])
    return Case.objects.filter(own | district)


def find(requester, by, text):
    """(cases, numbers read, problem)."""
    pattern = typed_lines.SAP_NUMBER if by == "slip" else typed_lines.METER_NUMBER
    label = "เลขที่ใบเบิก " if by == "slip" else "หมายเลขเครื่องวัดฯ "
    numbers, _, problem = typed_input.numbers_rule(text, pattern, label)
    if problem:
        return [], [], problem
    if len(numbers) > MAX_NUMBERS:
        return [], [], f"ค้นได้ครั้งละไม่เกิน {MAX_NUMBERS} เลข"
    cases = _scope(requester)
    if by == "slip":
        match = Q()
        for number in numbers:
            match |= Q(sap_number__istartswith=number)
        cases = cases.filter(match)
    else:
        lines = Q()
        for number in numbers:
            lines |= Q(details__contains=[{"numbers": [number]}])
        found = CaseLine.objects.filter(lines | Q(serials__has_any_keys=numbers))
        # รอบใหม่ Q4: a meter number may be the เลขติดตาม, a set's meter, or in รายละเอียด.
        other = Q()
        for number in numbers:
            other |= (
                Q(intake_data__paired_meter__contains=[number])
                | Q(intake_data__meters__contains=[{"numbers": [number]}])
                | Q(sap_kind="meter", sap_number__iexact=number)
                | Q(purpose__icontains=number)
                | Q(return_reason__icontains=number)
                | Q(description__icontains=number)
            )
        cases = cases.filter(Q(pk__in=found.values("case_id")) | other)
    rows = cases.select_related("requester__employee").order_by("-submitted_at")[:MAX_SHOWN]
    return list(rows), numbers, ""


def describe(case, requester):
    """One case as a search result; someone else's shows the sender's employee ID only."""
    from operations import conversation  # local: conversation builds on this module

    lines = [f"{case.number} · {conversation._kind_label(case.kind, case.topic)}"]
    if case.sap_number:
        lines.append(f"เลข: {case.sap_number}")
    stage = CaseMilestone.objects.filter(case=case).order_by("-on", "-pk").first()
    lines.append(f"สถานะ: {stage.label if stage else conversation._status_label(case)}")
    keyed = CaseMilestone.objects.filter(case=case, stage=CaseMilestone.Stage.KEYED).first()
    if keyed and keyed.planned_on:
        lines.append(f"กำหนดจ่าย: {keyed.planned_on:%d/%m}/{keyed.planned_on.year + 543}")
    method = (
        Appointment.objects.filter(case=case, status__in=Appointment.HOLDING)
        .exclude(method="")
        .first()
    )
    if method:
        from operations import warehouse  # local: warehouse builds on the models

        lines.append(f"วิธีรับ: {warehouse.method_label(case, method.method)}")
    if case.requester_id != requester.pk:
        employee = case.requester.employee
        lines.append(f"ผู้ส่ง: รหัสพนักงาน {employee.employee_id if employee else '-'}")
    return "\n".join(lines)


def on_text(turn, text):
    by = PROMPTS[turn.conversation.prompt]
    cases, numbers, problem = find(turn.requester, by, text)
    if problem:
        turn.say(problem, [("ยกเลิกการค้น", "m=mine")])
        return
    turn.set_prompt()
    again = [(LABELS[by], f"m=find_{by}"), ("เรื่องของฉัน", "m=mine")]
    if not cases:
        turn.say(f"ไม่พบเรื่องที่มี {', '.join(numbers)}", again)
        return
    open_ones = [c for c in cases if c.status not in workflow.TERMINAL]
    head = f"พบ {len(cases)} เรื่อง (ยังไม่จบ {len(open_ones)})"
    body = "\n\n".join(describe(c, turn.requester) for c in cases)
    own = [(c.number, f"c={c.number}") for c in cases if c.requester_id == turn.requester.pk]
    turn.say(f"{head}\n\n{body}", [*own[:8], *again])
