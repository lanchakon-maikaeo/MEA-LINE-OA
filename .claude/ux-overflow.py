# ruff: noqa: E501
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

session = Path(".claude/ux-session").read_text().strip()
JS = """() => { const w = document.documentElement.clientWidth; const out = [];
  const clipped = (el) => { for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
      const o = getComputedStyle(a).overflowX; if (o !== 'visible') return true; } return false; };
  for (const el of document.querySelectorAll('body *')) { const r = el.getBoundingClientRect();
    if (r.right > w + 1 && r.width > 0 && !el.closest('svg')) out.push(el.tagName + '.' + (el.className.baseVal ?? el.className) + ' right=' + Math.round(r.right) + ' w=' + Math.round(r.width) + ' clip=' + clipped(el)); }
  return [document.documentElement.scrollWidth, w, out.slice(0, 12)]; }"""
with sync_playwright() as p:
    b = p.chromium.launch()
    c = b.new_context(viewport={"width": int(sys.argv[1]), "height": 800})
    c.add_cookies([{"name": "sessionid", "value": session, "url": "http://127.0.0.1:8011"}])
    page = c.new_page()
    for path in sys.argv[2:]:
        page.goto("http://127.0.0.1:8011" + path, wait_until="networkidle")
        print(path, page.evaluate(JS))
    b.close()
