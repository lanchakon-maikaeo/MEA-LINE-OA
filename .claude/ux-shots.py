# Full-page screenshots of the local preview (http://127.0.0.1:8011) for the UX review.
#   python3 -m uv run --locked python .claude/ux-shots.py <outdir> [width] [path ...]
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
width = int(sys.argv[2]) if len(sys.argv) > 2 else 1440
paths = sys.argv[3:] or ["/console/"]
session = Path(".claude/ux-session").read_text().strip()
with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context(viewport={"width": width, "height": 900}, device_scale_factor=1)
    context.add_cookies([{"name": "sessionid", "value": session, "url": "http://127.0.0.1:8011"}])
    page = context.new_page()
    errors = []
    page.on(
        "console",
        lambda m: errors.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None,
    )
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    seen = 0
    page.on(
        "response", lambda r: errors.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None
    )
    for path in paths:
        page.goto(f"http://127.0.0.1:8011{path}", wait_until="networkidle")
        name = path.strip("/").replace("/", "_") or "root"
        page.screenshot(path=str(out / f"{name}-{width}.png"), full_page=True)
        overflow = page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        height = page.evaluate("document.documentElement.scrollHeight")
        print(f"{path}: height {height}px, horizontal overflow {overflow}px")
        for e in errors[seen:]:
            print("   ", e[:220])
        seen = len(errors)
    browser.close()
