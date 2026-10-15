"""หน้าที่ของกอง and the routing AI (ADR 0013; routing grill 14 Oct 2569 Q14-Q25, Q31).

Acceptance → tests:
- admins write each division's overview and งานที่ดูแล with two or more examples, every change
  audited; only what the AI reads moves the digest (DutyTests: Q14, Q17, Q25);
- admins and ส่วนกลาง read every division's duties, other staff their own (DutyTests: Q21);
- for an unclear case the AI proposes the entry's division, quoting the text, with personal data
  masked; ส่วนกลาง hands it on in one press with the AI's keyword filled in (ProposeTests: Q15,
  Q16, Q20, Q24 ก);
- in "direct" mode an entry sends the case straight there, and the division finishing triage
  without handing it on teaches the keyword; an overview answer stays a proposal; the words go
  first (DirectTests: Q15, Q20, Q24 ข, Q31);
- the evaluation runs on the admins' examples and the red-team set, any admin asks for it, a
  model that obeys the message fails it, and changing what the AI reads stops the AI until it
  is evaluated again (GateTests: Q17, Q25).
"""

import json
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, override_settings

from operations import ai, ai_routing, duties, options, policy, web_actions
from operations.management.commands.run_worker import run_once
from operations.models import (
    AIConfigVersion,
    AIEvalJob,
    AIRoutingRun,
    AuditEvent,
    Case,
    CaseTriage,
    Division,
    DutyEntry,
    Role,
    RoutingWord,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m13_org import OrgTestCase

RAW_KEY = "sk-test-" + "r" * 30 + "xyz"  # pragma: allowlist secret
LIGHT = ("ไฟถนนและเสาไฟ", "ไฟถนนดับ เสาไฟเอียง โคมแตก", ["ไฟถนนหน้าบ้านดับ", "เสาไฟเอียงจะล้ม"])
SIGN = ("ป้ายและอาคาร", "ป้ายประกาศ ประตู หน้าต่างของอาคารสำนักงาน", ["ป้ายประกาศหลุด", "ประตูห้องพัง"])
# Words the fake model reads, and what it answers for them: an entry title or a division code.
TRIGGERS = {"ไฟถนน": "ไฟถนนและเสาไฟ", "เสาไฟ": "ไฟถนนและเสาไฟ", "ป้ายประกาศ": "ป้ายและอาคาร"}
TRIGGERS.update({"ประตู": "ป้ายและอาคาร", "ห้องประชุม": "บท.", "ทดสอบมิเตอร์": "กคว."})


class FakeRouter:
    """The provider for routing calls: answers from TRIGGERS and ignores what the message
    asks, unless `obey` is set; records every body it is sent."""

    def __init__(self):
        self.bodies, self.obey = [], False

    def __call__(self, url, api_key, body, timeout):
        self.bodies.append(body)
        data = json.loads(body["messages"][-1]["content"])
        answer = {"entry": "", "division": "", "quote": "", "keyword": ""}
        if self.obey:
            for division in data["divisions"]:
                if division["code"] in data["message"]:
                    answer.update(division=division["code"], quote=division["code"])
                    return _content(answer)
        for word, target in TRIGGERS.items():
            if word in data["message"]:
                for division in data["divisions"]:
                    entry = next((e for e in division["duties"] if e["title"] == target), None)
                    if entry is not None:
                        answer.update(entry=entry["id"], quote=word, keyword=word)
                        return _content(answer)
                    if division["code"] == target:
                        answer.update(division=target, quote=word, keyword=word)
                        return _content(answer)
        return _content(answer)


def _content(answer):
    return {"choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}}]}


class RoutingTestCase(OrgTestCase):
    def setUp(self):
        super().setUp()
        settings = override_settings(AI_SECRET_KEY=Fernet.generate_key().decode(), AI_ENABLED=True)
        settings.enable()
        self.addCleanup(settings.disable)
        self.llm = FakeRouter()
        post = patch.object(ai, "_post", side_effect=self.llm)
        post.start()
        self.addCleanup(post.stop)
        self.dev = make_staff("dev", (Role.ADMIN, None), (Role.DEVELOPER_ADMIN, None))
        kae, bt = Division.objects.get(code="กองอุปกรณ์ฯ"), Division.objects.get(code="บท.")
        self.light = duties.add_entry(self.admin, kae, *LIGHT, "เริ่มกรอก")
        self.sign = duties.add_entry(self.admin, bt, *SIGN, "เริ่มกรอก")
        duties.set_overview(
            self.admin, bt, "งานธุรการ อาคาร ห้องประชุม", ["ห้องประชุมแอร์ไม่เย็น"], "เริ่มกรอก"
        )

    def ready(self):
        """A routing version: tested, evaluated on the examples and switched on."""
        credential = ai.save_credential(
            self.dev, "thaillm", "https://thaillm.or.th/api/v1", RAW_KEY, "ทดสอบ"
        )
        profile = ai.create_profile(self.dev, credential, "router-model")
        version = ai.create_version(self.dev, [profile], "ส่งกอง", task=AIConfigVersion.Task.ROUTING)
        ai.test_version(self.dev, version)
        version = ai.record_eval(version, ai_routing.run_gate(version))
        ai.activate(self.dev, version, "ผ่านการประเมิน")
        return version

    def unclear(self, text):
        _, case = self.submit({"kind": "problem", "description": text})
        run_once()  # the worker asks the AI (EVENT_ROUTING_AI)
        return Case.objects.get(pk=case.pk)

    def proposal(self, case):
        return CaseTriage.objects.get(case=case).current_recommendation

    def finish(self, user, case):
        """คัดกรองเสร็จ: the division keeps the case (a level first)."""
        self.act(user, case, "set_priority", {"priority": "P3", "reason": "ไม่เร่งด่วน"})
        self.act(user, case, "complete_triage")


class DutyTests(RoutingTestCase):
    def test_admins_write_duties_with_examples_and_only_what_the_ai_reads_moves_the_digest(self):
        with self.assertRaisesMessage(ValidationError, "อย่างน้อย 2 ข้อ"):
            duties.add_entry(
                self.admin, self.light.division, "หม้อแปลงบนเสา", "x" * 5, ["ข้อเดียว"], "r"
            )
        with self.assertRaises(PermissionDenied):
            duties.add_entry(self.kae, self.light.division, "งาน", "คำอธิบาย", ["ก" * 5] * 2, "r")
        before = duties.digest()
        duties.change_entry(self.admin, self.light, *LIGHT[:2], [*LIGHT[2], "โคมไฟถนนแตก"], "เพิ่ม")
        self.assertEqual(duties.digest(), before)  # examples are the test, not the prompt
        duties.change_entry(self.admin, self.light, "ไฟถนน", LIGHT[1], LIGHT[2], "ย่อหัวข้อ")
        self.assertNotEqual(duties.digest(), before)
        duties.retire_entry(self.admin, self.sign, "ย้ายไปเขียนใหม่")
        self.assertFalse(DutyEntry.objects.get(pk=self.sign.pk).is_active)
        actions = set(
            AuditEvent.objects.filter(action__startswith="duty.").values_list("action", flat=True)
        )
        self.assertEqual(
            actions,
            {
                "duty.entry_added",
                "duty.entry_changed",
                "duty.entry_retired",
                "duty.overview_changed",
            },
        )

    def test_admins_and_the_coordinator_read_every_division_and_others_their_own(self):
        for user, seen, hidden in (
            (self.exec, ["ไฟถนนและเสาไฟ", "ป้ายและอาคาร"], []),
            (self.kae, ["ไฟถนนและเสาไฟ"], ["ป้ายและอาคาร"]),
        ):
            client = Client()
            client.force_login(user)
            page = client.get("/duties/")
            for title in seen:
                self.assertContains(page, title)
            for title in hidden:
                self.assertNotContains(page, title)
        client = Client()
        client.force_login(self.kae)
        self.assertEqual(client.get("/console/admin/duties/").status_code, 403)
        client.force_login(self.admin)
        self.assertContains(client.get("/console/admin/duties/"), "ป้ายและอาคาร")


class ProposeTests(RoutingTestCase):
    def test_the_ai_proposes_the_entrys_division_and_the_coordinator_hands_it_on(self):
        self.ready()
        case = self.unclear("ไฟถนนหน้าซอยดับ ติดต่อคุณสมชาย 0812345678")
        self.assertEqual(case.owner_division.code, "ฝอจ.")
        run = AIRoutingRun.objects.get(case=case)
        self.assertEqual((run.status, run.entry, run.keyword), ("done", self.light, "ไฟถนน"))
        sent = json.dumps(self.llm.bodies[-1], ensure_ascii=False)
        self.assertNotIn("0812345678", sent)
        self.assertNotIn("สมชาย", sent)
        rec = self.proposal(case)
        self.assertEqual((rec.rules, rec.proposed_owner.code), (["R10"], "กองอุปกรณ์ฯ"))
        self.assertIn("AI เสนอ: งาน 'ไฟถนนและเสาไฟ' ของ กองอุปกรณ์ฯ จากข้อความ 'ไฟถนน'", rec.notes)
        self.act(self.exec, case, "start_triage")
        state = web_actions.situation(self.exec, Case.objects.get(pk=case.pk))
        form = web_actions.build_form("request_handoff", state, {})
        keywords = next(f for f in form.fields if f.name == "keywords")
        self.assertEqual(keywords.initial, "ไฟถนน")
        self.act(
            self.exec, case, "request_handoff", {"to_division": "กองอุปกรณ์ฯ", "keywords": "ไฟถนน"}
        )
        self.act(self.kae, case, "accept_handoff")
        self.assertEqual(RoutingWord.objects.get().source, "handoff")

    def test_an_overview_answer_is_only_a_proposal_even_in_direct_mode(self):
        self.ready()
        options.change(None, options.ROUTING_AI, "direct", "ทดสอบ")
        case = self.unclear("ห้องประชุมชั้นสามแอร์ไม่เย็น")
        self.assertEqual(case.owner_division.code, "ฝอจ.")
        rec = self.proposal(case)
        self.assertEqual(rec.proposed_owner.code, "บท.")
        self.assertTrue(any("ภาพรวมของกอง" in n and "ข้อเสนอเท่านั้น" in n for n in rec.notes))


class DirectTests(RoutingTestCase):
    def test_an_entry_sends_the_case_and_the_division_keeping_it_teaches_the_keyword(self):
        self.ready()
        options.change(None, options.ROUTING_AI, "direct", "ทดสอบ")
        case = self.unclear("เสาไฟหน้าบ้านเอียงมาก")
        self.assertEqual(case.owner_division.code, "กองอุปกรณ์ฯ")
        self.assertTrue(AIRoutingRun.objects.get(case=case).applied)
        decision = case.decisions.get(action="route_by_ai")
        self.assertEqual((decision.actor, decision.actor_label), (None, "AI ส่งกองตามหน้าที่"))
        self.act(self.kae, case, "start_triage")
        self.assertFalse(RoutingWord.objects.exists())  # starting to look is not keeping it
        self.finish(self.kae, case)
        word = RoutingWord.objects.get()
        self.assertEqual((word.word, word.source, word.accepted_by), ("เสาไฟ", "ai", self.kae))
        later = self.unclear("เสาไฟล้มขวางถนน")
        self.assertEqual(later.owner_division.code, "กองอุปกรณ์ฯ")
        self.assertFalse(AIRoutingRun.objects.filter(case=later).exists())  # the word went first

    def test_nothing_is_learned_when_the_division_handed_it_on_first(self):
        self.ready()
        options.change(None, options.ROUTING_AI, "direct", "ทดสอบ")
        case = self.unclear("ไฟถนนซอยเจ็ดดับ")
        self.act(self.kae, case, "start_triage")
        self.act(
            self.kae,
            case,
            "request_handoff",
            {"to_division": "บท.", "reason_category": "wrong_division", "reason": "ไม่ใช่งานเรา"},
        )
        self.act(self.bt, case, "accept_handoff")
        self.finish(self.bt, case)
        self.assertFalse(RoutingWord.objects.exists())


class GateTests(RoutingTestCase):
    def test_changing_what_the_ai_reads_stops_it_until_it_is_evaluated_again(self):
        self.ready()
        self.assertIsNotNone(ai_routing.usable())
        duties.change_entry(self.admin, self.sign, *SIGN[:2], [*SIGN[2], "หน้าต่างแตก"], "เพิ่ม")
        self.assertIsNotNone(ai_routing.usable())
        duties.change_entry(self.admin, self.sign, "ป้าย", SIGN[1], SIGN[2], "เปลี่ยนหัวข้อ")
        self.assertIsNone(ai_routing.usable())
        case = self.unclear("ไฟถนนซอยแปดดับ")
        self.assertFalse(AIRoutingRun.objects.filter(case=case).exists())
        self.assertEqual(self.proposal(case).proposed_owner.code, "ฝอจ.")
        client = Client()
        client.force_login(self.admin)
        client.post("/console/admin/duties/", {"action": "evaluate"})
        job = AIEvalJob.objects.get(version__task="routing", status="queued")
        self.assertEqual(job.requested_by, self.admin)

    def test_a_model_that_obeys_the_message_fails_and_thin_examples_cannot_be_evaluated(self):
        version = self.ready()
        self.llm.obey = True
        report = ai_routing.run_gate(version)
        self.assertTrue(all(run["redteam_violations"] for run in report["runs"]))
        self.assertFalse(ai.record_eval(version, report).eval_passed)
        self.assertIsNone(ai_routing.usable())
        DutyEntry.objects.filter(pk=self.light.pk).update(examples=["ข้อเดียว"])
        with self.assertRaisesMessage(ValidationError, "ตัวอย่างไม่ถึง 2 ข้อ"):
            ai_routing.request_eval(self.admin)
        self.assertEqual(ai_routing.misroutes(), (0, 0))
        self.assertTrue(policy.current_policy())  # the rules are untouched by any of this
