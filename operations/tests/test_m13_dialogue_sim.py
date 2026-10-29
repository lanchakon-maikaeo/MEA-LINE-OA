"""บทสนทนาจำลอง (PROJECT.md research proposal: intent graph + dual agent + validation/retry).

Acceptance → tests: the graph file is validated and a broken one is refused (GraphTests); seeded
dialogues over every scenario pass against the real bot, keep nothing, send nothing and replay
the same (SimulationTests); each rule catches the fault it is for when the bot is broken on
purpose (MutationTests); a text that disagrees with its labels is drawn again and a person
repairs a rejected answer, both counted (RetryTests); passed problem dialogues export as
candidate triage rows without การใช้ไฟฟ้า (ExportTests); a model may write the requester's texts,
and a rewrite that changes the story goes back with the reason and ends in the template
(ModelWriterTests); the command writes its files and is
refused in production (CommandTests); and the bot faults the simulator found stay fixed
(FoundBySimulatorTests).
"""

import copy
import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from operations import (
    ai,
    ai_triage,
    answers,
    conversation,
    dialogue_sim,
    dialogue_writer,
    identity,
    line_api,
    options,
    staff_line,
)
from operations.models import (
    AICredential,
    AIProfile,
    AuditEvent,
    Case,
    ChannelIdentity,
    Division,
    EquipmentType,
    NotificationDelivery,
    SystemOption,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import ALICE, LineTestCase, buttons, texts
from operations.tests.test_m13_org import real_world


def graph_with(**changes):
    graph = copy.deepcopy(dialogue_sim.load_graph())
    for key, value in changes.items():
        graph[key] = value
    return graph


def only(graph, scenario_id, **changes):
    scenario = next(s for s in graph["scenarios"] if s["id"] == scenario_id)
    scenario.update(changes)
    return graph


class SimTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        real_world(cls)
        # Enough meters that the bot switches to search (item_search), as with a real catalogue.
        for n in range(14):
            EquipmentType.objects.create(
                code=f"MTR_SIM_{n:02d}",
                name=f"มิเตอร์จำลองรุ่น {n:02d}",
                category="meter",
                unit="เครื่อง",
                is_serialized=True,
            )
        for division in Division.objects.filter(is_active=True):
            user = make_staff(f"staff{division.pk}")
            identity.grant_full_division(None, user, division, "ทดสอบบทสนทนาจำลอง", "TEST-SIM")
        # คลังคำตอบ by keyword (ADR 0012), as on the local demo, so questions are part of the walk.
        answers.load(
            json.loads(answers.DEFAULT.read_text(encoding="utf-8")),
            approve_as=identity.system_user(),
        )
        options.change(None, options.CHAT_ANSWERS, "faq", "ทดสอบบทสนทนาจำลอง")

    def run_sim(self, count=6, seed=1, scenarios=None, graph=None, only_=None):
        return dialogue_sim.simulate(count, seed, scenarios, graph, only_)


class GraphTests(TestCase):
    def test_the_graph_file_is_valid_and_names_known_moves(self):
        graph = dialogue_sim.load_graph()
        self.assertTrue(graph["is_synthetic"])
        moves = {i for moves in graph["steps"].values() for i, _ in moves}
        self.assertLessEqual(moves, set(dialogue_sim.INTENTS))
        self.assertLessEqual(set(graph["detours"]), set(dialogue_sim.DETOURS))

    def test_a_broken_graph_is_refused_with_every_problem(self):
        graph = graph_with(is_synthetic=False)
        graph["steps"]["kind"].append(["press_magic", 1])
        graph["scenarios"][0]["options"] = {"anonymous_reports": "maybe"}
        graph["scenarios"][1]["actor"] = "robot"
        graph["catalogue"] = {"at_least": {"spoon": 3, "meter": 999}}
        with self.assertRaises(ValidationError) as caught:
            dialogue_sim.validate_graph(graph)
        text = " ".join(caught.exception.messages)
        for part in (
            "is_synthetic",
            "press_magic",
            "anonymous_reports=maybe",
            "ผู้แจ้งต้องเป็น",
            "catalogue: spoon",
            "catalogue: meter",
        ):
            self.assertIn(part, text)


@override_settings(LINE_TYPED_SLIPS=True)  # ADR 0032: the earlier typed intake
class SimulationTests(SimTestCase):
    def test_seeded_dialogues_pass_keep_nothing_and_send_nothing(self):
        before = (
            Case.objects.count(),
            AuditEvent.objects.count(),
            ChannelIdentity.objects.count(),
            list(SystemOption.objects.values_list("key", "value")),
        )
        graph = dialogue_sim.load_graph()
        records = []
        with (
            patch.object(line_api, "reply", side_effect=AssertionError("reply sent")),
            patch.object(line_api, "push", side_effect=AssertionError("push sent")),
        ):
            for scenario in graph["scenarios"]:
                found, _ = self.run_sim(count=4, seed=11, scenarios=[scenario["id"]])
                records.extend(found)
        result = dialogue_sim.report(graph, records, 11)
        failures = [(r["id"], r["scenario"], r["note"]) for r in records if r["status"] != "passed"]
        self.assertEqual(failures, [])
        self.assertGreaterEqual(result["coverage"]["edges"], 0.85, result["coverage"]["missed"])
        self.assertEqual(result["coverage"]["detours"], 1.0)
        after = (
            Case.objects.count(),
            AuditEvent.objects.count(),
            ChannelIdentity.objects.count(),
            list(SystemOption.objects.values_list("key", "value")),
        )
        self.assertEqual(after, before)
        self.assertFalse(NotificationDelivery.objects.exists())
        for name in ("owner", "case", "summary", "staff_private", "ai_private", "anonymous"):
            self.assertGreater(result["checks"][name]["passed"], 0, name)
            self.assertEqual(result["checks"][name]["failed"], 0, name)

    def test_every_record_is_synthetic_and_carries_its_labels(self):
        records, _ = self.run_sim(count=8, seed=4)
        for record in records:
            self.assertTrue(record["is_synthetic"])
            self.assertTrue(record["id"].startswith("sim-"))
            self.assertIn("labels", record)
            self.assertEqual(len(record["turns"]), 2 * len(record["path"]))

    def test_one_dialogue_replays_the_same(self):
        first, _ = self.run_sim(seed=5, only_=3)
        again, _ = self.run_sim(seed=5, only_=3)
        said = [[t["said"] for t in r["turns"] if t["role"] == "user"] for r in (*first, *again)]
        self.assertEqual(said[0], said[1])
        self.assertEqual(first[0]["path"], again[0]["path"])

    def test_staff_link_alert_and_dual_role(self):
        records, result = self.run_sim(count=8, seed=2, scenarios=["staff"])
        self.assertEqual([r["status"] for r in records], ["passed"] * 8)
        steps = {tuple(p) for r in records for p in r["path"]}
        self.assertIn(("staff_detail", "press_push"), steps)
        self.assertTrue(any(r["labels"]["dual"] for r in records))
        self.assertEqual(result["checks"]["staff"]["failed"], 0)

    def test_a_small_catalogue_is_padded_for_the_run_only(self):
        """Transformers here are one item: the run adds synthetic ones so the search step is
        walked, and they go with the rollback (and never into the AI candidates)."""
        graph = dialogue_sim.load_graph()
        graph["catalogue"] = {"at_least": {"transformer": 14}}
        only(graph, "withdraw", categories=["transformer"], lines=[1, 1])
        before = EquipmentType.objects.count()
        records, result = self.run_sim(count=4, seed=6, scenarios=["withdraw"], graph=graph)
        self.assertEqual(EquipmentType.objects.count(), before)
        self.assertEqual(result["padded"], {"transformer": 13})
        self.assertEqual([r["status"] for r in records], ["passed"] * 4)
        steps = {tuple(p) for r in records for p in r["path"]}
        self.assertIn(("item_search", "search_item"), steps)
        self.assertIn("ของจำลองชั่วคราว", "\n".join(dialogue_sim.summary_lines(result)))
        padded = [r for r in records if r["labels"]["lines"][0]["item"].startswith("SIMPAD_")]
        self.assertTrue(padded)
        for record in padded:
            record["labels"].update(kind="problem", topic="transformer")
        self.assertEqual(dialogue_sim.triage_candidates(padded, "x")["cases"], [])

    def test_changing_ones_mind_about_another_item(self):
        graph = dialogue_sim.load_graph()
        graph["steps"]["more"] = [
            ["change_mind_add", 50],
            ["press_add_line", 1],
            ["press_continue", 1],
        ]
        records, _ = self.run_sim(count=3, scenarios=["withdraw"], graph=graph)
        self.assertEqual([r["status"] for r in records], ["passed"] * 3)
        steps = {tuple(p) for r in records for p in r["path"]}
        self.assertIn(("more", "change_mind_add"), steps)
        self.assertIn(("category", "press_lines_done"), steps)

    def test_a_problem_draft_cancelled_at_the_topic_makes_no_case(self):
        graph = dialogue_sim.load_graph()
        graph["steps"]["topic"] = [["press_discard", 1]]
        records, _ = self.run_sim(count=2, scenarios=["abandon_problem"], graph=graph)
        self.assertEqual([r["status"] for r in records], ["passed"] * 2)
        self.assertEqual([r["cases"] for r in records], [[], []])
        self.assertTrue(all(["topic", "press_discard"] in r["path"] for r in records))

    def test_questions_mid_dialogue_get_the_banks_answer(self):
        graph = dialogue_sim.load_graph()
        graph["limits"]["detour_rate"] = 1.0
        graph["detours"] = {"ask_question": graph["detours"]["ask_question"]}
        graph["personas"] = [dict(graph["personas"][0], detours=2)]
        records, result = self.run_sim(count=4, seed=8, scenarios=["withdraw"], graph=graph)
        self.assertEqual(
            [r["status"] for r in records], ["passed"] * 4, [r["note"] for r in records]
        )
        self.assertGreaterEqual(result["checks"]["answer"]["passed"], 4)
        self.assertEqual(result["checks"]["answer"]["failed"], 0)

    def test_production_is_refused(self):
        with override_settings(APP_ENV="production"), self.assertRaises(ValidationError):
            self.run_sim(count=1)


@override_settings(LINE_TYPED_SLIPS=True)  # ADR 0032: the earlier typed intake
class MutationTests(SimTestCase):
    """Break the bot on purpose: the rule meant for that fault must fail."""

    def failed(self, records):
        return {name for r in records for name, ok in r["checks"].items() if not ok}

    def test_a_lost_topic_sends_the_case_to_the_wrong_division(self):
        real = conversation._payload

        def without_topic(draft):
            payload = real(draft)
            payload.pop("topic", None)
            payload.pop("meters", None)  # else the contract refuses meters without the topic
            return payload

        with patch.object(conversation, "_payload", side_effect=without_topic):
            records, _ = self.run_sim(count=3, scenarios=["usage"])
        self.assertIn("case", self.failed(records))

    def test_requester_text_in_a_staff_alert_is_a_leak(self):
        real = staff_line._text

        def chatty(kind, case):
            return real(kind, case) + "\n" + case.description

        with patch.object(staff_line, "_text", side_effect=chatty):
            records, _ = self.run_sim(count=3, scenarios=["suggestion"])
        self.assertIn("staff_private", self.failed(records))

    def test_a_dash_in_a_bot_reply_breaks_the_style(self):
        real = conversation._kind_label
        with patch.object(conversation, "_kind_label", side_effect=lambda *a: real(*a) + " — ด่วน"):
            records, _ = self.run_sim(count=3, scenarios=["suggestion"])
        self.assertIn("style", self.failed(records))

    def test_a_hazard_without_guidance_fails(self):
        graph = only(dialogue_sim.load_graph(), "problem_equipment", hazard=1.0)
        with patch("operations.policy.hazard_guidance", return_value=""):
            records, _ = self.run_sim(count=3, scenarios=["problem_equipment"], graph=graph)
        self.assertIn("hazard", self.failed(records))

    def test_a_greeting_sent_to_waiting_staff_fails(self):
        # Staff asked for more; the person greets first. A bot that takes it as information
        # (the behaviour before 11 Oct 2569) breaks the info rule.
        graph = dialogue_sim.load_graph()
        graph["steps"]["awaiting_info"] = [["greet_waiting", 1]]
        with patch.object(answers, "small_talk", return_value=False):
            records, _ = self.run_sim(count=2, scenarios=["info_reply"], graph=graph)
        self.assertIn("info", self.failed(records))

    def test_staff_asking_for_more_is_answered_in_the_chat(self):
        records, _ = self.run_sim(count=4, scenarios=["info_reply"])
        self.assertEqual({r["status"] for r in records}, {"passed"})
        self.assertTrue(all(r["checks"].get("info") for r in records))

    def test_a_case_shown_to_someone_else_breaks_isolation(self):
        graph = dialogue_sim.load_graph()
        graph["limits"]["detour_rate"] = 1.0
        graph["detours"] = {"peek_case": {"weight": 1, "steps": ["idle"]}}
        graph["personas"] = [dict(graph["personas"][0], detours=1)]
        with patch.object(
            conversation.services, "visible_cases", side_effect=lambda actor: Case.objects.all()
        ):
            records, _ = self.run_sim(count=2, scenarios=["withdraw"], graph=graph)
        self.assertIn("isolation", self.failed(records))


@override_settings(LINE_TYPED_SLIPS=True)  # ADR 0032: the earlier typed intake
class RetryTests(SimTestCase):
    def test_a_text_that_names_another_category_is_drawn_again(self):
        graph = only(dialogue_sim.load_graph(), "problem_equipment", topics=["meter"], hazard=0)
        graph["texts"]["description"]["meter"] = [
            "มิเตอร์ติดกับหม้อแปลงใหม่ ไม่ทำงาน",
            "หม้อแปลงข้างมิเตอร์มีเสียง",
            "มิเตอร์และฟิวส์ใช้ไม่ได้",
            "มิเตอร์ซอย {soi} หน้าจอดับ",
        ]
        records, result = self.run_sim(count=4, scenarios=["problem_equipment"], graph=graph)
        self.assertGreater(result["redraws"], 0)
        for record in records:
            self.assertEqual(record["status"], "passed", record["note"])
            self.assertIn("หน้าจอดับ", record["labels"]["fields"]["description"])

    def test_no_agreeing_text_skips_the_dialogue_instead_of_blaming_the_bot(self):
        graph = only(dialogue_sim.load_graph(), "suggestion")
        graph["texts"]["description"]["suggestion"] = ["อยากให้แก้ค่าไฟแพง"]
        records, result = self.run_sim(count=2, scenarios=["suggestion"], graph=graph)
        self.assertEqual(result["skipped"], 2)
        self.assertEqual(result["failed"], 0)

    def test_a_number_in_words_is_repaired_with_digits(self):
        graph = dialogue_sim.load_graph()
        graph["personas"] = [dict(graph["personas"][0], quantity={"words": 1})]
        only(graph, "withdraw", categories=["meter"], lines=[1, 1])
        records, result = self.run_sim(count=3, scenarios=["withdraw"], graph=graph)
        self.assertEqual([r["status"] for r in records], ["passed"] * 3)
        self.assertGreaterEqual(result["repairs"].get("quantity", 0), 1)
        said = [t["said"] for r in records for t in r["turns"] if t["role"] == "user"]
        self.assertTrue(any("เครื่อง" in s and not any(c.isdigit() for c in s) for s in said))


class ExportTests(SimTestCase):
    def test_passed_problem_dialogues_become_candidate_triage_rows(self):
        records, _ = self.run_sim(count=30, seed=9)
        data = dialogue_sim.triage_candidates(records, "triage-sim-test")
        self.assertTrue(data["is_synthetic"])
        self.assertIsNone(data["frozen"])
        ids = {r["id"]: r for r in records}
        catalog = list(EquipmentType.objects.all())
        self.assertTrue(data["cases"])
        for row in data["cases"]:
            labels = ids[row["id"]]["labels"]
            self.assertNotIn(labels["topic"], ("usage", "suggestion"))
            self.assertEqual(row["expected"]["hazard"], labels["hazard"])
            plain = ai_triage.dataset_data(row, catalog)
            self.assertEqual(plain["kind"], "problem")
            if not row["lines"]:
                self.assertIn("equipment", row["expected"])


class CommandTests(SimTestCase):
    def test_the_command_writes_records_report_and_candidates(self):
        with tempfile.TemporaryDirectory() as folder:
            out = StringIO()
            call_command(
                "simulate_line_dialogues",
                count=4,
                seed=3,
                out=f"{folder}/d.jsonl",
                report=f"{folder}/r.json",
                ai_eval_out=f"{folder}/t.json",
                stdout=out,
            )
            lines = Path(f"{folder}/d.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 4)
            self.assertEqual(json.loads(Path(f"{folder}/r.json").read_text())["dialogues"], 4)
            self.assertIn("cases", json.loads(Path(f"{folder}/t.json").read_text()))
        self.assertIn("บทสนทนาจำลอง 4 บท", out.getvalue())

    def test_production_and_unknown_scenarios_are_refused(self):
        with self.assertRaises(CommandError):
            call_command("simulate_line_dialogues", count=1, scenario=["nope"], stdout=StringIO())
        with override_settings(APP_ENV="production"), self.assertRaises(CommandError):
            call_command("simulate_line_dialogues", count=1, stdout=StringIO())


class FoundBySimulatorTests(LineTestCase):
    """Faults the first simulated runs found in the bot, kept as plain regression tests."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        real_world(cls)

    def test_a_long_catalogue_still_lets_a_problem_skip_the_model(self):
        for n in range(14):
            EquipmentType.objects.create(
                code=f"MTR_L{n:02d}", name=f"มิเตอร์ L{n:02d}", category="meter", unit="เครื่อง"
            )
        self.approve(ALICE)
        self.say(self.postback(ALICE, "m=problem&topic=equipment"))  # the equipment card
        reply = self.press(ALICE, "มิเตอร์")
        self.assertIn("ค้นหา", texts(reply))
        self.assertIn("ไม่ระบุรุ่น", buttons(reply))
        reply = self.type(ALICE, "มิเตอร์ L")
        self.assertIn("พบหลายรายการ", texts(reply))
        self.assertIn("ไม่ระบุรุ่น", buttons(reply))
        reply = self.press(ALICE, "ไม่ระบุรุ่น")
        self.assertIn("อธิบายปัญหา", texts(reply))

    def test_the_summary_follows_the_questions_with_the_topics_labels(self):
        self.approve(ALICE)
        self.say(self.postback(ALICE, "m=problem&topic=suggestion"))  # the suggestion card
        self.type(ALICE, "อยากให้มีรอบรับของช่วงบ่าย (ทดสอบ)")
        self.type(ALICE, "ใช้กับคลังเขต (ทดสอบ)")
        summary = texts(self.press(ALICE, "ข้าม"))
        first = summary.index("ข้อเสนอแนะ: อยากให้มีรอบรับของช่วงบ่าย")
        self.assertLess(first, summary.index("รายละเอียดเพิ่ม: ใช้กับคลังเขต"))
        self.assertNotIn("ผลกระทบ", summary)


def answer(text):
    return {"choices": [{"message": {"content": text}}]}


class ModelWriterTests(SimTestCase):
    """The model is faked at the HTTP layer (ai._post): no call leaves the test."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = make_staff("ai-admin")
        credential = AICredential.objects.create(
            name="fake",
            base_url="https://api.opentyphoon.ai/v1",
            encrypted_key="x",
            created_by=cls.admin,
        )
        cls.profile = AIProfile.objects.create(
            credential=credential, model_id="fake-thai-8b", created_by=cls.admin
        )

    def writer(self, **kwargs):
        return dialogue_writer.writer_for("model", self.profile.pk, **kwargs)

    def suggestion_graph(self):
        graph = only(dialogue_sim.load_graph(), "suggestion")
        graph["texts"]["description"]["suggestion"] = ["เสนอให้เพิ่มรอบรับของช่วงบ่ายวันที่ {n}"]
        return graph

    def test_a_faithful_rewrite_is_what_the_person_types(self):
        sent = []

        def provider(url, key, body, timeout):
            sent.append(body)
            source = body["messages"][1]["content"]
            if "รอบรับ" not in source:
                return answer(f"คือ{source}")
            number = "".join(c for c in source if c.isdigit())
            return answer(f"ข้อความ: ขอเพิ่มรอบรับของตอนบ่ายวันที่ {number} ได้ไหมครับ")

        with (
            patch.object(ai, "_post", side_effect=provider),
            patch.object(ai, "decrypt_key", return_value="k"),
        ):
            records, result = dialogue_sim.simulate(
                2, 1, ["suggestion"], self.suggestion_graph(), writer=self.writer()
            )
        self.assertEqual([r["status"] for r in records], ["passed", "passed"])
        typed = records[0]["labels"]["fields"]["description"]
        self.assertTrue(typed.startswith("ขอเพิ่มรอบรับของตอนบ่าย"))  # the label prefix is cleaned
        self.assertEqual(records[0]["rewrites"][0]["by"], "model:fake-thai-8b")
        self.assertEqual(result["writer"]["by_model"], result["writer"]["texts"])
        self.assertEqual(sent[0]["model"], "fake-thai-8b")
        # Only template texts go to the provider: never a requester's name.
        surnames = dialogue_sim.load_graph()["texts"]["name"]["last"]
        self.assertFalse(
            [b for b in sent for n in surnames if n in json.dumps(b, ensure_ascii=False)]
        )

    def test_a_rewrite_that_changes_the_story_goes_back_with_the_reason(self):
        sent = []

        def provider(url, key, body, timeout):
            sent.append(body["messages"])
            source = body["messages"][1]["content"]
            if len(sent) == 1:
                return answer("อยากให้แก้ค่าไฟแพงด้วย")  # turns a suggestion into a usage story
            if len(sent) == 2:
                return answer(source.rstrip("0123456789 "))  # drops the number
            return answer(source.replace("เสนอให้", "อยากให้"))

        with (
            patch.object(ai, "_post", side_effect=provider),
            patch.object(ai, "decrypt_key", return_value="k"),
        ):
            records, result = dialogue_sim.simulate(
                1, 1, ["suggestion"], self.suggestion_graph(), writer=self.writer()
            )
        entry = records[0]["rewrites"][0]
        self.assertEqual(entry["by"], "model:fake-thai-8b")
        self.assertEqual(entry["tries"], 3)
        # Each try carries the earlier answers and the validator's reasons, in chat turns.
        self.assertEqual(sent[1][2], {"role": "assistant", "content": "อยากให้แก้ค่าไฟแพงด้วย"})
        self.assertIn("ใช้ไม่ได้เพราะข้อเสนอแนะมีคำของเรื่องการใช้ไฟฟ้า", sent[1][3]["content"])
        self.assertIn("หายไป", sent[2][-1]["content"])
        self.assertIn("วิธีพิมพ์ของคนนี้:", sent[0][0]["content"])
        self.assertTrue(records[0]["labels"]["fields"]["description"].startswith("อยากให้"))
        self.assertEqual(records[0]["status"], "passed")

    def test_a_model_that_never_keeps_the_story_leaves_the_template(self):
        with (
            patch.object(ai, "_post", return_value=answer("ข้อความเดิม: ไม่รู้")),
            patch.object(ai, "decrypt_key", return_value="k"),
        ):
            records, result = dialogue_sim.simulate(
                1, 1, ["suggestion"], self.suggestion_graph(), writer=self.writer()
            )
        entry = records[0]["rewrites"][0]
        self.assertEqual(entry["by"], "template")
        self.assertEqual(entry["tries"], dialogue_sim.load_graph()["limits"]["max_redraws"])
        self.assertTrue(records[0]["labels"]["fields"]["description"].startswith("เสนอให้"))
        self.assertEqual(records[0]["status"], "passed")
        self.assertIn("มีคำสั่งเรื่องวิธีพิมพ์ปนมากับข้อความ", result["writer"]["refused"])

    def test_what_the_first_live_run_let_through_is_refused(self):
        """OpenTyphoon, 10 Oct 2569: the style echoed into the text, digits made up, and the
        persona's description sent instead of the message."""
        bad = [
            "กดปุ่มตามที่บอทเสนอ; ไม่ต้องมีคำลงท้าย เสนอให้เพิ่มรอบรับของช่วงบ่ายวันที่ {n}",
            "1234567890",
            "ทักทายหรือพิมพ์นอกเรื่องระหว่างทาง",
        ]
        calls = []

        def provider(url, key, body, timeout):
            source = body["messages"][1]["content"]
            number = "".join(c for c in source if c.isdigit())
            calls.append(body)
            return answer(bad[len(calls) - 1].format(n=number) if len(calls) <= 3 else source)

        graph = self.suggestion_graph()
        graph["personas"] = [dict(graph["personas"][0], label="กดปุ่มตามที่บอทเสนอ")]
        with (
            patch.object(ai, "_post", side_effect=provider),
            patch.object(ai, "decrypt_key", return_value="k"),
        ):
            records, result = dialogue_sim.simulate(
                1, 1, ["suggestion"], graph, writer=self.writer()
            )
        refused = records[0]["rewrites"][0]["refused"]
        self.assertEqual(len(refused), 3)
        self.assertIn("มีคำสั่งเรื่องวิธีพิมพ์ปนมากับข้อความ", refused[0])
        self.assertIn("เนื้อความเปลี่ยน", refused[1])
        self.assertIn("เนื้อความเปลี่ยน", refused[2])
        self.assertEqual(dialogue_sim.overlap("ทดแทนของที่ส่งซ่อม", "1234567890"), 0)
        self.assertEqual(dialogue_sim.numbers_in("ค่าไฟสูงสามเดือน 2 รอบ"), ({"2"}, {"3"}))
        # Later live runs: a Chinese word slipped in; emoji are how people type and stay.
        self.assertTrue(dialogue_sim.FOREIGN.search("ตรวจมิเตอร์用电"))
        self.assertFalse(dialogue_sim.FOREIGN.search("ช่วยดูหน่อยครับ 🙏 (CA 9123)"))

    def test_a_rewrite_that_answers_the_suggestion_loses_its_meaning(self):
        def provider(url, key, body, timeout):
            source = body["messages"][1]["content"]
            if len(body["messages"]) == 2:  # answers it as if it were done
                return answer(source.replace("เสนอให้", "") + " ให้แล้วครับ")
            return answer(source.replace("เสนอให้", "ขอ"))

        with (
            patch.object(ai, "_post", side_effect=provider),
            patch.object(ai, "decrypt_key", return_value="k"),
        ):
            records, _ = dialogue_sim.simulate(
                1, 1, ["suggestion"], self.suggestion_graph(), writer=self.writer()
            )
        entry = records[0]["rewrites"][0]
        self.assertEqual(entry["refused"], ["ความต้องการของผู้แจ้งหายไป"])
        self.assertTrue(entry["to"].startswith("ขอเพิ่มรอบรับของ"))

    def test_provider_errors_and_the_call_budget_fall_back_to_templates(self):
        with (
            patch.object(ai, "_post", side_effect=ai.AIError("HTTP 503")),
            patch.object(ai, "decrypt_key", return_value="k"),
        ):
            writer = self.writer(max_calls=1)
            records, result = dialogue_sim.simulate(
                3, 1, ["suggestion"], self.suggestion_graph(), writer=writer
            )
        self.assertEqual(writer.calls, 1)
        self.assertEqual(writer.errors, 1)
        self.assertEqual(result["writer"]["by_model"], 0)
        self.assertEqual([r["status"] for r in records], ["passed"] * 3)

    def test_the_writer_needs_a_known_profile(self):
        for name, profile in (("model", None), ("model", 999999), ("poet", None)):
            with self.assertRaises(ValidationError):
                dialogue_writer.writer_for(name, profile)
        self.assertEqual(dialogue_writer.writer_for("templates").name, "templates")
        with self.assertRaises(CommandError):
            call_command("simulate_line_dialogues", count=1, writer="model", stdout=StringIO())
