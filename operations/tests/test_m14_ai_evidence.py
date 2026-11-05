"""Signed AI evaluation evidence carried to production (user, 24 Oct 2569, option ก)."""

import json
import os
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.urls import reverse

from operations import ai, ai_eval, ai_evidence, ai_release, answers
from operations.models import (
    AIConfigVersion,
    AICredential,
    AIExample,
    AIModel,
    AuditEvent,
)
from operations.tests.test_m14_typed_ai import TypedTestCase, passing_typed_gate

PEM, PUBLIC = ai_evidence.new_private_key()
OTHER_PEM, OTHER_PUBLIC = ai_evidence.new_private_key()


@override_settings(AI_EVIDENCE_PUBLIC_KEYS=(PUBLIC,))
class EvidenceTestCase(TypedTestCase):
    def evaluated(self, few_shot=0):
        version = ai.create_version(self.dev, self.profiles, few_shot=few_shot, typed_fields=True)
        ai.test_version(self.dev, version)
        return ai.record_eval(version, passing_typed_gate(version))

    def exported(self, version, pem=PEM):
        return json.dumps(ai_evidence.export(version, pem), ensure_ascii=False).encode()

    def as_on_production(self, version):
        """Production has no version with this configuration yet."""
        ai.reject(self.dev, version, "จำลองเครื่องที่ยังไม่มีรุ่นนี้")


class ImportTests(EvidenceTestCase):
    def test_imported_evidence_makes_a_candidate_that_still_needs_a_test_here(self):
        source = self.evaluated()
        raw = self.exported(source)
        self.as_on_production(source)
        version, warning = ai_evidence.import_evidence(self.dev, raw, "ผลประเมินจากเครื่องที่ประเมิน")
        self.assertNotEqual(version.pk, source.pk)
        self.assertEqual(version.status, AIConfigVersion.Status.CANDIDATE)
        self.assertIn(f"v{source.number}", version.note)
        self.assertEqual(ai_evidence.portable_digest(version), ai_evidence.portable_digest(source))
        self.assertTrue(version.eval_passed)
        self.assertEqual(warning, "")
        self.assertIn("connection test", ai.activation_blocker(version))
        ai.test_version(self.dev, version)
        version.refresh_from_db()
        self.assertIsNone(ai.activation_blocker(version))
        ai.activate(self.dev, version, "เปิดรุ่นที่นำเข้าผลประเมิน")
        event = AuditEvent.objects.get(action="ai.evidence_imported")
        self.assertEqual(event.data["source_version"], source.number)
        self.assertEqual(len(event.data["file_sha256"]), 64)
        self.assertTrue(event.data["examples_match"])

    def test_a_version_with_the_same_settings_takes_the_evidence(self):
        source = self.evaluated()
        raw = self.exported(source)
        AIConfigVersion.objects.filter(pk=source.pk).update(eval_passed=None, eval_result={})
        version, _ = ai_evidence.import_evidence(self.dev, raw, "นำเข้า")
        self.assertEqual(version.pk, source.pk)
        self.assertTrue(ai_eval.gate_passed(version.eval_result, version))

    def test_row_ids_and_numbers_do_not_change_the_configuration(self):
        source = self.evaluated()
        copies = [
            ai.create_profile(
                self.dev,
                p.credential,
                p.model_id,
                p.timeout_seconds,
                p.max_tokens,
                p.temperature,
                p.use_json_schema,
            )
            for p in source.chain()
        ]
        twin = ai.create_version(self.dev, copies, typed_fields=True)
        self.assertNotEqual(ai_eval.config_digest(twin), ai_eval.config_digest(source))
        self.assertEqual(ai_evidence.portable_digest(twin), ai_evidence.portable_digest(source))
        other = ai.create_version(self.dev, copies, few_shot=1, typed_fields=True)
        self.assertNotEqual(ai_evidence.portable_digest(other), ai_evidence.portable_digest(source))

    def test_different_examples_import_with_a_warning(self):
        source = self.evaluated(few_shot=2)
        raw = self.exported(source)
        self.as_on_production(source)
        AIExample.objects.create(text="ตัวอย่างจริงที่ตรวจแล้ว", draft=None, source="review")
        version, warning = ai_evidence.import_evidence(self.dev, raw, "นำเข้า")
        self.assertIn("ต่างจากตอนประเมิน", warning)
        self.assertTrue(version.eval_passed)
        self.assertFalse(
            AuditEvent.objects.get(action="ai.evidence_imported").data["examples_match"]
        )

    def test_the_settings_page_shows_where_the_evidence_came_from(self):
        source = self.evaluated()
        raw = self.exported(source)
        self.as_on_production(source)
        self.client.force_login(self.dev)
        response = self.client.post(
            reverse("admin-ai-action", args=["evidence"]),
            {"reason": "นำเข้าจากเว็บ", "evidence": SimpleUploadedFile("v.json", raw)},
        )
        version = AIConfigVersion.objects.order_by("-number").first()
        self.assertRedirects(
            response,
            f"{reverse('admin-ai')}?tab=releases#v{version.number}",
            fetch_redirect_response=False,
        )
        self.assertTrue(version.eval_passed)
        page = self.client.get(f"{reverse('admin-ai')}?tab=releases")
        self.assertContains(page, f"ผลประเมินของ v{source.number} อยู่ที่ v{version.number} บนเครื่องนี้")


class RefusalTests(EvidenceTestCase):
    def test_an_edited_report_breaks_the_signature(self):
        raw = self.exported(self.evaluated())
        data = json.loads(raw)
        data["bundle"]["report"]["profiles"][0]["metrics"]["intent_accuracy"] = 1.0
        data["bundle"]["report"]["note"] = "แก้ไขเอง"
        with self.assertRaisesMessage(ValidationError, "ลายเซ็น"):
            ai_evidence.import_evidence(self.dev, json.dumps(data).encode(), "นำเข้า")

    def test_only_keys_kept_here_are_trusted(self):
        raw = self.exported(self.evaluated(), pem=OTHER_PEM)
        with self.assertRaisesMessage(ValidationError, "ลายเซ็น"):
            ai_evidence.import_evidence(self.dev, raw, "นำเข้า")
        with override_settings(AI_EVIDENCE_PUBLIC_KEYS=()):
            with self.assertRaisesMessage(ValidationError, "ยังไม่ได้ตั้งกุญแจ"):
                ai_evidence.import_evidence(self.dev, raw, "นำเข้า")

    def test_evidence_of_other_code_is_refused_and_goes_stale_on_a_new_release(self):
        source = self.evaluated()
        raw = self.exported(source)
        self.as_on_production(source)
        with patch.object(ai_eval, "source_digest", return_value="0" * 64):
            with self.assertRaisesMessage(ValidationError, "โค้ดคนละรุ่น"):
                ai_evidence.import_evidence(self.dev, raw, "นำเข้า")
        version, _ = ai_evidence.import_evidence(self.dev, raw, "นำเข้า")
        ai.test_version(self.dev, version)
        version.refresh_from_db()
        with patch.object(ai_eval, "source_digest", return_value="0" * 64):
            self.assertIsNotNone(ai.activation_blocker(version))

    def test_an_edit_of_the_stored_evidence_blocks_the_switch(self):
        source = self.evaluated()
        raw = self.exported(source)
        self.as_on_production(source)
        version, _ = ai_evidence.import_evidence(self.dev, raw, "นำเข้า")
        ai.test_version(self.dev, version)
        result = AIConfigVersion.objects.get(pk=version.pk).eval_result
        self.assertTrue(ai_eval.gate_passed(result, version))
        result["at"] = "2000-01-01T00:00:00+00:00"
        self.assertFalse(ai_eval.gate_passed(result, version))

    def test_only_a_current_passing_evaluation_is_exported(self):
        version = ai.create_version(self.dev, self.profiles, typed_fields=True)
        with self.assertRaisesMessage(ValidationError, "no passing evaluation"):
            ai_evidence.export(version, PEM)
        source = self.evaluated()
        raw = self.exported(source)
        self.as_on_production(source)
        imported, _ = ai_evidence.import_evidence(self.dev, raw, "นำเข้า")
        with self.assertRaisesMessage(ValidationError, "no passing evaluation"):
            ai_evidence.export(imported, PEM)

    def test_not_evidence_or_other_tasks(self):
        with self.assertRaisesMessage(ValidationError, "ไม่ใช่ผลประเมิน"):
            ai_evidence.import_evidence(self.dev, b"{}", "นำเข้า")
        data = json.loads(self.exported(self.evaluated()))
        data["bundle"]["task"] = AIConfigVersion.Task.TRIAGE
        with self.assertRaisesMessage(ValidationError, "เฉพาะงานอ่านข้อความ"):
            ai_evidence.import_evidence(self.dev, json.dumps(data).encode(), "นำเข้า")


class SourceDigestTests(EvidenceTestCase):
    def test_a_missing_lock_file_fails_closed_without_breaking_the_settings_page(self):
        version = self.evaluated()
        is_file, read_bytes = Path.is_file, Path.read_bytes

        def no_lock(path):
            return path.name != "uv.lock" and is_file(path)

        def read(path):
            if path.name == "uv.lock":  # as in images built before 24 Oct 2569
                raise FileNotFoundError(path)
            return read_bytes(path)

        with (
            patch.object(Path, "is_file", autospec=True, side_effect=no_lock),
            patch.object(Path, "read_bytes", autospec=True, side_effect=read),
        ):
            digest = ai_eval.source_digest()
            self.assertNotEqual(version.eval_result["security"]["source_digest"], digest)
            self.assertFalse(ai_eval.gate_passed(version.eval_result, version))
            self.client.force_login(self.dev)
            self.assertEqual(self.client.get(reverse("admin-ai")).status_code, 200)


class CommandTests(EvidenceTestCase):
    def test_key_export_and_import_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "keys" / "evidence.pem"
            out = StringIO()
            call_command("make_ai_evidence_key", out=str(key), stdout=out)
            self.assertEqual(os.stat(key).st_mode & 0o777, 0o600)
            public = out.getvalue().split("AI_EVIDENCE_PUBLIC_KEYS=")[1].split()[0]
            with self.assertRaisesMessage(CommandError, "never overwritten"):
                call_command("make_ai_evidence_key", out=str(key))
            source = self.evaluated()
            evidence = Path(tmp) / "v.json"
            call_command(
                "export_ai_evidence",
                number=source.number,
                key=str(key),
                out=str(evidence),
                stdout=StringIO(),
            )
            self.as_on_production(source)
            out = StringIO()
            with override_settings(AI_EVIDENCE_PUBLIC_KEYS=(public,)):
                call_command(
                    "import_ai_evidence",
                    file=str(evidence),
                    username="dev",
                    reason="นำเข้าผ่านคำสั่ง",
                    stdout=out,
                )
            self.assertIn("Evidence attached", out.getvalue())


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


class ReleaseEvidenceTests(EvidenceTestCase):
    """ADR 0038: a whole release travels in one signed file and keeps its label."""

    def setUp(self):
        super().setUp()
        ai.save_credential(self.dev, "gemini", GEMINI_URL, "AIza" + "k" * 35, "เพิ่มคีย์")
        models = [
            AIModel.objects.get(model_id="gemini-3.8-flash"),
            ai.add_model(self.dev, self.credential, "primary-model"),
        ]
        self.label = ai_release.create(self.dev, models)
        for task, version in ai_release.members(self.label).items():
            report = {"gate": "x", "version": version.number, **ai_evidence.data_here(task)}
            if task == AIConfigVersion.Task.ROUTING:
                report = {"not_ready": "ยังไม่มีรายการงานที่ดูแล"}
            AIConfigVersion.objects.filter(pk=version.pk).update(
                eval_passed=True, eval_result=report
            )
        with patch.object(ai_release, "eval_blocker", return_value=None):
            self.raw = json.dumps(
                ai_evidence.export_release(self.label, PEM), ensure_ascii=False
            ).encode()
        self.source = {t: v.number for t, v in ai_release.members(self.label).items()}
        # Production does not have this release yet.
        AIConfigVersion.objects.filter(release=self.label).update(release="", status="rejected")

    def test_the_release_keeps_its_label_and_needs_a_test_here(self):
        self.assertTrue(ai_evidence.is_release(self.raw))
        with patch.object(ai_release, "eval_blocker", return_value=None):
            label = ai_evidence.import_release(self.dev, self.raw, "นำเข้ารุ่นรวม")
        self.assertEqual(label, self.label)
        found = ai_release.members(label)
        self.assertEqual(set(found), set(AIConfigVersion.Task.values))
        for task, version in found.items():
            self.assertNotEqual(version.number, self.source[task])
            self.assertEqual(version.eval_result["provenance"]["release"], label)
        extract = found[AIConfigVersion.Task.EXTRACT]
        self.assertEqual(extract.eval_result["version"], extract.number)
        self.assertEqual(
            ai_release.not_ready(found[AIConfigVersion.Task.ROUTING]), "ยังไม่มีรายการงานที่ดูแล"
        )
        self.assertFalse(any(ai_release.tested(v) for v in found.values()))
        with self.assertRaisesMessage(ValidationError, "นำเข้ารุ่น"):
            ai_evidence.import_release(self.dev, self.raw, "ซ้ำ")
        self.assertTrue(AuditEvent.objects.filter(action="ai.release_imported").exists())

    def test_the_command_saves_the_gemini_key_then_tests_and_uses_the_imported_release(self):
        gemini = AICredential.objects.get(name="gemini")
        ai.remove_key(self.dev, gemini, "production ยังไม่มีคีย์")
        args = ("import_ai_config", "--provider", "gemini", "--username", "dev")
        key = "AIza" + "p" * 35  # pragma: allowlist secret
        with patch.dict(os.environ, {"GOOGLE_GEMINI_API_KEY": key}):
            out = StringIO()
            call_command(*args, "--reason", "ใส่คีย์ production", stdout=out)
            self.assertIn("key saved", out.getvalue())
            call_command(*args, "--reason", "ซ้ำ", stdout=out)
            self.assertIn("key unchanged", out.getvalue())
            with (
                patch.object(ai_release, "eval_blocker", return_value=None),
                patch.object(ai, "eval_blocker", return_value=None),
            ):
                label = ai_evidence.import_release(self.dev, self.raw, "นำเข้ารุ่นรวม")
                call_command(
                    *args, "--reason", "ใช้รุ่นใหม่", "--release", label, "--activate", stdout=out
                )
        self.assertEqual(ai_release.current(), label)
        routing = ai_release.members(label)[AIConfigVersion.Task.ROUTING]
        self.assertNotEqual(routing.status, AIConfigVersion.Status.ACTIVE)  # not ready stays off
        with self.assertRaisesMessage(CommandError, "Evaluate a release on the Mac"):
            call_command(*args, "--reason", "x", "--evaluate", stdout=StringIO())

    def test_a_missing_provider_is_named(self):
        ai.remove_key(self.dev, AICredential.objects.get(name="gemini"), "เครื่องนี้ยังไม่มีคีย์")
        with self.assertRaisesMessage(ValidationError, "generativelanguage.googleapis.com"):
            ai_evidence.import_release(self.dev, self.raw, "นำเข้า")

    def test_different_data_here_is_refused_and_named(self):
        with patch.object(answers, "bank_digest", return_value="อื่น"):
            with self.assertRaisesMessage(ValidationError, "คลังคำตอบ"):
                ai_evidence.import_release(self.dev, self.raw, "นำเข้า")
        self.assertFalse(AIConfigVersion.objects.filter(release=self.label).exists())
