"""Walk การกระทบยอดทะเบียน (M11) on the demo database for a person to check.

Three fuse withdrawals are issued in manual_reference mode with synthetic SAP document numbers and
evidence; a synthetic SAP export then agrees with one, disagrees on the quantity of another,
lacks the third, and cites a case with no record here. Registry pages (/registry/) show the
result. Synthetic staff, codes and documents only; nothing reaches a real registry.
"""

import csv
import hashlib
import io
import secrets
import uuid
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test.utils import override_settings
from django.utils import timezone

from operations import attachments, registry, services
from operations import options as system_options
from operations.contracts import Actor, CommandFailed
from operations.management.commands.demo_withdrawal import STAFF, _grant, _user
from operations.management.demo import DemoCommand
from operations.models import (
    Attachment,
    AuthorityGrant,
    Case,
    District,
    EquipmentType,
    RegistryDiscrepancy,
    RegistryRecord,
    Requester,
    Role,
    RoleGrant,
)

CODES = {
    "MTR_SYN_A": "SIM-300001",
    "TRF_SYN_A": "SIM-300004",
    "FUSE_SYN_A": "SIM-300002",
    "CB_SYN_A": "SIM-300003",
}
MOVEMENTS = {"issue": "201 261", "return": "651", "status": "321 344", "reversal": "202"}
HEADER = ["Material Document", "Posting Date", "Movement Type", "Material", "Quantity", "Reference"]


class Command(DemoCommand):
    help = "Demo of M11 (manual_reference facts, a SAP export, discrepancies) on the demo DB."

    def handle_demo(self, *args, **options):
        quiet = StringIO()
        for command in ("load_districts", "seed_demo_units", "seed_demo_cases", "seed_demo_policy"):
            call_command(command, stdout=quiet)
        users = {name: _user(name, roles) for name, roles in STAFF.items()}
        users["sim-reg"] = _user("sim-reg", [(Role.STAFF, "SIM-REG")])
        if not registry.enabled() or registry.admins_only():  # the registry unit at work
            system_options.change(
                None, system_options.REGISTRY_ACCESS, "staff", "สาธิต: หน่วยทะเบียนจำลองทำงานเอง"
            )
        _grant(users["sim-approver"], AuthorityGrant.Activity.WITHDRAWAL_APPROVAL)
        _grant(users["sim-allocator"], AuthorityGrant.Activity.ALLOCATION)
        admin = (
            RoleGrant.objects.filter(role=Role.ADMIN, revoked_at__isnull=True)
            .select_related("user")
            .first()
        )
        admin = admin.user if admin else _admin()
        for code, official in CODES.items():
            item = EquipmentType.objects.get(code=code)
            if not item.official_code:
                registry.set_official_code(admin, item, official, "รหัสจำลองสำหรับ demo_registry")
        if not any((registry.config().movement_types or {}).values()):
            registry.update_config(admin, MOVEMENTS, {}, "", "ค่าจำลองสำหรับ demo_registry")
        self.users = users
        run = uuid.uuid4().hex[:6].upper()
        with override_settings(INVENTORY_MODE="manual_reference"):
            matched = self.withdrawal(8, f"SIM-49{run}01")
            short = self.withdrawal(5, f"SIM-49{run}02")
            missing = self.withdrawal(3, f"SIM-49{run}03")
        RegistryRecord.objects.filter(case__in=[matched, short, missing]).update(
            recorded_at=timezone.now() - timedelta(days=3)
        )
        today = timezone.localdate()
        day = today - timedelta(days=3)
        rows = [
            [f"SIM-49{run}01", f"{day:%d.%m.%Y}", "261", CODES["FUSE_SYN_A"], "8-", matched.number],
            [f"SIM-49{run}02", f"{day:%d.%m.%Y}", "261", CODES["FUSE_SYN_A"], "4-", short.number],
            [f"SIM-49{run}09", f"{day:%d.%m.%Y}", "261", CODES["FUSE_SYN_A"], "2-", missing.number],
        ]
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(HEADER)
        writer.writerows(rows)
        upload = registry.import_documents(
            users["sim-reg"],
            f"sap-mb51-demo-{run}.csv",
            buffer.getvalue().encode(),
            timezone.now() - timedelta(minutes=5),
            today - timedelta(days=7),
            today,
        )
        records = RegistryRecord.objects.filter(case__in=[matched, short, missing])
        states = [registry.record_state(r) for r in records]
        self.stdout.write(f"นำเข้า {upload.original_name}: {upload.rows} บรรทัด")
        self.stdout.write(f"ตรงกับ SAP: {states.count('matched')}")
        for d in RegistryDiscrepancy.objects.filter(found_by=upload).order_by("id"):
            self.stdout.write(f"• {d.get_kind_display()}: {d.detail}")
        self.stdout.write("ดูได้ที่ /registry/ (บัญชีกองทะเบียนหรือแอดมิน)")

    # --- helpers -----------------------------------------------------------------------------

    def act(self, who, case, action, payload=None):
        return services.perform_action(
            Actor.staff(who),
            case.pk,
            action,
            expected_revision=Case.objects.get(pk=case.pk).revision,
            idempotency_key=f"demo-{uuid.uuid4().hex}",
            payload=payload,
        )

    def withdrawal(self, quantity, document):
        u = self.users
        requester, _ = Requester.objects.get_or_create(
            display_name="ผู้แจ้งจำลอง D05",
            district=District.objects.get(code="D05"),
            defaults={"approved_by": u["sim-intake"]},
        )
        result = services.submit_case(
            Actor.for_requester(requester),
            idempotency_key=f"demo-{uuid.uuid4().hex}",
            payload={
                "kind": "withdrawal",
                "purpose": "ข้อมูลจำลองสำหรับ demo_registry",
                "lines": [{"item_code": "FUSE_SYN_A", "quantity": str(quantity)}],
            },
        )
        case = Case.objects.get(pk=result.case_id)
        self.act(u["sim-intake"], case, "start_triage")
        self.act(u["sim-intake"], case, "request_handoff", {"to_division": "SIM-EQP"})
        self.act(u["sim-eqp"], case, "accept_handoff")
        suggested = case.triage.suggested_priority
        self.act(u["sim-eqp"], case, "set_priority", {"priority": suggested})
        self.act(u["sim-eqp"], case, "complete_triage")
        lines = [{"line_no": 1, "quantity": str(quantity)}]
        self.act(u["sim-approver"], case, "approve", {"lines": lines})
        data = f"%PDF-1.4\n% synthetic SAP document {document} (demo_registry)\n%%EOF\n".encode()
        name = secrets.token_hex(16)
        path = attachments.storage_dir() / name
        path.write_bytes(data)
        path.chmod(0o600)
        evidence = Attachment.objects.create(
            uploaded_by=u["sim-eqp-issuer"],
            case=case,
            declared_name=f"{document}.pdf",
            content_type="application/pdf",
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            storage_name=name,
            note="หลักฐานจำลอง (demo_registry)",
            scan_status=Attachment.ScanStatus.PENDING_SCAN,
        )
        payload = {
            "recipient_name": "ผู้รับของจำลอง เขต D05",
            "document_number": document,
            "lines": lines,
            "evidence": [str(evidence.pk)],
        }
        try:
            self.act(u["sim-eqp-issuer"], case, "issue", payload)
        except CommandFailed:
            # Imported figures say other cases want the same fuses (Q8): allocate first.
            self.act(
                u["sim-allocator"],
                case,
                "allocate",
                {"lines": lines, "reason": "จัดสรรตามลำดับคิว (จำลอง)"},
            )
            self.act(u["sim-eqp-issuer"], case, "issue", payload)
        return Case.objects.get(pk=case.pk)


def _admin():
    user = _user("sim-mockup-admin", [])
    RoleGrant.objects.get_or_create(user=user, role=Role.ADMIN, division=None)
    return user
