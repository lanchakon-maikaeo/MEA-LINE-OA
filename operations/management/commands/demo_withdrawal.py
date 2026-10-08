"""Walk REQ_SYN_001 and REQ_SYN_004 of synthetic-data.json through M07 for a person to check.

Demo database only. Each run resets the fake inventory, creates new cases with synthetic staff
and synthetic authority (accounts that cannot log in), and prints the separate counters.
"""

import uuid
from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from operations import fulfilment, policy, services
from operations.contracts import Actor, CommandFailed
from operations.management.demo import DemoCommand
from operations.models import (
    AuthorityGrant,
    Case,
    District,
    Division,
    EquipmentType,
    Requester,
    Role,
    RoleGrant,
    StaffProfile,
)

A = AuthorityGrant.Activity
STAFF = {
    "sim-intake": [(Role.STAFF, "SIM-TRIAGE")],
    "sim-mtr": [(Role.STAFF, "SIM-MTR")],
    "sim-mtr-issuer": [(Role.ISSUER, "SIM-MTR")],
    "sim-eqp": [(Role.STAFF, "SIM-EQP")],
    "sim-eqp-issuer": [(Role.ISSUER, "SIM-EQP")],
    "sim-approver": [(Role.APPROVER, "SIM-MTR"), (Role.APPROVER, "SIM-EQP")],
    "sim-allocator": [(Role.SUPERVISOR, "SIM-REG")],
}


def _user(username, roles):
    user, created = get_user_model().objects.get_or_create(username=username)
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
        StaffProfile.objects.create(user=user, display_name=f"{username} (จำลอง)")
    for role, code in roles:
        RoleGrant.objects.get_or_create(
            user=user, role=role, division=Division.objects.get(code=code)
        )
    return user


def _grant(user, activity):
    if policy.active_grants(user, activity).exists():
        return
    AuthorityGrant.objects.create(
        user=user,
        activity=activity,
        valid_from=timezone.now() - timedelta(minutes=1),
        reason="อำนาจจำลองสำหรับคำสั่ง demo_withdrawal",
        approval_reference="SIM-DEMO-M07",
        is_synthetic=True,
    )


class Command(DemoCommand):
    help = "Demo of M07 (approve, allocate, partial issue, receipt) on the demo database."

    def handle_demo(self, *args, **options):
        call_command("seed_demo_policy", stdout=StringIO())
        call_command("seed_fake_inventory", stdout=StringIO())
        self.users = {name: _user(name, roles) for name, roles in STAFF.items()}
        _grant(self.users["sim-approver"], A.WITHDRAWAL_APPROVAL)
        _grant(self.users["sim-allocator"], A.ALLOCATION)
        self.req_syn_001()
        self.req_syn_004()

    # --- helpers -----------------------------------------------------------------------------

    def requester(self, code):
        district = District.objects.get(code=code)
        requester, _ = Requester.objects.get_or_create(
            display_name=f"ผู้แจ้งจำลอง {code}",
            district=district,
            defaults={"approved_by": self.users["sim-intake"]},
        )
        return requester

    def act(self, who, case, action, payload=None):
        actor = Actor.for_requester(who) if isinstance(who, Requester) else Actor.staff(who)
        return services.perform_action(
            actor,
            case.pk,
            action,
            expected_revision=Case.objects.get(pk=case.pk).revision,
            idempotency_key=f"demo-{uuid.uuid4().hex}",
            payload=payload,
        )

    def triage_to(self, case, division, staff):
        u = self.users
        self.act(u["sim-intake"], case, "start_triage")
        self.act(u["sim-intake"], case, "request_handoff", {"to_division": division})
        self.act(staff, case, "accept_handoff")
        self.act(staff, case, "set_priority", {"priority": "P3"})
        self.act(staff, case, "complete_triage")

    def submit(self, requester, code, quantity, purpose):
        result = services.submit_case(
            Actor.for_requester(requester),
            idempotency_key=f"demo-{uuid.uuid4().hex}",
            payload={
                "kind": "withdrawal",
                "purpose": purpose,
                "lines": [{"item_code": code, "quantity": quantity}],
            },
        )
        return Case.objects.get(pk=result.case_id)

    def show(self, title, case, expected):
        case.refresh_from_db()
        data = fulfilment.summary(case)
        self.stdout.write(f"\n{title}: {case.number} สถานะ {case.get_status_display()}")
        for line in data["lines"]:
            self.stdout.write(
                f"  {line['item']}: ขอ {line['requested']} อนุมัติ {line['approved']}"
                f" (ไม่อนุมัติ {line['not_approved']}) จ่าย {line['issued']}"
                f" รับ {line['received']} ค้างจ่าย {line['outstanding']} {line['unit']}"
            )
        for delivery in data["deliveries"]:
            self.stdout.write(
                f"  ส่งมอบครั้งที่ {delivery['seq']} {delivery['document_number']}:"
                f" {delivery['status_label']}"
            )
        self.stdout.write(f"  คาดหวัง (synthetic-data.json): {expected}")

    # --- scenarios -----------------------------------------------------------------------------

    def req_syn_001(self):
        u, d01 = self.users, self.requester("D01")
        case = self.submit(d01, "MTR_SYN_A", "3", "ขอเบิกมิเตอร์รุ่นจำลอง A จำนวน 3 เครื่อง เพื่อเปลี่ยนอุปกรณ์")
        self.triage_to(case, "SIM-MTR", u["sim-mtr"])
        self.act(
            u["sim-approver"],
            case,
            "approve",
            {"lines": [{"line_no": 1, "quantity": "2", "reason": "จำลองการอนุมัติบางส่วน"}]},
        )
        self.act(
            u["sim-mtr-issuer"],
            case,
            "issue",
            {
                "recipient_name": "ผู้รับของจำลอง เขต D01",
                "lines": [{"line_no": 1, "quantity": "1", "serials": ["MTR_SYN_001"]}],
            },
        )
        self.act(d01, case, "confirm", {"delivery": 1, "result": "full"})
        self.show(
            "REQ_SYN_001",
            case,
            "แสดงจำนวนแยกกัน ค้างจ่ายตามอนุมัติ 1 เครื่อง และไม่ปิดเรื่องเพียงเพราะจ่ายครั้งแรกแล้ว",
        )

    def req_syn_004(self):
        u, d04 = self.users, self.requester("D04")
        fuse = EquipmentType.objects.get(code="FUSE_SYN_A")
        case = self.submit(d04, fuse.code, "50", "ขอเบิกฟิวส์รุ่นจำลอง A จำนวน 50 ชิ้น")
        self.triage_to(case, "SIM-EQP", u["sim-eqp"])
        self.act(u["sim-approver"], case, "approve", {"lines": [{"line_no": 1, "quantity": "50"}]})
        issue = {
            "recipient_name": "ผู้รับของจำลอง เขต D04",
            "lines": [{"line_no": 1, "quantity": "40"}],
        }
        try:
            self.act(u["sim-eqp-issuer"], case, "issue", issue)
            refused = "ไม่ถูกปฏิเสธ (ผิดคาด)"
        except CommandFailed as exc:
            refused = exc.message
        self.stdout.write(f"\nREQ_SYN_004 จ่ายก่อนจัดสรร: {refused}")
        self.act(
            u["sim-allocator"],
            case,
            "allocate",
            {"lines": [{"line_no": 1, "quantity": "40"}], "reason": "ของพร้อมจ่ายมี 40 ชิ้น"},
        )
        self.act(u["sim-eqp-issuer"], case, "issue", issue)
        self.show(
            "REQ_SYN_004",
            case,
            "ยอดพร้อมจ่ายน้อยกว่าจำนวนขอ จ่ายบางส่วนตามที่จัดสรร และไม่สัญญาจ่ายครบ 50 ชิ้น",
        )
