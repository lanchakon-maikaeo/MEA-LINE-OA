"""Role-based LINE menu geometry. Fixed catalogue and release images are published by the worker."""

import logging

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from operations import identity, line_api
from operations.models import ChannelIdentity, Division, LineMenuItem, Requester

NAME = "faoj-menu-v2"  # everyone's menu
PREFIX = "faoj-"  # staff menus and their aliases: faoj-staff-work, faoj-sup-order, ...
WIDTH, HEIGHT, BAR, TAB_H = 2500, 1686, 286, 230
MAX_TILES, MAX_BAR = 6, 2
MAX_IMAGE = 1024 * 1024  # LINE's limit for a rich menu picture
A = LineMenuItem.Action
P = LineMenuItem.Place
T = LineMenuItem.Tab
# The tabs each role's menu shows, and every page that is published.
ROLE_TABS = {"staff": (T.REPORT, T.WORK), "sup": (T.REPORT, T.WORK, T.ORDER)}
# These remain the compact quick replies in chat.  The rich menu itself uses the six fixed
# service tiles from the catalogue below, plus its two bottom actions.
SERVICE_BUTTONS = (
    ("เบิกอุปกรณ์", "m=equipment&kind=withdrawal"),
    ("ส่งคืนอุปกรณ์", "m=equipment&kind=return"),
    ("แจ้งปัญหา", "m=problems"),
    ("ติดตามเรื่องของฉัน", "m=mine"),
)
PAGES = (
    "user",
    "employee-report",
    "staff-report",
    "staff-work",
    "sup-report",
    "sup-work",
    "sup-order",
)
PAGE_TITLES = {
    "user": "ทุกคน",
    "employee-report": "เจ้าหน้าที่ · บริการ",
    "staff-report": "เจ้าหน้าที่ · บริการ",
    "staff-work": "เจ้าหน้าที่ · งานของกอง",
    "sup-report": "Supervisor · บริการ",
    "sup-work": "Supervisor · งานของกอง",
    "sup-order": "Supervisor · สั่งงาน",
}
logger = logging.getLogger(__name__)


def bind_seed_divisions():
    """Complete the seed after the organisation is imported; preserve explicit menu routing."""
    mappings = {
        "Meter, CT, VT": ("กคว.",),
        "หม้อแปลง LBS RMU": ("กอจ.", "กองอุปกรณ์ฯ"),
        "หม้อแปลง, LBS, RMU": ("กอจ.", "กองอุปกรณ์ฯ"),  # historical seed
        "เครื่องมือและอุปกรณ์": ("กคอ.", "กองเครื่องมือฯ"),
        "ระบบ SMG, AMR": ("ศอค.",),
        "ตรวจสอบการใช้ไฟฟ้า": ("กตฟ.",),
    }
    for label, codes in mappings.items():
        division = Division.objects.filter(code__in=codes, is_synthetic=False).first()
        if division is None:
            continue
        for tile in LineMenuItem.objects.filter(label=label, place=P.TILE, tab=T.REPORT):
            if tile.division_id is None:
                tile.division = division
                tile.save(update_fields=["division"])
            tile.choices.filter(division__isnull=True).update(division=tile.division)


def tiles(tab=LineMenuItem.Tab.REPORT):
    return list(
        LineMenuItem.objects.filter(place=P.TILE, tab=tab, is_active=True)
        .select_related("division")
        .order_by("position", "pk")[:MAX_TILES]
    )


def bar(tab=LineMenuItem.Tab.REPORT):
    return list(
        LineMenuItem.objects.filter(place=P.BAR, tab=tab, is_active=True).order_by(
            "position", "pk"
        )[:MAX_BAR]
    )


def _split(page):
    role, _, tab = page.partition("-")
    return role, T.GUEST if page == "user" else (tab or T.REPORT)


def alias(page):
    return f"{PREFIX}{page}"


def areas(page="user"):
    """One role's tappable areas.

    Service pages deliberately share the six service tiles and two personal actions shown in
    the supplied menu.  Staff still get their role tabs, while their work and order pages keep
    their focused shortcuts.
    """
    role, tab = _split(page)
    tabs = ROLE_TABS.get(role, ())
    top_h = TAB_H if tabs else 0
    is_service_page = tab in {T.REPORT, T.GUEST}
    if is_service_page:
        top = [{"item": item} for item in tiles(T.REPORT)]
        bottom = [{"item": item} for item in bar(T.REPORT)]
    else:
        top = [{"item": item} for item in tiles(tab)[:3]]
        top.append({"item": None, "label": "เมนูเพิ่มเติม", "postback": "m=roles"})
        bottom = []
    tile_h = HEIGHT - top_h
    out = []
    for c, name in enumerate(tabs):
        width = WIDTH // len(tabs)
        out.append(
            {
                "tab": name,
                "role": role,
                "item": None,
                "x": c * width,
                "y": 0,
                "w": width,
                "h": top_h,
            }
        )
    if is_service_page:
        columns = 3
        rows = [top[i : i + columns] for i in range(0, len(top), columns)]
        grid_h = tile_h - (BAR if bottom else 0)
        for r, row in enumerate(rows):
            height = grid_h // len(rows)
            for c, button in enumerate(row):
                width = WIDTH // len(row)
                out.append(
                    {
                        **button,
                        "x": c * width,
                        "y": top_h + r * height,
                        "w": width,
                        "h": height,
                    }
                )
        for c, button in enumerate(bottom):
            width = WIDTH // len(bottom)
            out.append(
                {
                    **button,
                    "x": c * width,
                    "y": top_h + grid_h,
                    "w": width,
                    "h": BAR,
                }
            )
        return out

    columns = 2
    rows = [top[i : i + columns] for i in range(0, len(top), columns)]
    for r, row in enumerate(rows):
        height = tile_h // len(rows)
        for c, button in enumerate(row):
            width = WIDTH // len(row)
            out.append(
                {
                    **button,
                    "x": c * width,
                    "y": top_h + r * height,
                    "w": width,
                    "h": height,
                }
            )
    return out


def _area_action(area):
    if "postback" in area:
        return {"type": "postback", "data": area["postback"], "displayText": area["label"]}
    if area["item"] is None:
        name = area["tab"]
        return {
            "type": "richmenuswitch",
            "richMenuAliasId": alias(f"{area['role']}-{name}"),
            "data": f"tab={name}",
        }
    item = area["item"]
    return {"type": "postback", "data": f"m=item&id={item.pk}", "displayText": item.label}


def rich_menu(page="user"):
    """The LINE rich menu object of one page for the current data."""
    shapes = areas(page)
    if not any(a["item"] or "postback" in a for a in shapes):
        raise ValidationError(f"เมนู{PAGE_TITLES[page]} ยังไม่มีปุ่มที่เปิดใช้")
    return {
        "size": {"width": WIDTH, "height": HEIGHT},
        "selected": True,
        "name": NAME if page == "user" else alias(page),
        "chatBarText": "เมนูบริการ",
        "areas": [
            {
                "bounds": {"x": a["x"], "y": a["y"], "width": a["w"], "height": a["h"]},
                "action": _area_action(a),
            }
            for a in shapes
        ],
    }


def drawing():
    """What the browser draws for every page: the same boxes and words the rich menus use."""
    pages = {}
    for page in PAGES:
        _, tab = _split(page)
        boxes = []
        for a in areas(page):
            if "postback" in a:
                label, note, place = a["label"], "", "tile"
            elif a["item"] is None:
                label = "บริการ" if a["tab"] == T.REPORT else T(a["tab"]).label
                note, place = "", "tab"
            else:
                label, note, place = a["item"].label, a["item"].note, a["item"].place
            boxes.append(
                {
                    "label": label,
                    "note": note,
                    "place": place,
                    "active": place == "tab" and a["tab"] == tab,
                    "x": a["x"],
                    "y": a["y"],
                    "w": a["w"],
                    "h": a["h"],
                }
            )
        pages[page] = boxes
    return {"size": [WIDTH, HEIGHT], "pages": pages, "titles": PAGE_TITLES}


def _picture(data):
    if not data or len(data) > MAX_IMAGE:
        raise ValidationError("รูปเมนูต้องมีขนาดไม่เกิน 1 MB")
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    raise ValidationError("รูปเมนูต้องเป็น JPEG หรือ PNG")


def staff_page(user):
    """The service grid is the default; staff can switch to work and order tabs when needed."""
    return "sup-report" if identity.staff_scope(user).overseen else "staff-report"


def page_for_identity(row):
    user = row.staff_user
    if user and user.is_active and identity.staff_scope(user).has_any_access:
        return staff_page(user)
    requester = row.requester
    if (
        row.status == ChannelIdentity.Status.ACTIVE
        and requester
        and requester.status == Requester.Status.ACTIVE
        and not requester.is_external
        and requester.employee_id
        and requester.employee.is_active
        and requester.employee_link
        in {Requester.EmployeeLink.VERIFIED, Requester.EmployeeLink.ADMIN}
    ):
        return "employee-report"
    return "user"


def publish(actor, pictures, reason):
    from operations import line_menu_release

    return line_menu_release.publish(actor, pictures, reason)


def relink(identity_row):
    """One LINE's menu after its staff link or role changed: the staff page of its role, or
    everyone's menu again. Best effort: a later publish links everyone anyway."""
    if not settings.LINE_CHANNEL_ACCESS_TOKEN or not settings.OUTBOUND_ENABLED:
        return
    page = page_for_identity(identity_row)
    target = f"/v2/bot/user/{identity_row.external_id}/richmenu"
    try:
        if page == "user":
            line_api.call(target, method="DELETE")
            return
        menu_id = line_api.call(f"/v2/bot/richmenu/alias/{alias(page)}").get("richMenuId")
        if menu_id:
            line_api.call(f"{target}/{menu_id}", method="POST")
    except (line_api.LineApiError, OSError) as exc:
        logger.warning("staff rich menu link failed: %s", type(exc).__name__)


def relink_user(user):
    """After a role change: the LINE linked to this staff account, if any, after the commit."""
    from operations.models import ChannelIdentity

    row = ChannelIdentity.objects.filter(staff_user=user).first()
    if row is not None:
        transaction.on_commit(lambda: relink(row))
