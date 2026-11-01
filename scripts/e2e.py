import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright

RUN = secrets.token_hex(3).upper()
# K14: the real ฝอจ. structure (ADR 0008), one synthetic account per division. seed_org_demo
# switches the database (it refuses anything but a demo database) and keeps its own accounts'
# passwords, so these e2e accounts are added next to them.
REAL_ACCOUNTS = {"e2e-kcw": "กคว.", "e2e-kae": "กองอุปกรณ์ฯ", "e2e-ktf": "กตฟ."}
REAL_SETUP = (
    "import os\n"
    "from operations.management.commands.seed_org_demo import _account\n"
    f"for name, code in {REAL_ACCOUNTS!r}.items():\n"
    "    _account(name, code, os.environ['E2E_PASSWORD'])\n"
    "from django.contrib.auth import get_user_model\n"
    "from operations import work_orders\n"
    "from operations.models import Division, Role, RoleGrant, WorkOrder\n"
    "actor = get_user_model().objects.get(username='e2e-kcw')\n"
    "division = Division.objects.get(code='กคว.')\n"
    "RoleGrant.objects.get_or_create(user=actor, role=Role.SUPERVISOR,\n"
    "    division=division, revoked_at=None)\n"
    "# ADR 0040: only a department supervisor issues work orders.\n"
    "issuer = get_user_model().objects.get_or_create(username='e2e-dept')[0]\n"
    "RoleGrant.objects.get_or_create(user=issuer, role=Role.DEPARTMENT_SUPERVISOR,\n"
    "    division=None, revoked_at=None)\n"
    "for channel in WorkOrder.Channel.values:\n"
    "    title = 'E2E queue ' + channel\n"
    "    if not WorkOrder.objects.filter(issued_by=issuer, title=title).exists():\n"
    "        work_orders.create(issuer, title, division, actor, urgent=True, channel=channel)\n"
)
# The SIM-* journeys walk screening and the stock work, which ADR 0025 and ADR 0032 keep in the
# code behind options (default off); the real-structure journeys run with the defaults. The
# values found before the run are put back at the end, so a local demo database keeps its own.
OPTIONS = ("screening", "old_stock_work")
OPTIONS_READ = (
    "import json\n"
    "from operations.models import SystemOption\n"
    f"keys = {OPTIONS!r}\n"
    "values = dict(SystemOption.objects.filter(key__in=keys).values_list('key', 'value'))\n"
    "print(json.dumps({k: values.get(k) for k in keys}))\n"
)


def options_write(values):
    """Code for `manage.py shell -c`: None removes the row (the option's default applies)."""
    return (
        "from operations.models import SystemOption\n"
        f"for key, value in {values!r}.items():\n"
        "    if value is None:\n"
        "        SystemOption.objects.filter(key=key).delete()\n"
        "    else:\n"
        "        SystemOption.objects.update_or_create(key=key, defaults={'value': value})\n"
    )


def manage(env, *command):
    completed = subprocess.run(
        [sys.executable, "manage.py", *command], env=env, capture_output=True, text=True
    )
    if completed.returncode:
        raise RuntimeError(
            f"Demo setup command {command[0]!r} failed:\n{completed.stdout}{completed.stderr}"
        )
    return completed.stdout


def sign_in(browser, base_url, username, password, phone=False):
    """A fresh context per account (sessions never mix); keyboard-only sign-in."""
    viewport = {"width": 390, "height": 844} if phone else {"width": 1366, "height": 900}
    page = browser.new_context(viewport=viewport).new_page()
    page.goto(base_url + "/accounts/login/")
    page.get_by_label("ชื่อผู้ใช้").fill(username)
    page.get_by_label("รหัสผ่าน").fill(password)
    with page.expect_navigation(url="**/divisions/**"):
        page.keyboard.press("Enter")
    return page


def act(page, label, fill=None, submit=None):
    """Open an action from the case page (HTMX form), fill it and save; the page reloads."""
    link = page.locator("#actions").get_by_role("link", name=label, exact=True).first
    more = page.locator("#actions details.more > summary")
    if not link.is_visible() and more.count():  # irreversible actions sit behind this toggle
        more.click()
    if not link.count():
        offered = page.locator("#actions a").all_inner_texts()
        raise AssertionError(f"{label!r} is not offered on {page.url}: {offered}")
    link.click()
    form = page.locator("#action-slot form")
    expect(form).to_be_visible()
    for step in fill or []:
        step(form)
    page.evaluate("window.e2eStale = true")  # gone once HX-Redirect loads the page again
    with page.expect_response(lambda r: r.request.method == "POST" and "/act/" in r.url) as sent:
        form.get_by_role("button", name=submit or f"บันทึก: {label}").click(no_wait_after=True)
    if sent.value.status != 204:
        expect(page.locator("#action-slot .notice").first).to_be_visible()
        notices = page.locator("#action-slot .notice").all_inner_texts()
        raise AssertionError(f"{label} ({sent.value.status}): {notices}")
    page.wait_for_function("() => window.e2eStale !== true")
    page.wait_for_load_state()
    expect(page.locator(".notice--success")).to_contain_text("บันทึกแล้ว")


def open_case(page, base_url, number):
    page.goto(f"{base_url}/cases/{number}/")
    expect(page.get_by_role("heading", name=number)).to_be_visible()


def submit_case(page, base_url, kind, rows, **fields):
    """Staff file a case for the synthetic requester on the web form (round 1 Q16)."""
    page.goto(base_url + "/cases/new/")
    page.locator("#nc-requester").select_option(label="ผู้แจ้งทดสอบ E2E · การไฟฟ้านครหลวงเขตวัดเลียบ")
    page.locator(f"input[name=kind][value={kind}]").check()
    page.locator("#nc-note").fill(f"e2e {RUN}: เขตโทรแจ้ง")
    for name, value in fields.items():
        field = page.locator(f"[name={name}]")
        if field.evaluate("e => e.tagName") == "SELECT":
            field.select_option(value)
        else:
            field.fill(value)
    for i, (item, quantity, serials) in enumerate(rows):
        page.locator(f"[name=row-{i}-item]").select_option(item)
        page.locator(f"[name=row-{i}-quantity]").fill(quantity)
        page.locator(f"[name=row-{i}-serials]").fill(serials)
    with page.expect_navigation():
        page.get_by_role("button", name="บันทึกเรื่อง").click()


def file_case(page, base_url, kind, rows, **fields):
    """File a case the filer's division owns; the next page is that case."""
    submit_case(page, base_url, kind, rows, **fields)
    number = page.locator("h1").inner_text().strip()
    assert page.url.endswith(f"/cases/{number}/"), page.url
    return number


def triage_to(browser, base_url, password, number, division, staff):
    intake = sign_in(browser, base_url, "e2e-intake", password)
    open_case(intake, base_url, number)
    act(intake, "เริ่มคัดกรอง")

    def choose_handoff(form):
        target = form.locator("[name=to_division]")
        proposal = target.input_value()
        target.select_option(division)
        if proposal != division:
            form.locator("[name=reason_category]").select_option("wrong_division")
            form.locator("[name=reason]").fill("ผลคัดกรองยืนยันว่ากองปลายทางรับผิดชอบ")

    act(intake, "ส่งต่อให้กองอื่น", [choose_handoff])
    intake.context.close()
    owner = sign_in(browser, base_url, staff, password)
    open_case(owner, base_url, number)
    act(owner, "รับงานนี้")
    act(owner, "ตั้งระดับความเร่งด่วน", [lambda f: f.locator("[name=priority]").select_option("P3")])
    act(owner, "คัดกรองเสร็จ ส่งดำเนินการ")
    owner.context.close()


def journeys(browser, base_url, password):
    # 1. Withdrawal, partly issued: 10 fuses asked and approved, 4 issued, 6 outstanding.
    intake = sign_in(browser, base_url, "e2e-intake", password)
    number = file_case(
        intake, base_url, "withdrawal", [("FUSE_SYN_A", "10", "")], purpose="เปลี่ยนฟิวส์ตู้ย่อย"
    )
    intake.context.close()
    triage_to(browser, base_url, password, number, "SIM-EQP", "e2e-eqp")
    approver = sign_in(browser, base_url, "e2e-approver", password)
    open_case(approver, base_url, number)
    act(approver, "รับเรื่อง", [lambda f: f.locator("[name=line-1-quantity]").fill("10")])
    issuer = sign_in(browser, base_url, "e2e-issuer", password)
    open_case(issuer, base_url, number)
    act(
        issuer,
        "บันทึกการจ่ายของ",
        [
            lambda f: f.locator("[name=recipient_name]").fill("ผู้รับทดสอบ"),
            lambda f: f.locator("[name=line-1-quantity]").fill("4"),
        ],
    )
    expect(issuer.locator("[data-tour=lines]")).to_contain_text("จ่ายบางส่วน")
    issuer.screenshot(path="reports/e2e-m10-withdrawal.png", full_page=True)
    issuer.context.close()
    # The approver stops the rest (irreversible: the form asks for a tick first).
    approver.reload()
    act(
        approver,
        "ยุติส่วนที่ค้างจ่าย",
        [
            lambda f: f.locator("[name=lines][value='1']").check(),
            lambda f: f.locator("[name=reason]").fill("เขตแจ้งว่าใช้เท่าที่ได้รับ"),
            lambda f: f.locator("[name=confirm_irreversible]").check(),
        ],
        submit="ยืนยัน: ยุติส่วนที่ค้างจ่าย",
    )
    approver.context.close()

    # 2. Return waiting for inspection: the receiver records what arrived.
    intake = sign_in(browser, base_url, "e2e-intake", password)
    returned = f"E2E-{RUN}"
    number = file_case(
        intake,
        base_url,
        "return",
        [("MTR_SYN_A", "1", returned)],
        condition="ชำรุด",
        reason="ถอดเปลี่ยน",
    )
    intake.context.close()
    triage_to(browser, base_url, password, number, "SIM-MTR", "e2e-meter")
    receiver = sign_in(browser, base_url, "e2e-receiver", password)
    open_case(receiver, base_url, number)
    act(
        receiver,
        "บันทึกรับของคืน",
        [
            lambda f: f.locator("[name=line-1-serials]").fill(returned),
            lambda f: f.locator("[name=carrier_name]").fill("พนักงานเขต"),
        ],
    )
    expect(receiver.locator("[data-tour=units]")).to_contain_text("รอตรวจ")
    receiver.screenshot(path="reports/e2e-m10-return.png", full_page=True)
    receiver.context.close()

    # 3. Hazard problem on a phone: P0 at once; a route against the proposal needs a reason.
    intake = sign_in(browser, base_url, "e2e-intake", password, phone=True)
    number = file_case(
        intake,
        base_url,
        "problem",
        [("TRF_SYN_A", "", "")],
        description="มีควันขึ้นที่หม้อแปลง เสียงดังผิดปกติ",
    )
    expect(intake.locator("[data-tour=hazard]")).to_be_visible()
    expect(intake.locator(".case-head .prio--P0")).to_be_visible()
    # Keyboard only: the primary button is reachable and opens its form.
    act(intake, "เริ่มคัดกรอง")
    intake.locator("#actions").get_by_role("link", name="ส่งต่อให้กองอื่น").focus()
    intake.keyboard.press("Enter")
    form = intake.locator("#action-slot form")
    expect(form).to_be_visible()
    form.locator("[name=to_division]").select_option("SIM-MTR")
    form.locator("[name=reason]").fill("ช่างกองมิเตอร์อยู่หน้างาน")
    form.get_by_role("button", name="บันทึก: ส่งต่อให้กองอื่น").click()
    expect(intake.locator("#action-slot .notice--error")).to_contain_text("เลือกประเภทการส่งต่อ")
    expect(form.locator("[name=reason]")).to_have_value("ช่างกองมิเตอร์อยู่หน้างาน")
    form = intake.locator("#action-slot form")
    form.locator("[name=to_division]").select_option("SIM-MTR")
    form.locator("[name=reason_category]").select_option("new_evidence")
    with intake.expect_navigation():
        form.get_by_role("button", name="บันทึก: ส่งต่อให้กองอื่น").click()
    handoff = intake.locator("[data-tour=actions] .handoff")
    expect(handoff).to_contain_text("รอ SIM-MTR ตอบรับการส่งต่อ")
    # The organisation fixture may name the proposed owner differently; this journey verifies
    # that the recorded handoff is explicitly against the system proposal.
    expect(handoff).to_contain_text("ส่งต่างจากที่ระบบเสนอ")
    expect(handoff).to_contain_text("ช่างกองมิเตอร์อยู่หน้างาน")
    intake.screenshot(path="reports/e2e-m10-problem-phone.png", full_page=True)
    # Cancelling ends the waiting handoff, so no test case is left in SIM-MTR's incoming tab.
    act(
        intake,
        "ยกเลิกเรื่อง",
        [
            lambda f: f.locator("[name=reason]").fill(f"e2e {RUN}: จบการทดสอบ"),
            lambda f: f.locator("[name=confirm_irreversible]").check(),
        ],
        submit="ยืนยัน: ยกเลิกเรื่อง",
    )
    expect(intake.locator(".handoff")).to_have_count(0)
    intake.context.close()


def take_case(page, section=None):
    """Owner division (ADR 0025, ADR 0032): optionally name the แผนก, take the case at P3."""
    if section:
        act(page, "ระบุแผนก", [lambda f: f.locator("[name=section]").select_option(label=section)])
        expect(page.locator("dl.facts dd").first).to_contain_text(section)
    act(
        page,
        "รับเรื่อง",
        [
            lambda f: f.locator("[name=priority]").select_option("P3"),
            lambda f: f.locator("[name=reason]").fill(f"e2e {RUN}: ระดับที่ตกลงในการทดสอบ"),
        ],
    )


def answer_for_district(page):
    """The district answered by phone (its requester has no LINE): the case closes."""
    act(
        page,
        "บันทึกคำตอบแทนหน่วยงาน (เขต/ฝ่าย)",
        [
            lambda f: f.locator("[name=channel]").select_option("phone"),
            lambda f: f.locator("[name=intake_note]").fill(f"e2e {RUN}: เขตโทรแจ้ง"),
        ],
    )
    expect(page.locator(".case-head__title")).to_contain_text("ปิดเรื่อง")


def real_structure(browser, base_url, password):
    """K14: the real ฝอจ. structure (ADR 0008). No intake desk, so a case goes straight to the
    division the rules name; one account per division does every step (separation off)."""
    # 4. A meter and fuses in one request: one case per division, each sees only its own.
    kcw = sign_in(browser, base_url, "e2e-kcw", password)
    meter = file_case(
        kcw,
        base_url,
        "withdrawal",
        [("MTR_SYN_A", "1", ""), ("FUSE_SYN_A", "6", "")],
        purpose="เปลี่ยนมิเตอร์และฟิวส์ตู้ย่อย",
    )
    notice = kcw.locator(".notice--success")
    expect(notice).to_contain_text("แยกเป็น 2 เรื่องตามกอง")
    fuses = next(n for n in re.findall(r"W\d{2}-\d{6}", notice.inner_text()) if n != meter)
    expect(kcw.locator("dl.facts dd").first).to_contain_text("กคว. กองเครื่องวัดหน่วยไฟฟ้า")
    take_case(kcw)
    assert kcw.goto(f"{base_url}/cases/{fuses}/").status == 404
    kcw.context.close()
    kae = sign_in(browser, base_url, "e2e-kae", password)
    expect(kae.get_by_role("link", name=fuses)).to_be_visible()
    open_case(kae, base_url, fuses)
    take_case(kae, section="แผนกซ่อมสร้างอุปกรณ์ไฟฟ้า")
    # ADR 0032: the slip is issued in SAP, so no issue step shows; the case waits for the slip.
    expect(kae.locator(".case-head__title")).to_contain_text("รอดำเนินการ")
    expect(kae.locator("#actions")).not_to_contain_text("บันทึกการจ่ายของ")
    kae.screenshot(path="reports/e2e-real-withdrawal.png", full_page=True)
    kae.context.close()

    # 5. Electricity use (การใช้ไฟฟ้า): straight to กตฟ., a แผนก, resolved; other queues closed.
    ktf = sign_in(browser, base_url, "e2e-ktf", password, phone=True)
    number = file_case(
        ktf,
        base_url,
        "problem",
        [],
        topic="usage",
        description="สงสัยลักใช้ไฟ ต่อสายตรงไม่ผ่านมิเตอร์ (ข้อมูลจำลอง)",
    )
    expect(ktf.locator("dl.facts dd").first).to_contain_text("กตฟ. กองตรวจสอบการใช้ไฟฟ้า")
    # ADR 0021 (25 Oct 2569): such a case may reach the AI after masking unless kept away.
    expect(ktf.locator("dl.facts")).to_contain_text("การใช้ไฟฟ้า")
    take_case(ktf, section="แผนกตรวจสอบการใช้ไฟฟ้า 1")
    act(
        ktf,
        "บันทึกผลการแก้ปัญหา",
        [
            lambda f: f.locator("[name=category]").first.check(),
            lambda f: f.locator("[name=message]").fill("ตรวจแล้ว แก้การต่อสายเรียบร้อย (ข้อมูลจำลอง)"),
        ],
    )
    ktf.screenshot(path="reports/e2e-real-usage-phone.png", full_page=True)
    answer_for_district(ktf)
    assert ktf.goto(base_url + "/divisions/กคว./").status == 404
    # K15: the rules give a meter to กคว.; กตฟ. files it and stays on a page it may open.
    submit_case(ktf, base_url, "withdrawal", [("MTR_SYN_A", "1", "")], purpose="ขอมิเตอร์")
    assert ktf.url.endswith("/cases/new/"), ktf.url
    expect(ktf.locator(".notice--success")).to_contain_text("เข้าคิวคัดกรองของ กคว.")
    expect(ktf.locator(".notice--success")).to_contain_text("บัญชีนี้จึงเปิดดูไม่ได้")
    ktf.goto(base_url + "/reports/")
    expect(ktf.get_by_text("แผนกตรวจสอบการใช้ไฟฟ้า 1").first).to_be_visible()
    ktf.context.close()
    return meter, fuses, number


def mixed_queue(browser, base_url, password):
    """LINE/web orders share the normal queue; arrival is on the right on desktop."""
    for phone in (False, True):
        page = sign_in(browser, base_url, "e2e-kcw", password, phone=phone)
        line = page.locator(".qrow[data-kind=work_order]").filter(has_text="E2E queue line")
        web = page.locator(".qrow[data-kind=work_order]").filter(has_text="E2E queue web")
        expect(line).to_be_visible()
        expect(web).to_be_visible()
        expect(line.locator(".qrow__note")).to_contain_text("สั่งโดย")
        expect(line.locator(".qrow__note")).to_contain_text("ผ่าน LINE")
        expect(web.locator(".qrow__note")).to_contain_text("ผ่าน เว็บ")
        expect(line.locator(".qrow__meta time")).to_contain_text("เข้า")
        expect(page.locator("nav a[href='/orders/']")).to_have_count(0)
        expect(page.locator(".qrow:not([data-kind=work_order])").first).to_be_visible()
        if not phone:
            arrival = line.locator(".qrow__meta time").bounding_box()
            main = line.locator(".qrow__main").bounding_box()
            assert arrival["x"] > main["x"] + main["width"]
        name = "phone" if phone else "desktop"
        line.screenshot(path=f"reports/e2e-mixed-queue-{name}.png")
        number = line.locator(".qrow__link").inner_text()
        line.locator(".qrow__link").click()
        expect(page.get_by_role("heading", name=number, exact=True)).to_be_visible()
        expect(page.get_by_text("ผ่าน LINE", exact=False).first).to_be_visible()
        page.context.close()


with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
base_url = f"http://127.0.0.1:{port}"
Path("reports").mkdir(exist_ok=True)
saved = None
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
            assert page.get_by_role("heading", name="DED Center").count() == 1
            # M10: skip link first, then the brand, then the staff sign-in button.
            page.keyboard.press("Tab")
            assert page.locator(":focus").get_attribute("href") == "#main"
            # 31 Oct: the home page is the sign-in card and no longer links the health pages.
            page.goto(base_url + "/health/live/")
            assert page.request.get(base_url + "/health/ready/").json() == {"status": "ok"}
            page.screenshot(path="reports/e2e-health.png")

            # M01: keyboard login on a phone-width screen, own division only, logout.
            page.goto(base_url + "/accounts/login/")
            page.get_by_label("ชื่อผู้ใช้").focus()
            page.keyboard.type("e2e-meter")
            page.keyboard.press("Tab")
            page.keyboard.type(env["E2E_PASSWORD"])
            page.keyboard.press("Enter")
            # M10 round 2 Q9: one division lands straight on its queue.
            page.wait_for_url("**/divisions/SIM-MTR/")
            assert page.get_by_role("heading", name="SIM-MTR กองงานมิเตอร์ (จำลอง)").count() == 1
            assert page.get_by_role("link", name="แอดมิน").count() == 0
            page.screenshot(path="reports/e2e-console.png", full_page=True)
            assert page.goto(base_url + "/divisions/SIM-TRF/").status == 404
            assert page.goto(base_url + "/console/admin/users/").status == 403
            assert page.goto(base_url + "/divisions/SIM-MTR/").status == 200
            page.get_by_label("บัญชีของ").click()
            page.get_by_role("button", name="ออกจากระบบ").click()
            page.wait_for_url("**/accounts/login/")
            assert page.goto(base_url + "/divisions/SIM-MTR/").url.startswith(
                base_url + "/accounts/login/"
            )
            saved = json.loads(manage(env, "shell", "-c", OPTIONS_READ).splitlines()[-1])
            manage(env, "shell", "-c", options_write(dict.fromkeys(OPTIONS, "on")))
            journeys(browser, base_url, env["E2E_PASSWORD"])
            # The SIM-* journeys are done; switch the same database to the real structure.
            demo = {k: v for k, v in env.items() if k != "DEMO_STAFF_PASSWORD"}
            manage(demo, "seed_org_demo")
            manage(demo, "shell", "-c", REAL_SETUP)
            manage(demo, "shell", "-c", options_write(dict.fromkeys(OPTIONS, "off")))
            real_structure(browser, base_url, env["E2E_PASSWORD"])
            mixed_queue(browser, base_url, env["E2E_PASSWORD"])
            browser.close()
        print("PASS Chromium: Thai landing, skip link, web and database health")
        print("PASS Chromium: keyboard login, own-division scope, admin denied, logout")
        print("PASS Chromium: M10 withdrawal partly issued (old stock on: approve, issue)")
        print("PASS Chromium: M10 return waiting for inspection (receive on the case page)")
        print(
            "PASS Chromium: M10 hazard problem, route chosen against the proposal (phone, keyboard)"
        )
        print("PASS Chromium: real structure, one request split per division, taken in its แผนก")
        print(
            "PASS Chromium: LINE/web orders in normal queue, issuer/source/arrival, desktop/phone"
        )
        print(
            "PASS Chromium: real structure, electricity use to กตฟ., resolved (phone), scope, "
            "filing a case for another division"
        )
    finally:
        if saved is not None:
            manage(env, "shell", "-c", options_write(saved))
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
