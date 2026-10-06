from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from operations import line_api

NAME = "faoj-main-v1"
IMAGE = Path(__file__).resolve().parents[2] / "line_assets" / "rich-menu.png"
AREAS = [("m=new", "ส่งเรื่องใหม่"), ("m=mine", "เรื่องของฉัน"), ("m=track", "ติดตามเรื่อง")]


def rich_menu():
    width, height = 2500, 843
    cell = width // len(AREAS)
    return {
        "size": {"width": width, "height": height},
        "selected": True,
        "name": NAME,
        "chatBarText": "เมนู",
        "areas": [
            {
                "bounds": {"x": i * cell, "y": 0, "width": cell, "height": height},
                "action": {"type": "postback", "data": data, "displayText": label},
            }
            for i, (data, label) in enumerate(AREAS)
        ],
    }


class Command(BaseCommand):
    help = (
        "Create the 3-button rich menu on the LINE OA and make it the default "
        "(replaces older menus with the same name). Changes the live OA."
    )

    def handle(self, *args, **options):
        if not settings.LINE_CHANNEL_ACCESS_TOKEN:
            raise CommandError("Set LINE_CHANNEL_ACCESS_TOKEN in .env first.")
        old = [
            m["richMenuId"]
            for m in line_api.call("/v2/bot/richmenu/list").get("richmenus", [])
            if m.get("name") == NAME
        ]
        menu_id = line_api.call("/v2/bot/richmenu", rich_menu())["richMenuId"]
        line_api.call(
            f"/v2/bot/richmenu/{menu_id}/content",
            IMAGE.read_bytes(),
            base=settings.LINE_DATA_API_BASE,
            content_type="image/png",
        )
        line_api.call(f"/v2/bot/user/all/richmenu/{menu_id}", method="POST")
        for menu in old:
            line_api.call(f"/v2/bot/richmenu/{menu}", method="DELETE")
        self.stdout.write(f"Default rich menu set ({len(old)} older menu(s) removed).")
