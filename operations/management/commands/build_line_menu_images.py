"""Build checked-in menu images with the project's Thai fonts; never calls LINE."""

import base64
import hashlib
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from operations import line_menu, line_menu_catalog


class Command(BaseCommand):
    help = "Build release menu images locally (requires the dev Playwright browser). No LINE calls."

    def handle(self, *args, **options):
        from playwright.sync_api import sync_playwright

        line_menu_catalog.sync()
        data = line_menu.drawing()
        static = Path(settings.BASE_DIR) / "operations/static/operations"
        output = Path(settings.BASE_DIR) / "operations/line_assets"
        output.mkdir(exist_ok=True)
        font_css = ""
        for subset in ("thai", "latin"):
            for weight in (400, 600, 700):
                font = (
                    static / f"fonts/ibm-plex-sans-thai-{subset}-{weight}-normal.woff2"
                ).read_bytes()
                url = "data:font/woff2;base64," + base64.b64encode(font).decode()
                unicode_range = "U+0E00-0E7F" if subset == "thai" else "U+0000-00FF"
                font_css += (
                    "@font-face{font-family:MenuThai;font-weight:"
                    + str(weight)
                    + ";src:url('"
                    + url
                    + "');unicode-range:"
                    + unicode_range
                    + ";}"
                )
        canvases = "".join(
            f'<canvas data-menu-canvas="{page}" width="2500" height="1686"></canvas>'
            for page in line_menu.PAGES
        )
        script = (static / "line_menu.js").read_text()
        html = (
            f"<style>{font_css} body{{font-family:MenuThai,sans-serif}}</style>{canvases}"
            f'<script id="menu-drawing" type="application/json">{json.dumps(data)}</script>'
            f"<script>{script}</script>"
        )
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(html)
            page.evaluate("document.fonts.ready")
            page.wait_for_timeout(100)
            for name in line_menu.PAGES:
                encoded = page.locator(f'[data-menu-canvas="{name}"]').evaluate(
                    "c => c.toDataURL('image/jpeg', 0.85).split(',')[1]"
                )
                picture = base64.b64decode(encoded)
                line_menu._picture(picture)
                (output / f"{name}.jpg").write_bytes(picture)
            browser.close()
        digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        (output / "menu-manifest.json").write_text(json.dumps({"drawing_sha256": digest}) + "\n")
        self.stdout.write(f"Built {len(line_menu.PAGES)} images: {digest}")
