"""The move to GCP must lose nothing (Docs/release/gcp-move/0-data-safety.md, 2 Nov 2026)."""

import io
import json
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from operations import sealing
from operations.management.commands import decrypt_check, move_manifest
from operations.models import AICredential, RosterUpload


class DeepManifestTests(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        override = override_settings(MEDIA_ROOT=self.media.name)
        override.enable()
        self.addCleanup(override.disable)
        Path(self.media.name, "broadcast").mkdir()
        Path(self.media.name, "broadcast", "card.jpg").write_bytes(b"original")
        self.credential = AICredential.objects.create(
            name="move-check", base_url="https://example.com/v1", label="before"
        )

    def test_same_data_matches(self):
        source = move_manifest.manifest(deep=True)
        self.assertEqual(move_manifest.differences(source, move_manifest.manifest(deep=True)), [])

    def test_an_update_that_keeps_the_count_is_caught(self):
        source = move_manifest.manifest(deep=True)
        AICredential.objects.filter(pk=self.credential.pk).update(label="after")
        target = move_manifest.manifest(deep=True)
        self.assertEqual(source["tables"], target["tables"])
        problems = move_manifest.differences(source, target)
        self.assertEqual(len(problems), 1)
        self.assertIn(f"rows of {AICredential._meta.db_table}", problems[0])

    def test_a_changed_or_missing_file_is_caught(self):
        source = move_manifest.manifest(deep=True)
        Path(self.media.name, "broadcast", "card.jpg").write_bytes(b"changed")
        Path(self.media.name, "broadcast", "extra.jpg").write_bytes(b"new")
        problems = move_manifest.differences(source, move_manifest.manifest(deep=True))
        self.assertEqual(
            sorted(p.split(":")[0] for p in problems),
            ["file broadcast/card.jpg", "file broadcast/extra.jpg"],
        )

    def test_a_deep_source_makes_the_compare_deep(self):
        source = move_manifest.manifest(deep=True)
        self.assertIn(
            "the source manifest is deep",
            move_manifest.differences(source, move_manifest.manifest())[0],
        )
        with tempfile.NamedTemporaryFile("w", suffix=".json") as stream:
            json.dump(source, stream)
            stream.flush()
            out = io.StringIO()
            call_command("move_manifest", compare=stream.name, stdout=out)
        self.assertIn("move verified (deep)", out.getvalue())


class DecryptCheckTests(TestCase):
    def setUp(self):
        self.ai_key, self.pii_key = Fernet.generate_key().decode(), Fernet.generate_key().decode()
        with override_settings(AI_SECRET_KEY=self.ai_key, PII_SECRET_KEY=self.pii_key):
            AICredential.objects.create(
                name="sealed",
                base_url="https://example.com/v1",
                encrypted_key=Fernet(self.ai_key.encode()).encrypt(b"sk-test").decode(),
            )
            user = get_user_model().objects.create_user("roster-admin")
            RosterUpload.objects.create(
                uploaded_by=user, filename="roster.csv", sealed=sealing.seal("[]")
            )

    def test_the_right_keys_open_everything(self):
        with override_settings(AI_SECRET_KEY=self.ai_key, PII_SECRET_KEY=self.pii_key):
            report = decrypt_check.check()
            out = io.StringIO()
            call_command("decrypt_check", stdout=out)
        self.assertEqual(report["ai_keys"], (1, 1))
        self.assertEqual(report["roster_files"], (1, 1))
        self.assertIn("decrypt check passed", out.getvalue())
        self.assertNotIn("sk-test", out.getvalue())

    def test_wrong_keys_fail(self):
        other = Fernet.generate_key().decode()
        with override_settings(AI_SECRET_KEY=other, PII_SECRET_KEY=other):
            report = decrypt_check.check()
            with self.assertRaises(SystemExit) as exit_:
                call_command("decrypt_check", stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual(report["ai_keys"], (0, 1))
        self.assertEqual(report["roster_files"], (0, 1))
        self.assertEqual(exit_.exception.code, 1)


class SkeletonDeployTests(SimpleTestCase):
    def test_restore_point_follows_backup_mode(self):
        # The empty GCP skeleton has no Object Storage keys: its deploys take a dump instead.
        faoj = (Path(settings.BASE_DIR) / "deploy/server/bin/faoj").read_text()
        deploy = faoj[faoj.index("\ndeploy() {") :]
        self.assertIn("point=${BACKUP_MODE:-pgbackrest}", deploy[: deploy.index("\n}\n")])
        point = faoj[faoj.index("\nrestore_point() {") :]
        point = point[: point.index("\n}\n")]
        dump, incr = point.index("pg_dump -Fc faoj"), point.index("pgbackrest backup --type=incr")
        self.assertIn('[ "$2" = dump ]', point[:dump])
        self.assertLess(dump, incr)
