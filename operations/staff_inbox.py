"""The shared LINE inbox: DED cases and orders, scoped before pagination.

ADR 0042 Q4: one card per page, filter tabs on top and one row per item (number, title, status),
for แอดมินฝ่าย (every กอง, or one) and แอดมินกอง (their own) alike. New cases start work at
once (Q10), so there is no รอรับเรื่อง tab; the few cases still waiting show under กำลังดำเนินการ."""

from django.core.paginator import Paginator
from django.db.models import CharField, F, Value
from django.db.models.functions import Cast

from operations import identity, queues, work_orders, workflow
from operations.models import Division, WorkOrder

FILTERS = {"active": "กำลังดำเนินการ", "unassigned": "รอมอบหมาย", "all": "ทั้งหมด"}
PER_PAGE = 10
GREEN = "#16804A"


def picks_division(scope):
    """แอดมินฝ่าย and the system's admins see every กอง and may narrow the list to one."""
    return scope.is_department_supervisor or scope.is_admin or scope.is_support


def sources(user, status="active", division=None):
    scope = identity.staff_scope(user)
    divisions = set(scope.division_roles) | set(scope.overseen)
    if picks_division(scope):
        divisions |= set(Division.objects.filter(is_active=True).values_list("pk", flat=True))
    if division is not None:
        divisions &= {division}
    cases = queues.visible(user).filter(owner_division_id__in=divisions)
    orders = work_orders.visible(user).filter(division_id__in=divisions)
    if status == "active":
        cases = cases.filter(status__in=workflow.OPEN)
        orders = orders.filter(status__in=WorkOrder.OPEN)
    elif status == "unassigned":
        cases = cases.filter(status__in=workflow.OPEN, assignee__isnull=True)
        orders = orders.filter(status__in=WorkOrder.OPEN, assignee__isnull=True)
    return cases, orders


def page_for(user, status="active", division=None, page=1):
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
    page = Paginator(index.union(other).order_by("-arrived", "source", "-ref"), PER_PAGE).get_page(
        page
    )
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


def _tap(label, data, shown=None):
    return {"type": "postback", "label": label[:40], "data": data, "displayText": shown or label}


def row(turn, source, item):
    """One line: number, title, status; the whole line opens the item."""
    is_case = source == "case"
    title = (
        (item.description or item.purpose or item.return_reason or item.get_kind_display())
        if is_case
        else item.title
    )
    _, data = turn.button(
        item.number, "staff_case" if is_case else "staff_order", {source: str(item.pk)}
    )
    return {
        "type": "box",
        "layout": "horizontal",
        "spacing": "sm",
        "paddingTop": "8px",
        "paddingBottom": "8px",
        "action": _tap(item.number, data),
        "contents": [
            {"type": "text", "text": item.number, "size": "xs", "color": GREEN, "flex": 0},
            {"type": "text", "text": " ".join(title.split())[:60], "size": "sm", "flex": 5},
            {
                "type": "text",
                "text": item.get_status_display(),
                "size": "xxs",
                "color": "#888888",
                "flex": 2,
                "align": "end",
            },
        ],
    }


def show(turn, user, status="active", division=None, page=1):
    status = status if status in FILTERS else "active"
    try:
        division = int(division) if division else None
    except (ValueError, TypeError):
        division = -1
    rows, entries = page_for(user, status, division, page)
    state = {"status": status, "division": division}
    tabs = [
        {
            "type": "button",
            "height": "sm",
            "style": "primary" if key == status else "secondary",
            **({"color": GREEN} if key == status else {}),
            "action": _tap(label, turn.button(label, "staff_inbox", {**state, "status": key})[1]),
        }
        for key, label in FILTERS.items()
    ]
    lines = [row(turn, source, item) for source, item in entries] or [
        {"type": "text", "text": "ไม่มีงานในรายการนี้", "size": "sm", "color": "#888888"}
    ]
    footer = []
    for label, number in (("ก่อนหน้า", rows.number - 1), ("ถัดไป", rows.number + 1)):
        if 1 <= number <= rows.paginator.num_pages:
            data = turn.button(label, "staff_inbox", {**state, "page": number})[1]
            footer.append({"type": "button", "height": "sm", "action": _tap(label, data)})
    if picks_division(identity.staff_scope(user)):
        data = turn.button("เลือกกอง/ศูนย์", "staff_inbox_divisions", {"status": status})[1]
        footer.append({"type": "button", "height": "sm", "action": _tap("เลือกกอง/ศูนย์", data)})
    where = Division.objects.filter(pk=division).first() if division else None
    turn.set_prompt()
    bubble = {
        "type": "bubble",
        "size": "giga",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {"type": "text", "text": "งานสั่งที่ได้รับ", "weight": "bold", "size": "lg"},
                {
                    "type": "text",
                    "text": f"{where.code if where else 'ทุกกอง/ศูนย์ที่ดูแล'} · "
                    f"{rows.paginator.count} รายการ · หน้า {rows.number}/{rows.paginator.num_pages}",
                    "size": "xs",
                    "color": "#888888",
                },
                {"type": "box", "layout": "horizontal", "spacing": "sm", "contents": tabs},
                {"type": "separator"},
                *lines,
            ],
        },
    }
    if footer:
        bubble["footer"] = {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "contents": footer,
        }
    turn.replies.append({"type": "flex", "altText": "งานสั่งที่ได้รับ", "contents": bubble})


def choose_division(turn, user, status="active", page=1):
    if not picks_division(identity.staff_scope(user)):
        return show(turn, user)
    from operations import staff_orders_chat

    rows = Division.objects.filter(is_active=True, is_synthetic=False).order_by("code")
    buttons = [turn.button("ทุกกอง/ศูนย์", "staff_inbox", {"status": status})]
    buttons += [
        turn.button(d.code, "staff_inbox", {"status": status, "division": d.pk}) for d in rows
    ]
    staff_orders_chat.card(turn, "เลือกกองหรือศูนย์", buttons=buttons, plain=True)


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
