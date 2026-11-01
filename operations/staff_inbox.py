"""The shared LINE inbox: DED cases and orders, scoped before pagination."""

from django.core.paginator import Paginator
from django.db.models import CharField, F, Value
from django.db.models.functions import Cast
from django.utils import timezone

from operations import identity, queues, work_orders, workflow
from operations.models import Case, Division, WorkOrder

FILTERS = {
    "waiting": "งานรอรับเรื่อง",
    "active": "กำลังดำเนินการ",
    "unassigned": "รอมอบหมาย",
    "all": "ทั้งหมด",
}
WAITING = (Case.Status.SUBMITTED, Case.Status.TRIAGE, Case.Status.AWAITING_APPROVAL)


def picks_division(scope):
    """แอดมินฝ่าย and the system's admins see every กอง and may narrow the list to one."""
    return scope.is_department_supervisor or scope.is_admin or scope.is_support


def sources(user, status="waiting", division=None):
    scope = identity.staff_scope(user)
    divisions = set(scope.division_roles) | set(scope.overseen)
    if picks_division(scope):
        divisions |= set(Division.objects.filter(is_active=True).values_list("pk", flat=True))
    if division is not None:
        divisions &= {division}
    cases = queues.visible(user).filter(owner_division_id__in=divisions)
    orders = work_orders.visible(user).filter(division_id__in=divisions)
    if status == "waiting":
        cases = cases.filter(status__in=WAITING)
        orders = orders.filter(status=WorkOrder.Status.ASSIGNED)
    elif status == "active":
        cases = cases.filter(status__in=workflow.OPEN).exclude(status__in=WAITING)
        orders = orders.filter(status__in=WorkOrder.OPEN).exclude(status=WorkOrder.Status.ASSIGNED)
    elif status == "unassigned":
        cases = cases.filter(status__in=workflow.OPEN, assignee__isnull=True)
        orders = orders.filter(status__in=WorkOrder.OPEN, assignee__isnull=True)
    return cases, orders


def page_for(user, status="waiting", division=None, page=1):
    cases, orders = sources(user, status, division)
    index = (
        cases.order_by()
        .annotate(ref=Cast("pk", CharField()))
        .values("ref")
        .annotate(source=Value("case"), arrived=F("submitted_at"))
    )
    other = (
        orders.order_by()
        .annotate(ref=Cast("pk", CharField()))
        .values("ref")
        .annotate(source=Value("order"), arrived=F("created_at"))
    )
    page = Paginator(index.union(other).order_by("-arrived", "source", "-ref"), 5).get_page(page)
    entries = list(page)
    case_rows = (
        cases.filter(pk__in=[r["ref"] for r in entries if r["source"] == "case"])
        .select_related("owner_division", "assignee__staff_profile")
        .in_bulk()
    )
    order_rows = (
        orders.filter(pk__in=[r["ref"] for r in entries if r["source"] == "order"])
        .select_related("division", "assignee__staff_profile")
        .in_bulk()
    )
    case_rows = {str(k): v for k, v in case_rows.items()}
    order_rows = {str(k): v for k, v in order_rows.items()}
    return page, [
        (r["source"], (case_rows if r["source"] == "case" else order_rows)[r["ref"]])
        for r in entries
    ]


def card(turn, source, row):
    is_case = source == "case"
    division = row.owner_division if is_case else row.division
    title = (
        (row.description or row.purpose or row.return_reason or row.get_kind_display())
        if is_case
        else row.title
    )
    if is_case:
        due = (
            row.ack_clocks.filter(stopped_at__isnull=True)
            .order_by("due_at")
            .values_list("due_at", flat=True)
            .first()
        )
        due = timezone.localtime(due).strftime("%d/%m/%Y %H:%M") if due else "ยังไม่กำหนด"
    else:
        due = row.due_on.strftime("%d/%m/%Y") if row.due_on else "ยังไม่กำหนด"
    assignee = work_orders.name_of(row.assignee) if row.assignee else "รอมอบหมาย"
    _, data = turn.button(
        "ดูรายละเอียด", "staff_case" if is_case else "staff_order", {source: str(row.pk)}
    )
    return {
        "type": "bubble",
        "size": "kilo",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {
                    "type": "text",
                    "text": row.number,
                    "color": "#16804A",
                    "weight": "bold",
                    "size": "sm",
                },
                {"type": "text", "text": title[:160], "weight": "bold", "size": "lg", "wrap": True},
                {
                    "type": "text",
                    "text": f"{division.code} · {row.get_status_display()}",
                    "size": "sm",
                    "wrap": True,
                },
                {"type": "separator"},
                {
                    "type": "text",
                    "text": f"ผู้รับผิดชอบ: {assignee}\nกำหนด: {due}",
                    "size": "sm",
                    "wrap": True,
                    "color": "#555555",
                },
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#16804A",
                    "action": {"type": "postback", "label": "ดูรายละเอียด", "data": data},
                },
            ],
        },
    }


def show(turn, user, status="waiting", division=None, page=1):
    status = status if status in FILTERS else "waiting"
    try:
        division = int(division) if division else None
    except (ValueError, TypeError):
        division = -1
    rows, entries = page_for(user, status, division, page)
    state = {"status": status, "division": division}
    quick = [
        turn.button(label, "staff_inbox", {**state, "status": key})
        for key, label in FILTERS.items()
    ]
    for label, number in (("ย้อนกลับ", rows.number - 1), ("ถัดไป", rows.number + 1)):
        if 1 <= number <= rows.paginator.num_pages:
            quick.append(turn.button(label, "staff_inbox", {**state, "page": number}))
    if picks_division(identity.staff_scope(user)):
        quick.append(turn.button("เลือกกอง/ศูนย์", "staff_inbox_divisions", {"status": status}))
    turn.set_prompt()
    turn.say(
        f"{FILTERS[status]} · {rows.paginator.count} รายการ · "
        f"หน้า {rows.number}/{rows.paginator.num_pages}"
        + ("\nยังไม่มีงานสั่งหรือเรื่องในรายการนี้" if not entries else ""),
        quick,
    )
    if entries:
        turn.replies.append(
            {
                "type": "flex",
                "altText": "งานสั่งที่ได้รับ",
                "contents": {
                    "type": "carousel",
                    "contents": [card(turn, source, row) for source, row in entries],
                },
            }
        )


def choose_division(turn, user, status="waiting", page=1):
    if not picks_division(identity.staff_scope(user)):
        return show(turn, user)
    rows = Paginator(
        Division.objects.filter(is_active=True, is_synthetic=False).order_by("code"), 9
    ).get_page(page)
    quick = [turn.button("ทุกกอง/ศูนย์", "staff_inbox", {"status": status})]
    quick += [
        turn.button(d.code[:20], "staff_inbox", {"status": status, "division": d.pk}) for d in rows
    ]
    if rows.has_previous():
        quick.append(
            turn.button(
                "ย้อนกลับ",
                "staff_inbox_divisions",
                {"status": status, "page": rows.previous_page_number()},
            )
        )
    if rows.has_next():
        quick.append(
            turn.button(
                "ถัดไป", "staff_inbox_divisions", {"status": status, "page": rows.next_page_number()}
            )
        )
    turn.say("กรุณาเลือกกองหรือศูนย์ที่ต้องการดูงาน", quick)


def detail(turn, rows, buttons):
    """Keep actions in the same card as the details the supervisor is confirming."""
    contents = [
        {
            "type": "text",
            "text": rows[0],
            "size": "lg",
            "weight": "bold",
            "color": "#16804A",
            "wrap": True,
        },
        {"type": "separator"},
        *[{"type": "text", "text": row, "size": "sm", "wrap": True} for row in rows[1:] if row],
        {"type": "separator"},
    ]
    for label, data in buttons:
        action = (
            {"type": "uri", "label": label[:40], "uri": data}
            if data.startswith(("https://", "http://"))
            else {"type": "postback", "label": label[:40], "data": data}
        )
        contents.append(
            {
                "type": "button",
                "height": "sm",
                "style": "primary" if label == "ยืนยันรับเรื่อง" else "link",
                "color": "#16804A",
                "action": action,
            }
        )
    turn.replies.append(
        {
            "type": "flex",
            "altText": rows[0][:400],
            "contents": {
                "type": "carousel",
                "contents": [
                    {
                        "type": "bubble",
                        "size": "mega",
                        "body": {
                            "type": "box",
                            "layout": "vertical",
                            "spacing": "md",
                            "contents": contents,
                        },
                    }
                ],
            },
        }
    )
