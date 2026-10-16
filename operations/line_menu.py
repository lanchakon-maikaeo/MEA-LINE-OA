"""เมนู LINE (15 ต.ค. 2569 Q17–Q20, Q32; 16 ต.ค. 2569 Q2, Q9, Q13): the rich menus as data.

Everyone gets the แจ้งเรื่อง menu: up to six tiles (three to a row) and up to four buttons on a bar
below them; a tile opens its choices. Staff who linked LINE get their own menu with tabs on top,
แจ้งเรื่อง and งานของกอง, and Supervisors a third tab, สั่งงาน; LINE switches tabs by itself
(rich menu aliases). Editing changes what the bot answers at once. The pictures are drawn in the
browser from the same data and published with every save the admin confirms, replacing the live
menus and linking each staff LINE to the menu of its role.
"""

import logging

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from operations import identity, line_api
from operations.models import Division, LineMenuItem

NAME = "faoj-menu-v2"  # everyone's menu
PREFIX = "faoj-"  # staff menus and their aliases: faoj-staff-work, faoj-sup-order, ...
OLD_NAMES = {"faoj-main-v1", NAME}
WIDTH, HEIGHT, BAR, TAB_H = 2500, 1686, 286, 230
MAX_TILES, MAX_BAR, MAX_CHOICES = 6, 4, 12
MAX_IMAGE = 1024 * 1024  # LINE's limit for a rich menu picture
A = LineMenuItem.Action
P = LineMenuItem.Place
T = LineMenuItem.Tab
TILE_ACTIONS = {A.TOPIC, A.GENERAL, A.NEW, A.MINE, A.FIND}
CHOICE_ACTIONS = {A.TRACK, A.RETURN, A.PROBLEM, A.GENERAL}
BAR_ACTIONS = {A.MINE, A.FIND, A.NEW, A.GENERAL}
WORK_ACTIONS = {A.WORK_NEW, A.WORK_MINE, A.WORK_ORDERS, A.WORK_DUE, A.WORK_FIND, A.OPEN_WEB}
ORDER_ACTIONS = {
    A.ORDER_NEW,
    A.ORDER_ISSUED,
    A.ORDER_ASSIGN,
    A.ORDER_LATE,
    A.ORDER_TODAY,
    A.OPEN_WEB,
}
# The tabs each role's menu shows, and every page that is published.
ROLE_TABS = {"staff": (T.REPORT, T.WORK), "sup": (T.REPORT, T.WORK, T.ORDER)}
PAGES = ("user", "staff-report", "staff-work", "sup-report", "sup-work", "sup-order")
PAGE_TITLES = {
    "user": "ทุกคน",
    "staff-report": "เจ้าหน้าที่ · แจ้งเรื่อง",
    "staff-work": "เจ้าหน้าที่ · งานของกอง",
    "sup-report": "Supervisor · แจ้งเรื่อง",
    "sup-work": "Supervisor · งานของกอง",
    "sup-order": "Supervisor · สั่งงาน",
}
logger = logging.getLogger(__name__)
TOPICS = {
    "": "ไม่ระบุ",
    "meter": "มิเตอร์ (เลือกของ)",
    "transformer": "หม้อแปลง (เลือกของ)",
    "other": "อุปกรณ์อื่น (เลือกของ)",
    "system": "ระบบงาน",
    "usage": "การใช้ไฟฟ้า (ไม่ส่งให้ AI)",
    "suggestion": "ข้อเสนอแนะ",
}
KINDS = {"": "ทุกชนิด", "withdrawal": "เบิก", "return": "คืน", "problem": "แจ้งปัญหา"}


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
    return role, tab or T.REPORT


def alias(page):
    return f"{PREFIX}{page}"


def areas(page="user"):
    """[{item or tab, x, y, w, h}] of one page: the tab strip (staff menus), the tiles in rows
    of three, then the bar (แจ้งเรื่อง only)."""
    role, tab = _split(page)
    tabs = ROLE_TABS.get(role, ())
    top_h = TAB_H if tabs else 0
    top, bottom = tiles(tab), bar(tab) if tab == T.REPORT else []
    tile_h = HEIGHT - top_h - (BAR if bottom else 0)
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
    rows = [top[i : i + 3] for i in range(0, len(top), 3)]
    for r, row in enumerate(rows):
        height = tile_h // len(rows)
        for c, item in enumerate(row):
            width = WIDTH // len(row)
            out.append(
                {
                    "item": item,
                    "x": c * width,
                    "y": top_h + r * height,
                    "w": width,
                    "h": height,
                }
            )
    for c, item in enumerate(bottom):
        width = WIDTH // len(bottom)
        out.append({"item": item, "x": c * width, "y": HEIGHT - BAR, "w": width, "h": BAR})
    return out


def _area_action(area):
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
    if not any(a["item"] for a in shapes):
        raise ValidationError(f"เมนู{PAGE_TITLES[page]} ยังไม่มีปุ่มที่เปิดใช้")
    return {
        "size": {"width": WIDTH, "height": HEIGHT},
        "selected": True,
        "name": NAME if page == "user" else alias(page),
        "chatBarText": "เมนู",
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
            if a["item"] is None:
                label, note, place = T(a["tab"]).label, "", "tab"
            else:
                label, note, place = a["item"].label, a["item"].note, a["item"].place
            boxes.append(
                {
                    "label": label,
                    "note": note,
                    "place": place,
                    "active": a["item"] is None and a["tab"] == tab,
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


@transaction.atomic
def save(actor, item, values, reason):
    """Change one tile, bar button or choice (label, note, destination, order, on or off)."""
    identity.require_admin(actor)
    reason = identity._require_reason(reason)
    label = " ".join(str(values.get("label", "")).split())
    if not label or len(label) > 20:
        raise ValidationError("ชื่อปุ่มต้องมี 1 ถึง 20 ตัวอักษร (ข้อจำกัดของ LINE)")
    allowed = {
        T.WORK: WORK_ACTIONS,
        T.ORDER: ORDER_ACTIONS,
    }.get(item.tab) or {
        P.TILE: TILE_ACTIONS,
        P.BAR: BAR_ACTIONS,
        P.CHOICE: CHOICE_ACTIONS,
    }[item.place]
    action = values.get("item_action", item.action)
    if action not in allowed:
        raise ValidationError("การทำงานนี้ใช้กับปุ่มชนิดนี้ไม่ได้")
    code = values.get("division", "")
    division = Division.objects.filter(code=code, is_active=True).first() if code else None
    if code and division is None:
        raise ValidationError("ไม่พบกองนี้")
    topic, kind = values.get("topic", ""), values.get("kind", "")
    if topic not in TOPICS or kind not in KINDS:
        raise ValidationError("เลือกจากค่าที่มีให้เท่านั้น")
    before = {
        "label": item.label,
        "action": item.action,
        "division": item.division.code if item.division_id else "",
        "active": item.is_active,
    }
    item.label, item.note, item.action = label, str(values.get("note", ""))[:120], action
    item.division, item.topic, item.kind = division, topic, kind
    item.is_active = bool(values.get("is_active"))
    item.position = int(values.get("position") or 0)
    item.updated_by = actor
    item.save()
    identity.audit(
        actor,
        "line_menu.changed",
        item,
        reason,
        before=before,
        label=label,
        does=action,
        division=code,
        active=item.is_active,
    )
    return item


@transaction.atomic
def add(actor, place, parent, reason, tab=T.REPORT):
    identity.require_admin(actor)
    reason = identity._require_reason(reason)
    if tab not in T.values or (tab != T.REPORT and place != P.TILE):
        raise ValidationError("แท็บงานของกองและสั่งงานมีเฉพาะปุ่มใหญ่")
    if place == P.CHOICE and (parent is None or parent.place != P.TILE):
        raise ValidationError("ปุ่มย่อยต้องอยู่ในหัวข้อ")
    if place == P.CHOICE and parent.choices.count() >= MAX_CHOICES:
        raise ValidationError(f"หัวข้อหนึ่งมีปุ่มย่อยได้ไม่เกิน {MAX_CHOICES} ปุ่ม")
    action = {P.TILE: A.TOPIC, P.BAR: A.MINE, P.CHOICE: A.TRACK}[place]
    action = {T.WORK: A.WORK_NEW, T.ORDER: A.ORDER_NEW}.get(tab, action)
    count = LineMenuItem.objects.filter(place=place, parent=parent, tab=tab).count()
    item = LineMenuItem.objects.create(
        tab=tab,
        place=place,
        parent=parent if place == P.CHOICE else None,
        position=count + 1,
        label="ปุ่มใหม่",
        action=action,
        division=parent.division if parent else None,
        is_active=False,  # off until it is filled in
        updated_by=actor,
    )
    identity.audit(actor, "line_menu.added", item, reason, place=place)
    return item


def staff_page(user):
    """The menu page a staff LINE opens on: งานของกอง, with สั่งงาน for Supervisors."""
    return "sup-work" if identity.staff_scope(user).overseen else "staff-work"


def _staff_lines():
    """{page: [LINE user ids]} of every linked, following, active staff account."""
    from operations.models import ChannelIdentity

    groups = {}
    rows = ChannelIdentity.objects.filter(
        staff_user__isnull=False, staff_user__is_active=True, is_following=True
    ).select_related("staff_user")
    for row in rows:
        groups.setdefault(staff_page(row.staff_user), []).append(row.external_id)
    return groups


def publish(actor, pictures, reason):
    """Replace the live menus with the current data and these pictures, {page: bytes} (the
    staff pages may be left out to keep only everyone's menu), and link every staff LINE to
    the menu of its role. Only after the admin confirmed (Q32, 16 ต.ค. 2569 Q2)."""
    identity.require_admin(actor)
    reason = identity._require_reason(reason)
    if not settings.LINE_CHANNEL_ACCESS_TOKEN:
        raise ValidationError("เครื่องนี้ยังไม่ได้ตั้งค่า LINE จึงเปลี่ยนเมนูจริงไม่ได้")
    if isinstance(pictures, bytes | bytearray):
        pictures = {"user": bytes(pictures)}
    if not pictures.get("user"):
        raise ValidationError("ยังไม่มีรูปเมนูของทุกคน")
    pages = [page for page in PAGES if pictures.get(page)]
    kinds = {page: _picture(pictures[page]) for page in pages}
    menus = {page: rich_menu(page) for page in pages}
    created = {}
    try:
        existing = line_api.call("/v2/bot/richmenu/list").get("richmenus", [])
        for page in pages:
            menu_id = line_api.call("/v2/bot/richmenu", menus[page])["richMenuId"]
            created[page] = menu_id
            line_api.call(
                f"/v2/bot/richmenu/{menu_id}/content",
                pictures[page],
                base=settings.LINE_DATA_API_BASE,
                content_type=kinds[page],
            )
        line_api.call(f"/v2/bot/user/all/richmenu/{created['user']}", method="POST")
        staff_pages = [page for page in pages if page != "user"]
        if staff_pages:
            aliases = {
                a.get("richMenuAliasId")
                for a in line_api.call("/v2/bot/richmenu/alias/list").get("aliases", [])
            }
            for page in staff_pages:
                if alias(page) in aliases:
                    line_api.call(
                        f"/v2/bot/richmenu/alias/{alias(page)}", {"richMenuId": created[page]}
                    )
                else:
                    line_api.call(
                        "/v2/bot/richmenu/alias",
                        {"richMenuAliasId": alias(page), "richMenuId": created[page]},
                    )
            for page, users in _staff_lines().items():
                if page not in created:
                    continue
                for start in range(0, len(users), 500):
                    line_api.call(
                        "/v2/bot/richmenu/bulk/link",
                        {"richMenuId": created[page], "userIds": users[start : start + 500]},
                    )
        # Everyone's old menus go; old staff menus only when new staff pages replaced them.
        old = [
            m["richMenuId"]
            for m in existing
            if m["richMenuId"] not in created.values()
            and (
                m.get("name") in OLD_NAMES
                or (staff_pages and str(m.get("name", "")).startswith(PREFIX))
            )
        ]
        for previous in old:
            line_api.call(f"/v2/bot/richmenu/{previous}", method="DELETE")
    except line_api.LineApiError as exc:
        raise ValidationError(f"LINE ไม่รับเมนู ({exc})") from exc
    identity.audit(
        actor,
        "line_menu.published",
        actor,
        reason,
        rich_menus=created,
        buttons=[a["action"].get("displayText", "") for a in menus["user"]["areas"]],
        replaced=len(old),
    )
    return created["user"]


def relink(identity_row):
    """One LINE's menu after its staff link or role changed: the staff page of its role, or
    everyone's menu again. Best effort: a later publish links everyone anyway."""
    if not settings.LINE_CHANNEL_ACCESS_TOKEN:
        return
    user = identity_row.staff_user
    target = f"/v2/bot/user/{identity_row.external_id}/richmenu"
    try:
        if user is None or not user.is_active:
            line_api.call(target, method="DELETE")
            return
        menu_id = line_api.call(f"/v2/bot/richmenu/alias/{alias(staff_page(user))}").get(
            "richMenuId"
        )
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
