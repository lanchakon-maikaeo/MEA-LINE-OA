"""Walk sample LINE messages through the real AI the way the course notebook's run_agent does:
each agent step and tool call is printed, then extraction or the chat answer when the agent
hands the message on. Synthetic account, demo database only, everything rolled back and
nothing sent to LINE. It calls the configured provider (a few calls per message)."""

import time
import uuid

from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.db import transaction
from django.test import override_settings

from operations import agent_chat, ai, ai_answer, answers, services
from operations.contracts import Actor
from operations.management.demo import DemoCommand
from operations.models import (
    AIConfigVersion,
    AIRun,
    ChannelIdentity,
    Conversation,
    District,
    Requester,
    Role,
    RoleGrant,
    StaffProfile,
)

MESSAGES = (
    "สวัสดี ทำอะไรได้บ้าง",
    "ช่วยค้นรายการมิเตอร์",
    "เรื่องที่ฉันแจ้งไว้ถึงไหนแล้ว",
    "ขอเบิกฟิวส์ 2 ชิ้น สำหรับซ่อมตู้ควบคุม",
    "ต้องรอนานแค่ไหนกว่าจะได้ของ",
)


class Rollback(Exception):
    pass


class Command(DemoCommand):
    help = "Print the LINE agent's steps, tool calls and answers for sample messages (rolled back)."

    def add_arguments(self, parser):
        parser.add_argument("--message", action="append", help="Message to try (repeatable)")
        parser.add_argument(
            "--preset",
            choices=sorted(ai.PRESETS),
            help="Try this preset's profile (e.g. Typhoon 2.5, JSON schema, temperature 0.4) "
            "instead of the active versions",
        )
        parser.add_argument("--budget", type=float, default=20, help="Seconds per message")

    def handle_demo(self, *args, message, preset, budget, **options):
        try:
            with override_settings(AI_ENABLED=True, AI_MCP_ENABLED=True), transaction.atomic():
                self._walk(message or MESSAGES, preset, budget)
                raise Rollback
        except Rollback:
            self.stdout.write(self.style.NOTICE("Rolled back: nothing was kept or sent."))

    def _walk(self, messages, preset, budget):
        extract, answer = self._versions(preset)
        line = self._account()
        mode = answers.mode() if answers.mode() in ("choose", "write") else "write"
        self.stdout.write(
            f"Agent and extraction: v{extract.number} "
            f"({' → '.join(self._profile(p) for p in extract.chain())})\n"
            f"Chat answers: {f'v{answer.number} ({mode})' if answer else 'none'}\n"
        )
        for text in messages:
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n» {text}"))
            run = AIRun(identity=line, config_version=extract, result={})
            started = time.monotonic()
            result, attempts = agent_chat.plan(run, text, budget, self._step)
            if result is not None:
                self._result(result)
            else:
                self.stdout.write("  agent: intake flow")
                self._intake(text, extract, answer, line.requester, mode, budget)
            outcomes = ", ".join(a.get("outcome", "") for a in attempts)
            self.stdout.write(f"  [{int((time.monotonic() - started) * 1000)} ms] {outcomes}")

    def _versions(self, preset):
        if not preset:
            extract = ai.active_version()
            if extract is None:
                raise CommandError("No active extraction version; pass --preset.")
            return extract, ai_answer.active_version()
        # A throwaway developer-admin inside the rolled-back transaction makes the candidates.
        dev = get_user_model().objects.create_user(username="walkthrough-dev")
        StaffProfile.objects.create(user=dev, display_name="Walkthrough")
        RoleGrant.objects.create(user=dev, role=Role.DEVELOPER_ADMIN)
        RoleGrant.objects.create(user=dev, role=Role.ADMIN)
        try:
            extract = ai.create_preset_version(dev, preset, AIConfigVersion.Pipeline.HARNESS)
            answer = ai.create_preset_version(
                dev, preset, AIConfigVersion.Pipeline.SINGLE, task=AIConfigVersion.Task.ANSWER
            )
        except Exception as exc:  # noqa: BLE001 - show the admin-page message as it is
            raise CommandError(str(exc)) from exc
        return extract, answer

    def _account(self):
        district = District.objects.order_by("pk").first()
        if district is None:
            raise CommandError("Load districts first (make seed-demo).")
        approver = get_user_model().objects.create_user(username="walkthrough-approver")
        requester = Requester.objects.create(
            display_name="ผู้ใช้ทดลอง ระบบ", district=district, approved_by=approver
        )
        line = ChannelIdentity.objects.create(
            channel=ChannelIdentity.Channel.LINE,
            external_id="U" + uuid.uuid4().hex,
            requester=requester,
            status=ChannelIdentity.Status.ACTIVE,
        )
        Conversation.objects.create(identity=line)
        try:
            with transaction.atomic():
                services.submit_case(
                    Actor.for_requester(requester),
                    idempotency_key="walkthrough-problem-1",
                    payload={"kind": "problem", "description": "ไฟส่องสว่างหน้าอาคารดับ"},
                )
        except Exception:  # noqa: BLE001 - a sample case is optional
            self.stdout.write("(no sample case: this database needs more for a problem report)")
        return line

    @staticmethod
    def _profile(profile):
        schema = "JSON schema" if profile.use_json_schema else "schema in prompt"
        return f"{profile.model_id} t={profile.temperature} {schema}"

    def _step(self, event):
        if event["action"] == "tool":
            shown = ", ".join(f"{k}={v!r}" for k, v in event["arguments"].items())
            what = f"→ {event['tool']}({shown})"
        elif event["action"] == "reply":
            what = "→ reply"
        else:
            what = "→ intake"
        self.stdout.write(f"  [step {event['step']}] {what}  ({event['latency_ms']} ms)")

    def _result(self, result):
        kind = result["kind"]
        if kind == "reply":
            self.stdout.write(f"  ตอบ: {result['text']}")
            return
        if kind in agent_chat.SHOWN:
            self.stdout.write(f"  ผล {kind}: {result['total']} รายการ")
            if result.get("reply"):
                self.stdout.write(f"  ตอบ: {result['reply']}")
            return
        self.stdout.write(f"  ผล {kind}: {result}")

    def _intake(self, text, extract, answer, requester, mode, budget):
        extraction, attempts = ai.extract(text, None, extract, budget)
        requests = extraction.requests if extraction else []
        proposals = [p for p in requests if p.intent in ai.KIND_FOR_INTENT]
        if proposals:
            for proposal in proposals:
                items = ", ".join(f"{i.code or i.text}×{i.quantity or '?'}" for i in proposal.items)
                fields = ", ".join(f"{k}={v!r}" for k, v in proposal.fields.items())
                self.stdout.write(f"  ร่าง {proposal.intent}: {items or '-'} {fields}")
            return
        self.stdout.write(
            f"  extraction: no request ({', '.join(a.get('outcome', '') for a in attempts)})"
        )
        if answer is None:
            return
        result = ai_answer.answer(
            text, list(answers.approved()), answers.facts(requester), [], answer, mode, budget
        )
        if result and result["key"]:
            self.stdout.write(
                f"  คำตอบ {result['key']}: {result['reply'] or '(ข้อความที่รับรองแล้ว)'}"
                + (f"  [ไม่ใช้ข้อความ AI: {result['refused']}]" if result["refused"] else "")
            )
        else:
            self.stdout.write("  chat answer: unavailable")
