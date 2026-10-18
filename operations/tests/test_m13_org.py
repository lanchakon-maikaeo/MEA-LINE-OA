"""Real ฝอจ. structure (ADR 0008; real structure Q1–Q15, 9 Oct 2569).

Acceptance → tests: the structure loads idempotently with its แผนก (LoadOrgTests); with no
central intake a new case goes straight to the division the rules name, a mixed request splits
into one case per division, an unclear one goes to the coordinator, and การใช้ไฟฟ้า goes to กตฟ.
without the AI (DirectRoutingTests); the owner division triages, files on behalf and records the
district's answer, but not the person who resolved it (OwnerDeskTests); แผนก are proposed, set,
filtered and counted (SectionTests); each division checks its own SAP facts (RegistryTests); a
provisional code is renamed together with the policy (RenameTests); and LINE offers the topic
(LineTopicTests).
"""

import json
import uuid
from io import StringIO
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from operations import (
    ai_chat,
    conversation,
    identity,
    options,
    policy,
    problem_chat,
    problems,
    registry,
    reports,
    services,
    web_actions,
)
from operations.contracts import Actor, CommandFailed, ErrorCode
from operations.management.commands import load_org, seed_org_demo
from operations.models import (
    AckClock,
    AuditEvent,
    AuthorityGrant,
    Case,
    CaseTriage,
    District,
    Division,
    EquipmentType,
    RegistryDiscrepancy,
    Requester,
    Role,
    Section,
)
from operations.tests.test_m02_cases import ITEMS, make_staff
from operations.tests.test_m03_line import ALICE, LineTestCase, texts

S = Case.Status
A = AuthorityGrant.Activity


def real_world(target):
    """Districts, the real units, synthetic items and the FAJO-2 policy; no intake desk."""
    call_command("load_districts", stdout=StringIO())
    call_command("load_org", stdout=StringIO())
    for code, category, unit, serialized, divisible in ITEMS:
        EquipmentType.objects.get_or_create(
            code=code,
            defaults={
                "name": code,
                "category": category,
                "unit": unit,
                "is_serialized": serialized,
                "is_divisible": divisible,
            },
        )
    Division.objects.filter(is_intake=True).update(is_intake=False)
    version = policy.import_policy(policy.load_document(seed_org_demo.REAL_POLICY))
    if policy.active_policy() != version:
        policy.activate_policy(None, version, "ผู้ทดสอบ", "TEST-ORG", "ทดสอบโครงสร้างจริง")


class OrgTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        real_world(cls)
        cls.admin = make_staff("admin", (Role.ADMIN, None))
        cls.kcw = make_staff("kcw", (Role.STAFF, "กคว."))
        cls.kcw2 = make_staff("kcw2", (Role.STAFF, "กคว."))
        cls.kae = make_staff("kae", (Role.STAFF, "กองอุปกรณ์ฯ"))
        cls.kae2 = make_staff("kae2", (Role.STAFF, "กองอุปกรณ์ฯ"))
        cls.kae_resolver = make_staff("kaeresolver", (Role.RESOLVER, "กองอุปกรณ์ฯ"))
        cls.ktf = make_staff("ktf", (Role.STAFF, "กตฟ."))
        cls.bt = make_staff("bt", (Role.STAFF, "บท."))
        cls.exec = make_staff("exec", (Role.SUPERVISOR, "ฝอจ."))
        cls.req1 = Requester.objects.create(
            display_name="ผู้แจ้ง 1", district=District.objects.get(code="D01"), approved_by=cls.admin
        )

    def submit(self, payload, actor=None, idempotency_key=None):
        result = services.submit_case(
            actor or Actor.for_requester(self.req1),
            idempotency_key=idempotency_key or f"test-{uuid.uuid4().hex}",
            payload=payload,
        )
        return result, Case.objects.get(pk=result.case_id)

    def act(self, user, case, action, payload=None):
        return services.perform_action(
            Actor.staff(user),
            case.pk,
            action,
            expected_revision=Case.objects.get(pk=case.pk).revision,
            idempotency_key=f"test-{uuid.uuid4().hex}",
            payload=payload,
        )


def withdrawal(*lines):
    return {
        "kind": "withdrawal",
        "purpose": "งานทดสอบ",
        "lines": [{"item_code": code, "quantity": quantity} for code, quantity in lines],
    }


# --- The structure ------------------------------------------------------------------------------


class LoadOrgTests(TestCase):
    def test_loads_divisions_coordinator_and_sections_idempotently(self):
        document = json.loads(load_org.DEFAULT.read_text(encoding="utf-8"))
        first = load_org.load(document)
        self.assertEqual(first[0], 7)  # six กอง-level units and the coordinator
        self.assertEqual(load_org.load(document), (0, 0, 0))
        self.assertEqual(Division.objects.get(code="ฝอจ.").kind, Division.Kind.COORDINATION)
        self.assertEqual(
            list(Section.objects.filter(division__code="กคว.").values_list("name", flat=True)),
            ["แผนกซ่อมเครื่องวัด", "แผนกคลังเครื่องวัด", "แผนกทดสอบเครื่องวัด"],
        )
        self.assertEqual(Section.objects.get(name="งานวิเคราะห์ข้อมูลโครงข่าย").code, "วค.")
        self.assertFalse(Section.objects.filter(division__code="บท.").exists())
        self.assertFalse(Division.objects.filter(is_intake=True).exists())
        # A แผนก the file drops is closed, never deleted.
        document["divisions"][2]["sections"].pop()
        self.assertEqual(load_org.load(document)[2], 1)
        self.assertFalse(Section.objects.get(name="แผนกทดสอบเครื่องวัด").is_active)


# --- Straight to the division -------------------------------------------------------------------


class DirectRoutingTests(OrgTestCase):
    def test_a_meter_request_goes_straight_to_its_division(self):
        _, case = self.submit(withdrawal(("MTR_SYN_A", "2")))
        self.assertEqual(case.owner_division.code, "กคว.")
        clock = AckClock.objects.get(case=case, kind=AckClock.Kind.INTAKE)
        self.assertEqual(clock.division.code, "กคว.")

    def test_other_equipment_goes_to_the_distribution_equipment_division(self):
        _, case = self.submit(withdrawal(("FUSE_SYN_A", "5")))
        self.assertEqual(case.owner_division.code, "กองอุปกรณ์ฯ")

    def test_a_mixed_request_splits_into_one_case_per_division(self):
        key = f"test-{uuid.uuid4().hex}"
        payload = withdrawal(("MTR_SYN_A", "1"), ("TRF_SYN_A", "1"), ("FUSE_SYN_A", "2"))
        result, first = self.submit(payload, idempotency_key=key)
        self.assertEqual(len(result.split), 1)
        second = Case.objects.get(pk=result.split[0]["case_id"])
        self.assertEqual(first.owner_division.code, "กคว.")
        self.assertEqual(second.owner_division.code, "กองอุปกรณ์ฯ")
        self.assertEqual(second.related_case, first)
        self.assertEqual(
            sorted(second.lines.values_list("line_no", "item__code")),
            [(1, "TRF_SYN_A"), (2, "FUSE_SYN_A")],
        )
        self.assertEqual(first.decisions.get().data["split"], [first.number, second.number])
        # The same key replays both numbers and creates nothing.
        again, _ = self.submit(payload, idempotency_key=key)
        self.assertEqual(again, result)
        self.assertEqual(Case.objects.count(), 2)

    def test_an_unclear_problem_goes_to_the_coordinator(self):
        _, case = self.submit({"kind": "problem", "description": "ใช้งานไม่ได้ ไม่ทราบสาเหตุ"})
        self.assertEqual(case.owner_division.code, "ฝอจ.")
        rec = CaseTriage.objects.get(case=case).current_recommendation
        self.assertEqual((rec.rules, rec.proposed_owner.code), (["R10"], "ฝอจ."))
        self.assertIn("ผู้ประสานระดับฝ่าย", rec.next_action)

    def test_a_usage_matter_goes_to_ktf_and_never_to_the_ai(self):
        payload = {"kind": "problem", "topic": "usage", "description": "สงสัยลักใช้ไฟ บ้านเลขที่ 9"}
        with override_settings(AI_ENABLED=True):
            _, case = self.submit(payload)
        self.assertEqual((case.owner_division.code, case.topic), ("กตฟ.", "usage"))
        self.assertTrue(policy.private_topic(case))
        self.assertFalse(case.ai_triage_runs.exists())
        # Words alone are enough to keep a text from the AI.
        self.assertTrue(policy.usage_text("ลูกค้าร้องว่าค่าไฟแพงมาก"))
        self.assertFalse(policy.usage_text("หม้อแปลงมีเสียงดัง"))
        with self.assertRaises(CommandFailed) as caught:
            self.submit({**payload, "lines": [{"item_code": "MTR_SYN_A"}]})
        self.assertEqual(caught.exception.code, ErrorCode.VALIDATION_ERROR)

    def test_a_problem_naming_two_divisions_stays_whole_with_child_work_proposed(self):
        _, case = self.submit({"kind": "problem", "description": "มิเตอร์และหม้อแปลงเสียหายจากน้ำท่วม"})
        rec = CaseTriage.objects.get(case=case).current_recommendation
        self.assertEqual(rec.rules, ["R09"])
        self.assertEqual(case.owner_division.code, rec.proposed_owner.code)
        self.assertEqual(len(rec.child_work_owners), 1)


# --- The owner division does what the intake desk did -------------------------------------------


class OwnerDeskTests(OrgTestCase):
    def test_owner_starts_and_completes_triage_itself(self):
        _, case = self.submit({"kind": "problem", "topic": "transformer", "description": "เสียงดัง"})
        self.act(self.kae, case, "start_triage")
        self.act(self.kae, case, "set_priority", {"priority": "P2", "reason": "ทดสอบ"})
        self.act(self.kae, case, "complete_triage")
        self.assertEqual(Case.objects.get(pk=case.pk).status, S.IN_PROGRESS)

    def test_any_division_staff_file_on_behalf_and_the_rules_route(self):
        payload = {
            **withdrawal(("TRF_SYN_A", "1")),
            "requester_id": self.req1.pk,
            "channel": "phone",
            "intake_note": "โทรมา 09:00",
        }
        _, case = self.submit(payload, actor=Actor.staff(self.bt))
        self.assertEqual((case.owner_division.code, case.submitted_by), ("กองอุปกรณ์ฯ", self.bt))
        with self.assertRaises(CommandFailed) as caught:
            self.submit(payload, actor=Actor.staff(self.admin))
        self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)

    def test_filing_for_another_division_lands_on_a_page_the_filer_may_open(self):
        """K15 (found by the browser test on the real structure): the message names every case
        and its division, and the next page is never a case the filer cannot open."""
        form = {
            "idempotency_key": uuid.uuid4().hex,
            "requester_id": str(self.req1.pk),
            "kind": "withdrawal",
            "channel": "phone",
            "intake_note": "เขตโทรหา กตฟ. เรื่องมิเตอร์",
            "purpose": "ขอมิเตอร์",
            "row-0-item": "MTR_SYN_A",
            "row-0-quantity": "1",
        }
        client = Client()
        client.force_login(self.ktf)
        response = client.post("/cases/new/", form, follow=True)
        case = Case.objects.get(intake_note=form["intake_note"])
        self.assertEqual(case.owner_division.code, "กคว.")
        self.assertRedirects(response, "/cases/new/")
        self.assertContains(response, f"บันทึกเรื่อง {case.number} แทนผู้แจ้งแล้ว เข้าคิวคัดกรองของ กคว.")
        self.assertContains(response, "บัญชีนี้จึงเปิดดูไม่ได้")
        split = {
            **form,
            "idempotency_key": uuid.uuid4().hex,
            "intake_note": "คำขอเดียวสองกอง",
            "row-1-item": "FUSE_SYN_A",
            "row-1-quantity": "3",
        }
        client.force_login(self.kae)
        response = client.post("/cases/new/", split, follow=True)
        meter, fuses = Case.objects.filter(intake_note="คำขอเดียวสองกอง").order_by("number")
        self.assertEqual(
            (meter.owner_division.code, fuses.owner_division.code), ("กคว.", "กองอุปกรณ์ฯ")
        )
        self.assertRedirects(response, f"/cases/{fuses.number}/")
        self.assertContains(response, f"{meter.number} (กคว.), {fuses.number} (กองอุปกรณ์ฯ)")

    def resolved(self):
        _, case = self.submit({"kind": "problem", "topic": "transformer", "description": "ดับ"})
        self.act(self.kae, case, "start_triage")
        self.act(self.kae, case, "set_priority", {"priority": "P2", "reason": "ทดสอบ"})
        self.act(self.kae, case, "complete_triage")
        self.act(self.kae_resolver, case, "resolve", {"category": "fixed", "message": "แก้แล้ว"})
        return Case.objects.get(pk=case.pk)

    def test_another_member_records_the_districts_answer_not_the_resolver(self):
        options.change(self.admin, options.SEPARATION, "on", "")
        case = self.resolved()
        answer = {"channel": "phone", "intake_note": "เขตโทรแจ้งว่าเรียบร้อย"}
        with self.assertRaises(CommandFailed) as caught:
            self.act(self.kae_resolver, case, "confirm", answer)
        self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)
        with self.assertRaises(CommandFailed):
            self.act(self.kcw, case, "confirm", answer)  # another division
        offers = {o.name for o in web_actions.available(self.kae_resolver, case)}
        self.assertNotIn("confirm", offers)
        st = web_actions.situation(self.kae_resolver, case)
        hints = " ".join(
            h.text
            for h in web_actions.hints(st, web_actions.available(self.kae_resolver, case, st))
        )
        self.assertIn("ให้เจ้าหน้าที่อีกคนในกอง", hints)
        self.assertIn("confirm", {o.name for o in web_actions.available(self.kae2, case)})
        self.act(self.kae2, case, "confirm", answer)
        case.refresh_from_db()
        self.assertEqual(case.status, S.CLOSED)
        self.assertEqual(case.decisions.get(action="confirm").data["by"], "owner")


# --- แผนก -----------------------------------------------------------------------------------------


class SectionTests(OrgTestCase):
    def test_the_policy_proposes_a_section_and_the_division_changes_it(self):
        current = policy.active_policy()
        document = {
            **current.document,
            "code": "FAJO-2-S",
            "sections": [
                {
                    "kinds": ["withdrawal"],
                    "category": "meter",
                    "division": "กคว.",
                    "section": "แผนกคลังเครื่องวัด",
                }
            ],
        }
        version = policy.import_policy(document)
        policy.activate_policy(None, version, "ผู้ทดสอบ", "TEST", "ตารางแผนก")
        _, case = self.submit(withdrawal(("MTR_SYN_A", "1")))
        self.assertEqual(case.section.name, "แผนกคลังเครื่องวัด")
        test_section = Section.objects.get(name="แผนกทดสอบเครื่องวัด")
        self.act(
            self.kcw, case, "set_section", {"section": str(test_section.pk), "reason": "ทดสอบ"}
        )
        case.refresh_from_db()
        self.assertEqual(case.section, test_section)
        with self.assertRaises(CommandFailed):
            other = Section.objects.get(name="แผนกซ่อมบำรุงหม้อแปลง")
            self.act(self.kcw, case, "set_section", {"section": str(other.pk)})
        # A new owner starts from its own proposal (none here).
        self.act(self.kcw, case, "start_triage")
        self.act(
            self.kcw,
            case,
            "request_handoff",
            {"to_division": "กองอุปกรณ์ฯ", "reason_category": "wrong_division", "reason": "ทดสอบ"},
        )
        self.act(self.kae, case, "accept_handoff")
        self.assertIsNone(Case.objects.get(pk=case.pk).section)

    def test_queue_filter_and_report_by_section(self):
        _, mine = self.submit(withdrawal(("MTR_SYN_A", "1")))
        _, other = self.submit(withdrawal(("MTR_SYN_A", "2")))
        stock = Section.objects.get(name="แผนกคลังเครื่องวัด")
        self.act(self.kcw, mine, "set_section", {"section": str(stock.pk)})
        client = Client()
        client.force_login(self.kcw)
        page = client.get("/divisions/กคว./", {"tab": "host", "section": str(stock.pk)})
        numbers = [row.case.number for row in page.context["rows"]]
        self.assertEqual(numbers, [mine.number])
        page = client.get("/divisions/กคว./", {"tab": "host", "section": "none"})
        self.assertEqual([row.case.number for row in page.context["rows"]], [other.number])
        report = reports.division_report(self.kcw, Division.objects.get(code="กคว."))
        counts = {label: n for _, label, n in report["by_section"]}
        self.assertEqual((counts["แผนกคลังเครื่องวัด"], counts["ยังไม่ระบุแผนก"]), (1, 1))
        title, _, cases = reports.drilldown(
            self.kcw, Division.objects.get(code="กคว."), "section", str(stock.pk)
        )
        self.assertEqual(list(cases), [mine])
        self.assertIn("แผนกคลังเครื่องวัด", title)

    def test_admin_sets_a_staff_section_only_in_a_division_they_belong_to(self):
        stock = Section.objects.get(name="แผนกคลังเครื่องวัด")
        identity.set_section(self.admin, self.kcw, stock, "ทดสอบ")
        self.kcw.staff_profile.refresh_from_db()
        self.assertEqual(self.kcw.staff_profile.section, stock)
        with self.assertRaises(ValidationError):
            identity.set_section(self.admin, self.ktf, stock, "ผิดกอง")


# --- One account per division and ตัวเลือกระบบ (ADR 0009) -----------------------------------------


class OptionsTests(OrgTestCase):
    def test_options_start_safe_and_change_only_by_admin_with_a_reason(self):
        # 16 Oct 2569 Q4: separation is off by default; members do every step of their division.
        self.assertEqual(options.get(options.SEPARATION), "off")
        self.assertEqual(options.get(options.REGISTRATION), "admin")
        with self.assertRaises(PermissionDenied):
            options.change(self.kcw, options.SEPARATION, "on", "อยากแยก")
        with self.assertRaises(ValidationError):
            options.change(self.admin, options.SEPARATION, "maybe", "ค่าแปลก")
        options.change(self.admin, options.SEPARATION, "on", "")
        self.assertEqual(options.get(options.SEPARATION), "on")
        event = AuditEvent.objects.get(action="option.changed")
        self.assertEqual((event.data["previous"], event.data["value"]), ("off", "on"))
        client = Client()
        client.force_login(self.admin)
        page = client.get("/console/admin/options/")
        self.assertContains(page, "การแยกหน้าที่ในเรื่องเดียวกัน")
        self.assertEqual(event.reason, "")
        client.post(
            "/console/admin/options/",
            {"key": options.REGISTRATION, "value": "staff", "reason": "สะดวก"},
        )
        self.assertEqual(options.get(options.REGISTRATION), "staff")

    def test_one_account_does_every_step_of_its_division(self):
        solo = make_staff("solo")
        # 16 Oct 2569 Q3: one membership brings every step and the division's authority.
        roles, grants = identity.grant_full_division(
            self.admin, solo, Division.objects.get(code="กองอุปกรณ์ฯ"), "บัญชีเดียวของกอง", "REF-1"
        )
        self.assertEqual((roles, grants), (1, len(identity.DIVISION_AUTHORITY)))
        self.assertEqual(
            identity.grant_full_division(
                self.admin, solo, Division.objects.get(code="กองอุปกรณ์ฯ"), "ซ้ำ", "REF-1"
            ),
            (0, 0),
        )
        _, case = self.submit({"kind": "problem", "topic": "transformer", "description": "ดับ"})
        self.act(solo, case, "start_triage")
        self.act(solo, case, "set_priority", {"priority": "P2", "reason": "ทดสอบ"})
        self.act(solo, case, "complete_triage")
        self.act(solo, case, "resolve", {"category": "fixed", "message": "แก้แล้ว"})
        case.refresh_from_db()
        self.assertIn("confirm", {o.name for o in web_actions.available(solo, case)})
        self.act(solo, case, "confirm", {"channel": "phone", "intake_note": "เขตโทรมา"})
        self.assertEqual(Case.objects.get(pk=case.pk).status, S.CLOSED)

    def test_the_coordinator_account_gets_routing_decisions(self):
        boss = make_staff("boss")
        identity.grant_full_division(
            self.admin, boss, Division.objects.get(code="ฝอจ."), "ผู้บริหาร", "REF-2"
        )
        grant = AuthorityGrant.objects.get(user=boss)
        self.assertEqual((grant.activity, grant.division), (A.ROUTING_DECISION, None))
        with self.assertRaises(ValidationError):
            identity.grant_full_division(
                self.admin, self.admin, Division.objects.get(code="กคว."), "ตัวเอง", "REF-3"
            )

    def test_who_approves_registrations_is_an_option(self):
        registration = identity.submit_registration("line", "U" + "2" * 32, "ผู้แจ้งใหม่", "D01")
        client = Client()
        client.force_login(self.kcw)
        self.assertEqual(client.get("/console/admin/registrations/").status_code, 403)
        with self.assertRaises(PermissionDenied):
            identity.approve_registration(self.kcw, registration)
        options.change(self.admin, options.REGISTRATION, "supervisor", "หัวหน้างานอนุมัติได้")
        self.assertFalse(identity.may_decide_registrations(self.kcw))
        self.assertTrue(identity.may_decide_registrations(self.exec))  # a supervisor role
        options.change(self.admin, options.REGISTRATION, "staff", "กองละหนึ่งบัญชี")
        page = client.get("/console/admin/registrations/")
        self.assertContains(page, "ผู้แจ้งใหม่")
        self.assertNotContains(page, "เพิ่มผู้แจ้ง")  # creating requesters stays with admins
        identity.approve_registration(self.kcw, registration)
        self.assertEqual(Requester.objects.get(display_name="ผู้แจ้งใหม่").approved_by, self.kcw)


# --- SAP per division -----------------------------------------------------------------------------


class RegistryTests(OrgTestCase):
    def test_the_registry_is_for_admins_only_by_default(self):
        # 15 ต.ค. 2569 Q6: admins hold the registry; ตัวเลือกระบบ gives it back to divisions.
        _, meter = self.submit(withdrawal(("MTR_SYN_A", "1")))
        d = RegistryDiscrepancy.objects.create(
            kind="not_found", case=meter, document_number="49001", opened_at=timezone.now()
        )
        self.assertTrue(registry.admins_only())
        self.assertFalse(registry.may_open(self.kcw))
        self.assertFalse(registry.may_decide(self.kcw, d))
        self.assertTrue(registry.may_decide(self.admin, d))
        client = Client()
        client.force_login(self.kcw)
        self.assertNotContains(client.get("/console/"), 'href="/registry/"')
        options.change(self.admin, options.REGISTRY_ACCESS, "staff", "แต่ละกองตัดสินเอง")
        self.assertTrue(registry.may_decide(self.kcw, d))
        self.assertFalse(registry.may_decide(self.admin, d))  # admins read only then
        self.assertContains(client.get("/console/"), 'href="/registry/"')

    def test_each_division_sees_and_decides_its_own_discrepancies(self):
        options.change(self.admin, options.REGISTRY_ACCESS, "staff", "แต่ละกองตัดสินเอง")
        self.assertTrue(registry.per_division())
        _, meter = self.submit(withdrawal(("MTR_SYN_A", "1")))
        _, trf = self.submit(withdrawal(("TRF_SYN_A", "1")))
        now = timezone.now()
        mine = RegistryDiscrepancy.objects.create(
            kind="not_found", case=meter, document_number="49001", opened_at=now
        )
        theirs = RegistryDiscrepancy.objects.create(
            kind="not_found", case=trf, document_number="49002", opened_at=now
        )
        self.assertTrue(registry.is_registry_staff(self.kcw))
        self.assertEqual(list(registry.visible_discrepancies(self.kcw)), [mine])
        self.assertTrue(registry.may_decide(self.kcw, mine))
        self.assertFalse(registry.may_decide(self.kcw, theirs))
        with self.assertRaises(PermissionDenied):
            registry.accept(self.kcw, theirs.pk, "ไม่ใช่ของกองนี้")
        registry.accept(self.kcw, mine.pk, "ตรวจแล้ว")
        self.assertEqual(RegistryDiscrepancy.objects.get(pk=mine.pk).status, "resolved")


# --- Provisional codes --------------------------------------------------------------------------


class RenameTests(OrgTestCase):
    def test_rename_moves_the_policy_with_the_code(self):
        call_command(
            "rename_division_code",
            "--from=กองอุปกรณ์ฯ",
            "--to=กอจ.",
            "--username=admin",
            "--approved-by=เจ้าของกติกา",
            "--reference=TEST-RENAME",
            "--reason=ได้อักษรย่อทางการ",
            stdout=StringIO(),
        )
        active = policy.active_policy()
        self.assertEqual(active.code, "FAJO-2+R1")
        owners = {e["owner"] for e in active.document["responsibility"]}
        self.assertIn("กอจ.", owners)
        self.assertNotIn("กองอุปกรณ์ฯ", owners)
        _, case = self.submit(withdrawal(("TRF_SYN_A", "1")))
        self.assertEqual(case.owner_division.code, "กอจ.")


# --- LINE ---------------------------------------------------------------------------------------


class LineTopicTests(LineTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        real_world(cls)

    def test_the_requester_chooses_usage_and_it_reaches_ktf(self):
        self.approve(ALICE)
        self.say(self.postback(ALICE, "m=new"))
        self.press(ALICE, "แจ้งปัญหา/ข้อเสนอแนะ")
        self.assertIn("4. การใช้ไฟฟ้า (ลักใช้ไฟ ไฟแพง ร้องเรียน)", texts(self.sent[-1]))
        self.press(ALICE, "การใช้ไฟฟ้า")
        self.assertIn("แจ้งเรื่องที่พบ", texts(self.sent[-1]))
        self.type(ALICE, "สงสัยมีการลักใช้ไฟ (ข้อมูลทดสอบ)")
        self.press(ALICE, "ข้าม")
        summary = self.press(ALICE, "ข้าม")
        self.assertIn("สรุปเรื่อง: การใช้ไฟฟ้า", texts(summary))
        reply = self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual((case.owner_division.code, case.topic), ("กตฟ.", "usage"))
        self.assertIn(case.number, texts(reply))

    def test_a_suggestion_reaches_the_coordinator_and_is_acknowledged(self):
        self.approve(ALICE)
        self.say(self.postback(ALICE, "m=new"))
        self.press(ALICE, "แจ้งปัญหา/ข้อเสนอแนะ")
        self.press(ALICE, "ข้อเสนอแนะ")
        self.assertIn("กรุณาแจ้งข้อเสนอแนะ", texts(self.sent[-1]))
        self.type(ALICE, "อยากให้แจ้งเวลารับของล่วงหน้า (ทดสอบ)")
        self.press(ALICE, "ข้าม")
        summary = self.press(ALICE, "ข้าม")
        self.assertIn("สรุปเรื่อง: ข้อเสนอแนะ", texts(summary))
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual((case.owner_division.code, case.topic), ("ฝอจ.", "suggestion"))
        self.assertEqual(problems.categories_for(case), ["considered", "adopted", "not_feasible"])
        self.assertEqual(problem_chat.labels(case), ("รับทราบ", "มีข้อมูลเพิ่ม"))

    def test_an_equipment_problem_picks_the_topic_then_may_skip_the_item(self):
        self.approve(ALICE)
        self.say(self.postback(ALICE, "m=new"))
        self.press(ALICE, "แจ้งปัญหา/ข้อเสนอแนะ")
        self.press(ALICE, "หม้อแปลง")
        self.press(ALICE, "ไม่ระบุรุ่น")
        self.type(ALICE, "หม้อแปลงมีเสียงดัง (ทดสอบ)")
        self.press(ALICE, "ข้าม")
        self.press(ALICE, "ข้าม")
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual((case.owner_division.code, case.topic), ("กองอุปกรณ์ฯ", "transformer"))

    def test_a_mixed_request_lists_every_number(self):
        self.approve(ALICE)
        self.say(self.postback(ALICE, "m=new"))
        self.press(ALICE, "เบิก")
        self.press(ALICE, "มิเตอร์")
        self.press(ALICE, "MTR_SYN_A")
        self.type(ALICE, "1")
        self.press(ALICE, "เพิ่มรายการ")
        self.press(ALICE, "อุปกรณ์อื่น")
        self.press(ALICE, "TRF_SYN_A")
        self.type(ALICE, "1")
        self.press(ALICE, "ไปต่อ")
        self.type(ALICE, "งานขยายเขต")
        self.press(ALICE, "ข้าม")
        reply = self.press(ALICE, "ยืนยันส่งเรื่อง")
        numbers = list(Case.objects.order_by("number").values_list("number", flat=True))
        self.assertEqual(len(numbers), 2)
        self.assertIn("แยกเป็น 2 เรื่อง", texts(reply))
        for number in numbers:
            self.assertIn(number, texts(reply))

    def test_usage_text_never_starts_an_ai_draft(self):
        with (
            override_settings(AI_ENABLED=True),
            patch.object(ai_chat.ai, "active_version", return_value=object()),
        ):
            self.assertFalse(ai_chat.wants_ai(None, None, "ร้องเรียนค่าไฟแพงผิดปกติ"))
        self.assertEqual(
            conversation._topics(), ["meter", "transformer", "other", "usage", "suggestion"]
        )
