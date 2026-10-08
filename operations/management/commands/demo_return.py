"""Walk REQ_SYN_002 of synthetic-data.json through M08 for a person to check (M08 Q14).

Demo database only. The fake inventory fixture already shows MTR_SYN_005 waiting for
inspection (the registry after REQ_SYN_002's first arrival), so the demo first sets that unit
aside with a stated reason, then receives it through the system: the store ends where the
snapshot is, without editing the fixture or synthetic-data.json.
"""

import uuid
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.db import transaction

from operations import inventory, returns, services
from operations.contracts import Actor
from operations.management.commands.demo_withdrawal import STAFF, _grant, _user
from operations.management.demo import DemoCommand
from operations.models import (
    AuthorityGrant,
    Case,
    District,
    EquipmentType,
    FakeSerial,
    FakeStock,
    Requester,
    Role,
)

RETURN_STAFF = {
    **STAFF,
    "sim-mtr-receiver": [(Role.RECEIVER, "SIM-MTR")],
    "sim-mtr-inspector": [(Role.INSPECTOR, "SIM-MTR")],
}


class Command(DemoCommand):
    help = "Demo of M08 (return instructions, partial arrival, awaiting inspection) on the demo DB."

    def handle_demo(self, *args, **options):
        call_command("seed_demo_policy", stdout=StringIO())
        call_command("seed_fake_inventory", stdout=StringIO())
        self.users = {name: _user(name, roles) for name, roles in RETURN_STAFF.items()}
        _grant(self.users["sim-mtr-inspector"], AuthorityGrant.Activity.CONDITION_CERTIFICATION)
        meter = EquipmentType.objects.get(code="MTR_SYN_A")
        with transaction.atomic():
            inventory.lock_items([meter.pk])
            inventory.move_fake(
                meter,
                Decimal("1"),
                "MTR_SYN_005",
                inventory.AWAITING_INSPECTION,
                None,
                None,
                None,
                "seed:demo_return",
                "สภาพก่อน REQ_SYN_002 มาถึง (ข้อมูลจำลอง)",
                out=FakeSerial.Status.ISSUED,
            )
        self.req_syn_002(meter)

    def act(self, user, case, action, payload=None):
        return services.perform_action(
            Actor.staff(user),
            case.pk,
            action,
            expected_revision=Case.objects.get(pk=case.pk).revision,
            idempotency_key=f"demo-{uuid.uuid4().hex}",
            payload=payload,
        )

    def req_syn_002(self, meter):
        u = self.users
        district = District.objects.get(code="D02")
        requester, _ = Requester.objects.get_or_create(
            display_name="ผู้แจ้งจำลอง D02",
            district=district,
            defaults={"approved_by": u["sim-intake"]},
        )
        result = services.submit_case(
            Actor.for_requester(requester),
            idempotency_key=f"demo-{uuid.uuid4().hex}",
            payload={
                "kind": "return",
                "condition": "ตามที่แจ้งในข้อความ",
                "reason": "ขอส่งคืนมิเตอร์รุ่นจำลอง A จำนวน 2 เครื่อง",
                "source_reference": "ISSUE_HIST_SYN_020",
                "lines": [
                    {
                        "item_code": meter.code,
                        "quantity": "2",
                        "serials": ["MTR_SYN_005", "MTR_SYN_007"],
                    }
                ],
            },
        )
        case = Case.objects.get(pk=result.case_id)
        self.act(u["sim-intake"], case, "start_triage")
        self.act(u["sim-intake"], case, "request_handoff", {"to_division": "SIM-MTR"})
        self.act(u["sim-mtr"], case, "accept_handoff")
        self.act(u["sim-mtr"], case, "set_priority", {"priority": "P3"})
        self.act(u["sim-mtr"], case, "complete_triage")
        self.act(
            u["sim-mtr"],
            case,
            "return_instructions",
            {"place": "คลังกองงานมิเตอร์ (จำลอง)", "note": "ข้อมูลจำลอง"},
        )
        self.act(
            u["sim-mtr-receiver"],
            case,
            "receive",
            {
                "carrier_name": "ผู้นำส่งจำลอง เขต D02",
                "items": [{"item_code": meter.code, "serials": ["MTR_SYN_005"]}],
            },
        )
        case.refresh_from_db()
        stock = FakeStock.objects.get(item=meter)
        self.stdout.write(f"\nREQ_SYN_002: {case.number} สถานะ {case.get_status_display()}")
        self.stdout.write("  " + returns.status_text(case).replace("\n", "\n  "))
        self.stdout.write(
            f"  คลังจำลอง {meter.code}: พร้อมจ่าย {stock.available} รอตรวจ"
            f" {stock.awaiting_inspection} (เท่ากับ stock_snapshot)"
        )
        self.stdout.write(
            "  คาดหวัง (synthetic-data.json): บันทึกรับไว้จริง 1 เครื่อง ติดตามอีก 1 เครื่อง"
            " และยังไม่เพิ่มยอดพร้อมจ่ายของเครื่องที่รอตรวจ"
        )
