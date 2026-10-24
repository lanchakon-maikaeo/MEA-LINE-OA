"""Real MCP session plus LINE confirmation, authority and fallback regressions."""

import json
from unittest.mock import patch

from django.core.exceptions import PermissionDenied
from django.test import override_settings

from operations import agent_chat, agent_mcp, ai_chat, conversation, identity, services
from operations.contracts import Actor
from operations.models import AIRun, Case, ChannelIdentity, Conversation, Role
from operations.tests.test_m02_cases import make_staff, withdrawal
from operations.tests.test_m03_line import ALICE, BOB, buttons, texts
from operations.tests.test_m05_ai import AITestCase


class AgentTestCase(AITestCase):
    def setUp(self):
        super().setUp()
        setting = override_settings(AI_MCP_ENABLED=True)
        setting.enable()
        self.addCleanup(setting.disable)
        self.requester = self.alice
        self.line = ChannelIdentity.objects.get(external_id=ALICE)
        Conversation.objects.get_or_create(identity=self.line)

    def tools(self):
        return agent_mcp.Tools(self.line.pk)


class MCPTests(AgentTestCase):
    def agent_reply(self, message, selected):
        self.llm.default = {
            "choices": [{"message": {"content": json.dumps(selected, ensure_ascii=False)}}]
        }
        self.type(ALICE, message)
        run = AIRun.objects.latest("pk")
        with patch.object(conversation, "send_reply") as send:
            ai_chat.process(run.pk)
        run.refresh_from_db()
        reply = send.call_args.args[1]
        conversation._remember_choices(run.inbox, reply)
        return run, reply

    def test_greeting_with_capability_question_cannot_become_an_execution_proposal(self):
        self.line.staff_user = make_staff("chat-staff", (Role.STAFF, "SIM-MTR"))
        self.line.save(update_fields=["staff_user"])
        run, reply = self.agent_reply(
            "สวัสดี ทำอะไรได้บ้าง",
            {"tool": "prepare_action", "arguments": {"command": "บริการ"}},
        )
        self.assertEqual(run.status, AIRun.Status.ANSWERED)
        self.assertIn("เบิกและส่งคืนอุปกรณ์", texts(reply))
        self.assertIn("ดูงานของกอง", texts(reply))
        self.assertNotIn("สั่งงานตามสิทธิ์ Supervisor", texts(reply))
        self.assertNotIn("ตรวจคำสั่งก่อนดำเนินการ", texts(reply))
        self.assertNotIn("ยืนยัน", buttons(reply))
        self.assertFalse(Case.objects.exists())

    def test_external_capabilities_do_not_offer_withdrawal_or_staff_actions(self):
        self.requester.is_external = True
        self.requester.save(update_fields=["is_external"])
        _, reply = self.agent_reply("ช่วยอะไรได้บ้าง", None)
        self.assertIn("แจ้งปัญหา", texts(reply))
        self.assertNotIn("เบิกและส่งคืน", texts(reply))
        self.assertNotIn("ดูงานของกอง", texts(reply))

    def test_enabled_agent_answers_before_keyword_template(self):
        with patch("operations.answers.keyword_answer") as keyword:
            run, reply = self.agent_reply(
                "ต้องเริ่มใช้งานอย่างไร",
                {"reply": "เลือกบริการที่ต้องการ หรือพิมพ์รายละเอียดเพื่อเริ่มแจ้งเรื่อง"},
            )
        keyword.assert_not_called()
        self.assertEqual(run.status, AIRun.Status.ANSWERED)
        self.assertIn("เลือกบริการ", texts(reply))

    def test_service_question_and_followup_use_conversation_without_creating_draft(self):
        first, reply = self.agent_reply(
            "ต้องเริ่มใช้งานอย่างไร",
            {"reply": "สามารถเลือกบริการที่ต้องการ หรือพิมพ์รายละเอียดเพื่อเริ่มแจ้งเรื่อง"},
        )
        self.assertEqual(first.status, AIRun.Status.ANSWERED)
        self.assertNotIn("ยืนยัน", buttons(reply))
        second, _ = self.agent_reply(
            "แล้วถ้าจะติดตามต่อทำอย่างไร",
            {"reply": "เลือกติดตามเรื่องของฉันเพื่อดูเรื่องที่ท่านแจ้งไว้"},
        )
        payload = json.loads(self.llm.calls[-1]["body"]["messages"][1]["content"])
        self.assertEqual(payload["history"][0]["message"], "ต้องเริ่มใช้งานอย่างไร")
        self.assertEqual(payload["history"][0]["reply"], first.result["agent_reply"])
        self.assertEqual(second.status, AIRun.Status.ANSWERED)
        self.assertIsNone(Conversation.objects.get(identity=self.line).active_draft_id)
        self.assertFalse(Case.objects.exists())

    def test_changed_account_cannot_reuse_previous_agent_dialogue(self):
        self.agent_reply("ช่วยอะไรได้บ้าง", None)
        replacement = self.approve(BOB)
        ChannelIdentity.objects.filter(pk=self.line.pk).update(requester=replacement)
        self.agent_reply("ต้องเริ่มใช้งานอย่างไร", {"reply": "เลือกบริการที่ต้องการ"})
        payload = json.loads(self.llm.calls[-1]["body"]["messages"][1]["content"])
        self.assertEqual(payload["history"], [])

    def test_opening_service_menu_does_not_ask_for_confirmation(self):
        run, reply = self.agent_reply(
            "ช่วยเปิดหน้าบริการ",
            {"tool": "prepare_action", "arguments": {"command": "บริการ"}},
        )
        self.assertNotIn("ตรวจคำสั่งก่อนดำเนินการ", texts(reply))
        self.assertNotIn("ยืนยัน", buttons(reply))
        self.assertIn("เบิกอุปกรณ์", buttons(reply))
        self.assertNotIn("agent_proposal", run.result)
        self.assertFalse(Case.objects.exists())

    def test_conversational_reply_cannot_claim_completed_approval_or_other_case(self):
        for written in ("อนุมัติแล้ว", "เปิดเรื่อง W69-999999 ให้แล้ว", "กรุณาเข้า https://example.com"):
            with self.subTest(written=written):
                self.llm.default = {
                    "choices": [{"message": {"content": json.dumps({"reply": written})}}]
                }
                self.type(ALICE, "ช่วยอธิบายขั้นตอนเพิ่มเติม")
                run = AIRun.objects.latest("pk")
                result, attempts = agent_chat.plan(run, "ช่วยอธิบายขั้นตอนเพิ่มเติม", 10)
                self.assertIsNone(result, attempts)
                self.assertTrue(attempts)
        self.assertFalse(Case.objects.exists())

    def test_mcp_read_results_are_explained_by_model_and_stay_read_only(self):
        written = "พบรายการมิเตอร์ตามข้อมูลในระบบ สามารถเลือกรายการที่ต้องการได้"

        def respond(body):
            payload = json.loads(body["messages"][-1]["content"])
            selected = (
                {"reply": written}
                if "tool_result" in payload
                else {"tool": "list_equipment", "arguments": {"search": "มิเตอร์"}}
            )
            return {"choices": [{"message": {"content": json.dumps(selected, ensure_ascii=False)}}]}

        self.llm.by_model[self.models[0]] = respond
        self.llm.calls.clear()
        run, reply = self.agent_reply("ช่วยค้นหารายการมิเตอร์", None)
        self.assertEqual(len(self.llm.calls), 2)
        self.assertIn(written, texts(reply))
        self.assertEqual(run.result["agent_reply"], written)
        self.assertNotIn("ยืนยัน", buttons(reply))
        self.assertFalse(Case.objects.exists())
        self.assertIsNone(Conversation.objects.get(identity=self.line).active_draft_id)

    def test_equipment_lookup_uses_user_group_when_model_omits_search(self):
        tools = agent_mcp.Tools(self.line.pk, "ค้นหารายการมิเตอร์")
        result = tools.invoke("list_equipment")
        self.assertEqual(result["search"], "มิเตอร์")
        self.assertEqual([row["code"] for row in result["rows"]], ["MTR_SYN_A"])
        from operations.models import EquipmentType

        EquipmentType.objects.filter(code="MTR_SYN_A").update(line_group="ct")
        ct = agent_mcp.Tools(self.line.pk, "ค้นหา CT").invoke("list_equipment")
        self.assertEqual([row["code"] for row in ct["rows"]], ["MTR_SYN_A"])
        paged = self.tools().invoke("list_equipment", {"search": "CT"})
        self.assertEqual(paged["rows"], ct["rows"])
        self.assertEqual(
            agent_mcp.Tools(self.line.pk, "ค้นหา VT").invoke("list_equipment")["total"], 0
        )

    def test_real_mcp_scopes_cases_and_rejects_actor_override(self):
        mine = services.submit_case(
            Actor.for_requester(self.requester),
            idempotency_key="mcp-mine-001",
            payload=withdrawal(),
        )
        other = services.submit_case(
            Actor.for_requester(self.approve(BOB)),
            idempotency_key="mcp-other-01",
            payload=withdrawal(),
        )
        result = self.tools().invoke("list_cases")
        self.assertEqual([r["id"] for r in result["rows"]], [str(mine.case_id)])
        self.assertNotIn(str(other.case_id), json.dumps(result))
        with self.assertRaises(PermissionDenied):
            self.tools().invoke("list_cases", {"identity_id": self.line.pk})
        with self.assertRaises(PermissionDenied):
            self.tools().invoke("list_cases", {"scope": "staff"})
        with self.assertRaises(PermissionDenied):
            self.tools().invoke("execute_confirmed_action")

    def propose(self, command="ส่งเรื่องใหม่"):
        self.llm.default = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"tool": "prepare_action", "arguments": {"command": command}},
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }
        self.type(ALICE, "ช่วยเปิดรายการสำหรับส่งเรื่องให้หน่อย")
        run = AIRun.objects.latest("pk")
        with patch.object(conversation, "send_reply") as send:
            ai_chat.process(run.pk)
        reply = send.call_args.args[1]
        run.refresh_from_db()
        self.assertIn("ยืนยัน", buttons(reply), (texts(reply), run.status, run.attempts))
        conversation._remember_choices(run.inbox, reply)
        return run, reply

    def test_planner_proposes_and_real_mcp_confirmation_executes_once(self):
        run, reply = self.propose()
        self.assertIn("ตรวจคำสั่ง", texts(reply))
        self.assertFalse(Case.objects.exists())
        token = buttons(reply)["ยืนยัน"]
        accepted = self.say(self.postback(ALICE, token))
        run.refresh_from_db()
        self.assertEqual(run.result["agent_decision"], "confirmed")
        self.assertTrue(accepted)
        repeated = self.say(self.postback(ALICE, token))
        self.assertIn("ไปแล้ว", texts(repeated))

    def test_other_user_and_changed_context_cannot_confirm(self):
        run, reply = self.propose()
        self.approve(BOB)
        token = buttons(reply)["ยืนยัน"]
        self.say(self.postback(BOB, token))
        run.refresh_from_db()
        self.assertNotIn("agent_decision", run.result)
        Conversation.objects.filter(identity=self.line).update(prompt="track")
        self.assertIn("ขั้นตอนเปลี่ยน", texts(self.say(self.postback(ALICE, token))))
        run.refresh_from_db()
        self.assertNotIn("agent_decision", run.result)

    def test_cancel_never_executes(self):
        run, reply = self.propose()
        self.say(self.postback(ALICE, buttons(reply)["ยกเลิก"]))
        run.refresh_from_db()
        self.assertEqual(run.result["agent_decision"], "cancelled")
        self.assertIn("ไปแล้ว", texts(self.say(self.postback(ALICE, buttons(reply)["ยืนยัน"]))))

    def test_non_admin_cannot_prepare_approval(self):
        with self.assertRaises(PermissionDenied):
            self.tools().invoke("prepare_action", {"command": "approve 1"})

    def test_equipment_shortcuts_follow_current_requester_permissions(self):
        self.assertIn("เบิกอุปกรณ์", self.tools().invoke("get_context")["commands"])
        self.requester.is_external = True
        self.requester.save(update_fields=["is_external"])
        self.assertNotIn("เบิกอุปกรณ์", self.tools().invoke("get_context")["commands"])
        with self.assertRaises(PermissionDenied):
            self.tools().invoke("prepare_action", {"command": "เบิกอุปกรณ์"})
        self.assertEqual(
            self.tools().invoke("prepare_action", {"command": "แจ้งปัญหา"})["kind"], "proposal"
        )

    def test_revoked_staff_is_refused_on_confirmation(self):
        admin = make_staff("agent-admin", (Role.ADMIN, None))
        self.line.staff_user = admin
        self.line.save(update_fields=["staff_user"])
        run, reply = self.propose("รับแจ้งสมัคร เปิด")
        admin.is_active = False
        admin.save(update_fields=["is_active"])
        self.say(self.postback(ALICE, buttons(reply)["ยืนยัน"]))
        admin.staff_profile.refresh_from_db()
        self.assertFalse(admin.staff_profile.notify_registrations)

    def test_model_outage_preserves_guided_flow(self):
        with patch.object(
            agent_chat, "plan", return_value=(None, [{"outcome": "mcp unavailable"}])
        ):
            self.type(ALICE, "ขอเบิกฟิวส์ 2 ตัว")
            run = AIRun.objects.latest("pk")
            with patch.object(conversation, "send_reply") as send:
                ai_chat.process(run.pk)
        self.assertTrue(send.call_args.args[1])
        self.assertFalse(Case.objects.exists())

    def test_actual_admin_approval_waits_for_human_and_is_idempotent(self):
        self.line.staff_user = self.dev
        self.line.save(update_fields=["staff_user"])
        registration = identity.submit_registration("line", "mcp-applicant", "ผู้สมัคร ทดสอบ", "D01")
        run, reply = self.propose(f"approve {registration.pk}")
        registration.refresh_from_db()
        self.assertEqual(registration.status, "pending")
        token = buttons(reply)["ยืนยัน"]
        self.say(self.postback(ALICE, token))
        registration.refresh_from_db()
        self.assertEqual(registration.status, "approved")
        self.assertEqual(registration.decided_by, self.dev)
        self.assertIn("ไปแล้ว", texts(self.say(self.postback(ALICE, token))))

    def test_existing_withdrawal_summary_executes_via_confirmed_mcp(self):
        self.line.staff_user = make_staff("employee-requester", (Role.STAFF, "SIM-MTR"))
        self.line.save(update_fields=["staff_user"])
        with override_settings(AI_ENABLED=False):
            self.withdraw(ALICE)
        run, reply = self.propose("ยืนยันส่งเรื่อง")
        self.assertIn("เปลี่ยนมิเตอร์ชำรุด", texts(reply))
        self.assertFalse(Case.objects.exists())
        self.say(self.postback(ALICE, buttons(reply)["ยืนยัน"]))
        self.assertEqual(Case.objects.count(), 1)
        self.say(self.postback(ALICE, buttons(reply)["ยืนยัน"]))
        self.assertEqual(Case.objects.count(), 1)

    def test_mcp_schema_rejects_invalid_types_and_unknown_tools(self):
        for name, arguments in [
            ("list_cases", {"page": "1"}),
            ("list_equipment", {"search": []}),
            ("prepare_action", {"command": "approve 1", "confirmed": True}),
            ("shell", {}),
        ]:
            with self.assertRaises(PermissionDenied):
                self.tools().invoke(name, arguments)

    def test_mcp_callback_exception_rolls_back_changes(self):
        from django.db import transaction

        @transaction.atomic
        def fail():
            self.requester.display_name = "must rollback"
            self.requester.save(update_fields=["display_name"])
            raise ValueError("test failure")

        with self.assertRaises(PermissionDenied):
            agent_mcp.Tools(self.line.pk, execute=fail).invoke("execute_confirmed_action")
        self.requester.refresh_from_db()
        self.assertNotEqual(self.requester.display_name, "must rollback")

    def test_read_tools_paginate_and_case_detail_stays_scoped(self):
        for i in range(6):
            services.submit_case(
                Actor.for_requester(self.requester),
                idempotency_key=f"mcp-page-{i:04}",
                payload=withdrawal(),
            )
        result = self.tools().invoke("list_cases")
        self.assertEqual((result["total"], len(result["rows"])), (6, 5))
        second = self.tools().invoke("list_cases", {"page": 2})
        self.assertEqual(len(second["rows"]), 1)
        own = self.tools().invoke("get_case", {"number": result["rows"][0]["number"]})
        self.assertEqual(own["id"], result["rows"][0]["id"])
        other = services.submit_case(
            Actor.for_requester(self.approve(BOB)),
            idempotency_key="mcp-hidden-detail",
            payload=withdrawal(),
        )
        with self.assertRaises(PermissionDenied):
            self.tools().invoke("get_case", {"number": Case.objects.get(pk=other.case_id).number})
        with self.assertRaises(PermissionDenied):
            self.tools().invoke("admin_directory")

    def test_supervisor_command_keeps_final_order_confirmation(self):
        from operations.models import WorkOrder

        head = make_staff("agent-head", (Role.SUPERVISOR, "SIM-EQP"))
        self.line.staff_user = head
        self.line.save(update_fields=["staff_user"])
        run, reply = self.propose("ให้ทั้งกอง SIM-EQP ตรวจอุปกรณ์")
        self.assertFalse(WorkOrder.objects.exists())
        with patch("operations.staff_orders_chat.extract", return_value=None):
            draft = self.say(self.postback(ALICE, buttons(reply)["ยืนยัน"]))
        self.assertFalse(WorkOrder.objects.exists())
        self.assertIn("ยืนยัน", texts(draft))
        self.press(ALICE, "ยืนยัน", draft)
        self.assertEqual(WorkOrder.objects.count(), 1)

    def test_staff_only_account_uses_agent_queue_and_fallback(self):
        head = make_staff("agent-staff-only", (Role.SUPERVISOR, "SIM-EQP"))
        self.line.staff_user = head
        self.line.requester = None
        self.line.status = "pending"
        self.line.save(update_fields=["staff_user", "requester", "status"])
        with patch.object(agent_chat, "plan", return_value=(None, [])):
            self.type(ALICE, "ช่วยค้นงานที่ฉันต้องทำหน่อย")
            run = AIRun.objects.latest("pk")
            with patch.object(conversation, "send_reply") as send:
                ai_chat.process(run.pk)
        self.assertIn("งานของกอง", texts(send.call_args.args[1]))

    def test_provider_failure_uses_menu_without_executing(self):
        from operations import ai

        self.type(ALICE, "ช่วยค้นเรื่องที่ฉันแจ้งไว้หน่อย")
        run = AIRun.objects.latest("pk")
        with (
            patch.object(ai, "_post", side_effect=ai.AIError("offline")),
            patch.object(conversation, "send_reply") as send,
        ):
            ai_chat.process(run.pk)
        self.assertIn("ไม่สำเร็จ", texts(send.call_args.args[1]))
        self.assertTrue(buttons(send.call_args.args[1]))
        run.refresh_from_db()
        self.assertNotIn("agent_proposal", run.result)
        self.assertFalse(Case.objects.exists())

    def test_newer_message_makes_pending_agent_result_stale(self):
        self.type(ALICE, "ช่วยเปิดเมนูใหม่ให้หน่อย")
        old = AIRun.objects.latest("pk")
        self.type(ALICE, "เปลี่ยนเป็นค้นหาอุปกรณ์ให้หน่อย")
        with patch.object(agent_chat, "plan") as plan, patch.object(conversation, "send_reply"):
            self.assertEqual(ai_chat.process(old.pk), "stale")
        plan.assert_not_called()

    def test_relinked_requester_cannot_confirm_old_proposal(self):
        run, reply = self.propose()
        replacement = self.approve(BOB)
        ChannelIdentity.objects.filter(pk=self.line.pk).update(requester=replacement)
        self.assertIn("ขั้นตอนเปลี่ยน", texts(self.say(self.postback(ALICE, buttons(reply)["ยืนยัน"]))))
        run.refresh_from_db()
        self.assertNotIn("agent_decision", run.result)


def step(**decision):
    """One agent step in the JSON-schema shape: every field present, unused ones null."""
    full = {"action": "tool", "reply": None, "tool": None, "arguments": {}, **decision}
    return {"choices": [{"message": {"content": json.dumps(full, ensure_ascii=False)}}]}


class AgentLoopTests(AgentTestCase):
    """The agent as an LLM with tools in a loop (the course notebook's run_agent), kept to
    read tools, a few steps and the person's confirmation for every change."""

    def plan(self, message):
        self.type(ALICE, message)
        run = AIRun.objects.latest("pk")
        self.llm.calls.clear()
        trace = []
        result, attempts = agent_chat.plan(run, message, 20, trace.append)
        return result, attempts, trace

    def test_tools_are_offered_as_functions_and_steps_use_the_json_schema(self):
        tools = agent_mcp.function_tools(exclude={"get_context"})
        self.assertEqual(
            {t["function"]["name"] for t in tools},
            {t.name for t in agent_mcp.TOOLS} - {"get_context"},
        )
        self.assertTrue(all(t["type"] == "function" for t in tools))
        self.llm.default = step(action="intake")
        result, attempts, _ = self.plan("ขอเบิกฟิวส์ 2 ตัว")
        self.assertIsNone(result)
        self.assertEqual(attempts[-1]["outcome"], "mcp: intake flow")
        body = self.llm.calls[0]["body"]
        schema = body["response_format"]["json_schema"]["schema"]
        self.assertEqual(body["response_format"]["json_schema"]["name"], "line_agent")
        self.assertEqual(schema["properties"]["action"]["enum"], ["reply", "tool", "intake"])
        self.assertIn("list_cases", schema["properties"]["tool"]["enum"])
        self.assertEqual(
            set(schema["properties"]["arguments"]["required"]),
            {"page", "scope", "kind", "state", "number", "view", "search", "command"},
        )
        self.assertEqual(body["temperature"], float(self.profiles[0].temperature))
        self.assertIn('"type": "function"', body["messages"][0]["content"])

    def test_lookup_with_filters_then_reply_is_two_steps_and_read_only(self):
        mine = services.submit_case(
            Actor.for_requester(self.requester),
            idempotency_key="loop-closed-1",
            payload=withdrawal(),
        )
        Case.objects.filter(pk=mine.case_id).update(status=Case.Status.CLOSED)
        number = Case.objects.get(pk=mine.case_id).number
        self.llm.by_model[self.models[0]] = [
            step(
                tool="list_cases", arguments={"state": "closed", "kind": "withdrawal", "page": None}
            ),
            step(action="reply", reply=f"เรื่องเบิกที่จบแล้วมี 1 เรื่อง คือ {number}"),
        ]
        result, attempts, trace = self.plan("เรื่องเบิกที่จบแล้วของฉันมีอะไรบ้าง")
        self.assertEqual(result["kind"], "cases")
        self.assertEqual([row["number"] for row in result["rows"]], [number])
        self.assertEqual(result["reply"], f"เรื่องเบิกที่จบแล้วมี 1 เรื่อง คือ {number}")
        self.assertEqual([a["outcome"] for a in attempts], ["mcp: read", "agent: reply"])
        self.assertEqual([t["action"] for t in trace], ["tool", "reply"])
        # The second step sees the tool result as data, without internal IDs.
        seen = json.loads(self.llm.calls[1]["body"]["messages"][-1]["content"])["tool_result"]
        self.assertEqual(seen["tool"], "list_cases")
        self.assertNotIn(str(mine.case_id), json.dumps(seen))
        self.assertEqual(seen["result"]["rows"][0]["number"], number)

    def test_refused_tool_goes_back_to_the_model_which_explains(self):
        self.llm.by_model[self.models[0]] = [
            step(tool="list_cases", arguments={"scope": "staff"}),
            step(action="reply", reply="บัญชีนี้ดูได้เฉพาะเรื่องที่ท่านแจ้งเอง"),
        ]
        result, attempts, _ = self.plan("ขอดูงานของกองทั้งหมด")
        self.assertEqual(result, {"kind": "reply", "text": "บัญชีนี้ดูได้เฉพาะเรื่องที่ท่านแจ้งเอง"})
        self.assertEqual(attempts[0]["outcome"], "mcp: refused")
        seen = json.loads(self.llm.calls[1]["body"]["messages"][-1]["content"])["tool_result"]
        self.assertIn("error", seen["result"])

    def test_loop_stops_at_the_step_limit_and_shows_server_rows(self):
        self.llm.by_model[self.models[0]] = [
            step(tool="list_equipment", arguments={"search": "ฟิวส์"}),
            step(tool="list_equipment", arguments={"search": "มิเตอร์"}),
            step(tool="list_equipment", arguments={"search": "สายไฟ"}),
            step(tool="list_equipment", arguments={"search": "เบรกเกอร์"}),
        ]
        result, attempts, _ = self.plan("มีอุปกรณ์อะไรบ้าง")
        self.assertEqual(len(self.llm.calls), agent_chat.MAX_STEPS)
        self.assertEqual(result["kind"], "equipment")
        self.assertNotIn("reply", result)
        self.assertEqual(attempts[-1]["outcome"], "mcp: prepared")

    def test_repeated_tool_call_ends_the_loop(self):
        self.llm.default = step(tool="list_equipment", arguments={"search": "ฟิวส์"})
        result, attempts, _ = self.plan("ค้นฟิวส์")
        self.assertEqual(len(self.llm.calls), 2)
        self.assertEqual(result["kind"], "equipment")
        self.assertIn("agent: repeated tool call", [a["outcome"] for a in attempts])

    def test_invalid_step_answer_falls_back_to_the_next_model(self):
        self.llm.by_model[self.models[0]] = step(action="reply", reply="อนุมัติแล้ว")
        self.llm.by_model[self.models[1]] = step(action="reply", reply="เลือกบริการที่ต้องการจากเมนู")
        result, attempts, _ = self.plan("ต้องทำอย่างไรต่อ")
        self.assertEqual(result["text"], "เลือกบริการที่ต้องการจากเมนู")
        self.assertEqual(
            [a["outcome"] for a in attempts], ["agent: unavailable or invalid", "agent: reply"]
        )

    def test_prepare_action_still_ends_the_turn_with_a_confirmation(self):
        self.llm.default = step(tool="prepare_action", arguments={"command": "ยืนยันส่งเรื่อง"})
        self.line.staff_user = make_staff("loop-employee", (Role.STAFF, "SIM-MTR"))
        self.line.save(update_fields=["staff_user"])
        with override_settings(AI_ENABLED=False):
            self.withdraw(ALICE)
        result, attempts, _ = self.plan("ช่วยส่งเรื่องนี้ให้หน่อย")
        self.assertEqual(result, {"kind": "proposal", "command": "ยืนยันส่งเรื่อง"})
        self.assertEqual(len(self.llm.calls), 1)
        self.assertFalse(Case.objects.exists())

    def test_case_filters_scope_and_paging_keep_them(self):
        for i, kind in enumerate(("withdrawal", "withdrawal", "problem")):
            submitted = services.submit_case(
                Actor.for_requester(self.requester),
                idempotency_key=f"loop-filter-{i}",
                payload=withdrawal(),
            )
            Case.objects.filter(pk=submitted.case_id).update(kind=kind)
        tools = self.tools()
        self.assertEqual(tools.invoke("list_cases", {"kind": "problem"})["total"], 1)
        self.assertEqual(tools.invoke("list_cases", {"state": "closed"})["total"], 0)
        self.assertEqual(tools.invoke("list_cases", {"state": "all"})["total"], 3)
        with self.assertRaises(PermissionDenied):
            tools.invoke("list_cases", {"state": "deleted"})
        result = tools.invoke("list_cases", {"kind": "withdrawal", "state": "all"})
        self.assertEqual((result["state"], result["case_kind"]), ("all", "withdrawal"))

    def test_walkthrough_prints_each_step_and_keeps_nothing(self):
        from io import StringIO

        from django.core.management import call_command

        from operations.models import Requester

        self.llm.by_model[self.models[0]] = [
            step(tool="list_equipment", arguments={"search": "มิเตอร์"}),
            step(action="reply", reply="พบมิเตอร์ตามข้อมูลในระบบ"),
        ]
        before = (Requester.objects.count(), AIRun.objects.count(), Case.objects.count())
        out = StringIO()
        call_command("agent_walkthrough", "--message", "ช่วยค้นรายการมิเตอร์", stdout=out)
        printed = out.getvalue()
        self.assertIn("[step 0] → list_equipment(search='มิเตอร์')", printed)
        self.assertIn("[step 1] → reply", printed)
        self.assertIn("ตอบ: พบมิเตอร์ตามข้อมูลในระบบ", printed)
        self.assertIn("Rolled back", printed)
        self.assertEqual(
            (Requester.objects.count(), AIRun.objects.count(), Case.objects.count()), before
        )
