import os
import socket
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import sync_playwright

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
base_url = f"http://127.0.0.1:{port}"
Path("reports").mkdir(exist_ok=True)
with Path("reports/e2e-server.log").open("w") as log:
    process = subprocess.Popen(
        [".venv/bin/python", "manage.py", "runserver", f"127.0.0.1:{port}", "--noreload"],
        stdout=log,
        stderr=subprocess.STDOUT,
        env=os.environ.copy(),
    )
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError("Smoke server exited; see reports/e2e-server.log")
            try:
                with urlopen(base_url + "/health/ready/", timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("Smoke server did not become ready")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.goto(base_url)
            assert page.get_by_role("heading", name="ระบบรับเรื่อง ฝอจ.").count() == 1
            page.keyboard.press("Tab")
            assert page.locator(":focus").get_attribute("href") == "/health/live/"
            page.keyboard.press("Enter")
            page.wait_for_url("**/health/live/")
            assert page.request.get(base_url + "/health/ready/").json() == {"status": "ok"}
            page.screenshot(path="reports/e2e-health.png")
            browser.close()
        print("PASS Chromium: Thai landing, keyboard link, database readiness")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
