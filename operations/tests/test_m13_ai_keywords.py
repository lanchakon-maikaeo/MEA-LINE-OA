"""AI ยกคำสำคัญ (15 ต.ค. 2569 Q9, Q34): words from an unplaced case's masked text fill ฝอจ.'s
handoff form and are remembered when the receiving division accepts; the model never chooses the
division, never reads การใช้ไฟฟ้า text, and must pass its evaluation twice before it is used.
"""

import json
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.core.exceptions import ValidationError
from django.test import override_settings

from operations import ai, ai_keywords, pii, web_actions
from operations.management.commands.run_worker import run_once
from operations.models import AIConfigVersion, AIKeywordRun, Case, Role, RoutingWord
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m13_org import OrgTestCase

RAW_KEY = "sk-test-" + "k" * 30 + "xyz"  # pragma: allowlist secret


class FakeKeywords:
    """A provider that copies the first chunk of the message, as a careful model would, or (in
    `careless` mode) personal data and words that are not there."""

    def __init__(self):
        self.bodies, self.careless = [], False

    def __call__(self, url, api_key, body, timeout):
        self.bodies.append(body)
        text = json.loads(body["messages"][-1]["content"])["message"]
        if self.careless:
            words = ["สมชาย", "0812345678", "คำที่ไม่มี"]
        else:
            first = text.split(" ")[0]
            masked = pii.Masker.for_ai().mask(first)
            words = [first] if masked == first and "[" not in first else []
        content = json.dumps({"keywords": words}, ensure_ascii=False)
        return {"choices": [{"message": {"content": content}}]}


class KeywordTestCase(OrgTestCase):
    def setUp(self):
        super().setUp()
        settings = override_settings(AI_SECRET_KEY=Fernet.generate_key().decode(), AI_ENABLED=True)
        settings.enable()
        self.addCleanup(settings.disable)
        self.llm = FakeKeywords()
        post = patch.object(ai, "_post", side_effect=self.llm)
        post.start()
        self.addCleanup(post.stop)
        self.dev = make_staff("kwdev", (Role.ADMIN, None), (Role.DEVELOPER_ADMIN, None))

    def version(self):
        credential = ai.save_credential(
            self.dev, "thaillm", "https://thaillm.or.th/api/v1", RAW_KEY, "ทดสอบ"
        )
        profile = ai.create_profile(self.dev, credential, "keyword-model")
        version = ai.create_version(self.dev, [profile], "ยกคำ", task=AIConfigVersion.Task.KEYWORDS)
        return ai.test_version(self.dev, version)

    def ready(self):
        version = self.version()
        version = ai.record_eval(version, ai_keywords.run_gate(version))
        ai.activate(self.dev, version, "ผ่านการประเมิน")
        self.llm.bodies.clear()  # the evaluation's calls
        return version


class ValidateTests(KeywordTestCase):
    def test_only_words_of_the_text_that_are_not_personal_pass(self):
        text = "ป้ายประกาศหลุด ติดต่อคุณสมชาย 0812345678"
        raw = json.dumps({"keywords": ["ป้ายประกาศหลุด", "ไม่มีในข้อความ", "0812345678"]})
        words, problems = ai_keywords.validate(raw, text)
        self.assertEqual(words, ["ป้ายประกาศหลุด"])
        self.assertEqual(sorted(problems), ["not_in_text", "personal"])
        with self.assertRaises(ai.SchemaError):
            ai_keywords.validate(json.dumps({"division": "กคว."}), text)
        _, problems = ai_keywords.validate(json.dumps({"keywords": ["ป้าย"] * 5}), text)
        self.assertIn("too_many", problems)


class GateTests(KeywordTestCase):
    def test_a_careful_model_passes_twice_and_a_careless_one_never(self):
        version = self.version()
        with self.assertRaises(ValidationError):
            ai.activate(self.dev, version, "ยังไม่ได้ประเมิน")
        report = ai_keywords.run_gate(version)
        self.assertEqual(len(report["runs"]), 2)
        self.assertTrue(all(r["violations"] == 0 for r in report["runs"]))
        self.assertTrue(ai_keywords.gate_passed(report, version))
        self.llm.careless = True
        self.assertFalse(ai_keywords.gate_passed(ai_keywords.run_gate(version), version))


class UseTests(KeywordTestCase):
    def test_words_fill_the_handoff_form_and_are_learned_when_accepted(self):
        self.ready()
        _, case = self.submit(
            {"kind": "problem", "description": "ป้ายประกาศหลุด ติดต่อคุณสมชาย 0812345678"}
        )
        run_once()  # the worker asks the AI (EVENT_KEYWORDS_AI)
        run = AIKeywordRun.objects.get(case=case)
        self.assertEqual((run.status, run.keywords), ("done", ["ป้ายประกาศหลุด"]))
        sent = json.dumps(self.llm.bodies[-1], ensure_ascii=False)
        self.assertNotIn("0812345678", sent)
        self.assertNotIn("สมชาย", sent)
        self.assertEqual(Case.objects.get(pk=case.pk).owner_division.code, "ฝอจ.")  # no division
        self.act(self.exec, case, "start_triage")
        state = web_actions.situation(self.exec, Case.objects.get(pk=case.pk))
        form = web_actions.build_form("request_handoff", state, {})
        self.assertEqual(
            next(f for f in form.fields if f.name == "keywords").initial, "ป้ายประกาศหลุด"
        )
        self.act(
            self.exec,
            case,
            "request_handoff",
            {
                "to_division": "บท.",
                "keywords": "ป้ายประกาศหลุด",
                "reason_category": "wrong_division",
                "reason": "ป้ายของอาคาร",
            },
        )
        self.act(self.bt, case, "accept_handoff")
        self.assertEqual(RoutingWord.objects.get().word, "ป้ายประกาศหลุด")

    def test_usage_text_stays_from_the_ai_while_an_admin_keeps_it(self):
        # ADR 0021 lifted Q14 on 25 Oct 2569; ตัวเลือกระบบ usage_ai "never" brings it back.
        from operations import options

        options.change(None, options.USAGE_AI, "never", "ทดสอบกฎ Q14 เดิม")
        self.ready()
        from operations.models import District

        self.submit(
            {
                "kind": "problem",
                "topic": "usage",
                "description": "บ้านข้างๆ ลักใช้ไฟ",
                "meters": [
                    {
                        "number": "12345678",
                        "district": District.objects.order_by("code").first().code,
                    }
                ],  # 25 Oct 2569 Q11
            }
        )
        run_once()
        self.assertFalse(AIKeywordRun.objects.exists())
        self.assertEqual(self.llm.bodies, [])
