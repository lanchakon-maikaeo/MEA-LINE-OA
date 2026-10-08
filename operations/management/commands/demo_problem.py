"""Walk REQ_SYN_003 and scenario S06 through M09 for a person to check (M09 Q16).

Demo database only. REQ_SYN_003: a faulty transformer reported without its impact; staff ask for
it, triage to SIM-TRF, resolve, and the requester accepts. S06: one withdrawal of a meter and a
transformer (R09) gets child work for SIM-MTR and SIM-TRF; each division issues its own line,
and the child work closes with its lines (ADR 0004). Synthetic staff and authority only.
"""

import uuid
from io import StringIO

from django.core.management import call_command
from django.utils import timezone

from operations import policy, services
from operations.contracts import Actor
from operations.management.commands.demo_withdrawal import STAFF, _grant, _user
from operations.management.demo import DemoCommand
from operations.models import (
    AckClock,
    AuthorityGrant,
    Case,
    ChildWork,
    DeadlinePause,
    Delivery,
    District,
    Requester,
    Role,
)

PROBLEM_STAFF = {
    **STAFF,
    "sim-trf": [(Role.STAFF, "SIM-TRF")],
    "sim-trf-resolver": [(Role.RESOLVER, "SIM-TRF")],
    "sim-trf-issuer": [(Role.ISSUER, "SIM-TRF")],
}


class Command(DemoCommand):
    help = "Demo of M09 (problem resolution, deadlines and pauses, R09 child work) on the demo DB."

    def handle_demo(self, *args, **options):
        call_command("seed_demo_policy", stdout=StringIO())
        call_command("seed_fake_inventory", stdout=StringIO())
        self.users = {name: _user(name, roles) for name, roles in PROBLEM_STAFF.items()}
        _grant(self.users["sim-approver"], AuthorityGrant.Activity.WITHDRAWAL_APPROVAL)
        self.req_syn_003()
        self.s06()

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

    def submit(self, requester, payload):
        result = services.submit_case(
            Actor.for_requester(requester),
            idempotency_key=f"demo-{uuid.uuid4().hex}",
            payload=payload,
        )
        return Case.objects.get(pk=result.case_id)

    def clocks(self, case):
        rows = AckClock.objects.filter(case=case).order_by("id")
        return ", ".join(
            f"{c.get_kind_display()} {'ปิด' if c.stopped_at else 'เปิด'}"
            f" ครบ {timezone.localtime(c.due_at):%d/%m %H:%M}"
            + (f" พัก {c.paused_minutes} นาที" if c.paused_minutes else "")
            for c in rows
        )

    def req_syn_003(self):
        u, d03 = self.users, self.requester("D03")
        case = self.submit(
            d03,
            {
                "kind": "problem",
                "description": "หม้อแปลงรุ่นจำลอง A หมายเลข TRF_SYN_004 ขัดข้อง ขอให้ตรวจสอบ",
                "lines": [{"item_code": "TRF_SYN_A", "serials": ["TRF_SYN_004"]}],
            },
        )
        self.act(u["sim-intake"], case, "start_triage")
        self.act(u["sim-intake"], case, "request_info", {"reason": "ขอทราบผลกระทบต่อผู้ใช้ไฟ"})
        self.act(u["sim-intake"], case, "info_received")
        self.act(
            u["sim-intake"],
            case,
            "record_facts",
            {"facts": {"severe_impact": False}, "reason": "เขตแจ้งทางโทรศัพท์ว่ายังจ่ายไฟได้"},
        )
        self.act(u["sim-intake"], case, "request_handoff", {"to_division": "SIM-TRF"})
        self.act(u["sim-trf"], case, "accept_handoff")
        self.act(u["sim-trf"], case, "set_priority", {"priority": "P2"})
        self.act(u["sim-trf"], case, "complete_triage")
        due = policy.update_due(case)
        self.act(
            u["sim-trf-resolver"],
            case,
            "resolve",
            {
                "category": "fixed",
                "message": "เปลี่ยนฟิวส์แรงสูงของหม้อแปลงแล้ว จ่ายไฟปกติ (ข้อมูลจำลอง)",
                "internal_note": "ตรวจขั้วต่อด้วย",
            },
        )
        self.act(d03, case, "confirm")
        case.refresh_from_db()
        pauses = DeadlinePause.objects.filter(case=case).count()
        self.stdout.write(f"\nREQ_SYN_003: {case.number} สถานะ {case.get_status_display()}")
        self.stdout.write(f"  กำหนดอัปเดตที่ผู้แจ้งเห็น: {timezone.localtime(due):%d/%m/%Y %H:%M} น.")
        self.stdout.write(f"  กรอบเวลา: {self.clocks(case)}")
        self.stdout.write(f"  พักกรอบเวลาระหว่างรอข้อมูลจากเขต: {pauses} ครั้ง")
        self.stdout.write(
            "  คาดหวัง (synthetic-data.json): ถามผลกระทบเพิ่มเติมแล้วให้เจ้าหน้าที่คัดกรอง"
            " ไม่สร้างรายการคืนหรือเบิกทดแทนอัตโนมัติ"
        )
        others = Case.objects.filter(related_case=case).count()
        self.stdout.write(f"  เรื่องเบิก/คืนที่ระบบสร้างให้: {others}")

    def s06(self):
        u, d01 = self.users, self.requester("D01")
        case = self.submit(
            d01,
            {
                "kind": "withdrawal",
                "purpose": "งานปรับปรุงสถานีย่อยจำลอง ต้องใช้มิเตอร์และหม้อแปลง",
                "lines": [
                    {"item_code": "MTR_SYN_A", "quantity": "1"},
                    {"item_code": "TRF_SYN_A", "quantity": "1"},
                ],
            },
        )
        self.act(u["sim-intake"], case, "start_triage")
        self.act(u["sim-intake"], case, "set_priority", {"priority": "P3"})
        for division, line in (("SIM-MTR", 1), ("SIM-TRF", 2)):
            self.act(
                u["sim-intake"],
                case,
                "request_child_work",
                {"division": division, "task": "จ่ายรายการของกอง", "rule": "R09", "lines": [line]},
            )
        self.act(u["sim-intake"], case, "complete_triage")
        self.act(u["sim-mtr"], case, "accept_child_work", {"child": 1})
        self.act(u["sim-trf"], case, "accept_child_work", {"child": 2})
        both = [{"line_no": 1, "quantity": "1"}, {"line_no": 2, "quantity": "1"}]
        self.act(u["sim-approver"], case, "approve", {"lines": both})
        self.act(
            u["sim-mtr-issuer"],
            case,
            "issue",
            {
                "recipient_name": "ผู้รับจำลอง เขต D01",
                "lines": [{"line_no": 1, "quantity": "1", "serials": ["MTR_SYN_001"]}],
            },
        )
        self.act(
            u["sim-trf-issuer"],
            case,
            "issue",
            {
                "recipient_name": "ผู้รับจำลอง เขต D01",
                "lines": [{"line_no": 2, "quantity": "1", "serials": ["TRF_SYN_001"]}],
            },
        )
        for seq in (1, 2):
            self.act(d01, case, "confirm", {"delivery": seq, "result": "full"})
        case.refresh_from_db()
        self.stdout.write(
            f"\nS06: {case.number} สถานะ {case.get_status_display()}"
            f" เจ้าภาพ {case.owner_division.code}"
        )
        for child in ChildWork.objects.filter(case=case).select_related("division"):
            status = child.get_status_display()
            self.stdout.write(f"  งานย่อย {child.seq} {child.division.code}: {status}")
        for delivery in Delivery.objects.filter(case=case).select_related("issued_by"):
            self.stdout.write(
                f"  ส่งมอบครั้งที่ {delivery.seq} บันทึกโดย {delivery.issued_by.username}:"
                f" {delivery.get_status_display()}"
            )
        self.stdout.write(
            "  คาดหวัง (priority-engine-demo.json S06): SIM-TRIAGE ประสานหลัก มีงานย่อยแยกเจ้าภาพ"
        )
