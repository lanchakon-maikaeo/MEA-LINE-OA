import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import sync_playwright

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
base_url = f"http://127.0.0.1:{port}"
Path("reports").mkdir(exist_ok=True)
env = os.environ | {"E2E_PASSWORD": secrets.token_urlsafe(24)}
subprocess.run(
    [sys.executable, "manage.py", "prepare_e2e_user"], env=env, check=True, capture_output=True
)
with Path("reports/e2e-server.log").open("w") as log:
    process = subprocess.Popen(
        [sys.executable, "manage.py", "runserver", f"127.0.0.1:{port}", "--noreload"],
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
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

            # M01: keyboard login on a phone-width screen, own division only, logout.
            page.goto(base_url + "/accounts/login/")
            page.get_by_label("ชื่อผู้ใช้").focus()
            page.keyboard.type("e2e-meter")
            page.keyboard.press("Tab")
            page.keyboard.type(env["E2E_PASSWORD"])
            page.keyboard.press("Enter")
            page.wait_for_url("**/console/")
            assert page.get_by_role("link", name="SIM-MTR กองงานมิเตอร์ (จำลอง)").count() == 1
            page.screenshot(path="reports/e2e-console.png")
            assert page.goto(base_url + "/divisions/SIM-TRF/").status == 404
            assert page.goto(base_url + "/console/admin/users/").status == 403
            assert page.goto(base_url + "/divisions/SIM-MTR/").status == 200
            page.get_by_role("button", name="ออกจากระบบ").click()
            page.wait_for_url("**/accounts/login/")
            assert page.goto(base_url + "/divisions/SIM-MTR/").url.startswith(
                base_url + "/accounts/login/"
            )
            browser.close()
        print("PASS Chromium: Thai landing, keyboard link, database readiness")
        print("PASS Chromium: keyboard login, own-division scope, admin denied, logout")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
