"""M09 problems, child work, service-calendar deadlines and escalation (PROJECT.md M09, Q1–Q18,
ADR 0004).

Acceptance → tests: the owner stays until accept and a refusal keeps the age (M06, plus child
work: ChildWorkTests); loops go to the coordinator (disputed child work); a multi-division case
has one owner and owners per child (R09Tests); no answer is escalated (DeadlineTests); a
reported hazard reaches people without AI (HazardTests); a problem never creates a withdrawal or
return by itself (ProblemTests).
"""

import threading
import uuid
from datetime import datetime, timedelta
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from operations import ai, notifications, orchestration, policy, problem_chat, services, servicetime
from operations.contracts import Actor, CommandFailed, ErrorCode
from operations.models import (
    AckClock,
    AuditEvent,
    AuthorityGrant,
    Case,
    CaseTriage,
    ChannelIdentity,
    ChildWork,
    DeadlinePause,
    Delivery,
    OutboxEvent,
    PostbackToken,
    Role,
)
from operations.tests.test_m02_cases import make_staff, problem, setup_world, withdrawal
from operations.tests.test_m03_line import ALICE, BOB, LineTestCase, texts
from operations.tests.test_m07_fulfilment import Staffing, lines

S = Case.Status
K = Case.Kind
A = AuthorityGrant.Activity
KIND = AckClock.Kind
BKK = ZoneInfo("Asia/Bangkok")
CAL = servicetime.Calendar(policy.load_document()["calendar"])


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=BKK)


def faulty(**overrides):
    return problem(
        description="หม้อแปลงรุ่นจำลอง A ขัดข้อง",
        lines=[{"item_code": "TRF_SYN_A", "serials": ["TRF_SYN_004"]}],
        **overrides,
    )


class Problems(Staffing):
    def problem_staff(self):
        self.trf_staff = make_staff("trf", (Role.STAFF, "SIM-TRF"))
        self.trf_lead = make_staff("trflead", (Role.SUPERVISOR, "SIM-TRF"))
        self.resolver = make_staff("resolver", (Role.RESOLVER, "SIM-TRF"))
        self.trf_issuer = make_staff("trfissuer", (Role.ISSUER, "SIM-TRF"))
        self.reg = make_staff("reg", (Role.STAFF, "SIM-REG"))
        self.coordinator = make_staff("coord", (Role.SUPERVISOR, "SIM-SUPERVISOR"))
        Staffing.grant(self, self.coordinator, A.ROUTING_DECISION)

    def triaged(self, payload=None, priority="P2"):
        """A problem case at SIM-TRF, in progress."""
        case = self.submit(payload or faulty(impact="ร้านค้า 3 รายไม่มีไฟ"))
        self.act(self.intake, case, "start_triage")
        self.act(self.intake, case, "request_handoff", {"to_division": "SIM-TRF"})
        self.act(self.trf_staff, case, "accept_handoff")
        self.act(self.trf_staff, case, "set_priority", {"priority": priority, "reason": "ทดสอบ"})
        self.act(self.trf_staff, case, "complete_triage")
        return Case.objects.get(pk=case.pk)

    def resolved(self, case=None):
        case = case or self.triaged()
        self.act(
            self.resolver,
            case,
            "resolve",
            {"category": "fixed", "message": "เปลี่ยนอุปกรณ์แล้ว", "internal_note": "ช่างสมชาย"},
        )
        return Case.objects.get(pk=case.pk)

    def clock(self, case, kind, open_only=True):
        clocks = AckClock.objects.filter(case=case, kind=kind)
        if open_only:
            clocks = clocks.filter(stopped_at__isnull=True)
        return clocks.order_by("-id").first()

    def settle_outbox(self):
        OutboxEvent.objects.update(status=OutboxEvent.Status.DONE)


class ProblemTestCase(Problems, TestCase):
    @classmethod
    def setUpTestData(cls):
        setup_world(cls)
        Staffing.staff_up(cls)
        Problems.problem_staff(cls)

    def assertFails(self, code, fn, *args, **kwargs):
        with self.assertRaises(CommandFailed) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        return caught.exception


# --- Service calendar (Q7, Q13, Q17) -----------------------------------------------------------


class CalendarTests(TestCase):
    def test_business_time_skips_lunch_evenings_weekends_and_holidays(self):
        self.assertEqual(CAL.add(at("2026-10-07 11:30"), 120, "P1"), at("2026-10-07 14:30"))
        self.assertEqual(CAL.add(at("2026-10-07 15:00"), 60, "P1"), at("2026-10-08 08:00"))
        self.assertEqual(CAL.add(at("2026-10-07 06:00"), 30, "P2"), at("2026-10-07 08:00"))
        # Friday 15:00 + 1 hour → Monday 08:00; Monday 12 Oct + 1 day → Wednesday (13th holiday).
        self.assertEqual(CAL.add(at("2026-10-09 15:00"), 60, "P2"), at("2026-10-12 08:00"))
        self.assertEqual(CAL.add(at("2026-10-12 07:30"), 2 * 420, "P3"), at("2026-10-14 15:30"))
        self.assertEqual(CAL.add(at("2026-10-10 23:00"), 15, "P0"), at("2026-10-10 23:15"))
        self.assertEqual(CAL.minutes_between(at("2026-10-07 11:30"), at("2026-10-07 14:30")), 120)
        self.assertEqual(CAL.minutes_between(at("2026-10-07 07:30"), at("2026-10-07 15:30")), 420)

    def test_policies_with_bad_calendars_or_deadlines_are_refused(self):
        from operations.models import Division  # noqa: F401 - divisions need loading below

        call_command("load_districts", stdout=StringIO())
        call_command("seed_demo_units", stdout=StringIO())
        for broken in (
            {"calendar": {"periods": [["12:00", "07:30"]]}},
            {"calendar": {"periods": [["07:30", "12:00"]], "holidays": ["13/10/2026"]}},
            {"deadlines": {"resolve": {"P0": 1}}},
            {"deadlines": {"coffee": {"P0": 1, "P1": 1, "P2": 1, "P3": 1, "none": 1}}},
        ):
            with self.subTest(broken=broken), self.assertRaises(ValidationError):
                policy.validate_document({**policy.load_document(), **broken})


# --- Deadlines, pauses and escalation (Q8, Q9, Q12) ---------------------------------------------


class DeadlineTests(ProblemTestCase):
    def test_problem_steps_have_their_own_deadlines(self):
        case = self.submit(faulty(impact="ร้านค้า 3 รายไม่มีไฟ"))
        # Before triage the level is "awaiting triage": P3-like business-time budgets.
        first = self.clock(case, KIND.FIRST_RESPONSE)
        self.assertEqual(first.due_at, CAL.add(case.submitted_at, 2 * 420, ""))
        intake = self.clock(case, KIND.INTAKE)
        self.assertEqual(intake.due_at, CAL.add(case.submitted_at, 3 * 420, ""))  # calendar
        case = self.triaged(faulty(impact="ร้านค้า 3 รายไม่มีไฟ"))
        self.assertIsNone(self.clock(case, KIND.FIRST_RESPONSE))  # done at complete_triage
        resolve = self.clock(case, KIND.RESOLVE)
        self.assertEqual(resolve.due_at, CAL.add(resolve.started_at, 3 * 420, "P2"))
        self.assertEqual(policy.update_due(case), resolve.due_at)
        self.resolved(case)
        self.assertIsNone(self.clock(case, KIND.RESOLVE))

    def test_district_turn_pauses_and_supervisor_approves_hold_pauses(self):
        case = self.triaged()
        self.act(self.trf_staff, case, "request_info", {"reason": "ขอรูปหน้างาน"})
        clock = self.clock(case, KIND.RESOLVE)
        self.assertIsNotNone(clock.paused_at)
        self.settle_outbox()
        self.assertEqual(policy.sweep_clocks(clock.escalate_at + timedelta(days=30))["reminded"], 0)
        pause = DeadlinePause.objects.get(case=case)
        self.act(self.trf_staff, case, "info_received")
        pause.refresh_from_db()
        # Two business hours of district turn push the deadline by two business hours.
        DeadlinePause.objects.filter(pk=pause.pk).update(
            started_at=clock.started_at, ended_at=CAL.add(clock.started_at, 120, "P2")
        )
        self.act(self.trf_staff, case, "set_priority", {"priority": "P2", "reason": "ทบทวน"})
        clock.refresh_from_db()
        self.assertIsNone(clock.paused_at)
        self.assertEqual(clock.paused_minutes, 120)
        self.assertEqual(clock.due_at, CAL.add(clock.started_at, 3 * 420 + 120, "P2"))
        # A hold is FAOJ's own choice: no pause unless a supervisor approves one.
        self.act(self.trf_staff, case, "hold", {"reason": "รออะไหล่"})
        self.assertIsNone(self.clock(case, KIND.RESOLVE).paused_at)
        self.assertFails(
            ErrorCode.FORBIDDEN, self.act, self.trf_staff, case, "pause_deadlines", {"reason": "x"}
        )
        self.act(self.trf_lead, case, "pause_deadlines", {"reason": "รออะไหล่จากผู้ผลิต"})
        supervisor_pause = DeadlinePause.objects.get(case=case, kind="supervisor")
        self.assertEqual(supervisor_pause.approved_by, self.trf_lead)
        self.assertIsNotNone(self.clock(case, KIND.RESOLVE).paused_at)
        self.act(self.trf_staff, case, "resume")
        supervisor_pause.refresh_from_db()
        self.assertIsNotNone(supervisor_pause.ended_at)

    def test_a_new_level_retimes_from_the_original_start(self):
        case = self.triaged()
        clock = self.clock(case, KIND.RESOLVE)
        self.act(self.trf_staff, case, "set_priority", {"priority": "P1", "reason": "ผลกระทบเพิ่ม"})
        clock.refresh_from_db()
        self.assertEqual(clock.due_at, CAL.add(clock.started_at, 420, "P1"))

    def test_withdrawal_and_return_steps_are_timed_but_district_waits_are_not(self):
        case = self.ready()
        approval = self.clock(case, KIND.APPROVAL)
        self.assertEqual(approval.due_at, CAL.add(approval.started_at, 3 * 420, "P3"))
        self.act(self.approver, case, "approve", lines((1, "1", "บางส่วน")))
        self.assertIsNone(self.clock(case, KIND.APPROVAL))
        self.assertIsNotNone(self.clock(case, KIND.ISSUE))
        self.issue(self.issuer, case, 1, ["MTR_SYN_001"])
        # Waiting for the district's receipt is not FAOJ's deadline.
        self.assertFalse(AckClock.objects.filter(case=case, stopped_at__isnull=True).exists())

    def test_work_deadlines_escalate_once_to_the_division_supervisors(self):
        case = self.triaged()
        clock = self.clock(case, KIND.RESOLVE)
        self.settle_outbox()
        self.assertEqual(policy.sweep_clocks(clock.due_at)["reminded"], 1)
        self.assertEqual(policy.sweep_clocks(clock.escalate_at)["escalated"], 1)
        self.assertEqual(policy.sweep_clocks(clock.escalate_at + timedelta(days=1))["escalated"], 0)
        event = AuditEvent.objects.get(action="case.deadline_escalated")
        self.assertEqual(event.data["to"], "supervisors:SIM-TRF")
        # Not a routing matter: the coordinator neither sees it nor decides it.
        self.assertFalse(
            services.visible_cases(Actor.staff(self.coordinator)).filter(pk=case.pk).exists()
        )


# --- Problems (Q1–Q5, Q15) ----------------------------------------------------------------------


class ProblemTests(ProblemTestCase):
    def test_only_a_resolver_records_a_result_and_never_with_child_work_open(self):
        case = self.triaged()
        result = {"category": "fixed", "message": "แก้แล้ว"}
        self.assertFails(ErrorCode.FORBIDDEN, self.act, self.trf_staff, case, "resolve", result)
        self.assertFails(
            ErrorCode.VALIDATION_ERROR,
            self.act,
            self.resolver,
            case,
            "resolve",
            {"category": "x", "message": "y"},
        )
        self.act(
            self.trf_staff,
            case,
            "request_child_work",
            {"division": "SIM-REG", "task": "ตรวจทะเบียนเลขเครื่อง", "rule": "R07"},
        )
        error = self.assertFails(
            ErrorCode.VALIDATION_ERROR, self.act, self.resolver, case, "resolve", result
        )
        self.assertIn("งานย่อย", error.message)
        self.act(self.reg, case, "accept_child_work", {"child": 1})
        self.act(
            self.reg,
            case,
            "finish_child_work",
            {"child": 1, "result": "done", "summary": "ทะเบียนถูกต้อง"},
        )
        self.assertEqual(
            self.act(self.resolver, case, "resolve", result).status, S.AWAITING_CONFIRMATION
        )

    def test_accepting_closes_and_intake_may_answer_for_the_requester(self):
        case = self.resolved()
        self.assertFails(ErrorCode.FORBIDDEN, self.act, self.trf_staff, case, "confirm")
        self.assertFails(
            ErrorCode.VALIDATION_ERROR, self.act, self.intake, case, "confirm", {"channel": "phone"}
        )
        result = self.act(
            self.intake, case, "confirm", {"channel": "phone", "intake_note": "เขตโทรมา"}
        )
        self.assertEqual(result.status, S.CLOSED)

    def test_disputes_go_back_and_a_second_one_is_flagged(self):
        case = self.resolved()
        result = self.act(self.req1, case, "dispute_resolution", {"reason": "ไฟยังดับ"})
        self.assertEqual(result.status, S.IN_PROGRESS)
        case.refresh_from_db()
        self.assertEqual(case.owner_division.code, "SIM-TRF")
        self.assertIsNotNone(self.clock(case, KIND.RESOLVE))  # a new round of work is timed
        self.resolved(case)
        self.act(self.req1, case, "dispute_resolution", {"reason": "ยังเหมือนเดิม"})
        self.assertTrue(AuditEvent.objects.filter(action="case.repeat_dispute").exists())
        card = policy.card(Case.objects.get(pk=case.pk))
        self.assertTrue(card["problem"]["repeat_dispute"])
        self.assertEqual(card["problem"]["resolution"]["internal_note"], "ช่างสมชาย")

    def test_staff_close_only_after_seven_days_without_an_answer(self):
        case = self.resolved()
        close = {"reason": "ผู้แจ้งไม่ตอบ", "notice": "ปิดเรื่องเพราะไม่ได้รับคำตอบภายใน 7 วัน"}
        self.assertFails(ErrorCode.VALIDATION_ERROR, self.act, self.trf_staff, case, "close", close)
        case.decisions.filter(action="resolve").update(
            created_at=timezone.now() - timedelta(days=8)
        )
        self.assertEqual(self.act(self.trf_staff, case, "close", close).status, S.CLOSED)

    def test_reopen_within_thirty_days_by_the_requester_or_owner(self):
        case = self.resolved()
        self.act(self.req1, case, "confirm")
        result = self.act(self.req1, case, "reopen", {"reason": "เสียอีกแล้ว"})
        self.assertEqual(result.status, S.IN_PROGRESS)
        case.refresh_from_db()
        self.assertEqual(
            (case.owner_division.code, case.assignee, case.ended_at), ("SIM-TRF", None, None)
        )
        self.resolved(case)
        self.act(self.req1, case, "confirm")
        Case.objects.filter(pk=case.pk).update(ended_at=timezone.now() - timedelta(days=31))
        error = self.assertFails(
            ErrorCode.VALIDATION_ERROR, self.act, self.trf_staff, case, "reopen", {"reason": "x"}
        )
        self.assertIn("ยื่นเรื่องแจ้งปัญหา/ข้อเสนอแนะใหม่", error.message)

    def test_a_problem_creates_nothing_but_withdrawals_may_refer_to_it(self):
        case = self.resolved()
        before = Case.objects.count()
        self.act(self.req1, case, "confirm")
        self.assertEqual(Case.objects.count(), before)  # no automatic withdrawal or return
        linked = self.submit(withdrawal(related_case_number=case.number))
        self.assertEqual(linked.related_case, case)
        self.assertEqual(policy.card(case)["linked"], [linked.number])


# --- Child work (Q6) and R09 lines (Q11, Q18, ADR 0004) ------------------------------------------


class ChildWorkTests(ProblemTestCase):
    def test_request_accept_decline_dispute_and_coordinator_decision(self):
        case = self.triaged()
        ask = {"division": "SIM-REG", "task": "ตรวจทะเบียน", "rule": "R07"}
        # Another division does not even see the case.
        self.assertFails(ErrorCode.NOT_FOUND, self.act, self.meter, case, "request_child_work", ask)
        self.assertFails(
            ErrorCode.VALIDATION_ERROR,
            self.act,
            self.trf_staff,
            case,
            "request_child_work",
            {**ask, "division": "SIM-TRF"},
        )
        self.act(self.trf_staff, case, "request_child_work", ask)
        self.assertTrue(services.visible_cases(Actor.staff(self.reg)).filter(pk=case.pk).exists())
        self.assertIsNotNone(self.clock(case, KIND.CHILD_ACCEPT))
        self.assertFails(
            ErrorCode.FORBIDDEN, self.act, self.trf_staff, case, "accept_child_work", {"child": 1}
        )
        self.act(self.reg, case, "decline_child_work", {"child": 1, "reason": "ไม่ใช่งานทะเบียน"})
        self.assertIsNone(self.clock(case, KIND.CHILD_ACCEPT))
        self.act(self.trf_staff, case, "request_child_work", ask)
        child = ChildWork.objects.get(case=case, seq=2)
        self.assertEqual(child.status, ChildWork.Status.DISPUTED)  # asked again after a refusal
        self.assertTrue(
            services.visible_cases(Actor.staff(self.coordinator)).filter(pk=case.pk).exists()
        )
        decide = {"child": 2, "division": "SIM-REG", "reason": "เป็นงานทะเบียนตามกติกา R07"}
        self.assertFails(
            ErrorCode.FORBIDDEN, self.act, self.trf_lead, case, "decide_child_work", decide
        )
        self.act(self.coordinator, case, "decide_child_work", decide)
        child.refresh_from_db()
        self.assertEqual(child.status, ChildWork.Status.ACCEPTED)
        self.assertIsNotNone(self.clock(case, KIND.CHILD_RESULT))
        # A child division reads the case; acting on it beyond its child work is not its duty.
        self.assertFails(ErrorCode.FORBIDDEN, self.act, self.reg, case, "hold", {"reason": "x"})
        self.act(
            self.reg,
            case,
            "finish_child_work",
            {"child": 2, "result": "failed", "summary": "ไม่มีเอกสาร"},
        )
        self.assertIsNone(self.clock(case, KIND.CHILD_RESULT))

    def test_open_child_work_keeps_a_withdrawal_open_and_ends_with_the_case(self):
        case = self.approved(1)
        self.act(
            self.meter,
            case,
            "request_child_work",
            {"division": "SIM-REG", "task": "ตรวจทะเบียน", "rule": "R07"},
        )
        self.act(self.reg, case, "accept_child_work", {"child": 1})
        self.issue(self.issuer, case, 1, ["MTR_SYN_001"])
        result = self.act(self.req1, case, "confirm", {"delivery": 1, "result": "full"})
        self.assertEqual(result.status, S.AWAITING_CONFIRMATION)  # the registry check is open
        closed = self.act(
            self.reg, case, "finish_child_work", {"child": 1, "result": "done", "summary": "ถูกต้อง"}
        )
        self.assertEqual(closed.status, S.CLOSED)
        other = self.ready()
        self.act(self.meter, other, "request_child_work", {"division": "SIM-REG", "task": "x"})
        self.act(self.approver, other, "deny_approval", {"reason": "ไม่ตรงระเบียบ"})
        self.assertEqual(ChildWork.objects.get(case=other).status, ChildWork.Status.CANCELLED)


class R09Tests(ProblemTestCase):
    def r09(self):
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
        self.assertFails(ErrorCode.VALIDATION_ERROR, self.act, self.intake, case, "complete_triage")
        self.assertFails(
            ErrorCode.VALIDATION_ERROR,
            self.act,
            self.intake,
            case,
            "request_child_work",
            {"division": "SIM-MTR", "task": "x", "rule": "R09"},
        )
        for division, line in (("SIM-MTR", 1), ("SIM-TRF", 2)):
            self.act(
                self.intake,
                case,
                "request_child_work",
                {"division": division, "task": "จ่ายรายการของกอง", "rule": "R09", "lines": [line]},
            )
        self.act(self.intake, case, "complete_triage")
        self.act(self.approver, case, "approve", lines((1, "1"), (2, "1")))
        return Case.objects.get(pk=case.pk)

    def test_each_division_issues_its_own_line_once_it_accepted(self):
        case = self.r09()
        self.assertEqual(case.owner_division.code, "SIM-TRIAGE")
        # Before the child division accepts, the line is still with the owner.
        self.assertFails(ErrorCode.FORBIDDEN, self.issue, self.issuer, case, 1, ["MTR_SYN_001"])
        self.act(self.meter, case, "accept_child_work", {"child": 1})
        self.act(self.trf_staff, case, "accept_child_work", {"child": 2})
        self.assertFails(  # SIM-MTR's issuer does not issue SIM-TRF's line
            ErrorCode.FORBIDDEN,
            self.act,
            self.issuer,
            case,
            "issue",
            {
                "recipient_name": "x",
                "lines": [{"line_no": 2, "quantity": "1", "serials": ["TRF_SYN_001"]}],
            },
        )
        self.issue(self.issuer, case, 1, ["MTR_SYN_001"])
        self.act(
            self.trf_issuer,
            case,
            "issue",
            {
                "recipient_name": "x",
                "lines": [{"line_no": 2, "quantity": "1", "serials": ["TRF_SYN_001"]}],
            },
        )
        self.act(self.req1, case, "confirm", {"delivery": 1, "result": "full"})
        self.assertEqual(ChildWork.objects.get(case=case, seq=1).status, ChildWork.Status.DONE)
        self.assertEqual(ChildWork.objects.get(case=case, seq=2).status, ChildWork.Status.ACCEPTED)
        self.assertFails(
            ErrorCode.VALIDATION_ERROR,
            self.act,
            self.trf_staff,
            case,
            "finish_child_work",
            {"child": 2, "result": "done", "summary": "x"},
        )
        result = self.act(self.req1, case, "confirm", {"delivery": 2, "result": "full"})
        self.assertEqual(result.status, S.CLOSED)
        self.assertEqual(
            set(ChildWork.objects.filter(case=case).values_list("status", flat=True)), {"done"}
        )
        self.assertEqual(
            {d.issued_by.username for d in Delivery.objects.filter(case=case)},
            {"mtrissuer", "trfissuer"},
        )

    def test_a_line_belongs_to_one_open_child_work(self):
        case = self.submit(
            withdrawal(
                lines=[
                    {"item_code": "MTR_SYN_A", "quantity": "1"},
                    {"item_code": "TRF_SYN_A", "quantity": "1"},
                ]
            )
        )
        self.act(self.intake, case, "start_triage")
        ask = {"division": "SIM-MTR", "task": "x", "rule": "R09", "lines": [1]}
        self.act(self.intake, case, "request_child_work", ask)
        error = self.assertFails(
            ErrorCode.VALIDATION_ERROR,
            self.act,
            self.intake,
            case,
            "request_child_work",
            {**ask, "division": "SIM-EQP"},
        )
        self.assertIn("อยู่ในงานย่อย", error.message)
        # A refused-then-asked-again child makes the case the coordinator's to see (Q6) ...
        trf = {"division": "SIM-TRF", "task": "x", "rule": "R09", "lines": [2]}
        self.act(self.intake, case, "request_child_work", trf)
        self.act(self.trf_staff, case, "decline_child_work", {"child": 2, "reason": "ไม่ใช่งาน"})
        self.act(self.intake, case, "request_child_work", trf)
        self.assertEqual(ChildWork.objects.get(case=case, seq=3).status, ChildWork.Status.DISPUTED)
        # ... but only disputed child work is theirs to place.
        self.assertFails(
            ErrorCode.VALIDATION_ERROR,
            self.act,
            self.coordinator,
            case,
            "decide_child_work",
            {"child": 1, "division": "SIM-EQP", "reason": "x"},
        )

    def test_child_work_accepted_before_approval_stays_open(self):
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
        for division, line in (("SIM-MTR", 1), ("SIM-TRF", 2)):
            self.act(
                self.intake,
                case,
                "request_child_work",
                {"division": division, "task": "x", "rule": "R09", "lines": [line]},
            )
        self.act(self.meter, case, "accept_child_work", {"child": 1})
        self.assertEqual(ChildWork.objects.get(case=case, seq=1).status, ChildWork.Status.ACCEPTED)

    def test_each_child_line_has_its_own_issue_deadline(self):
        case = self.r09()
        self.act(self.meter, case, "accept_child_work", {"child": 1})
        clocks = AckClock.objects.filter(case=case, kind=KIND.ISSUE, stopped_at__isnull=True)
        self.assertEqual(
            {(c.division.code, c.child_work_id is not None) for c in clocks},
            {("SIM-MTR", True), ("SIM-TRIAGE", False)},
        )


# --- Hazards without AI (Q10) --------------------------------------------------------------------


class HazardTests(ProblemTestCase):
    @override_settings(AI_ENABLED=True)
    def test_a_reported_hazard_is_p0_and_guided_while_ai_is_down(self):
        with patch.object(ai, "active_version", side_effect=RuntimeError("provider down")):
            case = self.submit(problem(description="สายไฟขาดห้อย มีประกายไฟ"))
        state = CaseTriage.objects.get(case=case)
        self.assertEqual(state.priority_floor, "P0")
        clock = self.clock(case, KIND.INTAKE)
        self.assertEqual(clock.due_at, case.submitted_at + timedelta(minutes=15))  # continuous
        self.assertIn("ช่องทางฉุกเฉิน", policy.hazard_guidance(case))
        self.assertTrue(policy.card(case)["hazard_alert"])

    def test_staff_filed_hazards_are_acknowledged_by_push(self):
        ChannelIdentity.objects.create(
            channel="line", external_id=ALICE, requester=self.req1, status="active"
        )
        for description, expected in (("มีควันออกจากตู้", "hazard_ack"), ("มิเตอร์จอดับ", None)):
            result = services.submit_case(
                Actor.staff(self.intake),
                idempotency_key=f"k-{uuid.uuid4().hex}",
                payload={
                    **problem(description=description),
                    "requester_id": self.req1.pk,
                    "channel": "phone",
                    "intake_note": "เขตโทรมา",
                },
            )
            event = OutboxEvent.objects.get(
                event_type=orchestration.EVENT_CASE_SUBMITTED, subject_id=result.case_id
            )
            planned = notifications.plan_for_event(event)
            with self.subTest(description=description):
                self.assertEqual(planned.kind if planned else None, expected)
                if planned:
                    self.assertEqual(planned.level, "critical")


# --- LINE (Q1, Q2, Q4, Q14) ---------------------------------------------------------------------


class ProblemChatTests(Problems, LineTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        Staffing.staff_up(cls)
        Problems.problem_staff(cls)

    def setUp(self):
        super().setUp()
        self.req1 = self.approve(ALICE)
        self.approve(BOB, "D02")
        self.identity = ChannelIdentity.objects.get(external_id=ALICE)

    def card_buttons(self, case):
        _, message = problem_chat.resolution_card(case, self.identity)
        return {
            b["action"]["label"]: b["action"]["data"]
            for b in message["contents"]["footer"]["contents"]
        }

    def test_hazard_guidance_in_the_submission_reply(self):
        from operations import conversation
        from operations.models import Conversation, ConversationDraft

        conv, _ = Conversation.objects.get_or_create(identity=self.identity)
        replies = []
        for description, warned in (("หม้อแปลงมีควันและประกายไฟ", True), ("มิเตอร์จอดับ", False)):
            draft = ConversationDraft.objects.create(
                identity=self.identity,
                requester=self.req1,
                kind="problem",
                step="confirm",
                data={"lines": [], "fields": {"description": description}},
                updated_at=timezone.now(),
            )
            turn = conversation.Turn(None, {}, self.identity, conv, timezone.now())
            turn.requester = self.req1
            conversation._confirm(turn, draft)
            replies.append(texts(turn.replies))
            with self.subTest(description=description):
                self.assertEqual("คำแนะนำเร่งด่วน" in turn.replies[-1]["text"], warned)

    def test_accept_from_the_card(self):
        case = self.resolved()
        delivery_event = OutboxEvent.objects.filter(
            event_type=orchestration.EVENT_DECISION, payload__action="resolve"
        ).get()
        planned = notifications.plan_for_event(delivery_event)
        self.assertEqual((planned.kind, planned.level), ("resolution_request", "critical"))
        self.assertIn("เปลี่ยนอุปกรณ์แล้ว", planned.text)
        self.assertNotIn("ช่างสมชาย", planned.text)  # the internal note stays inside (Q15)
        refused = self.say(self.postback(BOB, self.card_buttons(case)["ยอมรับผล"]))
        self.assertIn("ปุ่มนี้ใช้ไม่ได้", texts(refused))
        reply = self.say(self.postback(ALICE, self.card_buttons(case)["ยอมรับผล"]))
        self.assertIn("ปิดแล้ว", texts(reply))
        self.assertEqual(Case.objects.get(pk=case.pk).status, S.CLOSED)

    def test_dispute_then_reopen_with_typed_reasons(self):
        case = self.resolved()
        self.say(self.postback(ALICE, self.card_buttons(case)["ยังไม่เรียบร้อย"]))
        reply = self.type(ALICE, "ไฟยังดับอยู่")
        self.assertIn("ดำเนินการต่อ", texts(reply))
        case.refresh_from_db()
        self.assertEqual(case.status, S.IN_PROGRESS)
        detail = texts(self.type(ALICE, case.number))
        self.assertIn("กำหนดอัปเดต", detail)
        self.resolved(case)
        self.act(self.req1, case, "confirm")
        PostbackToken.objects.all().update(expires_at=timezone.now() - timedelta(minutes=1))
        detail = self.type(ALICE, case.number)
        self.press(ALICE, "เปิดเรื่องใหม่", detail)
        self.type(ALICE, "กลับมาเสียอีก")
        self.assertEqual(Case.objects.get(pk=case.pk).status, S.IN_PROGRESS)


# --- Concurrency and the demo -------------------------------------------------------------------


class ConcurrencyTests(Problems, TransactionTestCase):
    def setUp(self):
        setup_world(self)
        self.staff_up()
        self.problem_staff()

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

    def at_revision(self, who, case, action, payload, revision):
        actor = Actor.staff(who) if hasattr(who, "is_authenticated") else Actor.for_requester(who)
        return services.perform_action(
            actor,
            case.pk,
            action,
            expected_revision=revision,
            idempotency_key=f"p-{uuid.uuid4().hex}",
            payload=payload,
        )

    def test_two_members_accepting_one_child_work_record_one_answer(self):
        case = self.triaged()
        self.act(
            self.trf_staff, case, "request_child_work", {"division": "SIM-REG", "task": "ตรวจ"}
        )
        reg2 = make_staff("reg2", (Role.STAFF, "SIM-REG"))
        revision = Case.objects.get(pk=case.pk).revision
        calls = [
            lambda u=u: self.at_revision(u, case, "accept_child_work", {"child": 1}, revision)
            for u in (self.reg, reg2)
        ]
        results, errors = self.run_parallel(calls)
        self.assertEqual((len(results), errors), (1, [ErrorCode.REVISION_CONFLICT]))

    def test_accept_and_dispute_at_once_record_one(self):
        case = self.resolved()
        revision = Case.objects.get(pk=case.pk).revision
        calls = [
            lambda: self.at_revision(self.req1, case, "confirm", None, revision),
            lambda: self.at_revision(
                self.req1, case, "dispute_resolution", {"reason": "ไม่ดี"}, revision
            ),
        ]
        results, errors = self.run_parallel(calls)
        self.assertEqual((len(results), errors), (1, [ErrorCode.REVISION_CONFLICT]))

    def test_parallel_sweeps_remind_once(self):
        case = self.triaged()
        clock = self.clock(case, KIND.RESOLVE)
        self.settle_outbox()
        results, errors = self.run_parallel(
            [lambda: policy.sweep_clocks(clock.due_at) for _ in range(3)]
        )
        self.assertEqual(errors, [])
        self.assertEqual(sum(r["reminded"] for r in results), 1)
        self.assertEqual(AuditEvent.objects.filter(action="case.deadline_overdue").count(), 1)


class DemoTests(ProblemTestCase):
    def test_demo_replays_req_syn_003_and_s06(self):
        call_command("demo_withdrawal", stdout=StringIO())  # demos re-run on one demo DB
        out = StringIO()
        call_command("demo_problem", stdout=out)
        text = out.getvalue()
        self.assertIn("REQ_SYN_003", text)
        self.assertIn("สถานะ ปิดเรื่อง", text)
        self.assertIn("พักกรอบเวลาระหว่างรอข้อมูลจากเขต: 1 ครั้ง", text)
        self.assertIn("เรื่องเบิก/คืนที่ระบบสร้างให้: 0", text)
        self.assertIn("งานย่อย 1 SIM-MTR: เสร็จ", text)
        self.assertIn("งานย่อย 2 SIM-TRF: เสร็จ", text)
        self.assertIn("เจ้าภาพ SIM-TRIAGE", text)
