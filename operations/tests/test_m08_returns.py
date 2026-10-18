"""M08 returns: instructions, arrivals, inspection, conflicts, discrepancies and closing
(PROJECT.md M08, Q1–Q15, ADR 0003).

Acceptance → tests: a return request adds no stock and arrived ≠ ready (ArrivalTests,
InspectionTests); partial return and closing the outstanding part (EndingTests, the REQ_SYN_002
demo); wrong/duplicate serials and no original document (ArrivalTests, DiscrepancyTests);
conflicting inspections (ConflictTests, ConcurrencyTests); discrepancies keep the owner under
R07 (ArrivalTests).
"""

import threading
import uuid
from decimal import Decimal
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase, override_settings

from operations import inventory, notifications, orchestration, policy, returns, services, workflow
from operations.contracts import Actor, CommandFailed, ErrorCode
from operations.models import (
    AuthorityGrant,
    Case,
    CaseLine,
    CaseTriage,
    ChannelIdentity,
    EquipmentType,
    FakeSerial,
    FakeStock,
    Inspection,
    IssuedSerial,
    OutboxEvent,
    ReturnUnit,
    Role,
)
from operations.tests.test_m02_cases import make_staff, setup_world, withdrawal
from operations.tests.test_m03_line import ALICE, LineTestCase, texts
from operations.tests.test_m07_fulfilment import Staffing

S = Case.Status
K = Case.Kind
A = AuthorityGrant.Activity
U = ReturnUnit


def meters_back(*serials):
    return {
        "kind": "return",
        "condition": "ถอดเปลี่ยน",
        "reason": "ครบวาระ",
        "lines": [
            {"item_code": "MTR_SYN_A", "quantity": str(len(serials)), "serials": list(serials)}
        ],
    }


def fuses_back(quantity):
    return {
        "kind": "return",
        "condition": "เหลือจากงาน",
        "reason": "งานเสร็จ",
        "source_reference": "ISSUE-1",
        "lines": [{"item_code": "FUSE_SYN_A", "quantity": str(quantity)}],
    }


class Returning(Staffing):
    def return_staff(self):
        self.receiver = make_staff(
            "receiver", (Role.RECEIVER, "SIM-MTR"), (Role.RECEIVER, "SIM-EQP")
        )
        self.inspector = make_staff(
            "inspector", (Role.INSPECTOR, "SIM-MTR"), (Role.INSPECTOR, "SIM-EQP")
        )
        self.certifier = make_staff(
            "certifier", (Role.INSPECTOR, "SIM-MTR"), (Role.INSPECTOR, "SIM-EQP")
        )
        self.third = make_staff("third", (Role.STAFF, "SIM-REG"))
        self.eqp_lead = make_staff("eqplead", (Role.SUPERVISOR, "SIM-EQP"))
        Staffing.grant(self, self.certifier, A.CONDITION_CERTIFICATION)
        Staffing.grant(self, self.third, A.CONDITION_CERTIFICATION)

    def returning(self, payload=None, division="SIM-MTR", requester=None):
        """A return case triaged to `division` and waiting for the goods."""
        staff = self.meter if division == "SIM-MTR" else self.eqp
        case = self.submit(payload or meters_back("MTR_SYN_007", "MTR_SYN_008"), requester)
        self.act(self.intake, case, "start_triage")
        self.act(self.intake, case, "request_handoff", {"to_division": division})
        self.act(staff, case, "accept_handoff")
        self.act(staff, case, "set_priority", {"priority": "P3"})
        self.act(staff, case, "complete_triage")
        return Case.objects.get(pk=case.pk)

    def receive(self, case, items, user=None, **extra):
        return self.act(
            user or self.receiver,
            case,
            "receive",
            {"carrier_name": "ผู้นำส่งจำลอง", "items": items, **extra},
        )

    def unit(self, case, seq=1):
        return U.objects.get(case=case, seq=seq)

    def inspect(self, case, split, user=None, seq=1, **extra):
        return self.act(
            user or self.inspector, case, "inspect", {"unit": seq, "split": split, **extra}
        )

    def figures(self, code):
        stock = FakeStock.objects.get(item__code=code)
        return stock.available, stock.awaiting_inspection, stock.awaiting_repair


class ReturnTestCase(Returning, TestCase):
    @classmethod
    def setUpTestData(cls):
        setup_world(cls)
        Staffing.staff_up(cls)
        Returning.return_staff(cls)

    def assertFails(self, code, fn, *args, **kwargs):
        with self.assertRaises(CommandFailed) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        return caught.exception

    def proposal(self, case):
        return CaseTriage.objects.get(case=case).current_recommendation


# --- Matrix ------------------------------------------------------------------------------------


class MatrixTests(ReturnTestCase):
    def test_m08_actions_belong_to_returns_only(self):
        for name in (
            "return_instructions",
            "receive",
            "inspect",
            "decide_inspection",
            "resolve_return_discrepancy",
            "terminate_return",
        ):
            self.assertEqual(workflow.ACTIONS[name].enabled_kinds, [K.RETURN], name)
        self.assertEqual(workflow.ACTIONS["reopen"].enabled_kinds, [K.PROBLEM])  # Q13, M07 Q16

    def test_returns_close_by_themselves_and_never_reopen(self):
        case = self.returning()
        self.act(
            self.supervisor,
            case,
            "terminate_return",
            {"lines": [1], "category": "not_returned", "reason": "เขตไม่คืน"},
        )
        self.assertEqual(Case.objects.get(pk=case.pk).status, S.CLOSED)
        self.assertFails(
            ErrorCode.INVALID_TRANSITION, self.act, self.meter, case, "reopen", {"reason": "มาช้า"}
        )
        open_case = self.returning(meters_back("MTR_SYN_009"))
        self.receive(open_case, [{"item_code": "MTR_SYN_A", "serials": ["MTR_SYN_009"]}])
        error = self.assertFails(
            ErrorCode.VALIDATION_ERROR, self.act, self.meter, open_case, "close", {"reason": "x"}
        )
        self.assertIn("Q11", error.message)


# --- Instructions and arrivals (Q2, Q3, Q4, Q6, Q7) ---------------------------------------------


class ArrivalTests(ReturnTestCase):
    def test_instructions_are_pushed_once(self):
        ChannelIdentity.objects.create(
            channel="line", external_id=ALICE, requester=self.req1, status="active"
        )
        case = self.returning()
        self.assertFails(
            ErrorCode.NOT_FOUND,
            self.act,
            self.eqp,
            case,
            "return_instructions",
            {"place": "คลัง"},
        )
        self.act(
            self.meter,
            case,
            "return_instructions",
            {"place": "คลังกองงานมิเตอร์", "from_date": "2026-10-08", "to_date": "2026-10-10"},
        )
        self.act(self.meter, case, "return_instructions", {"place": "คลังสาขา 2"})
        events = OutboxEvent.objects.filter(
            event_type=orchestration.EVENT_DECISION, payload__action="return_instructions"
        ).order_by("id")
        first, second = (notifications.plan_for_event(e) for e in events)
        self.assertEqual((first.kind, first.level), ("return_instructions", "critical"))
        self.assertIn("คลังกองงานมิเตอร์", first.text)
        self.assertIsNone(second)
        self.assertEqual(returns.latest_instructions(case)["place"], "คลังสาขา 2")

    def test_a_request_adds_nothing_and_arrived_is_not_ready(self):
        before = self.figures("MTR_SYN_A")
        case = self.returning()
        self.assertEqual(self.figures("MTR_SYN_A"), before)  # แจ้งคืนไม่เพิ่ม stock
        result = self.receive(case, [{"item_code": "MTR_SYN_A", "serials": ["MTR_SYN_007"]}])
        self.assertEqual(result.status, S.IN_PROGRESS)
        available, awaiting, _ = self.figures("MTR_SYN_A")
        self.assertEqual((available, awaiting), (before[0], before[1] + 1))
        self.assertEqual(
            FakeSerial.objects.get(serial="MTR_SYN_007").status,
            FakeSerial.Status.AWAITING_INSPECTION,
        )
        line = CaseLine.objects.get(case=case)
        self.assertEqual((line.returned_quantity, line.return_outstanding), (1, 1))
        summary = returns.summary(case)
        self.assertTrue(summary["partially_returned"])
        self.assertTrue(summary["no_source_document"])  # accepted without the original document
        self.assertEqual(summary["lines"][0]["awaiting"], "1")

    def test_only_a_receiver_of_the_owner_division_after_triage(self):
        case = self.submit(meters_back("MTR_SYN_007"))
        self.act(self.intake, case, "start_triage")
        self.assertFails(
            ErrorCode.INVALID_TRANSITION,
            self.receive,
            case,
            [{"item_code": "MTR_SYN_A", "serials": ["MTR_SYN_007"]}],
            self.intake,
        )
        case = self.returning()
        self.assertFails(
            ErrorCode.FORBIDDEN,
            self.receive,
            case,
            [{"item_code": "MTR_SYN_A", "serials": ["MTR_SYN_007"]}],
            self.meter,
        )

    def test_input_errors_are_refused_but_what_arrived_is_always_recorded(self):
        case = self.returning()
        for items in (
            [{"item_code": "MTR_SYN_A", "serials": ["MTR_SYN_007", "MTR_SYN_007"]}],
            [{"item_code": "NOPE", "quantity": "1"}],
            [{"item_code": "MTR_SYN_A", "quantity": "2", "serials": ["MTR_SYN_007"]}],
        ):
            with self.subTest(items=items):
                self.assertFails(ErrorCode.VALIDATION_ERROR, self.receive, case, items)
        self.receive(
            case,
            [
                {
                    "item_code": "MTR_SYN_A",
                    "serials": ["MTR_SYN_007", "MTR_SYN_009", "MTR_SYN_001"],
                },
                {"item_code": "CB_SYN_A", "quantity": "1"},
            ],
        )
        units = {u.serial or u.item.code: u for u in U.objects.filter(case=case)}
        self.assertEqual(units["MTR_SYN_007"].discrepancy, "")
        self.assertEqual(units["MTR_SYN_009"].discrepancy, U.Discrepancy.OUTSIDE_REQUEST)
        self.assertEqual(units["MTR_SYN_001"].discrepancy, U.Discrepancy.DUPLICATE_STORE)
        self.assertEqual(units["CB_SYN_A"].discrepancy, U.Discrepancy.OUTSIDE_REQUEST)
        self.assertIsNone(units["CB_SYN_A"].line)
        # The duplicate leaves the store alone; the others are held waiting for inspection.
        self.assertEqual(
            FakeSerial.objects.get(serial="MTR_SYN_001").status, FakeSerial.Status.AVAILABLE
        )
        self.assertEqual(units["MTR_SYN_001"].placed, {})
        self.assertEqual(CaseLine.objects.get(case=case).returned_quantity, 1)
        proposal = self.proposal(case)
        self.assertEqual(proposal.rules, ["R07"])  # owner kept, registry helps
        self.assertEqual(proposal.facts["serial_mismatch"]["source"], "return")
        self.assertEqual(Case.objects.get(pk=case.pk).owner_division.code, "SIM-MTR")

    def test_excess_counted_quantity_is_kept_apart(self):
        case = self.returning(fuses_back(10), "SIM-EQP")
        self.receive(case, [{"item_code": "FUSE_SYN_A", "quantity": "12"}])
        matched, excess = U.objects.filter(case=case).order_by("seq")
        self.assertEqual((matched.quantity, matched.discrepancy), (10, ""))
        self.assertEqual((excess.quantity, excess.discrepancy), (2, U.Discrepancy.EXCESS))
        self.assertEqual(self.figures("FUSE_SYN_A")[1], 12)  # all of it is physically here

    def test_a_serial_this_system_issued_comes_back_and_closes_its_issue(self):
        out = self.approved(1)
        self.issue(self.issuer, out, 1, ["MTR_SYN_001"])
        case = self.returning(meters_back("MTR_SYN_001"))
        self.receive(case, [{"item_code": "MTR_SYN_A", "serials": ["MTR_SYN_001"]}])
        unit = self.unit(case)
        self.assertEqual(unit.discrepancy, "")
        self.assertFalse(IssuedSerial.objects.get(serial="MTR_SYN_001").active)
        self.assertIn(out.number, returns.summary(case)["units"][0]["source"])
        # Issued to D01, returned by D02: the goods are back but it is a discrepancy.
        again = self.approved(1)
        self.issue(self.issuer, again, 1, ["MTR_SYN_002"])
        other = self.returning(meters_back("MTR_SYN_002"), requester=self.req2)
        self.receive(other, [{"item_code": "MTR_SYN_A", "serials": ["MTR_SYN_002"]}])
        self.assertEqual(self.unit(other).discrepancy, U.Discrepancy.OTHER_DISTRICT)
        self.assertFalse(IssuedSerial.objects.get(serial="MTR_SYN_002").active)

    def test_a_returned_serial_can_be_issued_again_after_inspection(self):
        out = self.approved(1)
        self.issue(self.issuer, out, 1, ["MTR_SYN_001"])
        case = self.returning(meters_back("MTR_SYN_001"))
        self.receive(case, [{"item_code": "MTR_SYN_A", "serials": ["MTR_SYN_001"]}])
        self.inspect(case, {"ready": "1"}, self.certifier)
        self.assertEqual(Case.objects.get(pk=case.pk).status, S.CLOSED)
        second = self.approved(1)
        self.issue(self.issuer, second, 1, ["MTR_SYN_001"])
        self.assertEqual(IssuedSerial.objects.filter(serial="MTR_SYN_001", active=True).count(), 1)


# --- Inspection (Q5, Q6) -------------------------------------------------------------------------


class InspectionTests(ReturnTestCase):
    def arrived(self, payload=None, division="SIM-MTR", items=None):
        case = self.returning(payload, division)
        self.receive(case, items or [{"item_code": "MTR_SYN_A", "serials": ["MTR_SYN_007"]}])
        return Case.objects.get(pk=case.pk)

    def test_ready_needs_certification_and_moves_stock(self):
        case = self.arrived()
        self.assertFails(ErrorCode.FORBIDDEN, self.inspect, case, {"ready": "1"}, self.meter)
        error = self.assertFails(ErrorCode.FORBIDDEN, self.inspect, case, {"ready": "1"})
        self.assertIn("Q5", error.message)
        available, awaiting, _ = self.figures("MTR_SYN_A")
        self.inspect(case, {"ready": "1"}, self.certifier)
        self.assertEqual(self.figures("MTR_SYN_A")[:2], (available + 1, awaiting - 1))
        self.assertEqual(
            FakeSerial.objects.get(serial="MTR_SYN_007").status, FakeSerial.Status.AVAILABLE
        )
        inspection = Inspection.objects.get()
        self.assertEqual(
            (inspection.split, inspection.grant.activity),
            ({"ready": "1"}, A.CONDITION_CERTIFICATION),
        )

    def test_results_need_the_inspector_role(self):
        case = self.arrived()
        error = self.assertFails(
            ErrorCode.FORBIDDEN, self.inspect, case, {"repair": "1"}, self.meter
        )
        self.assertIn("Q5", error.message)

    def test_repair_and_other_need_only_the_inspector_role(self):
        case = self.arrived(
            meters_back("MTR_SYN_007", "MTR_SYN_008"),
            items=[{"item_code": "MTR_SYN_A", "serials": ["MTR_SYN_007", "MTR_SYN_008"]}],
        )
        _, _, repair = self.figures("MTR_SYN_A")
        self.inspect(case, {"repair": "1"})
        self.assertEqual(self.figures("MTR_SYN_A")[2], repair + 1)
        result = self.act(
            self.inspector,
            case,
            "inspect",
            {"unit": 2, "split": {"other": "1"}, "reason": "จำหน่าย"},
        )
        self.assertEqual(result.status, S.CLOSED)  # nothing outstanding, everything judged
        self.assertEqual(
            FakeSerial.objects.get(serial="MTR_SYN_008").status, FakeSerial.Status.DISPOSED
        )

    def test_the_receiver_does_not_inspect_what_they_received(self):
        both = make_staff("both", (Role.RECEIVER, "SIM-MTR"), (Role.INSPECTOR, "SIM-MTR"))
        case = self.returning()
        self.receive(case, [{"item_code": "MTR_SYN_A", "serials": ["MTR_SYN_007"]}], both)
        error = self.assertFails(ErrorCode.FORBIDDEN, self.inspect, case, {"repair": "1"}, both)
        self.assertIn("Q5", error.message)

    def test_a_counted_return_is_split_between_outcomes(self):
        case = self.arrived(
            fuses_back(10), "SIM-EQP", [{"item_code": "FUSE_SYN_A", "quantity": "10"}]
        )
        available, awaiting, repair = self.figures("FUSE_SYN_A")
        for split in (
            {"ready": "7", "repair": "2"},
            {"pending": "5", "ready": "5"},
            {"broken": "10"},
        ):
            with self.subTest(split=split):
                self.assertFails(
                    ErrorCode.VALIDATION_ERROR, self.inspect, case, split, self.certifier
                )
        self.act(
            self.certifier,
            case,
            "inspect",
            {"unit": 1, "split": {"ready": "7", "repair": "2", "other": "1"}, "reason": "ไหม้ 1"},
        )
        self.assertEqual(self.figures("FUSE_SYN_A"), (available + 7, awaiting - 10, repair + 2))
        self.assertEqual(returns.summary(case)["lines"][0]["ready"], "7")

    def test_pending_is_not_a_result_and_a_later_result_is_not_a_conflict(self):
        case = self.arrived()
        self.inspect(case, {"pending": "1"}, reason="ส่งตรวจภายนอก")
        self.assertEqual(self.unit(case).status, U.Status.AWAITING)
        self.inspect(case, {"repair": "1"})
        self.assertEqual(self.unit(case).status, U.Status.FINAL)
        self.assertFails(ErrorCode.VALIDATION_ERROR, self.inspect, case, {"pending": "1"})

    def test_ready_stock_refreshes_waiting_withdrawals(self):
        short = self.ready(withdrawal(lines=[{"item_code": "MTR_SYN_A", "quantity": "4"}]))
        self.assertIn("R08", self.proposal(short).rules)  # 4 > 3 ready
        case = self.arrived()
        with self.captureOnCommitCallbacks(execute=True):
            self.inspect(case, {"ready": "1"}, self.certifier)
        self.assertNotIn("R08", self.proposal(short).rules)

    @override_settings(INVENTORY_MODE="manual_reference")
    def test_manual_reference_spots_a_serial_already_received_elsewhere(self):
        first = self.returning(meters_back("M-501"))
        self.receive(
            first, [{"item_code": "MTR_SYN_A", "serials": ["M-501"]}], document_number="R1"
        )
        second = self.returning(meters_back("M-501"))
        self.receive(
            second, [{"item_code": "MTR_SYN_A", "serials": ["M-501"]}], document_number="R2"
        )
        self.assertEqual(self.unit(second).discrepancy, U.Discrepancy.DUPLICATE_CASE)
        self.assertFalse(self.unit(second).counted)

    @override_settings(INVENTORY_MODE="manual_reference")
    def test_manual_reference_conflict_decisions_cite_the_registry(self):
        case = self.returning()
        self.receive(case, [{"item_code": "MTR_SYN_A", "serials": ["M-601"]}], document_number="R1")
        self.act(
            self.meter,
            case,
            "resolve_return_discrepancy",
            {"unit": 1, "resolution": "accepted", "replaces": "MTR_SYN_007", "reason": "เลขจริง"},
        )
        self.inspect(case, {"repair": "1"}, reference="INS-1")
        self.inspect(case, {"ready": "1"}, self.certifier, reference="INS-2")
        decide = {"unit": 1, "split": {"repair": "1"}, "reason": "ตรวจซ้ำ"}
        self.assertFails(
            ErrorCode.VALIDATION_ERROR, self.act, self.third, case, "decide_inspection", decide
        )
        self.act(self.third, case, "decide_inspection", {**decide, "reference": "INS-3"})
        self.assertEqual(self.unit(case).status, U.Status.FINAL)

    @override_settings(INVENTORY_MODE="manual_reference")
    def test_manual_reference_records_documents_and_changes_no_figures(self):
        before = self.figures("MTR_SYN_A")
        case = self.returning()
        items = [{"item_code": "MTR_SYN_A", "serials": ["MTR_SYN_007"]}]
        self.assertFails(ErrorCode.VALIDATION_ERROR, self.receive, case, items)
        self.receive(case, items, document_number="REG-RCV-1")
        self.assertFails(ErrorCode.VALIDATION_ERROR, self.inspect, case, {"repair": "1"})
        self.inspect(case, {"repair": "1"}, reference="REG-INS-1")
        self.assertEqual(self.figures("MTR_SYN_A"), before)
        self.assertEqual(self.unit(case).placed, {})


# --- Conflicting inspections (Q9) -------------------------------------------------------------


class ConflictTests(ReturnTestCase):
    def test_a_second_different_result_needs_a_third_certifier(self):
        # A conflict can only be recorded while the case is open (here MTR_SYN_008 is still
        # out); a closed return is final and later doubts are a problem case (Q11/Q13).
        case = self.returning()
        self.receive(case, [{"item_code": "MTR_SYN_A", "serials": ["MTR_SYN_007"]}])
        self.inspect(case, {"repair": "1"})
        self.assertFails(ErrorCode.VALIDATION_ERROR, self.inspect, case, {"repair": "1"})  # same
        available, awaiting, repair = self.figures("MTR_SYN_A")
        result = self.inspect(case, {"ready": "1"}, self.certifier)
        self.assertEqual(self.unit(case).status, U.Status.CONFLICT)
        self.assertEqual(self.figures("MTR_SYN_A"), (available, awaiting + 1, repair - 1))
        self.assertEqual(result.status, S.IN_PROGRESS)
        self.assertFails(ErrorCode.VALIDATION_ERROR, self.inspect, case, {"other": "1"}, reason="x")
        decide = {"unit": 1, "split": {"ready": "1"}, "reason": "ทดสอบซ้ำแล้วใช้ได้"}
        for user in (self.certifier, self.inspector):
            with self.subTest(user=user.username):
                code = ErrorCode.FORBIDDEN
                self.assertFails(code, self.act, user, case, "decide_inspection", decide)
        visible = services.visible_cases(Actor.staff(self.third))
        self.assertTrue(visible.filter(pk=case.pk).exists())  # certifiers see conflicts to decide
        decided = self.act(self.third, case, "decide_inspection", decide)
        self.assertEqual(decided.status, S.IN_PROGRESS)
        self.assertEqual(self.figures("MTR_SYN_A")[:2], (available + 1, awaiting))
        stop = {"lines": [1], "category": "not_returned", "reason": "เขตไม่คืนเครื่องที่ 2"}
        self.assertEqual(self.act(self.supervisor, case, "terminate_return", stop).status, S.CLOSED)
        self.assertEqual(
            list(Inspection.objects.values_list("kind", flat=True)),
            ["inspect", "inspect", "decide"],
        )

    def test_a_conflict_on_goods_already_issued_again_is_flagged_not_moved(self):
        case = self.returning()
        self.receive(case, [{"item_code": "MTR_SYN_A", "serials": ["MTR_SYN_007"]}])
        self.inspect(case, {"ready": "1"}, self.certifier)
        out = self.approved(1)
        self.issue(self.issuer, out, 1, ["MTR_SYN_007"])
        before = self.figures("MTR_SYN_A")
        result = self.inspect(case, {"repair": "1"})
        self.assertEqual(result.status, S.IN_PROGRESS)  # MTR_SYN_008 is still outstanding
        self.assertEqual(self.figures("MTR_SYN_A"), before)
        self.assertIn("ออกจากคลัง", self.unit(case).conflict_note)


# --- Discrepancies (Q10) --------------------------------------------------------------------------


class DiscrepancyTests(ReturnTestCase):
    def test_an_unexpected_serial_replaces_the_requested_one(self):
        case = self.returning(meters_back("MTR_SYN_007"))
        self.receive(case, [{"item_code": "MTR_SYN_A", "serials": ["MTR_SYN_009"]}])
        self.assertFails(ErrorCode.VALIDATION_ERROR, self.inspect, case, {"repair": "1"})
        accept = {
            "unit": 1,
            "resolution": "accepted",
            "replaces": "MTR_SYN_007",
            "reason": "เขตพิมพ์เลขผิดในคำขอ",
        }
        error = self.assertFails(
            ErrorCode.FORBIDDEN, self.act, self.receiver, case, "resolve_return_discrepancy", accept
        )
        self.assertIn("Q10", error.message)
        self.act(self.meter, case, "resolve_return_discrepancy", accept)
        line = CaseLine.objects.get(case=case)
        self.assertEqual((line.returned_quantity, line.return_outstanding), (1, 0))
        self.assertNotIn("R07", self.proposal(case).rules)
        self.assertEqual(self.inspect(case, {"repair": "1"}).status, S.CLOSED)

    def test_duplicates_need_the_registry_fixed_first(self):
        case = self.returning(meters_back("MTR_SYN_001"))
        self.receive(case, [{"item_code": "MTR_SYN_A", "serials": ["MTR_SYN_001"]}])
        base = {"unit": 1, "reason": "ทะเบียนผิด", "reference": "REG-FIX-1"}
        self.assertFails(
            ErrorCode.VALIDATION_ERROR,
            self.act,
            self.meter,
            case,
            "resolve_return_discrepancy",
            {**base, "resolution": "accepted"},
        )
        adjusted = {**base, "resolution": "registry_adjusted"}
        self.assertFails(
            ErrorCode.VALIDATION_ERROR,
            self.act,
            self.meter,
            case,
            "resolve_return_discrepancy",
            adjusted,
        )
        meter = EquipmentType.objects.get(code="MTR_SYN_A")
        inventory.adjust_fake(
            self.admin, meter, Decimal("-1"), "ทะเบียนนับเกิน", remove_serials=["MTR_SYN_001"]
        )
        self.act(self.meter, case, "resolve_return_discrepancy", adjusted)
        unit = self.unit(case)
        self.assertEqual(unit.placed, {"awaiting_inspection": "1"})
        self.assertTrue(unit.counted)

    def test_sending_back_needs_a_document_and_leaves_custody(self):
        case = self.returning(meters_back("MTR_SYN_007"))
        self.receive(case, [{"item_code": "MTR_SYN_A", "serials": ["MTR_SYN_009"]}])
        _, awaiting, _ = self.figures("MTR_SYN_A")
        back = {"unit": 1, "resolution": "sent_back", "reason": "ไม่ใช่ของ ฝอจ."}
        self.assertFails(
            ErrorCode.VALIDATION_ERROR,
            self.act,
            self.meter,
            case,
            "resolve_return_discrepancy",
            back,
        )
        self.act(self.meter, case, "resolve_return_discrepancy", {**back, "reference": "OUT-1"})
        self.assertEqual(self.unit(case).status, U.Status.SENT_BACK)
        self.assertEqual(self.figures("MTR_SYN_A")[1], awaiting - 1)
        self.assertEqual(CaseLine.objects.get(case=case).return_outstanding, 1)
        self.assertEqual(Case.objects.get(pk=case.pk).status, S.IN_PROGRESS)


# --- Ending (Q8, Q11) ---------------------------------------------------------------------------


class EndingTests(ReturnTestCase):
    def test_req_syn_002_partial_return_then_the_rest_is_stopped(self):
        ChannelIdentity.objects.create(
            channel="line", external_id=ALICE, requester=self.req1, status="active"
        )
        case = self.returning(meters_back("MTR_SYN_007", "MTR_SYN_008"))
        self.receive(case, [{"item_code": "MTR_SYN_A", "serials": ["MTR_SYN_007"]}])
        self.inspect(case, {"repair": "1"})
        self.assertEqual(Case.objects.get(pk=case.pk).status, S.IN_PROGRESS)  # 1 still out
        stop = {"lines": [1], "category": "lost", "reason": "เขตแจ้งว่าหาย"}
        self.assertFails(ErrorCode.FORBIDDEN, self.act, self.meter, case, "terminate_return", stop)
        self.assertFails(
            ErrorCode.VALIDATION_ERROR, self.act, self.supervisor, case, "terminate_return", stop
        )
        result = self.act(
            self.supervisor, case, "terminate_return", {**stop, "reference": "REG-LOST-9"}
        )
        self.assertEqual(result.status, S.CLOSED)
        event = OutboxEvent.objects.filter(
            event_type=orchestration.EVENT_DECISION, payload__action="terminate_return"
        ).get()
        planned = notifications.plan_for_event(event)
        self.assertEqual((planned.kind, planned.level), ("case_closed", "normal"))
        self.assertIn("มาถึง 1", planned.text)
        self.assertIn("ยุติ 1", planned.text)

    def test_stopping_everything_before_anything_arrived_closes(self):
        case = self.returning()
        result = self.act(
            self.supervisor,
            case,
            "terminate_return",
            {"lines": [1], "category": "not_returned", "reason": "เขตไม่คืน"},
        )
        self.assertEqual(result.status, S.CLOSED)

    def test_no_cancel_once_goods_arrived(self):
        case = self.returning()
        self.receive(case, [{"item_code": "MTR_SYN_A", "serials": ["MTR_SYN_007"]}])
        error = self.assertFails(
            ErrorCode.VALIDATION_ERROR, self.act, self.meter, case, "cancel", {"reason": "x"}
        )
        self.assertIn("Q8", error.message)
        other = self.returning(meters_back("MTR_SYN_008"))
        self.assertEqual(
            self.act(self.meter, other, "cancel", {"reason": "ซ้ำ"}).status, S.CANCELLED
        )


# --- Admin, LINE, demo ---------------------------------------------------------------------------


class AdminAndCommandTests(ReturnTestCase):
    def test_certification_grants_take_a_scope_but_no_cap(self):
        user = make_staff("cert2", (Role.INSPECTOR, "SIM-EQP"))
        grant = policy.grant_authority(
            self.admin, user, A.CONDITION_CERTIFICATION, "ช่างรับรอง", "คำสั่งจำลอง", category="other"
        )
        self.assertEqual(grant.category, "other")
        with self.assertRaises(ValidationError):
            policy.grant_authority(
                self.admin,
                user,
                A.CONDITION_CERTIFICATION,
                "x",
                "y",
                item=EquipmentType.objects.get(code="FUSE_SYN_A"),
                max_quantity=Decimal("5"),
            )

    def test_admin_adjusts_any_fake_figure(self):
        meter = EquipmentType.objects.get(code="MTR_SYN_A")
        inventory.adjust_fake(
            self.admin,
            meter,
            Decimal("-1"),
            "ตรวจนับ",
            remove_serials=["MTR_SYN_005"],
            bucket="awaiting_inspection",
        )
        self.assertEqual(self.figures("MTR_SYN_A")[1], 0)
        client = Client()
        client.force_login(self.admin)
        self.assertContains(client.get("/console/admin/inventory/"), "รอตรวจ")

    def test_demo_replays_req_syn_002(self):
        out = StringIO()
        call_command("demo_return", stdout=out)
        text = out.getvalue()
        self.assertIn("ขอคืน 2 มาถึง 1 พร้อมใช้ 0 ซ่อม 0 อื่น 0 รอตรวจ 1 ยุติ 0 ค้างส่ง 1", text)
        self.assertIn("พร้อมจ่าย 3.00 รอตรวจ 1.00", text)
        self.assertIn("รับคืนบางส่วน", text)


class ReturnChatTests(Returning, LineTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        Staffing.staff_up(cls)
        cls.supervisor = make_staff("supervisor", (Role.SUPERVISOR, "SIM-MTR"))
        Returning.return_staff(cls)

    def setUp(self):
        super().setUp()
        self.req1 = self.approve(ALICE)

    def test_tracking_shows_the_return_counts(self):
        case = self.returning()
        self.act(self.meter, case, "return_instructions", {"place": "คลังกองงานมิเตอร์"})
        self.receive(case, [{"item_code": "MTR_SYN_A", "serials": ["MTR_SYN_007"]}])
        body = texts(self.type(ALICE, case.number))
        self.assertIn("รับคืนบางส่วน", body)
        self.assertIn("ขอคืน 2 มาถึง 1", body)
        self.assertIn("ส่งคืนที่: คลังกองงานมิเตอร์", body)


# --- Concurrency (race) --------------------------------------------------------------------------


class ConcurrencyTests(Returning, TransactionTestCase):
    def setUp(self):
        setup_world(self)
        self.staff_up()
        self.return_staff()

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

    def test_the_same_serial_arriving_twice_at_once_counts_once(self):
        first = self.returning(meters_back("MTR_SYN_007"))
        second = self.returning(meters_back("MTR_SYN_007"), requester=self.req1)
        calls = [
            lambda case=case: self.receive(
                case, [{"item_code": "MTR_SYN_A", "serials": ["MTR_SYN_007"]}]
            )
            for case in (first, second)
        ]
        results, errors = self.run_parallel(calls)
        self.assertEqual((len(results), errors), (2, []))
        units = list(U.objects.filter(serial="MTR_SYN_007"))
        self.assertEqual(sum(1 for u in units if u.counted), 1)
        duplicates = {U.Discrepancy.DUPLICATE_STORE, U.Discrepancy.DUPLICATE_CASE}
        self.assertEqual([u.discrepancy in duplicates for u in units].count(True), 1)

    def test_parallel_inspections_of_one_unit_record_one(self):
        case = self.returning()
        self.receive(case, [{"item_code": "MTR_SYN_A", "serials": ["MTR_SYN_007"]}])
        revision = Case.objects.get(pk=case.pk).revision
        calls = [
            lambda user=user, split=split: services.perform_action(
                Actor.staff(user),
                case.pk,
                "inspect",
                expected_revision=revision,
                idempotency_key=f"ins-{uuid.uuid4().hex}",
                payload={"unit": 1, "split": split},
            )
            for user, split in ((self.inspector, {"repair": "1"}), (self.certifier, {"ready": "1"}))
        ]
        results, errors = self.run_parallel(calls)
        self.assertEqual((len(results), errors), (1, [ErrorCode.REVISION_CONFLICT]))
        self.assertEqual(Inspection.objects.count(), 1)
        self.assertEqual(self.unit(case).status, U.Status.FINAL)
