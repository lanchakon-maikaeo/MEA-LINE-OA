"""M07 withdrawal: approval, allocation, issue, receipt and closing (PROJECT.md M07, Q1–Q23,
ADR 0003).

Acceptance → tests: separate counters (CounterTests), partial outstanding and REQ_SYN_001/004
(Scenario*), short stock never promised (AllocationTests), concurrent issue within stock and
approval (ConcurrencyTests + the database constraint), double receipt and idempotency
(ReceiptTests, ConcurrencyTests), cancellation after issue with a resolution and no stock
reversal (CancelTests).
"""

import threading
import uuid
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from config.environment import inventory_mode
from operations import (
    fulfilment,
    inventory,
    line_api,
    notifications,
    options,
    orchestration,
    policy,
    receipts,
    services,
    workflow,
)
from operations.contracts import Actor, CommandFailed, ErrorCode
from operations.models import (
    Allocation,
    AuthorityGrant,
    Case,
    CaseLine,
    CaseTriage,
    ChannelIdentity,
    Decision,
    Delivery,
    EquipmentType,
    FakeSerial,
    FakeStock,
    FulfilmentEntry,
    IssuedSerial,
    NotificationDelivery,
    OutboxEvent,
    PostbackToken,
    Receipt,
    Role,
    StockMovement,
    SystemOption,
)
from operations.tests.test_m02_cases import key, make_staff, setup_world, withdrawal
from operations.tests.test_m03_line import ALICE, BOB, LineTestCase, texts

S = Case.Status
K = Case.Kind
A = AuthorityGrant.Activity
D = Delivery.Status


def evidence_file(user, case, status="pending_scan"):
    """A staff upload standing for the scanned SAP document (bytes are not needed here)."""
    from operations.models import Attachment

    return Attachment.objects.create(
        uploaded_by=user,
        case=case,
        declared_name="sap-document.pdf",
        content_type="application/pdf",
        size=100,
        sha256="0" * 64,
        storage_name="",
        scan_status=status,
    )


def fuse(quantity):
    return withdrawal(lines=[{"item_code": "FUSE_SYN_A", "quantity": str(quantity)}])


def lines(*rows):
    return {
        "lines": [dict(zip(("line_no", "quantity", "reason"), row, strict=False)) for row in rows]
    }


class Staffing:
    """Staff, grants and helpers shared by TestCase and TransactionTestCase classes."""

    def staff_up(self):
        # ADR 0032: these tests cover the stock work kept behind ตัวเลือกระบบ old_stock_work.
        SystemOption.objects.update_or_create(key=options.OLD_STOCK, defaults={"value": "on"})
        call_command("seed_demo_policy", stdout=StringIO())
        call_command("seed_fake_inventory", stdout=StringIO())
        self.issuer = make_staff("mtrissuer", (Role.ISSUER, "SIM-MTR"))
        self.eqp = make_staff("eqp", (Role.STAFF, "SIM-EQP"))
        self.eqp_issuer = make_staff("eqpissuer", (Role.ISSUER, "SIM-EQP"))
        self.eqp_issuer2 = make_staff("eqpissuer2", (Role.ISSUER, "SIM-EQP"))
        # A role is a duty: this approver gets authority only from the grant below (Q2).
        self.approver = make_staff("approver", (Role.APPROVER, "SIM-MTR"))
        self.allocator = make_staff("allocator", (Role.SUPERVISOR, "SIM-REG"))
        Staffing.grant(self, self.approver, A.WITHDRAWAL_APPROVAL)
        Staffing.grant(self, self.allocator, A.ALLOCATION)

    def grant(self, user, activity, division=None, **extra):
        return AuthorityGrant.objects.create(
            user=user,
            activity=activity,
            division=division,
            valid_from=timezone.now() - timedelta(minutes=1),
            reason="ทดสอบ",
            approval_reference="TEST-M07",
            granted_by=self.admin,
            **extra,
        )

    def submit(self, payload=None, requester=None):
        result = services.submit_case(
            Actor.for_requester(requester or self.req1),
            idempotency_key=key(),
            payload=payload or withdrawal(),
        )
        return Case.objects.get(pk=result.case_id)

    def act(self, who, case, action, payload=None, idempotency_key=None):
        staff = hasattr(who, "is_authenticated")
        actor = Actor.staff(who) if staff else Actor.for_requester(who)
        if staff:
            payload = Staffing.with_evidence(who, case, action, payload)
        return services.perform_action(
            actor,
            case.pk,
            action,
            expected_revision=Case.objects.get(pk=case.pk).revision,
            idempotency_key=idempotency_key or key(),
            payload=payload,
        )

    @staticmethod
    def with_evidence(user, case, action, payload):
        """M11: in manual_reference mode the web stores the SAP document with the fact; the
        fixtures attach a synthetic one the same way unless the test passes its own."""
        from operations import registry

        if inventory.mode() != inventory.MANUAL or action not in registry.EVIDENCE_ACTIONS:
            return payload
        payload = dict(payload or {})
        if "evidence" not in payload:
            payload["evidence"] = [str(evidence_file(user, case).pk)]
        return payload

    def ready(self, payload=None, division="SIM-MTR", staff=None, requester=None):
        """Submitted, triaged to `division` and waiting for approval."""
        staff = staff or (self.meter if division == "SIM-MTR" else self.eqp)
        case = self.submit(payload, requester)
        self.act(self.intake, case, "start_triage")
        self.act(self.intake, case, "request_handoff", {"to_division": division})
        self.act(staff, case, "accept_handoff")
        self.act(staff, case, "set_priority", {"priority": "P3"})
        self.act(staff, case, "complete_triage")
        return Case.objects.get(pk=case.pk)

    def approved(self, quantity, payload=None, division="SIM-MTR"):
        case = self.ready(payload, division)
        self.act(self.approver, case, "approve", lines((1, str(quantity), "ทดสอบ")))
        return Case.objects.get(pk=case.pk)

    def issue(self, user, case, quantity, serials=None, **extra):
        row = {"line_no": 1, "quantity": str(quantity)}
        if serials is not None:
            row["serials"] = serials
        return self.act(
            user, case, "issue", {"recipient_name": "ผู้รับจำลอง", "lines": [row], **extra}
        )

    def r09_children(self, case):
        """M09 Q18: an R09 case sends child work for each line before triage completes."""
        for line in CaseLine.objects.filter(case=case).select_related("item"):
            division = {"meter": "SIM-MTR", "transformer": "SIM-TRF"}.get(
                line.item.category, "SIM-EQP"
            )
            self.act(
                self.intake,
                case,
                "request_child_work",
                {
                    "division": division,
                    "task": "รายการของกอง",
                    "rule": "R09",
                    "lines": [line.line_no],
                },
            )

    def line(self, case, number=1):
        return CaseLine.objects.get(case=case, line_no=number)

    def stock(self, code):
        return FakeStock.objects.get(item__code=code).available


class FulfilmentTestCase(Staffing, TestCase):
    @classmethod
    def setUpTestData(cls):
        setup_world(cls)
        Staffing.staff_up(cls)

    def assertFails(self, code, fn, *args, **kwargs):
        with self.assertRaises(CommandFailed) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        return caught.exception

    def proposal(self, case):
        return (
            CaseTriage.objects.select_related("current_recommendation")
            .get(case=case)
            .current_recommendation
        )


# --- Matrix and settings ---------------------------------------------------------------------


class MatrixTests(FulfilmentTestCase):
    def test_m07_actions_belong_to_withdrawals_only(self):
        # ADR 0025: approval belongs to every kind; the rest stays with withdrawals.
        for name in ("approve", "deny_approval"):
            self.assertEqual(
                workflow.ACTIONS[name].enabled_kinds, [K.WITHDRAWAL, K.RETURN, K.PROBLEM], name
            )
        for name in (
            "allocate",
            "issue",
            "terminate_outstanding",
            "resolve_discrepancy",
        ):
            spec = workflow.ACTIONS[name]
            self.assertTrue(spec.enabled, name)
            self.assertEqual(spec.enabled_kinds, [K.WITHDRAWAL], name)
        self.assertEqual(
            workflow.ACTIONS["confirm"].enabled_kinds, [K.WITHDRAWAL, K.RETURN, K.PROBLEM]
        )  # ADR 0055: a return followed by its number is confirmed too
        self.assertEqual(
            workflow.ACTIONS["close"].enabled_kinds, [K.WITHDRAWAL, K.RETURN, K.PROBLEM]
        )
        # Q16: no reopening of withdrawals (nor returns, M08 Q13); problems only (M09).
        self.assertEqual(workflow.ACTIONS["reopen"].enabled_kinds, [K.PROBLEM])

    def test_a_closed_withdrawal_cannot_be_reopened(self):
        case = self.approved(3)
        self.act(self.approver, case, "terminate_outstanding", {"lines": [1], "reason": "ยุติ"})
        self.assertEqual(Case.objects.get(pk=case.pk).status, S.CLOSED)
        self.assertFails(
            ErrorCode.INVALID_TRANSITION,
            self.act,
            self.meter,
            case,
            "reopen",
            {"reason": "ของชำรุด"},
        )

    def test_the_fake_inventory_is_refused_in_production(self):
        self.assertEqual(inventory_mode("local", ""), "fake")
        self.assertEqual(inventory_mode("staging", ""), "fake")
        self.assertEqual(inventory_mode("production", ""), "manual_reference")
        with self.assertRaises(ImproperlyConfigured):
            inventory_mode("production", "fake")
        with self.assertRaises(ImproperlyConfigured):
            inventory_mode("local", "spreadsheet")

    def test_contract_lists_m07_payload_fields(self):
        from operations.contracts import contract_document

        actions = contract_document()["actions"]
        self.assertIn("lines", actions["approve"]["payload"])
        self.assertIn("document_number", actions["issue"]["payload"])
        self.assertFalse(actions["deny_approval"]["reason_required"])


# --- Approval (Q2, Q3, Q4, Q13) ----------------------------------------------------------------


class ApprovalTests(FulfilmentTestCase):
    def test_counters_stay_separate_and_less_than_requested_needs_a_reason(self):
        case = self.ready()
        result = self.act(self.approver, case, "approve", lines((1, "2")))
        self.assertEqual(result.status, S.AWAITING_EXECUTION)
        line = self.line(case)
        self.assertEqual(
            (line.requested_quantity, line.approved_quantity, line.issued_quantity),
            (Decimal("3"), Decimal("2"), Decimal("0")),
        )
        self.assertEqual(line.outstanding, Decimal("2"))
        entry = FulfilmentEntry.objects.get(line=line)
        self.assertEqual((entry.kind, entry.quantity), (FulfilmentEntry.Kind.APPROVED, 2))
        self.assertEqual(entry.case_revision, Case.objects.get(pk=case.pk).revision)

    def test_approvals_are_final_and_never_above_the_request(self):
        case = self.ready()
        self.assertFails(
            ErrorCode.VALIDATION_ERROR, self.act, self.approver, case, "approve", lines((1, "4"))
        )
        self.assertFails(
            ErrorCode.VALIDATION_ERROR,
            self.act,
            self.approver,
            case,
            "approve",
            lines((1, "1.5", "ครึ่ง")),
        )
        self.act(self.approver, case, "approve", lines((1, "3")))
        self.assertFails(
            ErrorCode.INVALID_TRANSITION, self.act, self.approver, case, "approve", lines((1, "3"))
        )

    def test_authority_comes_from_grants_not_roles_or_admin(self):
        case = self.ready()
        role_only = make_staff("roleonly", (Role.APPROVER, "SIM-MTR"))
        for user in (role_only, self.admin, self.meter):
            with self.subTest(user=user.username):
                self.assertFails(
                    ErrorCode.FORBIDDEN, self.act, user, case, "approve", lines((1, "3"))
                )
        capped = make_staff("capped", (Role.STAFF, "SIM-REG"))
        meter_item = EquipmentType.objects.get(code="MTR_SYN_A")
        self.grant(capped, A.WITHDRAWAL_APPROVAL, item=meter_item, max_quantity=Decimal("2"))
        self.assertFails(ErrorCode.FORBIDDEN, self.act, capped, case, "approve", lines((1, "3")))
        self.act(capped, case, "approve", lines((1, "2", "เพดานอำนาจ")))
        self.assertEqual(self.line(case).approved_quantity, 2)

    def test_scope_by_division_and_category(self):
        case = self.ready(fuse(5), "SIM-EQP")
        meters = make_staff("meters", (Role.STAFF, "SIM-REG"))
        self.grant(meters, A.WITHDRAWAL_APPROVAL, category="meter")
        other_division = make_staff("trfapprover", (Role.STAFF, "SIM-REG"))
        self.grant(other_division, A.WITHDRAWAL_APPROVAL, division=self.trf_division())
        # Out of their scope the case is not even visible (other divisions' queues stay closed).
        for user in (meters, other_division):
            self.assertFails(ErrorCode.NOT_FOUND, self.act, user, case, "approve", lines((1, "5")))
        self.assertFalse(services.visible_cases(Actor.staff(meters)).filter(pk=case.pk).exists())

    def trf_division(self):
        from operations.models import Division

        return Division.objects.get(code="SIM-TRF")

    def test_each_line_may_have_its_own_approver_and_the_case_waits_for_all(self):
        case = self.submit(
            withdrawal(
                lines=[
                    {"item_code": "MTR_SYN_A", "quantity": "1"},
                    {"item_code": "TRF_SYN_A", "quantity": "1"},
                ]
            )
        )
        self.act(self.intake, case, "start_triage")
        self.act(self.intake, case, "set_priority", {"priority": "P3"})
        self.r09_children(case)
        self.act(self.intake, case, "complete_triage")  # R09: intake completes
        meter_only = make_staff("meteronly", (Role.STAFF, "SIM-REG"))
        self.grant(meter_only, A.WITHDRAWAL_APPROVAL, category="meter")
        first = self.act(meter_only, case, "approve", lines((1, "1")))
        self.assertEqual(first.status, S.AWAITING_APPROVAL)
        self.assertFails(
            ErrorCode.FORBIDDEN, self.act, meter_only, case, "approve", lines((2, "1"))
        )
        second = self.act(self.approver, case, "approve", lines((2, "0", "ไม่มีงบ")))
        self.assertEqual(second.status, S.AWAITING_EXECUTION)

    def test_nothing_approved_is_rejected_with_a_reason(self):
        case = self.ready()
        result = self.act(self.approver, case, "approve", lines((1, "0", "ไม่ตรงระเบียบ")))
        self.assertEqual(result.status, S.REJECTED)
        other = self.ready()
        result = self.act(self.approver, other, "deny_approval", {"reason": "ซ้ำกับเรื่องเดิม"})
        self.assertEqual(result.status, S.REJECTED)
        self.assertEqual(self.line(other).approval_reason, "ซ้ำกับเรื่องเดิม")

    def test_a_decided_line_is_not_decided_again_while_others_wait(self):
        case = self.submit(
            withdrawal(
                lines=[
                    {"item_code": "MTR_SYN_A", "quantity": "2"},
                    {"item_code": "TRF_SYN_A", "quantity": "1"},
                ]
            )
        )
        self.act(self.intake, case, "start_triage")
        self.act(self.intake, case, "set_priority", {"priority": "P3"})
        self.r09_children(case)
        self.act(self.intake, case, "complete_triage")
        self.act(self.approver, case, "approve", lines((1, "1", "บางส่วน")))
        error = self.assertFails(
            ErrorCode.VALIDATION_ERROR, self.act, self.approver, case, "approve", lines((1, "2"))
        )
        self.assertIn("Q13", error.message)
        self.assertEqual(self.line(case).approved_quantity, 1)

    def test_deny_is_refused_once_something_was_approved(self):
        case = self.submit(
            withdrawal(
                lines=[
                    {"item_code": "MTR_SYN_A", "quantity": "1"},
                    {"item_code": "TRF_SYN_A", "quantity": "1"},
                ]
            )
        )
        self.act(self.intake, case, "start_triage")
        self.act(self.intake, case, "set_priority", {"priority": "P3"})
        self.r09_children(case)
        self.act(self.intake, case, "complete_triage")
        self.act(self.approver, case, "approve", lines((1, "1")))
        self.assertFails(
            ErrorCode.VALIDATION_ERROR,
            self.act,
            self.approver,
            case,
            "deny_approval",
            {"reason": "x"},
        )

    def test_whoever_filed_for_the_requester_does_not_approve_it(self):
        self.grant(self.intake, A.WITHDRAWAL_APPROVAL)
        result = services.submit_case(
            Actor.staff(self.intake),
            idempotency_key=key(),
            payload={
                **withdrawal(),
                "requester_id": self.req1.pk,
                "channel": "phone",
                "intake_note": "เขตโทรมา",
            },
        )
        case = Case.objects.get(pk=result.case_id)
        self.act(self.intake2, case, "start_triage")
        self.act(self.intake2, case, "request_handoff", {"to_division": "SIM-MTR"})
        self.act(self.meter, case, "accept_handoff")
        self.act(self.meter, case, "set_priority", {"priority": "P3"})
        self.act(self.meter, case, "complete_triage")
        error = self.assertFails(
            ErrorCode.FORBIDDEN, self.act, self.intake, case, "approve", lines((1, "3"))
        )
        self.assertIn("Q4", error.message)

    def test_grant_rules_for_the_new_activities(self):
        meter_item = EquipmentType.objects.get(code="MTR_SYN_A")
        user = make_staff("granted", (Role.STAFF, "SIM-MTR"))

        def grant(**kwargs):
            return policy.grant_authority(
                self.admin, user, reason="r", approval_reference="x", **kwargs
            )

        with self.assertRaises(ValidationError):
            grant(activity=A.WITHDRAWAL_APPROVAL, max_quantity=Decimal("5"))  # cap needs an item
        with self.assertRaises(ValidationError):
            grant(activity=A.WITHDRAWAL_APPROVAL, category="other", item=meter_item)
        with self.assertRaises(ValidationError):
            grant(activity=A.ROUTING_DECISION, item=meter_item)
        with self.assertRaises(ValidationError):
            grant(activity=A.ALLOCATION, item=meter_item, max_quantity=Decimal("1"))
        made = grant(activity=A.WITHDRAWAL_APPROVAL, item=meter_item, max_quantity=Decimal("5"))
        self.assertEqual(made.category, "meter")
        with self.assertRaises(IntegrityError), transaction.atomic():
            AuthorityGrant.objects.create(
                user=user,
                activity=A.WITHDRAWAL_APPROVAL,
                max_quantity=Decimal("1"),
                valid_from=timezone.now(),
                reason="r",
                approval_reference="x",
            )


# --- Issue (Q5, Q10, Q11) ---------------------------------------------------------------------


class IssueTests(FulfilmentTestCase):
    def test_req_syn_001_partial_issue_keeps_the_case_open(self):
        case = self.ready()
        self.act(self.approver, case, "approve", lines((1, "2", "จำลองการอนุมัติบางส่วน")))
        result = self.issue(self.issuer, case, 1, ["MTR_SYN_001"])
        self.assertEqual(result.status, S.IN_PROGRESS)
        delivery = Delivery.objects.get(case=case)
        self.assertTrue(delivery.document_number.startswith("SIM-ISS"))
        self.assertEqual(self.stock("MTR_SYN_A"), 2)
        self.assertEqual(
            FakeSerial.objects.get(serial="MTR_SYN_001").status, FakeSerial.Status.ISSUED
        )
        self.act(self.req1, case, "confirm", {"delivery": 1, "result": "full"})
        case.refresh_from_db()
        self.assertEqual(case.status, S.IN_PROGRESS)  # not closed by the first delivery
        self.assertFails(  # double receipt of one delivery
            ErrorCode.VALIDATION_ERROR,
            self.act,
            self.req1,
            case,
            "confirm",
            {"delivery": 1, "result": "full"},
        )
        self.assertEqual(Receipt.objects.count(), 1)
        line = self.line(case)
        self.assertEqual(
            [line.requested_quantity, line.approved_quantity, line.issued_quantity],
            [3, 2, 1],
        )
        self.assertEqual((line.received_quantity, line.outstanding), (1, 1))
        summary = fulfilment.summary(case)
        self.assertTrue(summary["partially_issued"])
        self.assertEqual(summary["lines"][0]["not_approved"], "1")

    def test_only_an_issuer_of_the_owner_division_who_did_not_approve_issues(self):
        case = self.approved(3)
        for user in (self.meter, self.admin):
            with self.subTest(user=user.username):
                self.assertFails(ErrorCode.FORBIDDEN, self.issue, user, case, 1, ["MTR_SYN_001"])
        # Another division's issuer does not even see the case.
        self.assertFails(ErrorCode.NOT_FOUND, self.issue, self.eqp_issuer, case, 1, ["MTR_SYN_001"])
        both = make_staff("both", (Role.ISSUER, "SIM-MTR"))
        self.grant(both, A.WITHDRAWAL_APPROVAL)
        other = self.ready()
        self.act(both, other, "approve", lines((1, "3")))
        error = self.assertFails(ErrorCode.FORBIDDEN, self.issue, both, other, 1, ["MTR_SYN_001"])
        self.assertIn("Q4", error.message)

    def test_issue_stays_within_outstanding_approval(self):
        case = self.approved(2)
        self.assertFails(
            ErrorCode.VALIDATION_ERROR,
            self.issue,
            self.issuer,
            case,
            3,
            ["MTR_SYN_001", "MTR_SYN_002", "MTR_SYN_003"],
        )
        self.issue(self.issuer, case, 2, ["MTR_SYN_001", "MTR_SYN_002"])
        line = self.line(case)
        # The database refuses counters past the approval even if a service were wrong.
        with self.assertRaises(IntegrityError), transaction.atomic():
            CaseLine.objects.filter(pk=line.pk).update(issued_quantity=Decimal("3"))

    def test_serial_rules(self):
        case = self.approved(2)
        for serials in (
            ["MTR_SYN_001"],  # count differs
            ["MTR_SYN_001", "NOPE"],  # unknown
            ["MTR_SYN_001", "MTR_SYN_004"],  # reserved in the registry
            ["MTR_SYN_005", "MTR_SYN_001"],  # waiting for inspection
        ):
            with self.subTest(serials=serials):
                self.assertFails(
                    ErrorCode.VALIDATION_ERROR, self.issue, self.issuer, case, 2, serials
                )
        fuses = self.approved(5, fuse(5), "SIM-EQP")
        self.assertFails(ErrorCode.VALIDATION_ERROR, self.issue, self.eqp_issuer, fuses, 5, ["F1"])
        self.assertEqual(self.stock("MTR_SYN_A"), 3)

    def test_the_stores_own_guards_hold_without_the_case_checks(self):
        fuse_item = EquipmentType.objects.get(code="FUSE_SYN_A")
        meter = EquipmentType.objects.get(code="MTR_SYN_A")
        with transaction.atomic():
            inventory.lock_items([fuse_item.pk])
            with self.assertRaises(inventory.InventoryError):
                inventory.take_fake(fuse_item, Decimal("41"), [], None, None, "test", "DOC")
        self.assertEqual(self.stock("FUSE_SYN_A"), 40)
        case = self.approved(1)
        self.issue(self.issuer, case, 1, ["MTR_SYN_001"])
        error = self.assertFails(
            ErrorCode.VALIDATION_ERROR, fulfilment._serials_out, meter, ["MTR_SYN_001"]
        )
        self.assertIn("ยังไม่มีบันทึกรับคืน", error.message)

    def test_fake_store_numbers_its_documents_and_never_goes_below_zero(self):
        case = self.approved(5, fuse(5), "SIM-EQP")
        self.assertFails(
            ErrorCode.VALIDATION_ERROR,
            self.issue,
            self.eqp_issuer,
            case,
            5,
            document_number="REG-1",
        )
        FakeStock.objects.filter(item__code="FUSE_SYN_A").update(available=Decimal("3"))
        self.assertFails(ErrorCode.VALIDATION_ERROR, self.issue, self.eqp_issuer, case, 5)
        with self.assertRaises(IntegrityError), transaction.atomic():
            FakeStock.objects.filter(item__code="FUSE_SYN_A").update(available=Decimal("-1"))

    @override_settings(INVENTORY_MODE="manual_reference")
    def test_manual_reference_records_the_registry_document_and_flags_short_figures(self):
        inventory.import_snapshot(
            self.admin,
            [{"item_id": "FUSE_SYN_A", "available": 10}],
            timezone.now() - timedelta(hours=1),
            "ทะเบียนจำลอง",
            True,
        )
        case = self.approved(8, fuse(8), "SIM-EQP")
        self.assertFails(ErrorCode.VALIDATION_ERROR, self.issue, self.eqp_issuer, case, 8)
        self.issue(self.eqp_issuer, case, 8, document_number="REG-2569-0001")
        figures = inventory.availability(EquipmentType.objects.get(code="FUSE_SYN_A"))
        self.assertEqual((figures.available, figures.stale), (Decimal("2"), False))
        other = self.approved(5, fuse(5), "SIM-EQP")
        # Short by the imported figures: allocation decides (Q8), then the fact is recorded
        # with a flag instead of being blocked (Q10).
        self.act(
            self.allocator,
            other,
            "allocate",
            {"lines": [{"line_no": 1, "quantity": "2"}], "reason": "มี 2"},
        )
        self.issue(self.eqp_issuer, other, 2, document_number="REG-2569-0002")
        self.assertEqual(self.stock("FUSE_SYN_A"), 40)  # the fake store is not used here

    @override_settings(INVENTORY_MODE="manual_reference")
    def test_manual_reference_never_blocks_a_recorded_fact_on_figures(self):
        inventory.import_snapshot(
            self.admin,
            [{"item_id": "FUSE_SYN_A", "available": 1}],
            timezone.now() - timedelta(hours=30),
            "ทะเบียนจำลอง",
            True,
        )
        fuse_item = EquipmentType.objects.get(code="FUSE_SYN_A")
        self.assertTrue(inventory.availability(fuse_item).stale)
        self.assertIsNone(inventory.contended(fuse_item))
        case = self.approved(5, fuse(5), "SIM-EQP")
        self.assertIsNone(self.proposal(case).facts["shortage"]["value"])  # unknown, not false
        self.issue(self.eqp_issuer, case, 5, document_number="REG-9")
        self.assertIn("ยอดนำเข้า", Delivery.objects.get(case=case).stock_flag)

    @override_settings(INVENTORY_MODE="manual_reference")
    def test_manual_reference_serial_cannot_go_out_twice(self):
        first, second = self.approved(1), self.approved(1)
        self.issue(self.issuer, first, 1, ["M-77"], document_number="REG-1")
        self.assertFails(
            ErrorCode.VALIDATION_ERROR,
            self.issue,
            self.issuer,
            second,
            1,
            ["M-77"],
            document_number="REG-2",
        )
        self.assertEqual(IssuedSerial.objects.filter(serial="M-77", active=True).count(), 1)


# --- Allocation and shortage (Q8, Q9, Q20) -----------------------------------------------------


class AllocationTests(FulfilmentTestCase):
    def test_req_syn_004_short_stock_is_never_promised(self):
        case = self.ready(fuse(50), "SIM-EQP")
        proposal = self.proposal(case)
        self.assertIn("R08", proposal.rules)
        self.assertEqual(proposal.facts["shortage"]["source"], "stock")
        self.assertEqual(proposal.proposed_owner.code, "SIM-EQP")  # shortage never moves it
        self.act(self.approver, case, "approve", lines((1, "50")))  # approval ≠ promise
        error = self.assertFails(ErrorCode.VALIDATION_ERROR, self.issue, self.eqp_issuer, case, 40)
        self.assertIn("Q8", error.message)
        self.act(
            self.allocator,
            case,
            "allocate",
            {"lines": [{"line_no": 1, "quantity": "40"}], "reason": "มีพร้อมจ่าย 40"},
        )
        self.assertFails(ErrorCode.VALIDATION_ERROR, self.issue, self.eqp_issuer, case, 41)
        result = self.issue(self.eqp_issuer, case, 40)
        self.assertEqual(result.status, S.IN_PROGRESS)
        line = self.line(case)
        self.assertEqual((line.issued_quantity, line.outstanding), (40, 10))
        allocation = Allocation.objects.get(line=line)
        self.assertEqual(
            (allocation.used_quantity, allocation.end_reason), (40, Allocation.End.USED)
        )
        self.assertEqual(self.stock("FUSE_SYN_A"), 0)
        self.assertEqual(
            CaseTriage.objects.get(case=case).effective_priority, "P3"
        )  # never lowered or raised by shortage

    def test_allocator_decides_between_competing_cases(self):
        first = self.approved(30, fuse(30), "SIM-EQP")
        second = self.approved(20, fuse(20), "SIM-EQP")
        self.assertTrue(inventory.contended(EquipmentType.objects.get(code="FUSE_SYN_A")))
        self.assertFails(ErrorCode.VALIDATION_ERROR, self.issue, self.eqp_issuer, first, 30)

        def allocate(case, quantity):
            return self.act(
                self.allocator,
                case,
                "allocate",
                {"lines": [{"line_no": 1, "quantity": str(quantity)}], "reason": "P1 ก่อน"},
            )

        allocate(first, 30)
        self.assertFails(ErrorCode.VALIDATION_ERROR, allocate, second, 20)  # 50 > 40
        self.assertFails(ErrorCode.VALIDATION_ERROR, allocate, second, 25)  # > outstanding
        allocate(second, 10)
        self.issue(self.eqp_issuer, first, 30)
        self.issue(self.eqp_issuer, second, 10)
        self.assertEqual(self.line(second).outstanding, 10)

    def test_allocation_only_when_short_and_not_by_its_own_issuer(self):
        case = self.approved(5, fuse(5), "SIM-EQP")
        self.assertFails(
            ErrorCode.VALIDATION_ERROR,
            self.act,
            self.allocator,
            case,
            "allocate",
            {"lines": [{"line_no": 1, "quantity": "5"}], "reason": "ไม่ได้ขาด"},
        )
        self.assertFails(
            ErrorCode.FORBIDDEN,
            self.act,
            self.eqp_issuer,
            case,
            "allocate",
            {"lines": [{"line_no": 1, "quantity": "5"}], "reason": "x"},
        )
        big = self.approved(45, fuse(45), "SIM-EQP")
        self.grant(self.eqp_issuer2, A.ALLOCATION)
        self.act(
            self.eqp_issuer2,
            big,
            "allocate",
            {"lines": [{"line_no": 1, "quantity": "35"}], "reason": "แบ่ง"},
        )
        error = self.assertFails(ErrorCode.FORBIDDEN, self.issue, self.eqp_issuer2, big, 35)
        self.assertIn("Q20", error.message)

    def test_allocations_are_void_when_stock_falls_below_them(self):
        first = self.approved(30, fuse(30), "SIM-EQP")
        second = self.approved(20, fuse(20), "SIM-EQP")
        for case, quantity in ((first, 30), (second, 10)):
            self.act(
                self.allocator,
                case,
                "allocate",
                {"lines": [{"line_no": 1, "quantity": str(quantity)}], "reason": "แบ่ง"},
            )
        fuse_item = EquipmentType.objects.get(code="FUSE_SYN_A")
        inventory.adjust_fake(self.admin, fuse_item, Decimal("-15"), "ตรวจนับแล้วชำรุด 15")
        self.assertFalse(Allocation.objects.filter(ended_at__isnull=True).exists())
        self.assertEqual(
            set(Allocation.objects.values_list("end_reason", flat=True)),
            {Allocation.End.VOID_STOCK},
        )
        self.assertFails(ErrorCode.VALIDATION_ERROR, self.issue, self.eqp_issuer, first, 25)

    def test_stock_changes_refresh_the_shortage_of_open_cases(self):
        case = self.ready(fuse(30), "SIM-EQP")
        self.assertNotIn("R08", self.proposal(case).rules)
        fuse_item = EquipmentType.objects.get(code="FUSE_SYN_A")
        with self.captureOnCommitCallbacks(execute=True):
            inventory.adjust_fake(self.admin, fuse_item, Decimal("-20"), "ส่งซ่อม 20")
        proposal = self.proposal(case)
        self.assertIn("R08", proposal.rules)
        self.assertIn("ต้องใช้ 30 พร้อมจ่าย 20", proposal.facts["shortage"]["evidence"])
        movement = StockMovement.objects.filter(kind=StockMovement.Kind.ADJUST).get()
        self.assertEqual(movement.quantity, Decimal("-20"))

    def test_no_figures_means_shortage_unknown(self):
        FakeStock.objects.filter(item__code="CB_SYN_A").delete()
        case = self.ready(withdrawal(lines=[{"item_code": "CB_SYN_A", "quantity": "3"}]), "SIM-EQP")
        fact = self.proposal(case).facts["shortage"]
        self.assertEqual((fact["value"], fact["source"]), (None, "stock"))
        self.assertNotIn("R08", self.proposal(case).rules)


# --- Receipt and discrepancies (Q4, Q6, Q18, Q19) ---------------------------------------------


class ReceiptTests(FulfilmentTestCase):
    def test_full_receipt_closes_and_a_second_receipt_is_refused_or_replayed(self):
        case = self.approved(2)
        self.issue(self.issuer, case, 2, ["MTR_SYN_001", "MTR_SYN_002"])
        self.assertEqual(Case.objects.get(pk=case.pk).status, S.AWAITING_CONFIRMATION)
        k = key()
        first = self.act(self.req1, case, "confirm", {"delivery": 1, "result": "full"}, k)
        self.assertEqual(first.status, S.CLOSED)
        replay = services.perform_action(
            Actor.for_requester(self.req1),
            case.pk,
            "confirm",
            expected_revision=first.revision - 1,
            idempotency_key=k,
            payload={"delivery": 1, "result": "full"},
        )
        self.assertEqual(replay, first)
        self.assertEqual(Receipt.objects.count(), 1)
        self.assertFails(
            ErrorCode.INVALID_TRANSITION,
            self.act,
            self.req1,
            case,
            "confirm",
            {"delivery": 1, "result": "full"},
        )

    def test_only_the_owner_or_intake_on_their_behalf_confirms(self):
        case = self.approved(2)
        self.issue(self.issuer, case, 2, ["MTR_SYN_001", "MTR_SYN_002"])
        self.assertFails(
            ErrorCode.NOT_FOUND,
            self.act,
            self.req2,
            case,
            "confirm",
            {"delivery": 1, "result": "full"},
        )
        self.assertFails(
            ErrorCode.FORBIDDEN,
            self.act,
            self.meter,
            case,
            "confirm",
            {"delivery": 1, "result": "full"},
        )
        self.assertFails(  # the channel is a choice to make; the note is not forced (13 พ.ย.)
            ErrorCode.VALIDATION_ERROR,
            self.act,
            self.intake,
            case,
            "confirm",
            {"delivery": 1, "result": "full"},
        )
        self.act(
            self.intake,
            case,
            "confirm",
            {"delivery": 1, "result": "full", "channel": "phone"},
        )
        receipt = Receipt.objects.get()
        self.assertEqual((receipt.recorded_by, receipt.channel), (self.intake, "phone"))

    def test_the_issuer_never_confirms_their_own_delivery(self):
        both = make_staff("intakeissuer", (Role.ISSUER, "SIM-MTR"), (Role.STAFF, "SIM-TRIAGE"))
        case = self.approved(1)
        self.issue(both, case, 1, ["MTR_SYN_001"])
        error = self.assertFails(
            ErrorCode.FORBIDDEN,
            self.act,
            both,
            case,
            "confirm",
            {"delivery": 1, "result": "full", "channel": "phone", "intake_note": "x"},
        )
        self.assertIn("Q4", error.message)

    def test_mismatch_keeps_the_owner_and_adds_r07_then_a_recount_settles_it(self):
        case = self.approved(2)
        self.issue(self.issuer, case, 2, ["MTR_SYN_001", "MTR_SYN_002"])
        self.act(
            self.req1,
            case,
            "confirm",
            {
                "delivery": 1,
                "result": "mismatch",
                "lines": [{"line_no": 1, "serials": ["MTR_SYN_001"]}],
            },
        )
        delivery = Delivery.objects.get(case=case)
        self.assertEqual(delivery.status, D.MISMATCH)
        self.assertEqual(self.line(case).received_quantity, 1)
        proposal = self.proposal(case)
        self.assertEqual(proposal.rules[0], "R07")
        self.assertEqual(proposal.facts["serial_mismatch"]["source"], "delivery")
        self.assertEqual(Case.objects.get(pk=case.pk).owner_division.code, "SIM-MTR")
        # The requester recounted: a later full receipt supersedes, history stays (Q18).
        result = self.act(self.req1, case, "confirm", {"delivery": 1, "result": "full"})
        self.assertEqual(result.status, S.CLOSED)
        self.assertEqual(Receipt.objects.filter(delivery=delivery).count(), 2)
        self.assertEqual(self.line(case).received_quantity, 2)

    def test_mismatch_that_equals_the_issue_counts_as_full(self):
        case = self.approved(5, fuse(5), "SIM-EQP")
        self.issue(self.eqp_issuer, case, 5)
        self.act(
            self.req1,
            case,
            "confirm",
            {"delivery": 1, "result": "mismatch", "lines": [{"line_no": 1, "quantity": "5"}]},
        )
        self.assertEqual(Receipt.objects.get().result, Receipt.Result.FULL)

    def test_an_issue_recording_error_is_corrected_by_appending(self):
        case = self.approved(10, fuse(10), "SIM-EQP")
        self.issue(self.eqp_issuer, case, 10)
        self.act(
            self.req1,
            case,
            "confirm",
            {"delivery": 1, "result": "mismatch", "lines": [{"line_no": 1, "quantity": "8"}]},
        )
        fix = {
            "delivery": 1,
            "resolution": "issue_error",
            "reason": "นับผิดตอนจ่าย",
            "lines": [{"line_no": 1, "quantity": "8"}],
        }
        error = self.assertFails(
            ErrorCode.FORBIDDEN, self.act, self.eqp_issuer, case, "resolve_discrepancy", fix
        )
        self.assertIn("Q18", error.message)
        result = self.act(self.eqp, case, "resolve_discrepancy", fix)
        self.assertEqual(result.status, S.IN_PROGRESS)  # 2 back to outstanding
        line = self.line(case)
        self.assertEqual((line.issued_quantity, line.outstanding), (8, 2))
        kinds = list(FulfilmentEntry.objects.filter(line=line).values_list("kind", "quantity"))
        self.assertIn((FulfilmentEntry.Kind.CORRECTED, Decimal("-2")), kinds)
        self.assertEqual(self.stock("FUSE_SYN_A"), 30)  # no automatic stock change (Q18)
        delivery = Delivery.objects.get(case=case)
        self.assertEqual((delivery.status, delivery.resolution), (D.RESOLVED, "issue_error"))
        self.assertEqual(delivery.lines.get().quantity, 10)  # the recorded row never changes

    def test_lost_goods_need_the_registry_document(self):
        case = self.approved(10, fuse(10), "SIM-EQP")
        self.issue(self.eqp_issuer, case, 10)
        self.act(
            self.req1,
            case,
            "confirm",
            {"delivery": 1, "result": "mismatch", "lines": [{"line_no": 1, "quantity": "9"}]},
        )
        lost = {"delivery": 1, "resolution": "lost", "reason": "ตกหล่นระหว่างขนส่ง"}
        self.assertFails(
            ErrorCode.VALIDATION_ERROR, self.act, self.eqp, case, "resolve_discrepancy", lost
        )
        result = self.act(
            self.eqp, case, "resolve_discrepancy", {**lost, "reference": "REG-LOST-1"}
        )
        self.assertEqual(result.status, S.CLOSED)

    def test_staff_close_only_after_seven_days_without_an_answer(self):
        case = self.approved(1)
        self.issue(self.issuer, case, 1, ["MTR_SYN_001"])
        close = {
            "reason": "ผู้แจ้งไม่ตอบ",
            "reference": "SIM-ISS69-000001",
            "notice": "ปิดเรื่องเพราะไม่ได้รับคำตอบภายใน 7 วัน",  # ADR 0028
        }
        self.assertFails(ErrorCode.VALIDATION_ERROR, self.act, self.meter, case, "close", close)
        Delivery.objects.filter(case=case).update(issued_at=timezone.now() - timedelta(days=8))
        self.assertFails(
            ErrorCode.VALIDATION_ERROR, self.act, self.meter, case, "close", {"reason": "x"}
        )
        result = self.act(self.meter, case, "close", close)
        self.assertEqual(result.status, S.CLOSED)
        self.assertEqual(Delivery.objects.get(case=case).status, D.UNCONFIRMED)

    def test_a_reported_mismatch_blocks_closing(self):
        case = self.approved(1)
        self.issue(self.issuer, case, 1, ["MTR_SYN_001"])
        self.act(
            self.req1,
            case,
            "confirm",
            {"delivery": 1, "result": "mismatch", "lines": [{"line_no": 1, "serials": []}]},
        )
        Delivery.objects.filter(case=case).update(issued_at=timezone.now() - timedelta(days=8))
        self.assertFails(
            ErrorCode.VALIDATION_ERROR,
            self.act,
            self.meter,
            case,
            "close",
            {"reason": "x", "reference": "y"},
        )


# --- Termination and cancellation (Q7, Q19) ----------------------------------------------------


class EndingTests(FulfilmentTestCase):
    def test_terminating_the_rest_after_a_partial_issue(self):
        case = self.approved(3)
        self.issue(self.issuer, case, 1, ["MTR_SYN_001"])
        stop = {"lines": [1], "reason": "งานยกเลิกบางส่วน"}
        self.assertFails(
            ErrorCode.FORBIDDEN, self.act, self.meter, case, "terminate_outstanding", stop
        )
        result = self.act(self.approver, case, "terminate_outstanding", stop)
        self.assertEqual(result.status, S.AWAITING_CONFIRMATION)
        line = self.line(case)
        self.assertEqual((line.terminated_quantity, line.outstanding), (2, 0))
        self.assertEqual(
            self.act(self.req1, case, "confirm", {"delivery": 1, "result": "full"}).status,
            S.CLOSED,
        )

    def test_terminating_everything_before_any_issue_closes_without_issue(self):
        case = self.approved(3)
        result = self.act(
            self.approver, case, "terminate_outstanding", {"lines": [1], "reason": "ไม่มีของ"}
        )
        self.assertEqual(result.status, S.CLOSED)
        decision = Decision.objects.get(action_id=result.action_id)
        self.assertEqual(decision.data["outcome"], "ยุติโดยไม่มีการจ่าย")

    def test_cancel_after_approval_needs_approval_authority(self):
        case = self.approved(3)
        self.assertFails(
            ErrorCode.FORBIDDEN, self.act, self.meter, case, "cancel", {"reason": "เขตขอยกเลิก"}
        )
        self.assertFails(
            ErrorCode.VALIDATION_ERROR,
            self.act,
            self.approver,
            case,
            "cancel",
            {"reason": "เขตขอยกเลิก", "resolution": "keep"},
        )
        result = self.act(self.approver, case, "cancel", {"reason": "เขตขอยกเลิก"})
        self.assertEqual(result.status, S.CANCELLED)
        self.assertEqual(self.line(case).terminated_quantity, 3)

    def test_cancel_after_issue_resolves_goods_out_without_putting_stock_back(self):
        case = self.approved(10, fuse(10), "SIM-EQP")
        self.issue(self.eqp_issuer, case, 4)
        cancel = {"reason": "งานยกเลิก"}
        self.assertFails(
            ErrorCode.VALIDATION_ERROR, self.act, self.approver, case, "cancel", cancel
        )
        self.assertFails(
            ErrorCode.VALIDATION_ERROR,
            self.act,
            self.approver,
            case,
            "cancel",
            {**cancel, "resolution": "adjusted"},
        )
        result = self.act(self.approver, case, "cancel", {**cancel, "resolution": "will_return"})
        self.assertEqual(result.status, S.CANCELLED)
        self.assertEqual(self.stock("FUSE_SYN_A"), 36)  # never put back here (ADR 0003)
        self.assertEqual(self.line(case).outstanding, 0)
        ended = Case.objects.get(pk=case.pk).ended_at
        # Goods already out can still be confirmed; the case stays cancelled.
        again = self.act(self.req1, case, "confirm", {"delivery": 1, "result": "full"})
        self.assertEqual(again.status, S.CANCELLED)
        self.assertEqual(Case.objects.get(pk=case.pk).ended_at, ended)
        self.assertFalse(Case.objects.filter(kind=K.RETURN).exists())  # nothing made for them

    def test_cancel_before_approval_stays_with_the_owner_division(self):
        case = self.ready()
        self.assertFails(
            ErrorCode.VALIDATION_ERROR,
            self.act,
            self.meter,
            case,
            "cancel",
            {"reason": "x", "resolution": "keep"},
        )
        self.assertEqual(self.act(self.meter, case, "cancel", {"reason": "ซ้ำ"}).status, S.CANCELLED)


# --- Visibility (Q12), handoff (Q14, Q23) -----------------------------------------------------


class VisibilityTests(FulfilmentTestCase):
    def test_authority_holders_see_what_waits_for_them_and_what_they_decided(self):
        outsider = make_staff("regapprover", (Role.STAFF, "SIM-REG"))
        meter_item = EquipmentType.objects.get(code="MTR_SYN_A")
        self.grant(outsider, A.WITHDRAWAL_APPROVAL, item=meter_item)
        meters = self.ready()
        transformers = self.ready(
            withdrawal(lines=[{"item_code": "TRF_SYN_A", "quantity": "1"}]),
            "SIM-TRF",
            self.transformer,
        )
        visible = set(services.visible_cases(Actor.staff(outsider)).values_list("pk", flat=True))
        self.assertIn(meters.pk, visible)
        self.assertNotIn(transformers.pk, visible)
        self.act(outsider, meters, "approve", lines((1, "3")))
        # Seeing a case is not a duty in it: other actions still need the owner division.
        self.assertFails(ErrorCode.FORBIDDEN, self.act, outsider, meters, "hold", {"reason": "x"})
        self.act(outsider, meters, "terminate_outstanding", {"lines": [1], "reason": "ไม่มีงาน"})
        self.assertEqual(Case.objects.get(pk=meters.pk).status, S.CLOSED)
        # Read access stays for the case they decided.
        self.assertTrue(services.visible_cases(Actor.staff(outsider)).filter(pk=meters.pk).exists())

    def test_decisions_survive_a_handoff_and_work_continues_while_one_waits(self):
        case = self.ready(
            withdrawal(
                lines=[
                    {"item_code": "FUSE_SYN_A", "quantity": "5"},
                    {"item_code": "CB_SYN_A", "quantity": "2"},
                ]
            ),
            "SIM-EQP",
        )
        self.act(self.approver, case, "approve", lines((1, "5")))
        self.act(
            self.eqp,
            case,
            "request_handoff",
            {"to_division": "SIM-MTR", "reason_category": "new_evidence", "reason": "ทดสอบ"},
        )
        # Q23: the owner stays responsible until the target accepts; approval goes on.
        self.act(self.approver, case, "approve", lines((2, "2")))
        self.assertEqual(Case.objects.get(pk=case.pk).status, S.AWAITING_EXECUTION)
        self.act(self.meter, case, "accept_handoff")
        self.assertEqual(self.line(case).approved_quantity, 5)  # Q14: still valid
        self.assertFails(ErrorCode.NOT_FOUND, self.issue, self.eqp_issuer, case, 5)
        self.issue(self.issuer, case, 5)


# --- Notifications and LINE (Q15) --------------------------------------------------------------


class NotificationTests(FulfilmentTestCase):
    def setUp(self):
        self.identity = ChannelIdentity.objects.create(
            channel="line", external_id=ALICE, requester=self.req1, status="active"
        )

    def plan(self, action):
        event = OutboxEvent.objects.filter(
            event_type=orchestration.EVENT_DECISION, payload__action=action
        ).latest("id")
        return notifications.plan_for_event(event)

    def test_a_delivery_asks_for_receipt_with_bound_buttons(self):
        case = self.approved(2)
        self.issue(self.issuer, case, 1, ["MTR_SYN_001"])
        delivery = self.plan("issue")
        self.assertEqual((delivery.kind, delivery.level), ("receipt_request", "critical"))
        self.assertEqual(delivery.message["type"], "flex")
        labels = [b["action"]["label"] for b in delivery.message["contents"]["footer"]["contents"]]
        self.assertEqual(labels, ["ได้รับครบ", "ไม่ตรง"])
        tokens = PostbackToken.objects.filter(identity=self.identity, action__startswith="receipt_")
        self.assertEqual(tokens.count(), 2)
        self.assertEqual(tokens.first().value, {"case": str(case.pk), "delivery": 1})
        self.assertIn("MTR_SYN_001", delivery.text)

    def test_partial_approval_waits_and_not_approved_is_normal(self):
        case = self.ready()
        self.act(self.approver, case, "approve", lines((1, "2", "บางส่วน")))
        self.assertIsNone(self.plan("approve"))
        other = self.ready()
        self.act(self.approver, other, "deny_approval", {"reason": "ไม่ตรงระเบียบ"})
        planned = self.plan("deny_approval")
        self.assertEqual((planned.kind, planned.level), ("case_not_approved", "normal"))
        self.assertIn("ไม่ตรงระเบียบ", planned.text)

    def test_send_uses_the_prepared_card(self):
        case = self.approved(1)
        self.issue(self.issuer, case, 1, ["MTR_SYN_001"])
        delivery = self.plan("issue")
        sent = []
        with (
            override_settings(
                OUTBOUND_ENABLED=True,
                LINE_CHANNEL_ACCESS_TOKEN="t",
                LINE_PUSH_ENABLED=True,
                LINE_ALLOWED_RECIPIENTS=frozenset({ALICE}),
            ),
            patch.object(line_api, "push", side_effect=lambda *a: sent.append(a)),
        ):
            notifications.send(delivery.pk)
        self.assertEqual(sent[0][1][0]["type"], "flex")
        self.assertEqual(NotificationDelivery.objects.get(pk=delivery.pk).status, "sent")


class ReceiptChatTests(Staffing, LineTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        Staffing.staff_up(cls)

    def setUp(self):
        super().setUp()
        self.req1 = self.approve(ALICE)
        self.approve(BOB, "D02")
        self.identity = ChannelIdentity.objects.get(external_id=ALICE)

    def card_tokens(self, case, seq=1):
        _, message = receipts.delivery_card(case, seq, self.identity)
        return {
            b["action"]["label"]: b["action"]["data"]
            for b in message["contents"]["footer"]["contents"]
        }

    def test_full_receipt_from_the_card(self):
        case = self.approved(2)
        self.issue(self.issuer, case, 2, ["MTR_SYN_001", "MTR_SYN_002"])
        reply = self.say(self.postback(ALICE, self.card_tokens(case)["ได้รับครบ"]))
        self.assertIn("ได้รับครบ", texts(reply))
        self.assertIn("ปิดแล้ว", texts(reply))
        self.assertEqual(Delivery.objects.get(case=case).status, D.CONFIRMED)
        again = self.say(self.postback(ALICE, self.card_tokens(case)["ได้รับครบ"]))
        self.assertIn("ยืนยันหรือปิดแล้ว", texts(again))
        self.assertEqual(Receipt.objects.count(), 1)

    def test_mismatch_is_asked_line_by_line_without_ai(self):
        case = self.approved(10, fuse(10), "SIM-EQP")
        self.issue(self.eqp_issuer, case, 10)
        question = self.say(self.postback(ALICE, self.card_tokens(case)["ไม่ตรง"]))
        self.assertIn("ได้รับจริงเท่าไร", texts(question))
        self.assertIn(
            "ได้ครบ 10", [i["action"]["label"] for i in question[-1]["quickReply"]["items"]]
        )
        retry = self.type(ALICE, "แปดชิ้น")
        self.assertIn("พิมพ์เป็นตัวเลข", texts(retry))
        reply = self.type(ALICE, "๘")
        self.assertIn("ของไม่ตรง", texts(reply))
        receipt = Receipt.objects.get()
        self.assertEqual((receipt.result, receipt.lines[0]["quantity"]), ("mismatch", "8"))

    def test_serial_answers_and_buttons_of_others(self):
        case = self.approved(2)
        self.issue(self.issuer, case, 2, ["MTR_SYN_001", "MTR_SYN_002"])
        tokens = self.card_tokens(case)
        refused = self.say(self.postback(BOB, tokens["ไม่ตรง"]))
        self.assertIn("ปุ่มนี้ใช้ไม่ได้", texts(refused))
        self.say(self.postback(ALICE, tokens["ไม่ตรง"]))
        reply = self.type(ALICE, "MTR_SYN_001")
        self.assertIn("ของไม่ตรง", texts(reply))
        self.assertEqual(Receipt.objects.get().lines[0]["serials"], ["MTR_SYN_001"])

    def test_case_detail_shows_counts_and_fresh_buttons(self):
        case = self.approved(3)
        self.issue(self.issuer, case, 1, ["MTR_SYN_001"])
        PostbackToken.objects.all().update(expires_at=timezone.now() - timedelta(minutes=1))
        reply = self.type(ALICE, case.number)
        body = texts(reply)
        self.assertIn("ขอ 3 อนุมัติ 3 จ่าย 1 รับ 0 ค้าง 2", body)
        self.assertIn("จ่ายบางส่วน", body)
        self.press(ALICE, "ได้รับครบ ครั้งที่1", reply)
        self.assertEqual(Delivery.objects.get(case=case).status, D.CONFIRMED)


# --- Admin, commands ------------------------------------------------------------------------


class AdminAndCommandTests(FulfilmentTestCase):
    def test_inventory_page_is_for_admins(self):
        client = Client()
        client.force_login(self.admin)
        page = client.get("/console/admin/inventory/")
        self.assertContains(page, "คลังจำลอง")
        self.assertContains(page, "MTR_SYN_001")
        response = client.post(
            "/console/admin/inventory/adjust/",
            {
                "item": EquipmentType.objects.get(code="CB_SYN_A").pk,
                "delta": "2",
                "reason": "รับเข้า",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stock("CB_SYN_A"), 10)
        response = client.post(
            "/console/admin/inventory/import/",
            {
                "rows": '[{"item_id": "CB_SYN_A", "available": 7}]',
                "as_of": "2026-10-07T08:00",
                "source": "ทะเบียนจำลอง",
            },
        )
        self.assertEqual(response.status_code, 302)
        staff = Client()
        staff.force_login(self.meter)
        self.assertEqual(staff.get("/console/admin/inventory/").status_code, 403)

    def test_policy_page_grants_scoped_approval_authority(self):
        client = Client()
        client.force_login(self.admin)
        user = make_staff("scoped", (Role.STAFF, "SIM-EQP"))
        client.post(
            "/console/admin/policy/",
            {
                "user": user.pk,
                "activity": A.WITHDRAWAL_APPROVAL,
                "item": EquipmentType.objects.get(code="FUSE_SYN_A").pk,
                "max_quantity": "20",
                "approval_reference": "คำสั่งจำลอง 2/2569",
                "reason": "ผู้อนุมัติฟิวส์",
            },
        )
        grant = AuthorityGrant.objects.get(user=user)
        self.assertEqual((grant.category, grant.max_quantity), ("other", Decimal("20")))
        self.assertContains(client.get("/console/admin/policy/"), "ไม่เกิน 20")

    @override_settings(INVENTORY_MODE="manual_reference")
    def test_manual_mode_imports_figures_and_refuses_fake_adjustments(self):
        out = StringIO()
        call_command("import_stock_snapshot", stdout=out)
        self.assertIn("Imported 5 rows", out.getvalue())
        with self.assertRaises(ValidationError):
            inventory.adjust_fake(
                self.admin, EquipmentType.objects.get(code="CB_SYN_A"), Decimal("1"), "x"
            )
        with self.assertRaises(ValidationError):
            inventory.import_snapshot(
                self.admin,
                [{"item_id": "CB_SYN_A", "available": 1}],
                timezone.now() + timedelta(days=1),
                "อนาคต",
            )

    def test_demo_walks_req_syn_001_and_004(self):
        out = StringIO()
        call_command("demo_withdrawal", stdout=out)
        text = out.getvalue()
        self.assertIn("MTR_SYN_A: ขอ 3 อนุมัติ 2 (ไม่อนุมัติ 1) จ่าย 1 รับ 1 ค้างจ่าย 1", text)
        self.assertIn("FUSE_SYN_A: ขอ 50 อนุมัติ 50 (ไม่อนุมัติ 0) จ่าย 40 รับ 0 ค้างจ่าย 10", text)
        self.assertIn("Q8", text)
        self.assertEqual(Case.objects.filter(kind=Case.Kind.WITHDRAWAL).count(), 2)


# --- Concurrency (race) -------------------------------------------------------------------------


class ConcurrencyTests(Staffing, TransactionTestCase):
    def setUp(self):
        setup_world(self)
        self.staff_up()

    def run_parallel(self, calls):
        barrier = threading.Barrier(len(calls))
        results, errors = [], []

        def run(call):
            try:
                barrier.wait()
                results.append(call())
            except CommandFailed as exc:
                errors.append(exc.code)
            finally:
                connection.close()

        threads = [threading.Thread(target=run, args=(call,)) for call in calls]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results, errors

    def test_parallel_allocations_never_exceed_ready_stock(self):
        first = self.approved(25, fuse(25), "SIM-EQP")
        second = self.approved(25, fuse(25), "SIM-EQP")
        calls = [
            lambda case=case: self.act(
                self.allocator,
                case,
                "allocate",
                {"lines": [{"line_no": 1, "quantity": "25"}], "reason": "แข่งกัน"},
            )
            for case in (first, second)
        ]
        results, errors = self.run_parallel(calls)
        self.assertEqual((len(results), errors), (1, [ErrorCode.VALIDATION_ERROR]))
        self.assertEqual(Allocation.objects.filter(ended_at__isnull=True).count(), 1)

    def test_parallel_issues_from_one_store_stay_within_it(self):
        first = self.approved(20, fuse(20), "SIM-EQP")
        second = self.approved(20, fuse(20), "SIM-EQP")
        users = (self.eqp_issuer, self.eqp_issuer2)
        calls = [
            lambda case=case, user=user: self.issue(user, case, 20)
            for case, user in zip((first, second), users, strict=True)
        ]
        results, errors = self.run_parallel(calls)
        self.assertEqual((len(results), errors), (2, []))
        self.assertEqual(self.stock("FUSE_SYN_A"), 0)
        third = self.approved(1, fuse(1), "SIM-EQP")
        with self.assertRaises(CommandFailed):
            self.issue(self.eqp_issuer, third, 1)
        self.assertEqual(self.stock("FUSE_SYN_A"), 0)

    def test_parallel_retries_of_one_issue_make_one_delivery(self):
        case = self.approved(5, fuse(5), "SIM-EQP")
        k = key()
        revision = Case.objects.get(pk=case.pk).revision
        payload = {"recipient_name": "x", "lines": [{"line_no": 1, "quantity": "5"}]}
        calls = [
            lambda: services.perform_action(
                Actor.staff(self.eqp_issuer),
                case.pk,
                "issue",
                expected_revision=revision,
                idempotency_key=k,
                payload=payload,
            )
            for _ in range(3)
        ]
        results, errors = self.run_parallel(calls)
        self.assertEqual((len(results), errors), (3, []))
        self.assertEqual(len({r.action_id for r in results}), 1)
        self.assertEqual(Delivery.objects.filter(case=case).count(), 1)
        self.assertEqual(self.stock("FUSE_SYN_A"), 35)

    def test_parallel_receipts_of_one_delivery_record_once(self):
        case = self.approved(5, fuse(5), "SIM-EQP")
        self.issue(self.eqp_issuer, case, 5)
        revision = Case.objects.get(pk=case.pk).revision
        calls = [
            lambda: services.perform_action(
                Actor.for_requester(self.req1),
                case.pk,
                "confirm",
                expected_revision=revision,
                idempotency_key=f"rcpt-{uuid.uuid4().hex}",
                payload={"delivery": 1, "result": "full"},
            )
            for _ in range(2)
        ]
        results, errors = self.run_parallel(calls)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(Receipt.objects.count(), 1)
        self.assertEqual(Case.objects.get(pk=case.pk).status, S.CLOSED)
