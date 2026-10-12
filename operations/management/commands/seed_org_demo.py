"""Switch the local demo database to the real ฝอจ. structure (real structure Q8 a, ADR 0008).

Demo database only. Loads the real units and the FAJO-2 policy, ends the synthetic central
intake (so new cases go straight to the division), closes the SIM-* units (their old cases stay
as history), and gives each real division one synthetic account that does every step of that
division (user, 10 Oct 2569), with the separation of duties turned off in ตัวเลือกระบบ
(ADR 0009). Earlier per-role demo accounts are suspended, never deleted; requesters, LINE
identities and cases are kept. The accounts' password comes from DEMO_STAFF_PASSWORD when set
(.env.demo-staff), else they cannot log in.
"""

import json
import os

from django.contrib.auth import get_user_model
from django.db import transaction

from operations import answers, employees, identity, options, policy, services
from operations.contracts import Actor
from operations.management.commands import load_org
from operations.management.demo import DemoCommand
from operations.models import Division, PolicyVersion, Requester, StaffProfile

REAL_POLICY = load_org.DEFAULT.parent / "policy_fajo_v2.json"
EMPLOYEES = load_org.DEFAULT.parent / "employees_synthetic.csv"
# The synthetic heads of division in employees_synthetic.csv, for linking LINE by employee ID.
EMPLOYEE_IDS = {"kcw": "SYN20001", "kae": "SYN20002", "ktf": "SYN20003", "faoj": "SYN20009"}

# One account per division (username → division code).
ACCOUNTS = {
    "kcw": "กคว.",
    "kae": "กองอุปกรณ์ฯ",
    "ktf": "กตฟ.",
    "kkm": "กองเครื่องมือฯ",
    "sok": "ศอค.",
    "bt": "บท.",
    "faoj": "ฝอจ.",
}
# The per-role accounts of the first switch (9 Oct 2569), now suspended.
EARLIER = (
    "kcw-staff",
    "kcw-issuer",
    "kcw-approver",
    "kcw-resolver",
    "kae-staff",
    "kae-issuer",
    "kae-approver",
    "kae-resolver",
    "ktf-staff",
    "ktf-resolver",
    "kkm-staff",
    "sok-staff",
    "bt-staff",
    "faoj-exec",
)
CASES = [
    (
        "org-demo-1",
        "D01",
        {
            "kind": "withdrawal",
            "purpose": "เปลี่ยนมิเตอร์ชำรุด (จำลอง)",
            "lines": [{"item_code": "MTR_SYN_A", "quantity": "2"}],
        },
    ),
    (
        "org-demo-2",
        "D02",
        {
            "kind": "withdrawal",
            "purpose": "งานขยายเขต (จำลอง) ขอมิเตอร์และหม้อแปลง",
            "lines": [
                {"item_code": "MTR_SYN_A", "quantity": "1"},
                {"item_code": "TRF_SYN_A", "quantity": "1"},
            ],
        },
    ),
    (
        "org-demo-3",
        "D03",
        {"kind": "problem", "description": "หม้อแปลงมีเสียงดังผิดปกติ (จำลอง)", "topic": "transformer"},
    ),
    (
        "org-demo-4",
        "D04",
        {
            "kind": "problem",
            "topic": "usage",
            "description": "สงสัยลักใช้ไฟ ต่อไฟตรงไม่ผ่านมิเตอร์ (ข้อมูลจำลอง)",
        },
    ),
    (
        "org-demo-5",
        "D01",
        {"kind": "problem", "description": "ระบบที่หน้างานใช้ไม่ได้ ยังไม่ทราบสาเหตุ (จำลอง)"},
    ),
]


def _account(username, code, password):
    division = Division.objects.get(code=code)
    user, created = get_user_model().objects.get_or_create(username=username)
    if created or password:
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.is_active = True
        user.save(update_fields=["password", "is_active"])
    StaffProfile.objects.update_or_create(
        user=user, defaults={"display_name": f"บัญชี {code} (จำลอง)"}
    )
    identity.grant_full_division(
        None, user, division, "บัญชีเดียวของกองสำหรับสาธิตบนเครื่อง", "LOCAL-DEMO-ORG"
    )
    return user


class Command(DemoCommand):
    help = "Use the real ฝอจ. structure on the local demo database (keeps earlier data)."

    @transaction.atomic
    def handle_demo(self, *args, **options_):
        divisions, sections, _ = load_org.load(
            json.loads(load_org.DEFAULT.read_text(encoding="utf-8"))
        )
        self.stdout.write(f"Real units: {divisions} divisions, {sections} sections changed.")
        version = policy.import_policy(policy.load_document(REAL_POLICY))
        if version.status != PolicyVersion.Status.ACTIVE:
            policy.activate_policy(
                None, version, "ผู้ใช้ (สาธิตบนเครื่อง)", "LOCAL-DEMO", "ใช้โครงสร้างจริงบนเครื่อง"
            )
        Division.objects.filter(is_intake=True).update(is_intake=False)
        closed = Division.objects.filter(code__startswith="SIM-", is_active=True).update(
            is_active=False
        )
        # The user's choices of 10 Oct 2569 (ADR 0009, ADR 0010) on the local demo.
        chosen = {
            options.SEPARATION: "off",
            options.REGISTRATION_CHECK: "employee_auto",
            options.REGISTRATION_LOCATION: "optional",
            options.ANONYMOUS: "on",
            options.STAFF_LINK: "code_or_employee",
            options.STAFF_LINE_ACTIONS: "act",
            options.CHAT_ANSWERS: "write",  # ก+ข+ค, AI approved by the user (ADR 0012)
            options.CHAT_FOLLOWUP: "ai",  # "ใช่": the AI words the next question (ADR 0012)
            # 11 Oct 2569 "ปรับที่ยังปรับต่อได้": questions and new requests at typed steps.
            options.CHAT_TYPED: "check",
        }
        for key, value in chosen.items():
            if options.get(key) != value:
                options.change(None, key, value, "ค่าที่ผู้ใช้เลือก 10 ต.ค. 2569 (สาธิตบนเครื่อง)")
        employees.import_csv(
            None,
            EMPLOYEES.read_text(encoding="utf-8"),
            "วันเกิด (วว/ดด/ปปปป)",
            synthetic=True,
        )
        # คลังคำตอบ: the draft bank approved as synthetic text for the local demo only.
        answers.load(
            json.loads(answers.DEFAULT.read_text(encoding="utf-8")),
            approve_as=identity.system_user(),
        )
        password = os.environ.get("DEMO_STAFF_PASSWORD", "")
        users = [_account(name, code, password) for name, code in ACCOUNTS.items()]
        for name, employee_id in EMPLOYEE_IDS.items():
            StaffProfile.objects.filter(user__username=name).update(employee_id=employee_id)
        suspended = (
            get_user_model()
            .objects.filter(username__in=EARLIER, is_active=True)
            .update(is_active=False)
        )
        made = 0
        for key, district, payload in CASES:
            requester = Requester.objects.filter(display_name=f"ผู้แจ้งจำลอง {district}").first()
            if requester is None:
                continue
            services.submit_case(
                Actor.for_requester(requester), idempotency_key=key, payload=payload
            )
            made += 1
        self.stdout.write(
            f"Policy {version.code} active; central intake ended; {closed} SIM-* units closed; "
            f"{len(users)} division accounts (every step of their division); {suspended} earlier "
            f"accounts suspended; separation of duties off; {made} example requests."
        )
