# ruff: noqa: E501
# Top of one element at several widths: python .claude/ux-top.py <outdir> <path> <selector> <height> <width...>
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

out, path, selector, height = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
session = Path(".claude/ux-session").read_text().strip()
with sync_playwright() as p:
    browser = p.chromium.launch()
    for width in map(int, sys.argv[5:]):
        context = browser.new_context(
            viewport={"width": width, "height": 900}, device_scale_factor=2
        )
        context.add_cookies(
            [{"name": "sessionid", "value": session, "url": "http://127.0.0.1:8011"}]
        )
        page = context.new_page()
        page.goto(f"http://127.0.0.1:8011{path}", wait_until="networkidle")
        box = page.locator(selector).first.bounding_box()
        name = path.strip("/").replace("/", "_") or "root"
        page.screenshot(
            path=f"{out}/{name}-top-{width}.png",
            full_page=True,
            clip={
                "x": box["x"],
                "y": box["y"],
                "width": box["width"],
                "height": min(height, box["height"]),
            },
        )
        print(
            width,
            "overflow",
            page.evaluate("document.documentElement.scrollWidth - innerWidth"),
            "height",
            page.evaluate("document.documentElement.scrollHeight"),
        )
        context.close()
    browser.close()
