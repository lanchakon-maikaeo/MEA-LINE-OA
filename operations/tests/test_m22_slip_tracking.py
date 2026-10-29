"""DED Center follows slips issued in SAP (ADR 0032, grill ขอบเขตระบบ 29 ต.ค. 2569).

Acceptance → tests: with ตัวเลือกระบบ old_stock_work off (the default) the division takes a slip
case whole with รับเรื่อง, nobody sees the stock buttons, the stock page or the quantity columns,
and turning the option on brings them back (WebTests); typed text never opens a เบิก or ส่งคืน
draft and gets the fixed reply with the equipment to follow, from the old buttons and the kind
step too, while the AI's reading of a typed slip is refused the same way (LineTests); a version
that knows DED Center follows slips needs the menu path and plays the locked dialogue-v2
(AiTests).
"""

import hashlib
from decimal import Decimal
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.test import Client
from django.utils import timezone

from operations import (
    ai,
    ai_chat,
    ai_eval,
    conversation,
    line_menu_catalog,
    options,
    services,
    web_actions,
)
from operations.contracts import Actor
from operations.models import (
    AIConfigVersion,
    AICredential,
    AIProfile,
    Case,
    ChannelIdentity,
    Conversation,
    ConversationDraft,
    Role,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import ALICE, buttons, texts
from operations.tests.test_m13_line_menu import MenuTestCase
from operations.tests.test_m16_no_screening import NoScreeningTestCase


class WebTests(NoScreeningTestCase):
    def slip(self, sap):
        """A withdrawal with an item row, as the M07 per-line approval needs."""
        result = services.submit_case(
            Actor.for_requester(self.requester),
            idempotency_key=f"slip-{sap}",
            payload={
                "kind": "withdrawal",
                "division_code": "กคว.",
                "sap_number": sap,
                "purpose": "ติดตั้ง",
                "lines": [{"item_code": "MTR_SYN_A", "quantity": "1", "serials": []}],
            },
        )
        return Case.objects.get(pk=result.case_id)

    def take(self, client, case, key):
        return client.post(
            f"/cases/{case.number}/act/approve/",
            {
                "priority": "P3",
                "reason": "ตามที่กองตกลง",
                "revision": case.revision,
                "idempotency_key": key,
            },
        )

    def test_a_slip_case_is_taken_whole_and_the_stock_work_is_hidden(self):
        self.assertEqual(Case.Status.AWAITING_APPROVAL.label, "รอกองรับเรื่อง")
        case = self.slip("4500103201")
        self.assertTrue(case.lines.exists())
        offers = {o.name: o.label for o in web_actions.available(self.approver, case)}
        self.assertEqual(offers["approve"], "รับเรื่อง")
        self.assertEqual(offers["deny_approval"], "ไม่รับเรื่อง")
        client = Client()
        client.force_login(self.approver)
        form = client.get(f"/cases/{case.number}/act/approve/").content.decode()
        self.assertNotIn("จำนวนที่อนุมัติ", form)
        self.assertIn(self.take(client, case, "slip-take-1").status_code, (200, 302))
        case.refresh_from_db()
        self.assertEqual(case.status, "awaiting_execution")
        self.assertIsNone(case.lines.first().approved_quantity)
        offered = {o.name for o in web_actions.available(self.approver, case)}
        self.assertFalse(offered & web_actions.STOCK)
        page = client.get(f"/cases/{case.number}/").content.decode()
        self.assertIn("รายละเอียดตามใบ", page)
        self.assertNotIn(">ค้างจ่าย</th>", page)
        self.assertEqual(client.get(f"/cases/{case.number}/act/issue/").status_code, 404)

    def test_the_stock_pages_come_back_only_with_the_option(self):
        client = Client()
        client.force_login(self.flow_admin)
        self.assertEqual(client.get("/console/admin/inventory/").status_code, 404)
        self.assertNotIn("คลังของ", client.get("/console/admin/").content.decode())
        options.change(None, options.OLD_STOCK, "on", "ทดสอบงานคลังเดิม")
        self.assertEqual(client.get("/console/admin/inventory/").status_code, 200)
        case = self.slip("4500103202")
        approver = Client()
        approver.force_login(self.approver)
        form = approver.get(f"/cases/{case.number}/act/approve/").content.decode()
        self.assertIn("จำนวนที่อนุมัติ", form)  # the M07 per-line decision is back


class LineTests(MenuTestCase):
    def setUp(self):
        super().setUp()
        line_menu_catalog.sync()

    def slip_drafts(self):
        return ConversationDraft.objects.filter(
            kind__in=["withdrawal", "return"], status=ConversationDraft.Status.OPEN
        )

    def test_a_typed_withdrawal_gets_the_fixed_reply_and_the_equipment_to_follow(self):
        reply = self.type(ALICE, "ขอเบิกมิเตอร์ 1 เฟส 5 ลูก")
        self.assertIn(conversation.SLIP_REFUSAL, texts(reply))
        self.assertIn("SAP", texts(reply))
        self.assertIn("มิเตอร์", buttons(reply))
        self.assertFalse(self.slip_drafts().exists())
        self.assertIn("ติดตามใบเบิก", buttons(self.press(ALICE, "มิเตอร์")))

    def test_a_return_naming_no_equipment_offers_the_equipment_tiles(self):
        reply = self.type(ALICE, "อยากคืนของที่ชำรุด")
        self.assertIn(conversation.SLIP_REFUSAL, texts(reply))
        self.assertIn("Meter, CT, VT", buttons(reply))
        self.assertFalse(self.slip_drafts().exists())

    def test_the_old_buttons_the_typed_menu_word_and_the_kind_step(self):
        reply = self.say(self.postback(ALICE, "m=equipment&kind=withdrawal"))
        self.assertIn(conversation.SLIP_REFUSAL, texts(reply))
        self.assertIn("เลือกอุปกรณ์ที่ต้องการติดตาม", texts(self.type(ALICE, "ติดตามใบเบิก")))
        reply = self.say(self.postback(ALICE, "m=new"))
        self.assertIn("ติดตามใบส่งคืน", buttons(reply))
        reply = self.press(ALICE, "ติดตามใบส่งคืน")
        self.assertIn("เลือกอุปกรณ์ที่ต้องการติดตาม", texts(reply))
        self.assertFalse(self.slip_drafts().exists())
        self.assertFalse(
            ConversationDraft.objects.filter(status=ConversationDraft.Status.OPEN).exists()
        )

    def test_the_ai_reading_of_a_typed_slip_opens_no_draft(self):
        identity = ChannelIdentity.objects.get(requester=self.requester)
        chat, _ = Conversation.objects.get_or_create(identity=identity)
        turn = conversation.Turn(None, {}, identity, chat, timezone.now())
        turn.requester = self.requester
        withdrawal = ai.Proposal(
            "withdrawal", [ai.ProposedItem("TYPED_METER", "มิเตอร์", Decimal("5"), [])]
        )
        problem = ai.Proposal("problem", fields={"description": "ระบบ SMG ดึงข้อมูลไม่ได้"})
        run = SimpleNamespace(pk=None, result={})  # what the evaluation passes
        ai_chat.apply(turn, None, ai.Extraction([withdrawal], []), run)
        self.assertIn(conversation.SLIP_REFUSAL, texts(turn.replies))
        self.assertFalse(self.slip_drafts().exists())
        turn.replies.clear()
        ai_chat.apply(turn, None, ai.Extraction([withdrawal, problem], []), run)
        self.assertIn(conversation.SLIP_REFUSAL, texts(turn.replies))  # the rest goes on
        self.assertTrue(ConversationDraft.objects.filter(kind="problem").exists())
        self.assertFalse(self.slip_drafts().exists())


class AiTests(NoScreeningTestCase):
    def test_a_version_knowing_slips_needs_the_menu_path_and_plays_dialogue_v2(self):
        dev = make_staff("slip-dev", (Role.ADMIN, None), (Role.DEVELOPER_ADMIN, None))
        credential = AICredential.objects.create(
            name="fake", base_url="https://api.opentyphoon.ai/v1", encrypted_key="x", created_by=dev
        )
        profile = AIProfile.objects.create(
            credential=credential, model_id="fake-thai-8b", created_by=dev
        )
        with self.assertRaises(ValidationError):
            ai.create_version(dev, [profile], typed_fields=True, slip_facts=True)
        version = ai.create_version(
            dev, [profile], typed_fields=True, menu_fields=True, slip_facts=True
        )
        self.assertEqual(ai.eval_gate_for(version), ai.SLIP_EVAL_GATE)
        self.assertEqual(ai_eval.dialogue_sets(version), ("dialogue2",))
        older = AIConfigVersion(typed_fields=True, menu_fields=True)
        self.assertEqual(ai.eval_gate_for(older), ai.MENU_EVAL_GATE)
        path = ai_eval.DATASETS["dialogue2"]
        self.assertEqual(
            hashlib.sha256(path.read_bytes()).hexdigest(), ai_eval.DIALOGUE_LOCKS["dialogue2"]
        )
        dataset, _, dialogues = ai_eval.load(path)
        self.assertEqual(dataset, "dialogue-v2")
        refusals = [d for d in dialogues if d["id"].startswith("S")]
        self.assertTrue(all(d["expected"] == [] for d in refusals))
        self.assertTrue(all(conversation.SLIP_REFUSAL in d["reply_has"]["0"] for d in refusals))
