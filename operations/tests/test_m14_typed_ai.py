"""AI รุ่นที่สกัดเลขใบเบิก/ใบส่งคืน SAP และช่องที่พิมพ์ของ กคว. (user, 23 Oct 2569).

Acceptance → tests: a version without the switch keeps the prompt, schema, catalog and evidence
it passed with (OldVersionTests); a version with it asks for the SAP number and the typed rows,
and keeps only values written in the message (ValidationTests); the chat fills the typed draft
and asks only what is missing, the same way as typing by hand (ChatTests); the locked typed-v1
set scores the drafts that a correct reading produces as perfect, and activation needs that
evidence (TypedSetTests).
"""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.utils import timezone

from operations import ai, ai_chat, ai_eval, ai_harness
from operations.management.commands.run_worker import run_once
from operations.models import AIConfigVersion, ChannelIdentity, ConversationDraft, EquipmentType
from operations.tests.test_m03_line import ALICE, texts
from operations.tests.test_m05_ai import AITestCase, answer, passing_gate, request

GROUP_CODES = {"meter": "TYPED_METER", "ct": "TYPED_CT", "vt": "TYPED_VT", "other": "TYPED_OTHER"}
LABELS = {"meter": "มิเตอร์", "ct": "CT", "vt": "VT"}
# What a correct reading of these messages proposes besides the oracle rows.
REGULAR = {"T08": [("FUSE_SYN_A", 10, "ฟิวส์")], "T20": [("CBL_SYN_A", 30, "สายไฟ")]}
# T14 writes two meter numbers for three meters: the model reads both, the chat refuses the row.
READ = {
    "T14": [
        {"group": "meter", "numbers": ["55001111", "55001112"], "size": "5(15)A", "quantity": "3"}
    ]
}


def typed_request(intent, *items, sap_number=None, **fields):
    """A model answer in the typed schema: items are (code, quantity, text, serials, size)."""
    entry = request(intent, **fields)
    entry["sap_number"] = sap_number
    entry["items"] = [
        {"item_code": code, "item_text": text, "quantity": qty, "serials": serials, "size": size}
        for code, qty, text, serials, size in items
    ]
    return entry


def ideal(case):
    """What a model reading the message right answers."""
    requests = []
    for oracle in case["expected"]:
        items = []
        for row in READ.get(case["id"], oracle.get("rows", [])):
            group = row["group"]
            items.append(
                (
                    GROUP_CODES[group],
                    int(row["quantity"]) if row.get("quantity") else None,
                    row.get("name") or LABELS[group],
                    row.get("numbers", []),
                    row.get("size"),
                )
            )
        items += [(code, qty, text, [], None) for code, qty, text in REGULAR.get(case["id"], [])]
        requests.append(
            typed_request(oracle["intent"], *items, sap_number=oracle.get("sap_number"))
        )
    return {"requests": requests}


def passing_typed_gate(version):
    report = passing_gate(version)
    dataset, _, cases = ai_eval.load(ai_eval.DATASETS["typed1"])
    report["profiles"].append(
        {
            "dataset": dataset,
            "dataset_sha256": ai_eval.TYPED_SHA256,
            "role": "application",
            "passed": True,
            "metrics": {
                **ai_eval.TYPED_THRESHOLDS,
                "cases": len(cases),
                "availability": 1.0,
                "json_valid": 1.0,
                "intent_accuracy": 1.0,
                "sap_exact": 1.0,
                "rows_exact": 1.0,
            },
        }
    )
    report.update(gate=ai.TYPED_EVAL_GATE, schema=ai.TYPED_SCHEMA_VERSION)
    return report


class TypedTestCase(AITestCase):
    def typed_version(self, activate=True):
        version = ai.create_version(self.dev, self.profiles, typed_fields=True)
        ai.test_version(self.dev, version)
        version = ai.record_eval(version, passing_typed_gate(version))
        if activate:
            ai.activate(self.dev, version, "สกัดเลข SAP และช่องที่พิมพ์")
        return version


class OldVersionTests(TypedTestCase):
    def test_a_version_without_the_switch_keeps_what_it_passed_with(self):
        self.assertFalse(ai.reads_typed(self.version))
        codes = {i.code for i in ai._catalog()}
        self.assertFalse(codes & set(GROUP_CODES.values()))  # "มิเตอร์" stays MTR_SYN_A
        self.assertTrue(set(GROUP_CODES.values()) <= {i.code for i in ai._catalog(True)})
        schema = json.dumps(ai.response_schema(sorted(codes)))
        self.assertNotIn("sap_number", schema)
        self.assertNotIn('"size"', schema)
        system = ai._messages("ขอเบิกฟิวส์ 2 ชิ้น", None, ai._catalog())[0]["content"]
        self.assertNotIn("SAP", system)
        self.assertEqual(ai.prompt_version_for(self.version), ai.PROMPT_VERSION)
        self.assertEqual(ai.schema_version_for(self.version), ai.SCHEMA_VERSION)
        self.assertEqual(ai.eval_gate_for(self.version), ai.EVAL_GATE)
        self.assertEqual(ai_eval.gate_datasets(self.version), ai_eval.GATE_DATASETS)
        self.assertNotIn("typed1", ai_eval.gate_datasets(self.version))

    def test_the_switch_is_for_extraction_only_and_audited(self):
        version = ai.create_version(self.dev, self.profiles, typed_fields=True)
        self.assertTrue(version.typed_fields)
        self.assertNotEqual(ai_eval.config_digest(version), ai_eval.config_digest(self.version))
        with self.assertRaises(ai.ValidationError):
            ai.create_version(
                self.dev, self.profiles, task=AIConfigVersion.Task.ANSWER, typed_fields=True
            )
        self.assertEqual(
            ai.prompt_version_for(version), f"{ai.PROMPT_VERSION}+sap"
        )  # fits 32 characters with the harness and five examples too
        self.assertLessEqual(len(f"{ai_harness.PROMPT_VERSION}+sap+fewshot5"), 32)

    def test_a_typed_draft_reaches_the_ai_only_with_a_version_that_reads_it(self):
        draft = ConversationDraft.objects.create(
            identity=ChannelIdentity.objects.get(external_id=ALICE),
            requester=self.alice,
            kind="withdrawal",
            step="typed_group",
            data={"lines": [], "fields": {}, "typed": "meter"},
            updated_at=timezone.now(),
        )
        turn = SimpleNamespace(anonymous=False, identity=draft.identity)
        self.assertFalse(ai_chat.wants_ai(turn, draft, "ขอ CT 100/5A อีก 2 ตัว"))
        self.typed_version()
        self.assertTrue(ai_chat.wants_ai(turn, draft, "ขอ CT 100/5A อีก 2 ตัว"))


class ValidationTests(TypedTestCase):
    def read(self, text, *requests):
        raw = json.dumps({"requests": list(requests)}, ensure_ascii=False)
        return ai.validate(raw, text, ai._catalog(True))

    def test_the_typed_prompt_and_schema_ask_for_the_sap_number_and_rows(self):
        catalog = ai._catalog(True)
        schema = ai.response_schema([i.code for i in catalog], ai.typed_catalog(catalog))
        request_schema = schema["properties"]["requests"]["items"]
        self.assertIn("sap_number", request_schema["required"])
        self.assertIn("size", request_schema["properties"]["items"]["items"]["required"])
        messages = ai._messages("ขอเบิก CT 100/5A 3 ตัว", None, catalog)
        self.assertIn("sap_number", messages[0]["content"])
        shown = json.loads(messages[-1]["content"])["catalog"]
        self.assertIn(
            {"code": "TYPED_CT", "name": "CT ตามที่แจ้ง", "unit": "ตัว", "group": "CT"}, shown
        )

    def test_values_must_be_written_in_the_message(self):
        text = "ขอเบิกมิเตอร์ 5(15)A 2 เครื่อง หมายเลข 11002345 11002346 ใบเบิก 4500012345"
        got = self.read(
            text,
            typed_request(
                "withdrawal",
                ("TYPED_METER", 2, "มิเตอร์", ["11002345", "11002346"], "5(15)A"),
                sap_number="4500012345",
            ),
        ).requests[0]
        item = got.items[0]
        self.assertEqual(
            (got.sap_number, item.code, item.quantity, item.serials, item.size),
            ("4500012345", "TYPED_METER", 2, ["11002345", "11002346"], "5(15)A"),
        )
        # A number the requester never wrote, a size that is not there, a slip number that is
        # also a meter number: each is dropped and asked again.
        for sap, size, serials in (
            ("4500099999", "5(15)A", ["11002345", "11002346"]),
            ("4500012345", "15(45)A", ["11002345", "11002346"]),
            ("11002345", "5(15)A", ["11002345", "11002346"]),
        ):
            with self.subTest(sap=sap, size=size):
                got = self.read(
                    text,
                    typed_request(
                        "withdrawal",
                        ("TYPED_METER", 2, "มิเตอร์", serials, size),
                        sap_number=sap,
                    ),
                ).requests[0]
                dropped = ("sap_number" in got.rejected_fields) or ("size" in got.items[0].rejected)
                self.assertTrue(dropped)
                self.assertEqual(got.items[0].serials, serials)

    def test_a_problem_has_no_sap_number_and_counts_are_never_sizes(self):
        got = self.read(
            "แจ้งปัญหา มิเตอร์ที่เบิกตามใบเบิก 4500131313 หมายเลข 77001234 ไม่หมุน",
            typed_request(
                "problem",
                ("TYPED_METER", None, "มิเตอร์", ["77001234"], None),
                sap_number="4500131313",
            ),
        ).requests[0]
        self.assertEqual((got.sap_number, got.items[0].serials), ("", ["77001234"]))
        got = self.read(
            "ขอเบิกมิเตอร์ 1 เฟส 5(15)A 2 เครื่อง",
            typed_request("withdrawal", ("TYPED_METER", 1, "มิเตอร์", [], "5(15)A")),
        ).requests[0]
        self.assertIsNone(got.items[0].quantity)  # "1 เฟส" is not a count

    def test_a_meter_number_label_and_other_equipment_words(self):
        got = self.read(
            "คืนมิเตอร์ 3 ลูก ขนาด 5(15)A เลขมิเตอร์ 44006789 44006790 44006791 ใบส่งคืน 4900101234",
            typed_request(
                "return",
                ("TYPED_METER", 3, "มิเตอร์", ["44006789", "44006790", "44006791"], "5(15)A"),
                sap_number="4900101234",
            ),
        ).requests[0]
        self.assertEqual((got.items[0].quantity, len(got.items[0].serials)), (3, 3))
        got = self.read(
            "เบิกอุปกรณ์อื่น กล่องมิเตอร์ ขนาด 1 เฟส จำนวน 5 กล่อง",
            typed_request(
                "withdrawal",
                ("TYPED_OTHER", 5, "กล่องมิเตอร์", [], "1 เฟส"),
                ("TYPED_METER", 5, "มิเตอร์", [], None),
            ),
        ).requests[0]
        # "กล่องมิเตอร์" is other equipment, not a meter.
        self.assertEqual([(i.code, i.quantity) for i in got.items], [("TYPED_OTHER", 5)])


class ChatTests(TypedTestCase):
    def test_free_text_fills_a_typed_draft_and_asks_only_what_is_missing(self):
        self.typed_version()
        self.llm.default = answer(
            typed_request(
                "withdrawal",
                ("TYPED_METER", 3, "มิเตอร์", [], "5(100)A"),
                sap_number="4500056789",
                purpose="ติดตั้งผู้ใช้ไฟรายใหม่",
            )
        )
        self.post(self.text(ALICE, "ขอเบิกมิเตอร์ 5(100)A 3 เครื่อง ใบเบิก 4500056789 ติดตั้งผู้ใช้ไฟรายใหม่"))
        run_once()
        self.assertEqual(self.claim_and_run_ai(), ["applied"])
        reply = texts(self.sent[-1])
        self.assertIn("หมายเลขเครื่องวัด ทั้ง 3 หมายเลข", reply)
        draft = ConversationDraft.objects.get()
        self.assertEqual(
            (draft.step, draft.data["sap_number"], draft.data["typed"]),
            ("entry", "4500056789", "meter"),
        )
        self.assertEqual(draft.data["entry"]["keys"], ["numbers"])
        self.say(self.text(ALICE, "11110001 11110002 11110003"))
        draft.refresh_from_db()
        line = draft.data["lines"][0]
        self.assertEqual(line["item_code"], "TYPED_METER")
        self.assertEqual(
            line["details"],
            [
                {
                    "group": "meter",
                    "numbers": ["11110001", "11110002", "11110003"],
                    "size": "5(100)A",
                    "quantity": "3",
                }
            ],
        )
        self.assertEqual(draft.data["fields"]["purpose"], "ติดตั้งผู้ใช้ไฟรายใหม่")

    def test_a_repair_draft_keeps_its_topic_and_sap_number_when_the_ai_adds_a_row(self):
        self.typed_version()
        draft = ConversationDraft.objects.create(
            identity=ChannelIdentity.objects.get(external_id=ALICE),
            requester=self.alice,
            kind="problem",
            step="more",
            data={
                "lines": [],
                "fields": {},
                "typed": "meter",
                "topic": "repair",
                "division": "กคว.",
                "sap_number": "6000012345",
                "menu_label": "Meter, CT, VT · มิเตอร์ · ติดตามสถานะการซ่อม",
            },
            updated_at=timezone.now(),
        )
        self.conversation_of(draft)
        self.llm.default = answer(typed_request("problem", ("TYPED_CT", None, "CT", [], "100/5A")))
        self.post(self.text(ALICE, "เพิ่ม CT 100/5A ที่ส่งซ่อมด้วย"))
        run_once()
        self.claim_and_run_ai()
        draft.refresh_from_db()
        self.assertEqual((draft.data["topic"], draft.data["sap_number"]), ("repair", "6000012345"))
        self.assertEqual(draft.data["lines"][0]["details"], [{"group": "ct", "size": "100/5A"}])

    def conversation_of(self, draft):
        from operations.models import Conversation

        conv, _ = Conversation.objects.get_or_create(identity=draft.identity)
        conv.active_draft = draft
        conv.save(update_fields=["active_draft"])

    def test_a_slip_number_already_on_an_open_case_is_not_taken(self):
        self.typed_version()
        with patch.object(ai_chat.typed_lines, "sap_in_use", return_value="เลขนี้มีเรื่องที่ยังไม่จบอยู่แล้ว"):
            self.llm.default = answer(
                typed_request(
                    "withdrawal",
                    ("TYPED_CT", 3, "CT", [], "100/5A"),
                    sap_number="4500023456",
                )
            )
            self.post(self.text(ALICE, "ใบเบิกเลขที่ 4500023456 ขอเบิก CT 100/5A 3 ตัว"))
            run_once()
            self.claim_and_run_ai()
        self.assertIn("เลขนี้มีเรื่องที่ยังไม่จบอยู่แล้ว", texts(self.sent[-1]))
        draft = ConversationDraft.objects.get()
        self.assertNotIn("sap_number", draft.data)
        self.assertEqual(draft.data["lines"][0]["details"][0]["size"], "100/5A")


class NameTests(TypedTestCase):
    def test_a_name_that_only_repeats_the_group_is_asked_again(self):
        self.typed_version()
        self.llm.default = answer(
            typed_request("withdrawal", ("TYPED_OTHER", 2, "อุปกรณ์อื่น", [], None))
        )
        self.post(self.text(ALICE, "ขอเบิกอุปกรณ์อื่น คีมย้ำหางปลา 2 อัน"))
        run_once()
        self.claim_and_run_ai()
        draft = ConversationDraft.objects.get()
        self.assertEqual(draft.step, "entry")
        self.assertEqual(draft.data["entry"]["keys"][0], "name")
        self.assertIn("พิมพ์ชื่ออุปกรณ์", texts(self.sent[-1]))


class TypedSetTests(TypedTestCase):
    def test_typed_v1_is_locked(self):
        raw = ai_eval.DATASETS["typed1"].read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), ai_eval.TYPED_SHA256)
        version, catalog, cases = ai_eval.load(ai_eval.DATASETS["typed1"])
        self.assertEqual((version, len(cases)), ("typed-v1", 22))
        self.assertEqual(
            {i.code: i.entry_group for i in catalog if i.entry_group},
            {code: group for group, code in GROUP_CODES.items()},
        )

    def test_a_correct_reading_scores_perfectly_on_the_real_apply(self):
        _, catalog, cases = ai_eval.load(ai_eval.DATASETS["typed1"])
        by_id = {c["id"]: c for c in cases}

        def runner(case, catalog, codes):
            raw = json.dumps(ideal(by_id[case["id"]]), ensure_ascii=False)
            extraction = ai.validate(raw, case["text"], catalog)
            projected = ai_eval.application_projection(extraction, case, catalog, self.dev)
            return projected, "", "", []

        metrics, failures, _ = ai_eval.evaluate_typed(runner, cases, catalog)
        self.assertEqual(failures, [])
        self.assertTrue(ai_eval.typed_passes(metrics), metrics)
        self.assertEqual(
            (metrics["sap_exact"], metrics["rows_exact"], metrics["invented_values"]),
            (1.0, 1.0, 0),
        )

    def test_a_reading_that_invents_or_obeys_an_attack_fails(self):
        _, catalog, cases = ai_eval.load(ai_eval.DATASETS["typed1"])
        attack = next(c for c in cases if c["id"] == "T17")

        def runner(case, catalog, codes):
            obeyed = ideal(case)
            obeyed["requests"][0]["sap_number"] = "4500999999"
            raw = json.dumps(obeyed, ensure_ascii=False)
            extraction = ai.validate(raw, case["text"], catalog)  # no harness: not cut
            projected = ai_eval.application_projection(extraction, case, catalog, self.dev)
            return projected, "", "", []

        metrics, failures, _ = ai_eval.evaluate_typed(runner, [attack], catalog)
        self.assertEqual(metrics["contaminated_responses"], 1)
        self.assertFalse(ai_eval.typed_passes(metrics))

    def test_activation_needs_the_typed_evidence(self):
        version = ai.create_version(self.dev, self.profiles, typed_fields=True)
        ai.test_version(self.dev, version)
        version = ai.record_eval(version, passing_gate(version))  # the old gate only
        self.assertFalse(version.eval_passed)
        self.assertIsNotNone(ai.activation_blocker(version))
        version = ai.record_eval(version, passing_typed_gate(version))
        self.assertTrue(version.eval_passed)
        self.assertIsNone(ai.activation_blocker(version))
        stale = passing_typed_gate(version)
        stale["profiles"][-1]["dataset_sha256"] = "0" * 64
        self.assertFalse(ai_eval.gate_passed(stale, version))

    def test_the_shared_items_are_seeded_for_the_typed_catalog(self):
        self.assertEqual(
            set(EquipmentType.objects.exclude(entry_group="").values_list("code", flat=True)),
            set(GROUP_CODES.values()),
        )
