import itertools
import json
import secrets
import threading
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, OperationalError, connection, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from operations import identity, options, services, workflow
from operations.contracts import Actor, CommandFailed, ErrorCode, contract_document
from operations.models import (
    ActionReceipt,
    AuditEvent,
    Case,
    CaseNumberCounter,
    Decision,
    District,
    Division,
    EquipmentType,
    OutboxEvent,
    Requester,
    Role,
    RoleGrant,
    StaffProfile,
    SystemOption,
)

User = get_user_model()
S = Case.Status
K = Case.Kind
_numbers = itertools.count(1)
DEFAULT = object()  # lets tests pass "" or None deliberately

ITEMS = [
    ("MTR_SYN_A", "meter", "เครื่อง", True, False),
    ("TRF_SYN_A", "transformer", "เครื่อง", True, False),
    ("FUSE_SYN_A", "other", "ชิ้น", False, False),
    ("CB_SYN_A", "other", "ชิ้น", False, False),
    ("CBL_SYN_A", "other", "เมตร", False, True),
]


def key():
    return f"test-{uuid.uuid4().hex}"


def withdrawal(**overrides):
    payload = {
        "kind": "withdrawal",
        "purpose": "เปลี่ยนมิเตอร์ชำรุด",
        "lines": [{"item_code": "MTR_SYN_A", "quantity": "3"}],
    }
    payload.update(overrides)
    return payload


def return_payload(**overrides):
    payload = {
        "kind": "return",
        "condition": "ชำรุด",
        "reason": "ถอดเปลี่ยน",
        "lines": [{"item_code": "MTR_SYN_A", "quantity": "2", "serials": ["S1", "S2"]}],
    }
    payload.update(overrides)
    return payload


def problem(**overrides):
    payload = {"kind": "problem", "description": "หม้อแปลงขัดข้อง"}
    payload.update(overrides)
    return payload


def make_staff(username, *roles):
    user = User.objects.create_user(username=username, password=secrets.token_urlsafe(16))
    StaffProfile.objects.create(user=user, display_name=username.title())
    for role, code in roles:
        RoleGrant.objects.create(
            user=user, role=role, division=Division.objects.get(code=code) if code else None
        )
    return user


def setup_world(target):
    """Shared reference data for TestCase and TransactionTestCase classes."""
    # The SIM world keeps the design before 16 Oct 2569 Q3: each division's work is split into
    # duties and consecutive steps need different people, so the duty tests test that mode.
    # ...and screens new cases, as before ADR 0025 (tests of the new way switch it off).
    for key, value in (
        (options.DIVISION_WORK, "roles"),
        (options.SEPARATION, "on"),
        (options.SCREENING, "on"),
    ):
        SystemOption.objects.update_or_create(key=key, defaults={"value": value})
    call_command("load_districts", stdout=StringIO())
    call_command("seed_demo_units", stdout=StringIO())
    for code, category, unit, serialized, divisible in ITEMS:
        EquipmentType.objects.create(
            code=code,
            name=code,
            category=category,
            unit=unit,
            is_serialized=serialized,
            is_divisible=divisible,
        )
    target.admin = make_staff("admin", (Role.ADMIN, None))
    target.intake = make_staff("intake", (Role.STAFF, "SIM-TRIAGE"))
    target.intake2 = make_staff("intake2", (Role.STAFF, "SIM-TRIAGE"))
    target.meter = make_staff("meter", (Role.STAFF, "SIM-MTR"))
    target.meter2 = make_staff("meter2", (Role.STAFF, "SIM-MTR"))
    target.supervisor = make_staff("supervisor", (Role.SUPERVISOR, "SIM-MTR"))
    target.transformer = make_staff("transformer", (Role.STAFF, "SIM-TRF"))
    target.triage = Division.objects.get(code="SIM-TRIAGE")
    target.mtr = Division.objects.get(code="SIM-MTR")
    target.req1 = Requester.objects.create(
        display_name="ผู้แจ้ง 1", district=District.objects.get(code="D01"), approved_by=target.admin
    )
    target.req2 = Requester.objects.create(
        display_name="ผู้แจ้ง 2", district=District.objects.get(code="D02"), approved_by=target.admin
    )


class CaseTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        setup_world(cls)

    def as_requester(self, requester=None):
        return Actor.for_requester(requester or self.req1)

    def as_staff(self, user):
        return Actor.staff(user)

    def submit(self, actor=None, payload=None, idempotency_key=DEFAULT):
        return services.submit_case(
            actor or self.as_requester(),
            idempotency_key=key() if idempotency_key is DEFAULT else idempotency_key,
            payload=payload or withdrawal(),
        )

    def act(self, actor, case, action, revision=DEFAULT, payload=None, idempotency_key=DEFAULT):
        case_id = case.case_id if hasattr(case, "case_id") else case.pk
        if revision is DEFAULT:
            revision = Case.objects.get(pk=case_id).revision
        return services.perform_action(
            actor,
            case_id,
            action,
            expected_revision=revision,
            idempotency_key=key() if idempotency_key is DEFAULT else idempotency_key,
            payload=payload,
        )

    def assertFails(self, code, fn, *args, **kwargs):
        with self.assertRaises(CommandFailed) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        return caught.exception

    def make_case(self, kind, status, owner=None, requester=None):
        """A case placed directly in a status (test-only shortcut around the matrix)."""
        requester = requester or self.req1
        now = timezone.now()
        return Case.objects.create(
            number=f"X-{next(_numbers):06d}",
            kind=kind,
            status=status,
            resume_status=S.TRIAGE if status in workflow.PAUSED else "",
            requester=requester,
            district=requester.district,
            owner_division=owner or self.mtr,
            channel=Case.Channel.LINE,
            submitted_at=now,
            updated_at=now,
        )


# --- Executable transition matrix -------------------------------------------------------------

WORKING = {
    K.WITHDRAWAL: {
        "triage",
        "awaiting_approval",
        "awaiting_execution",
        "in_progress",
        "awaiting_confirmation",
    },
    # ADR 0025: returns and problem reports wait for the owner division's approval too.
    K.RETURN: {"triage", "awaiting_approval", "awaiting_execution", "in_progress"},
    K.PROBLEM: {"triage", "awaiting_approval", "in_progress", "awaiting_confirmation"},
}
PAUSED = {"awaiting_info", "on_hold"}
# Agreed design (grilling Q4, Q15, Q16), written independently of workflow.py.
EXPECTED_SOURCES = {
    "start_triage": {k: {"submitted"} for k in K},
    "claim": {k: WORKING[k] | PAUSED for k in K},
    "assign": {k: WORKING[k] | PAUSED for k in K},
    "request_info": {k: WORKING[k] - {"awaiting_confirmation"} for k in K},
    "info_received": {k: {"awaiting_info"} for k in K},
    "hold": {k: set(WORKING[k]) for k in K},
    "resume": {k: {"on_hold"} for k in K},
    "cancel": {k: {"submitted"} | WORKING[k] | PAUSED for k in K},
    "decline": {k: {"triage"} for k in K},
}
EXPECTED_TARGET = {
    "start_triage": "triage",
    "request_info": "awaiting_info",
    "hold": "on_hold",
    "cancel": "cancelled",
    "decline": "rejected",
}


class TransitionMatrixTests(CaseTestCase):
    def actor_and_payload(self, action):
        payloads = {
            "assign": (self.supervisor, {"assignee_id": self.meter.pk}),
            "request_info": (self.meter, {"reason": "ขอรูปถ่าย"}),
            "hold": (self.meter, {"reason": "รอของเข้าคลัง"}),
            "cancel": (self.meter, {"reason": "เขตแจ้งยกเลิก"}),
            "decline": (self.meter, {"reason": "เรื่องซ้ำ"}),
        }
        user, payload = payloads.get(action, (self.meter, None))
        return self.as_staff(user), payload

    def test_matrix_encodes_the_agreed_m02_design(self):
        for action, per_kind in EXPECTED_SOURCES.items():
            spec = workflow.ACTIONS[action]
            self.assertTrue(spec.enabled, action)
            for kind in K:
                self.assertEqual(
                    {str(s) for s in spec.transitions[kind]}, per_kind[kind], (action, kind)
                )
        # M06 enables its own actions; their matrix is tested in test_m06_policy.
        enabled = {
            name for name, spec in workflow.ACTIONS.items() if spec.enabled and spec.module == "M02"
        }
        self.assertEqual(enabled, set(EXPECTED_SOURCES))

    def test_every_enabled_action_from_every_status_is_allowed_exactly_per_matrix(self):
        for action in EXPECTED_SOURCES:
            actor, payload = self.actor_and_payload(action)
            for kind in K:
                for status in S:
                    with self.subTest(action=action, kind=kind, status=status):
                        case = self.make_case(kind, status)
                        allowed = status in EXPECTED_SOURCES[action][kind]
                        if not allowed:
                            self.assertFails(
                                ErrorCode.INVALID_TRANSITION,
                                self.act,
                                actor,
                                case,
                                action,
                                payload=payload,
                            )
                            case.refresh_from_db()
                            self.assertEqual((case.status, case.revision), (status, 1))
                            continue
                        result = self.act(actor, case, action, payload=payload)
                        if action in {"info_received", "resume"}:
                            expected = "triage"  # the resume status make_case recorded
                        else:
                            expected = EXPECTED_TARGET.get(action, status)
                        self.assertEqual((result.status, result.revision), (expected, 2))

    def test_actions_of_later_modules_are_refused_not_faked(self):
        case = self.make_case(K.WITHDRAWAL, S.AWAITING_APPROVAL)
        for name, spec in workflow.ACTIONS.items():
            # Shared lifecycle actions arrive per kind (withdrawal with M07, Q16 excludes
            # reopen for withdrawals: that is an invalid transition, tested in M07).
            if spec.enabled_for(K.WITHDRAWAL):
                continue
            with self.subTest(action=name):
                error = self.assertFails(
                    ErrorCode.ACTION_NOT_ENABLED,
                    self.act,
                    self.as_staff(self.admin),
                    case,
                    name,
                )
                self.assertIn(spec.module, error.message)
        case.refresh_from_db()
        self.assertEqual((case.status, case.revision), (S.AWAITING_APPROVAL, 1))
        self.assertFalse(Decision.objects.filter(case=case).exists())

    def test_matrix_stays_inside_each_kind_lifecycle(self):
        for spec in workflow.ACTIONS.values():
            for kind, mapping in spec.transitions.items():
                allowed = (
                    set(workflow.KIND_STATUSES[kind])
                    | workflow.PAUSED
                    | workflow.TERMINAL
                    | {workflow.RESUME}
                )
                for source, targets in mapping.items():
                    self.assertIn(source, allowed - {workflow.RESUME}, (spec.name, kind))
                    self.assertTrue(targets <= allowed, (spec.name, kind, source))
                    if source in workflow.TERMINAL:
                        # Only reopen leaves a terminal status; deliveries made before a
                        # withdrawal was cancelled can still be confirmed or settled (M07 Q7)
                        # without leaving it.
                        receipt_after_cancel = (
                            kind == K.WITHDRAWAL
                            and source == S.CANCELLED
                            and spec.name in {"confirm", "resolve_discrepancy"}
                            and targets == {S.CANCELLED}
                        )
                        self.assertTrue(
                            receipt_after_cancel or (spec.name, source) == ("reopen", S.CLOSED),
                            (spec.name, kind, source),
                        )

    def test_every_lifecycle_status_is_reachable_and_can_reach_closed(self):
        for kind in K:
            edges = {}
            for spec in workflow.ACTIONS.values():
                for source, targets in spec.transitions.get(kind, {}).items():
                    edges.setdefault(source, set()).update(t for t in targets if t != "resume")
            seen, frontier = set(), [S.SUBMITTED]
            while frontier:
                status = frontier.pop()
                if status not in seen:
                    seen.add(status)
                    frontier.extend(edges.get(status, ()))
            expected = set(workflow.KIND_STATUSES[kind]) | workflow.PAUSED | workflow.TERMINAL
            self.assertEqual(seen, expected, kind)

    def test_return_cases_close_without_district_confirmation(self):
        for spec in workflow.ACTIONS.values():
            for targets in spec.transitions[K.RETURN].values():
                self.assertNotIn(S.AWAITING_CONFIRMATION, targets)
        self.assertEqual(workflow.ACTIONS["close"].targets(K.RETURN, S.IN_PROGRESS), {S.CLOSED})

    def test_contract_document_exports_matrix_actions_and_errors(self):
        document = json.loads(json.dumps(contract_document(), ensure_ascii=False))
        self.assertEqual(set(document["actions"]), set(workflow.ACTIONS))
        self.assertEqual(len(document["matrix"]), len(workflow.matrix_rows()))
        for code in ("REVISION_CONFLICT", "IDEMPOTENCY_CONFLICT", "ACTION_NOT_ENABLED"):
            self.assertIn(code, document["errors"])
        out = StringIO()
        call_command("export_contract", stdout=out)
        self.assertEqual(json.loads(out.getvalue())["schema_version"], 1)


# --- Submission ------------------------------------------------------------------------------


class SubmissionTests(CaseTestCase):
    def test_requester_submission_creates_owned_numbered_case_with_history(self):
        result = self.submit()
        case = Case.objects.get(pk=result.case_id)
        year = (timezone.localtime().year + 543) % 100
        self.assertEqual(case.number, f"{case.owner_division.code}{year:02d}-000001")  # ADR 0045
        self.assertEqual((case.status, case.revision, case.kind), (S.SUBMITTED, 1, K.WITHDRAWAL))
        self.assertEqual(case.owner_division, self.triage)
        self.assertEqual(case.district, self.req1.district)
        self.assertIsNone(case.assignee)
        self.assertIsNone(case.submitted_by)
        self.assertEqual(case.channel, Case.Channel.LINE)
        self.assertEqual(case.lines.get().requested_quantity, Decimal("3.00"))
        decision = Decision.objects.get(case=case)
        self.assertEqual((decision.action, decision.to_status), ("submit", "submitted"))
        self.assertEqual(str(decision.action_id), result.action_id)
        audit = AuditEvent.objects.get(target_type="Case", target_id=str(case.pk))
        self.assertEqual(audit.actor_label, f"requester:{self.req1.pk}")
        event = OutboxEvent.objects.get(case=case)
        self.assertEqual(
            (event.event_type, event.case_revision, event.schema_version, event.status),
            ("case.submitted", 1, 1, "pending"),
        )

    def test_numbers_are_per_owner_division_and_buddhist_year_in_bangkok_time(self):
        late_2569 = datetime(2026, 12, 31, 16, 59, tzinfo=UTC)  # 23:59 in Bangkok
        new_year_2570 = datetime(2026, 12, 31, 17, 30, tzinfo=UTC)  # 00:30 on 1 Jan
        with patch("operations.services.timezone.now", return_value=late_2569):
            first = self.submit()
            second = self.submit()
            problem_case = self.submit(payload=problem())
        with patch("operations.services.timezone.now", return_value=new_year_2570):
            next_year = self.submit()
        # ADR 0045: the owner division's code; its withdrawals and problems share one sequence.
        code = Case.objects.get(pk=first.case_id).owner_division.code
        problem_code = Case.objects.get(pk=problem_case.case_id).owner_division.code
        self.assertEqual(
            [first.case_number, second.case_number],
            [f"{code}69-000001", f"{code}69-000002"],
        )
        expected = 3 if problem_code == code else 1
        self.assertEqual(problem_case.case_number, f"{problem_code}69-{expected:06d}")
        self.assertEqual(next_year.case_number, f"{code}70-000001")

    def test_quantity_and_unit_rules(self):
        invalid = {
            "float": [{"item_code": "FUSE_SYN_A", "quantity": 3.0}],
            "fraction of counted unit": [{"item_code": "FUSE_SYN_A", "quantity": "1.5"}],
            "three decimals": [{"item_code": "CBL_SYN_A", "quantity": "12.345"}],
            "zero": [{"item_code": "FUSE_SYN_A", "quantity": "0"}],
            "negative": [{"item_code": "FUSE_SYN_A", "quantity": "-1"}],
            "text": [{"item_code": "FUSE_SYN_A", "quantity": "สิบ"}],
            "missing quantity": [{"item_code": "FUSE_SYN_A"}],
            "unknown item": [{"item_code": "NOPE", "quantity": "1"}],
            "duplicate item": [
                {"item_code": "FUSE_SYN_A", "quantity": "1"},
                {"item_code": "FUSE_SYN_A", "quantity": "2"},
            ],
            "serials on request": [{"item_code": "MTR_SYN_A", "quantity": "1", "serials": ["S"]}],
            "unknown line field": [{"item_code": "FUSE_SYN_A", "quantity": "1", "x": 1}],
        }
        for label, lines in invalid.items():
            with self.subTest(label):
                self.assertFails(
                    ErrorCode.VALIDATION_ERROR, self.submit, payload=withdrawal(lines=lines)
                )
        ok = self.submit(
            payload=withdrawal(lines=[{"item_code": "CBL_SYN_A", "quantity": "12.35"}])
        )
        line = Case.objects.get(pk=ok.case_id).lines.get()
        self.assertEqual(line.requested_quantity, Decimal("12.35"))
        whole = self.submit(payload=withdrawal(lines=[{"item_code": "FUSE_SYN_A", "quantity": 50}]))
        self.assertEqual(
            Case.objects.get(pk=whole.case_id).lines.get().requested_quantity, Decimal("50")
        )
        self.assertEqual(Case.objects.count(), 2)

    def test_minimum_fields_per_kind(self):
        invalid = {
            "withdrawal without purpose": withdrawal(purpose=" "),
            "withdrawal without lines": withdrawal(lines=[]),
            "bad needed_by": withdrawal(needed_by="31/12/2569"),
            "return without reason": return_payload(reason=""),
            "return without condition": return_payload(condition=""),
            "return serial count": return_payload(
                lines=[{"item_code": "MTR_SYN_A", "quantity": "2", "serials": ["S1"]}]
            ),
            "serial on unserialised item": return_payload(
                lines=[{"item_code": "FUSE_SYN_A", "quantity": "1", "serials": ["S1"]}]
            ),
            "problem without description": problem(description=""),
            "unknown kind": {"kind": "loan", "purpose": "x"},
            "field of another kind": withdrawal(description="x"),
            "requester sends staff fields": withdrawal(requester_id=1),
        }
        for label, payload in invalid.items():
            with self.subTest(label):
                self.assertFails(ErrorCode.VALIDATION_ERROR, self.submit, payload=payload)
        self.assertEqual(Case.objects.count(), 0)
        self.submit(payload=withdrawal(needed_by="2026-12-01"))
        self.submit(payload=return_payload(source_reference="ISSUE-1"))
        self.submit(payload=problem(lines=[{"item_code": "TRF_SYN_A", "serials": ["T4"]}]))
        self.assertEqual(Case.objects.count(), 3)

    def test_problem_may_reference_only_the_same_requesters_case(self):
        own = self.submit()
        other = self.submit(actor=self.as_requester(self.req2))
        linked = self.submit(payload=problem(related_case_number=own.case_number))
        self.assertEqual(str(Case.objects.get(pk=linked.case_id).related_case_id), own.case_id)
        self.assertFails(
            ErrorCode.VALIDATION_ERROR,
            self.submit,
            payload=problem(related_case_number=other.case_number),
        )

    def test_inactive_requester_and_missing_intake_are_refused(self):
        self.req1.status = Requester.Status.SUSPENDED
        self.req1.save()
        self.assertFails(ErrorCode.FORBIDDEN, self.submit)
        Division.objects.update(is_intake=False)
        self.assertFails(
            ErrorCode.DEPENDENCY_UNAVAILABLE, self.submit, actor=self.as_requester(self.req2)
        )
        self.assertFails(ErrorCode.AUTH_REQUIRED, self.submit, actor=self.req2)


class OnBehalfSubmissionTests(CaseTestCase):
    def on_behalf(self, **overrides):
        payload = withdrawal(
            requester_id=self.req1.pk, channel="phone", intake_note="โทรแจ้ง 09:30 น."
        )
        payload.update(overrides)
        return payload

    def test_intake_staff_files_a_case_owned_by_the_requester(self):
        result = self.submit(actor=self.as_staff(self.intake), payload=self.on_behalf())
        case = Case.objects.get(pk=result.case_id)
        self.assertEqual((case.requester, case.submitted_by), (self.req1, self.intake))
        self.assertEqual((case.channel, case.intake_note), ("phone", "โทรแจ้ง 09:30 น."))
        # The requester owns it and sees it; the filing staff sees it through the intake unit.
        self.assertEqual(services.get_case(self.as_requester(), case.pk), case)
        self.assertEqual(services.get_case(self.as_staff(self.intake), case.pk), case)

    def test_only_intake_staff_and_only_with_channel_and_note(self):
        for user in (self.meter, self.admin):
            with self.subTest(user=user.username):
                self.assertFails(
                    ErrorCode.FORBIDDEN,
                    self.submit,
                    actor=self.as_staff(user),
                    payload=self.on_behalf(),
                )
        for label, payload in {
            "line channel": self.on_behalf(channel="line"),
            "no note": self.on_behalf(intake_note=""),
            "no requester": {k: v for k, v in self.on_behalf().items() if k != "requester_id"},
            "unknown requester": self.on_behalf(requester_id=999999),
        }.items():
            with self.subTest(label):
                self.assertFails(
                    ErrorCode.VALIDATION_ERROR,
                    self.submit,
                    actor=self.as_staff(self.intake),
                    payload=payload,
                )
        self.req1.status = Requester.Status.SUSPENDED
        self.req1.save()
        self.assertFails(
            ErrorCode.VALIDATION_ERROR,
            self.submit,
            actor=self.as_staff(self.intake),
            payload=self.on_behalf(),
        )

    def test_requester_without_line_is_created_by_admin_and_served_by_intake(self):
        with self.assertRaises(PermissionDenied):
            identity.create_requester(self.meter, "คุณสมชาย", "D05", "ไม่ใช้ LINE")
        requester = identity.create_requester(self.admin, " คุณสมชาย  ใจดี ", "D05", "")
        self.assertEqual(requester.display_name, "คุณสมชาย ใจดี")
        self.assertFalse(requester.channelidentity_set.exists())
        self.assertTrue(
            AuditEvent.objects.filter(
                action="requester.created_direct", target_id=str(requester.pk)
            ).exists()
        )
        result = self.submit(
            actor=self.as_staff(self.intake), payload=self.on_behalf(requester_id=requester.pk)
        )
        self.assertEqual(Case.objects.get(pk=result.case_id).district.code, "D05")


# --- Idempotency, revisions and atomicity ----------------------------------------------------


class IdempotencyTests(CaseTestCase):
    def test_same_key_and_payload_replays_the_original_result(self):
        k = key()
        first = self.submit(idempotency_key=k)
        again = self.submit(idempotency_key=k)
        self.assertEqual(first, again)
        self.assertEqual(Case.objects.count(), 1)
        self.assertEqual(ActionReceipt.objects.count(), 1)
        self.assertEqual(OutboxEvent.objects.count(), 1)

    def test_same_key_with_a_different_payload_conflicts(self):
        k = key()
        self.submit(idempotency_key=k)
        self.assertFails(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            self.submit,
            idempotency_key=k,
            payload=withdrawal(purpose="อื่น"),
        )
        self.assertEqual(Case.objects.count(), 1)

    def test_keys_are_scoped_per_actor(self):
        k = key()
        self.submit(idempotency_key=k)
        self.submit(actor=self.as_requester(self.req2), idempotency_key=k)
        self.assertEqual(Case.objects.count(), 2)

    def test_action_retry_replays_even_after_the_revision_moved_on(self):
        case = self.submit()
        k = key()
        first = self.act(self.as_staff(self.intake), case, "start_triage", 1, idempotency_key=k)
        self.act(self.as_staff(self.intake), case, "hold", payload={"reason": "รอ"})
        again = self.act(self.as_staff(self.intake), case, "start_triage", 1, idempotency_key=k)
        self.assertEqual(first, again)
        self.assertFails(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            self.act,
            self.as_staff(self.intake),
            case,
            "start_triage",
            1,
            {"reason": "ต่าง"},
            k,
        )

    def test_replay_rechecks_current_permission(self):
        k = key()
        self.submit(idempotency_key=k)
        self.req1.status = Requester.Status.SUSPENDED
        self.req1.save()
        self.assertFails(ErrorCode.FORBIDDEN, self.submit, idempotency_key=k)
        case = self.submit(actor=self.as_requester(self.req2))
        k2 = key()
        self.act(self.as_staff(self.intake), case, "start_triage", 1, idempotency_key=k2)
        identity.change_division(self.admin, self.intake, self.triage, self.mtr, "ย้ายกอง")
        self.assertFails(
            ErrorCode.NOT_FOUND,
            self.act,
            self.as_staff(self.intake),
            case,
            "start_triage",
            1,
            idempotency_key=k2,
        )

    def test_submission_replay_rechecks_that_staff_can_still_see_the_case(self):
        k = key()
        payload = withdrawal(requester_id=self.req1.pk, channel="letter", intake_note="หนังสือ")
        self.submit(actor=self.as_staff(self.intake), payload=payload, idempotency_key=k)
        identity.change_division(self.admin, self.intake, self.triage, self.mtr, "ย้ายกอง")
        self.assertFails(
            ErrorCode.NOT_FOUND,
            self.submit,
            actor=self.as_staff(self.intake),
            payload=payload,
            idempotency_key=k,
        )

    def test_stale_revision_never_overwrites(self):
        case = self.submit()
        self.act(self.as_staff(self.intake), case, "start_triage", 1)
        self.assertFails(
            ErrorCode.REVISION_CONFLICT,
            self.act,
            self.as_staff(self.intake2),
            case,
            "claim",
            1,
        )
        stored = Case.objects.get(pk=case.case_id)
        self.assertEqual((stored.assignee, stored.revision), (self.intake, 2))
        for bad in (0, "2", True, None):
            with self.subTest(revision=bad):
                self.assertFails(
                    ErrorCode.VALIDATION_ERROR,
                    self.act,
                    self.as_staff(self.intake2),
                    case,
                    "claim",
                    bad,
                )

    def test_short_or_unsafe_idempotency_keys_are_refused(self):
        for bad in ("", "short", "has space in it", None, 12345678):
            with self.subTest(key=bad):
                self.assertFails(ErrorCode.VALIDATION_ERROR, self.submit, idempotency_key=bad)

    def assertNothingPersisted(self):
        self.assertEqual(Case.objects.count(), 0)
        self.assertFalse(CaseNumberCounter.objects.filter(last_value__gt=0).exists())
        self.assertEqual(Decision.objects.count(), 0)
        self.assertEqual(ActionReceipt.objects.count(), 0)
        self.assertEqual(OutboxEvent.objects.count(), 0)
        self.assertFalse(AuditEvent.objects.filter(target_type="Case").exists())

    def test_submission_rolls_back_entirely_when_outbox_fails(self):
        with patch.object(OutboxEvent.objects, "create", side_effect=OperationalError("down")):
            self.assertFails(ErrorCode.DEPENDENCY_UNAVAILABLE, self.submit)
        self.assertNothingPersisted()
        # The rolled-back number is reused: numbering stays gap-free.
        self.assertTrue(self.submit().case_number.endswith("-000001"))

    def test_submission_rolls_back_entirely_when_audit_fails(self):
        with (
            patch("operations.services.identity.audit", side_effect=IntegrityError("audit")),
            self.assertRaises(IntegrityError),
        ):
            self.submit()
        self.assertNothingPersisted()

    def test_transition_rolls_back_when_outbox_fails(self):
        case = self.submit()
        with patch.object(OutboxEvent.objects, "create", side_effect=OperationalError("down")):
            self.assertFails(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                self.act,
                self.as_staff(self.intake),
                case,
                "start_triage",
                1,
            )
        stored = Case.objects.get(pk=case.case_id)
        self.assertEqual((stored.status, stored.revision, stored.assignee), (S.SUBMITTED, 1, None))
        self.assertEqual(Decision.objects.filter(case=stored).count(), 1)
        self.assertEqual(ActionReceipt.objects.count(), 1)


# --- Scope and action permissions ------------------------------------------------------------


class ScopeAndPermissionTests(CaseTestCase):
    def test_cases_are_invisible_outside_requester_and_division_scope(self):
        own = self.submit()
        other = self.submit(actor=self.as_requester(self.req2))
        self.assertEqual(
            [str(c.pk) for c in services.visible_cases(self.as_requester())], [own.case_id]
        )
        self.assertEqual(services.visible_cases(self.as_staff(self.intake)).count(), 2)
        self.assertEqual(services.visible_cases(self.as_staff(self.meter)).count(), 0)
        self.assertEqual(services.visible_cases(self.as_staff(self.admin)).count(), 2)
        for actor, case_id in [
            (self.as_requester(), other.case_id),
            (self.as_staff(self.meter), own.case_id),
            (self.as_requester(), uuid.uuid4()),
            (self.as_requester(), "not-a-uuid"),
        ]:
            with self.subTest(actor=actor.key, case=str(case_id)):
                error = self.assertFails(ErrorCode.NOT_FOUND, services.get_case, actor, case_id)
                self.assertNotIn(other.case_number, str(error))
                self.assertFails(
                    ErrorCode.NOT_FOUND,
                    services.perform_action,
                    actor,
                    case_id,
                    "approve",
                    expected_revision=1,
                    idempotency_key=key(),
                )

    def test_staff_without_role_and_unknown_actions_are_refused(self):
        case = self.submit()
        norole = make_staff("norole")
        self.assertFails(
            ErrorCode.FORBIDDEN, self.act, self.as_staff(norole), case, "start_triage", 1
        )
        self.assertFails(
            ErrorCode.VALIDATION_ERROR, self.act, self.as_staff(self.intake), case, "teleport", 1
        )

    def test_requester_cancels_own_case_only_before_work_starts(self):
        case = self.submit()
        self.assertFails(
            ErrorCode.FORBIDDEN, self.act, self.as_requester(), case, "start_triage", 1
        )
        result = self.act(self.as_requester(), case, "cancel", 1)
        stored = Case.objects.get(pk=case.case_id)
        self.assertEqual((result.status, stored.ended_at is not None), (S.CANCELLED, True))

        triaged = self.submit()
        self.act(self.as_staff(self.intake), triaged, "start_triage", 1)
        self.assertEqual(self.act(self.as_requester(), triaged, "cancel", 2).status, S.CANCELLED)

        started = self.make_case(K.WITHDRAWAL, S.AWAITING_EXECUTION)
        self.assertFails(ErrorCode.FORBIDDEN, self.act, self.as_requester(), started, "cancel")
        cancelled = self.act(self.as_staff(self.meter), started, "cancel", payload={"reason": "ซ้ำ"})
        self.assertEqual(cancelled.status, S.CANCELLED)

    def test_taking_a_case_needs_membership_while_admins_can_manage(self):
        case = self.submit()
        self.assertFails(
            ErrorCode.FORBIDDEN, self.act, self.as_staff(self.admin), case, "start_triage", 1
        )
        result = self.act(self.as_staff(self.intake), case, "start_triage", 1)
        self.assertEqual(Case.objects.get(pk=case.case_id).assignee, self.intake)
        self.assertFails(
            ErrorCode.VALIDATION_ERROR, self.act, self.as_staff(self.intake), case, "claim"
        )
        self.act(self.as_staff(self.intake2), case, "claim", result.revision)
        self.assertEqual(Case.objects.get(pk=case.case_id).assignee, self.intake2)
        held = self.act(self.as_staff(self.admin), case, "hold", payload={"reason": "ตรวจสอบ"})
        self.assertEqual(held.status, S.ON_HOLD)
        resumed = self.act(self.as_staff(self.intake), case, "resume")
        self.assertEqual(resumed.status, S.TRIAGE)

    def test_only_supervisors_or_admins_assign_and_only_to_owner_staff(self):
        case = self.make_case(K.WITHDRAWAL, S.IN_PROGRESS)
        assign = {"assignee_id": self.meter2.pk}
        self.assertFails(
            ErrorCode.FORBIDDEN, self.act, self.as_staff(self.meter), case, "assign", payload=assign
        )
        for bad in (self.transformer.pk, self.admin.pk, 999999, "x"):
            with self.subTest(assignee=bad):
                self.assertFails(
                    ErrorCode.VALIDATION_ERROR,
                    self.act,
                    self.as_staff(self.supervisor),
                    case,
                    "assign",
                    payload={"assignee_id": bad},
                )
        self.act(self.as_staff(self.supervisor), case, "assign", payload=assign)
        self.act(self.as_staff(self.admin), case, "assign", payload={"assignee_id": self.meter.pk})
        decisions = Decision.objects.filter(case=case, action="assign").order_by("id")
        self.assertEqual(
            [d.data for d in decisions],
            [
                {"assignee_id": self.meter2.pk, "previous_assignee_id": None},
                {"assignee_id": self.meter.pk, "previous_assignee_id": self.meter2.pk},
            ],
        )

    def test_awaiting_info_remembers_where_to_resume(self):
        case = self.make_case(K.RETURN, S.AWAITING_EXECUTION)
        self.assertFails(
            ErrorCode.VALIDATION_ERROR, self.act, self.as_staff(self.meter), case, "request_info"
        )
        paused = self.act(
            self.as_staff(self.meter), case, "request_info", payload={"reason": "ขอเลขเครื่อง"}
        )
        stored = Case.objects.get(pk=case.pk)
        self.assertEqual(
            (paused.status, stored.resume_status), (S.AWAITING_INFO, S.AWAITING_EXECUTION)
        )
        back = self.act(self.as_staff(self.meter), case, "info_received")
        stored.refresh_from_db()
        self.assertEqual((back.status, stored.resume_status), (S.AWAITING_EXECUTION, ""))
        event = OutboxEvent.objects.filter(case=case).latest("id")
        self.assertEqual(
            (event.event_type, event.case_revision, event.payload["from_status"]),
            ("decision.recorded", 3, "awaiting_info"),
        )

    def test_triage_can_decline_with_a_reason(self):
        case = self.submit()
        self.act(self.as_staff(self.intake), case, "start_triage", 1)
        result = self.act(
            self.as_staff(self.intake), case, "decline", payload={"reason": "ซ้ำกับเรื่องเดิม"}
        )
        self.assertEqual(result.status, S.REJECTED)
        self.assertEqual(
            Decision.objects.get(case_id=case.case_id, action="decline").reason, "ซ้ำกับเรื่องเดิม"
        )


# --- Model invariants, intake configuration and demo data ------------------------------------


class InvariantAndAdminTests(CaseTestCase):
    def test_history_rows_are_append_only(self):
        result = self.submit()
        decision = Decision.objects.get(case_id=result.case_id)
        decision.reason = "แก้"
        with self.assertRaises(ValueError):
            decision.save()
        with self.assertRaises(ValueError):
            decision.delete()

    def test_database_rejects_broken_case_rows(self):
        broken = [
            {"status": S.AWAITING_INFO, "resume_status": ""},
            {"status": S.TRIAGE, "resume_status": S.SUBMITTED},
            {"channel": Case.Channel.PHONE},
            {"owner_division": None},
        ]
        for overrides in broken:
            with self.subTest(overrides), transaction.atomic(), self.assertRaises(IntegrityError):
                fields = {
                    "number": f"BAD-{next(_numbers)}",
                    "kind": K.PROBLEM,
                    "status": S.SUBMITTED,
                    "requester": self.req1,
                    "district": self.req1.district,
                    "owner_division": self.mtr,
                    "channel": Case.Channel.LINE,
                    "submitted_at": timezone.now(),
                    "updated_at": timezone.now(),
                }
                fields.update(overrides)
                Case.objects.create(**fields)

    def test_intake_division_is_single_and_admin_managed(self):
        self.assertEqual(Division.objects.get(is_intake=True), self.triage)
        with self.assertRaises(PermissionDenied):
            identity.set_intake_division(self.meter, self.mtr, "ทดลอง")
        with self.assertRaises(ValidationError):
            identity.set_division_active(self.admin, self.triage, False, "ปิด")
        identity.set_intake_division(self.admin, self.mtr, "ย้ายจุดรับเรื่อง")
        self.assertEqual(list(Division.objects.filter(is_intake=True)), [self.mtr])
        self.assertEqual(Case.objects.get(pk=self.submit().case_id).owner_division, self.mtr)
        identity.set_division_active(self.admin, self.triage, False, "ปิด")
        with self.assertRaises(ValidationError):
            identity.set_intake_division(self.admin, self.triage, "กลับ")

    def test_admin_console_sets_intake_and_creates_requesters(self):
        client = Client()
        client.force_login(self.admin)
        response = client.post(
            f"/console/admin/divisions/{self.mtr.pk}/set-intake/", {"reason": "ย้าย"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Division.objects.get(pk=self.mtr.pk).is_intake)
        client.post(
            "/console/admin/requesters/new/",
            {"display_name": "คุณสมหญิง", "district": "D07", "reason": "ไม่ใช้ LINE"},
        )
        self.assertTrue(Requester.objects.filter(display_name="คุณสมหญิง").exists())
        self.assertContains(client.get("/console/admin/registrations/"), "คุณสมหญิง")
        staff = Client()
        staff.force_login(self.meter)
        self.assertEqual(staff.post("/console/admin/requesters/new/", {}).status_code, 403)

    def test_demo_cases_seed_idempotently(self):
        EquipmentType.objects.all().delete()
        for _ in range(2):
            call_command("seed_demo_cases", stdout=StringIO())
        self.assertEqual(Case.objects.count(), 4)
        self.assertEqual(EquipmentType.objects.filter(is_synthetic=True).count(), 5)
        self.assertTrue(EquipmentType.objects.get(code="CBL_SYN_A").is_divisible)
        returned = Case.objects.get(kind=K.RETURN)
        self.assertEqual(returned.lines.get().serials, ["MTR_SYN_005", "MTR_SYN_007"])
        problem_case = Case.objects.get(kind=K.PROBLEM)
        self.assertEqual(problem_case.lines.get().serials, ["TRF_SYN_004"])
        with override_settings(APP_ENV="production"), self.assertRaises(CommandError):
            call_command("seed_demo_cases", stdout=StringIO())


# --- Concurrency on PostgreSQL ---------------------------------------------------------------


class ConcurrencyTests(TransactionTestCase):
    def setUp(self):
        setup_world(self)

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

    def submit_call(self, idempotency_key):
        return lambda: services.submit_case(
            Actor.for_requester(self.req1), idempotency_key=idempotency_key, payload=withdrawal()
        )

    def test_two_staff_taking_the_same_case_leave_one_owner(self):
        case = self.submit_call(key())()
        calls = [
            lambda user=user: services.perform_action(
                Actor.staff(user),
                case.case_id,
                "start_triage",
                expected_revision=1,
                idempotency_key=key(),
            )
            for user in (self.intake, self.intake2)
        ]
        results, errors = self.run_parallel(calls)
        self.assertEqual((len(results), errors), (1, [ErrorCode.REVISION_CONFLICT]))
        stored = Case.objects.get(pk=case.case_id)
        self.assertEqual((stored.revision, Decision.objects.filter(case=stored).count()), (2, 2))

    def test_parallel_retries_of_one_key_create_one_case(self):
        k = key()
        results, errors = self.run_parallel([self.submit_call(k) for _ in range(4)])
        self.assertEqual((len(results), errors), (4, []))
        self.assertEqual(len({r.case_id for r in results}), 1)
        self.assertEqual(Case.objects.count(), 1)

    def test_parallel_submissions_get_gap_free_numbers(self):
        results, errors = self.run_parallel([self.submit_call(key()) for _ in range(6)])
        self.assertEqual(errors, [])
        self.assertEqual(sorted(int(r.case_number[-6:]) for r in results), list(range(1, 7)))
