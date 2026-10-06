import json
import os
import secrets
import subprocess
import sys
from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connection
from django.db.migrations.recorder import MigrationRecorder
from django.test import SimpleTestCase, TestCase, override_settings

from config.environment import read_environment, require_demo_database
from operations.management.demo import DemoCommand
from operations.testing import NonEmptyTestRunner


class EnvironmentTests(SimpleTestCase):
    def test_secure_settings_pass_deployment_checks_and_reject_unverified_db_tls(self):
        url = "postgresql://demo:dummy@localhost/mea_prod"  # pragma: allowlist secret
        env = os.environ | {
            "APP_ENV": "production",
            "DJANGO_SECRET_KEY": secrets.token_urlsafe(64),
            "DATABASE_URL": url + "?sslmode=verify-full",
            "ALLOWED_HOSTS": "example.test",
            "CSRF_TRUSTED_ORIGINS": "https://example.test",
        }
        command = [sys.executable, "manage.py", "check", "--deploy", "--fail-level", "WARNING"]
        result = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        env["DATABASE_URL"] = env["DATABASE_URL"].replace("verify-full", "require")
        result = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires sslmode=verify-full", result.stderr)

    def test_configuration_rejects_missing_or_unknown_environment_and_weak_secret(self):
        env = {
            "APP_ENV": "ci",
            "DJANGO_SECRET_KEY": secrets.token_urlsafe(64),
            "DATABASE_URL": "postgresql://demo:dummy@localhost/mea_ci",  # pragma: allowlist secret
            "ALLOWED_HOSTS": "localhost",
        }
        self.assertEqual(read_environment(env)[0], "ci")
        for changes in (
            {"APP_ENV": ""},
            {"APP_ENV": "prod"},
            {"DJANGO_SECRET_KEY": "a" * 64},
            {"DATABASE_URL": "sqlite:///tmp/db"},
            {"ALLOWED_HOSTS": "*"},
            {"APP_ENV": "production", "CSRF_TRUSTED_ORIGINS": "http://example.test"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ImproperlyConfigured):
                read_environment(env | changes)

    def test_demo_guard_checks_environment_database_name_and_host(self):
        db = settings.DATABASES["default"].copy()
        for env in ("local", "ci"):
            require_demo_database(env, db)
        for env, changes in (
            ("production", {}),
            ("staging", {}),
            ("local", {"NAME": "production"}),
            ("local", {"HOST": "production.example.test"}),
        ):
            with self.subTest(env=env, changes=changes), self.assertRaises(CommandError):
                require_demo_database(env, db | changes)

    def test_demo_command_cannot_reach_fixture_write_in_production(self):
        command = DemoCommand()
        with override_settings(APP_ENV="production"), patch.object(command, "handle_demo") as write:
            with self.assertRaises(CommandError):
                command.handle()
            write.assert_not_called()

    def test_flush_rejects_production_before_accessing_database(self):
        with override_settings(APP_ENV="production"), self.assertRaises(CommandError):
            call_command("flush", interactive=False, stdout=StringIO())

    def test_unknown_module_and_empty_suite_fail(self):
        with self.assertRaises(CommandError):
            call_command("test_module", module="M99")
        with self.assertRaises(CommandError):
            NonEmptyTestRunner(verbosity=0).build_suite(["config"])


class FoundationTests(TestCase):
    def test_postgres_migrations_and_transaction_roundtrip(self):
        self.assertEqual(connection.vendor, "postgresql")
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM django_migrations")
            self.assertGreater(cursor.fetchone()[0], 0)
        user = get_user_model().objects.create_user(username="m00-demo")
        self.assertTrue(get_user_model().objects.filter(pk=user.pk).exists())

    def test_health_and_page_are_available_without_ai(self):
        with patch.dict(
            "os.environ", {"AI_ENABLED": "false", "THAILLM_BASE_URL": "http://invalid"}
        ):
            self.assertEqual(self.client.get("/health/live/").json(), {"status": "ok"})
            self.assertEqual(self.client.get("/health/ready/").json(), {"status": "ok"})
            self.assertContains(self.client.get("/"), "ระบบรับเรื่อง ฝอจ.")
        self.assertEqual(self.client.post("/health/live/").status_code, 405)

    def test_database_failure_is_503_without_details_and_liveness_stays_up(self):
        with patch.object(connection, "cursor", side_effect=OperationalError("private details")):
            response = self.client.get("/health/ready/")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {"status": "unavailable"})
            self.assertEqual(self.client.get("/health/live/").status_code, 200)

    def test_unapplied_migration_makes_readiness_fail(self):
        # Code deployed ahead of its schema must not receive traffic (rolled back by TestCase).
        MigrationRecorder(connection).record_unapplied("sessions", "0001_initial")
        response = self.client.get("/health/ready/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})

    def test_invalid_host_is_rejected(self):
        self.assertEqual(
            self.client.get("/health/live/", HTTP_HOST="untrusted.test").status_code, 400
        )


class DemoDataTests(SimpleTestCase):
    """Fixtures later imported by M01/M02/M06 must stay internally consistent."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.data = json.loads((settings.BASE_DIR / "synthetic-data.json").read_text())
        cls.demo = json.loads((settings.BASE_DIR / "priority-engine-demo.json").read_text())

    def test_eighteen_named_districts_and_consistent_stock(self):
        self.assertTrue(self.data["metadata"]["contains_synthetic_data"])
        districts = self.data["districts"]
        self.assertEqual(len(districts), 18)
        self.assertEqual(len({d["id"] for d in districts}), 18)
        self.assertEqual(len({d["name"] for d in districts}), 18)
        items = {item["id"] for item in self.data["items"]}
        for row in self.data["stock_snapshot"]:
            with self.subTest(item=row["item_id"]):
                self.assertIn(row["item_id"], items)
                parts = ("available", "reserved", "awaiting_inspection", "awaiting_repair")
                self.assertEqual(sum(row[k] for k in parts), row["total_on_hand"])
        for case in self.data["example_cases"]:
            with self.subTest(case=case["id"]):
                self.assertIn(case["district_id"], {d["id"] for d in districts})
                for line in case.get("lines", []):
                    self.assertIn(line["item_id"], items)

    def test_priority_scenarios_reference_known_units_rules_and_items(self):
        self.assertTrue(self.demo["metadata"]["is_synthetic"])
        self.assertFalse(self.demo["metadata"]["production_policy"])
        district_ids = {d["id"] for d in self.data["districts"]}
        items = {item["id"] for item in self.data["items"]}
        units = {d["id"] for d in self.demo["divisions"]}
        units |= {r["id"] for r in self.demo["coordination_roles"]}
        for scenario in self.demo["scenarios"]:
            facts, expected = scenario["facts"], scenario["expected"]
            with self.subTest(scenario=scenario["id"]):
                self.assertIn(scenario["district_id"], district_ids)
                if facts.get("item_id"):
                    self.assertIn(facts["item_id"], items)
                for key in ("current_owner", "proposed_owner"):
                    if expected.get(key):
                        self.assertIn(expected[key], units)
                self.assertLessEqual(set(expected.get("support_units", [])), units)
                self.assertLessEqual(set(expected.get("rules", [])), set(self.demo["rule_ids"]))
