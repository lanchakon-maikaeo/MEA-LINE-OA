"""เมนู LINE (15 ต.ค. 2569 Q17–Q20, Q32): the rich menu as data an admin edits.

Up to six tiles (three to a row) and up to four buttons on a bar below them. A tile opens its
choices; a choice tracks the person's cases, starts a return or a problem for the tile's division,
or asks that division a question. Editing changes what the bot answers at once; the picture on
the LINE OA changes only when an admin publishes it (drawn in the browser from the same data, or
an uploaded picture), which replaces the live rich menu.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from operations import identity, line_api
from operations.models import Division, LineMenuItem

NAME = "faoj-menu-v2"
OLD_NAMES = {"faoj-main-v1", NAME}
WIDTH, HEIGHT, BAR = 2500, 1686, 286
MAX_TILES, MAX_BAR, MAX_CHOICES = 6, 4, 12
MAX_IMAGE = 1024 * 1024  # LINE's limit for a rich menu picture
A = LineMenuItem.Action
P = LineMenuItem.Place
TILE_ACTIONS = {A.TOPIC, A.GENERAL, A.NEW, A.MINE, A.FIND}
CHOICE_ACTIONS = {A.TRACK, A.RETURN, A.PROBLEM, A.GENERAL}
BAR_ACTIONS = {A.MINE, A.FIND, A.NEW, A.GENERAL}
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


def tiles():
    return list(
        LineMenuItem.objects.filter(place=P.TILE, is_active=True)
        .select_related("division")
        .order_by("position", "pk")[:MAX_TILES]
    )


def bar():
    return list(
        LineMenuItem.objects.filter(place=P.BAR, is_active=True).order_by("position", "pk")[
            :MAX_BAR
        ]
    )


def areas():
    """[(item, x, y, width, height)] of the picture: tiles in rows of three, then the bar."""
    top, bottom = tiles(), bar()
    tile_h = HEIGHT - (BAR if bottom else 0)
    rows = [top[i : i + 3] for i in range(0, len(top), 3)]
    out = []
    for r, row in enumerate(rows):
        height = tile_h // len(rows)
        for c, item in enumerate(row):
            width = WIDTH // len(row)
            out.append((item, c * width, r * height, width, height))
    for c, item in enumerate(bottom):
        width = WIDTH // len(bottom)
        out.append((item, c * width, tile_h, width, BAR))
    return out


def rich_menu():
    """The LINE rich menu object for the current data."""
    shapes = areas()
    if not shapes:
        raise ValidationError("ยังไม่มีปุ่มที่เปิดใช้")
    return {
        "size": {"width": WIDTH, "height": HEIGHT},
        "selected": True,
        "name": NAME,
        "chatBarText": "เมนู",
        "areas": [
            {
                "bounds": {"x": x, "y": y, "width": w, "height": h},
                "action": {
                    "type": "postback",
                    "data": f"m=item&id={item.pk}",
                    "displayText": item.label,
                },
            }
            for item, x, y, w, h in shapes
        ],
    }


def drawing():
    """What the browser draws: the same boxes and words the rich menu uses."""
    return [
        {
            "label": item.label,
            "note": item.note,
            "place": item.place,
            "x": x,
            "y": y,
            "w": w,
            "h": h,
        }
        for item, x, y, w, h in areas()
    ] + [{"size": [WIDTH, HEIGHT]}]


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
def add(actor, place, parent, reason):
    identity.require_admin(actor)
    reason = identity._require_reason(reason)
    if place == P.CHOICE and (parent is None or parent.place != P.TILE):
        raise ValidationError("ปุ่มย่อยต้องอยู่ในหัวข้อ")
    if place == P.CHOICE and parent.choices.count() >= MAX_CHOICES:
        raise ValidationError(f"หัวข้อหนึ่งมีปุ่มย่อยได้ไม่เกิน {MAX_CHOICES} ปุ่ม")
    action = {P.TILE: A.TOPIC, P.BAR: A.MINE, P.CHOICE: A.TRACK}[place]
    count = LineMenuItem.objects.filter(place=place, parent=parent).count()
    item = LineMenuItem.objects.create(
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


def publish(actor, picture, reason):
    """Replace the live rich menu with the current data and this picture (Q32: confirmed)."""
    identity.require_admin(actor)
    reason = identity._require_reason(reason)
    if not settings.LINE_CHANNEL_ACCESS_TOKEN:
        raise ValidationError("เครื่องนี้ยังไม่ได้ตั้งค่า LINE จึงเปลี่ยนเมนูจริงไม่ได้")
    content_type = _picture(picture)
    menu = rich_menu()
    try:
        old = [
            m["richMenuId"]
            for m in line_api.call("/v2/bot/richmenu/list").get("richmenus", [])
            if m.get("name") in OLD_NAMES
        ]
        menu_id = line_api.call("/v2/bot/richmenu", menu)["richMenuId"]
        line_api.call(
            f"/v2/bot/richmenu/{menu_id}/content",
            picture,
            base=settings.LINE_DATA_API_BASE,
            content_type=content_type,
        )
        line_api.call(f"/v2/bot/user/all/richmenu/{menu_id}", method="POST")
        for previous in old:
            line_api.call(f"/v2/bot/richmenu/{previous}", method="DELETE")
    except line_api.LineApiError as exc:
        raise ValidationError(f"LINE ไม่รับเมนู ({exc})") from exc
    identity.audit(
        actor,
        "line_menu.published",
        actor,
        reason,
        rich_menu=menu_id,
        buttons=[a["action"]["displayText"] for a in menu["areas"]],
        replaced=len(old),
    )
    return menu_id
