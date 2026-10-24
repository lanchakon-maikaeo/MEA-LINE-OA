# ruff: noqa: E501
# Screenshot one element of the local preview: python .claude/ux-element.py <out.png> <width> <path> <css selector>
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

out, width, path, selector = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
session = Path(".claude/ux-session").read_text().strip()
with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context(viewport={"width": width, "height": 900}, device_scale_factor=2)
    context.add_cookies([{"name": "sessionid", "value": session, "url": "http://127.0.0.1:8011"}])
    page = context.new_page()
    page.goto(f"http://127.0.0.1:8011{path}", wait_until="networkidle")
    page.locator(selector).first.screenshot(path=out)
    browser.close()
