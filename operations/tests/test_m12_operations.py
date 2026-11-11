"""M12 infrastructure and operations (PROJECT.md M12; Q1–Q15, 8 ตุลาคม 2569).

Acceptance → tests: ช่องทางรับเรื่องสำรอง reaches the system as an on-behalf case on the
fallback-form channel with its เวลาแจ้งจริง, while clocks start when it is recorded
(FallbackIntakeTests); the runtime database role cannot rewrite history (DatabaseGrantTests);
alert metrics carry ages and counts only (OpsMetricsTests); a restore drill notices missing or
changed files (VerifyRestoreTests); synthetic seeding is refused in production
(SeedStagingTests); a release manifest pins every image (ReleaseManifestTests); the host stack,
edge, database access, OCI budget and pipelines keep the rules of E.2.1/E.7/E.8
(HostStackTests, EdgeTests, BudgetTests, PipelineTests); staging follows CI's main tag only when
switched on and production takes only what staging passed (AutoDeployTests, option ข 12 ต.ค.
2569). Runtime evidence (rehearsal deploy, rollback, backups, restore drill) is in PROJECT.md M12.
"""

import hashlib
import importlib.util
import json
import re
import subprocess
import tempfile
import uuid
from datetime import date, timedelta
from pathlib import Path

import yaml
from django.conf import settings
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from operations.management.commands import apply_db_grants, ops_metrics, verify_restore
from operations.models import AckClock, AppendOnlyModel, Attachment, Case, WebhookInbox
from operations.tests.test_m10_web import WebTestCase

ROOT = Path(settings.BASE_DIR)
SERVER = ROOT / "deploy/server"
OCI = ROOT / "deploy/oci"
PINNED = re.compile(r"@sha256:[0-9a-f]{64}$")


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FallbackIntakeTests(WebTestCase):
    def form(self, **extra):
        return {
            "idempotency_key": uuid.uuid4().hex,
            "requester_id": str(self.req1.pk),
            "kind": "withdrawal",
            "channel": "fallback_form",
            "intake_note": "แบบฟอร์มสำรอง แถวที่ 3",
            "purpose": "เปลี่ยนมิเตอร์ชำรุด",
            "row-0-item": "MTR_SYN_A",
            "row-0-quantity": "2",
            **extra,
        }

    def reported(self, hours):
        return timezone.localtime(timezone.now() - timedelta(hours=hours)).strftime(
            "%Y-%m-%dT%H:%M"
        )

    def test_fallback_form_case_keeps_the_reported_time_but_clocks_start_at_recording(self):
        client = self.client_for(self.intake)
        response = client.post("/cases/new/", self.form(reported_at=self.reported(3)))
        case = Case.objects.get(channel=Case.Channel.FALLBACK_FORM)
        self.assertRedirects(response, f"/cases/{case.number}/")
        self.assertAlmostEqual(
            (case.submitted_at - case.reported_at).total_seconds(), 3 * 3600, delta=120
        )
        clock = AckClock.objects.get(case=case, kind=AckClock.Kind.INTAKE)
        self.assertGreaterEqual(clock.started_at, case.submitted_at)
        page = client.get(f"/cases/{case.number}/")
        self.assertContains(page, "เวลาแจ้งจริง")
        self.assertContains(page, "ยื่นทาง แบบฟอร์มสำรอง")
        export = client.get("/divisions/SIM-TRIAGE/export.csv").content.decode()
        self.assertIn("เวลาแจ้งจริง", export.splitlines()[0])

    def test_the_fallback_form_needs_a_plausible_reported_time(self):
        client = self.client_for(self.intake)
        for value in ("", self.reported(-2), self.reported(24 * 31), "เมื่อวาน"):
            response = client.post("/cases/new/", self.form(reported_at=value))
            self.assertEqual(response.status_code, 200, value)
            self.assertContains(response, "notice--error")
        self.assertFalse(Case.objects.filter(channel=Case.Channel.FALLBACK_FORM).exists())
        # Other staff channels may carry it too; a phone call needs none.
        client.post("/cases/new/", self.form(channel="phone"))
        self.assertIsNone(Case.objects.get(channel="phone").reported_at)


class DatabaseGrantTests(SimpleTestCase):
    def test_history_tables_stay_append_only_for_the_runtime_role(self):
        sql = apply_db_grants.statements("faoj_app")
        from django.apps import apps

        history = [m for m in apps.get_models() if issubclass(m, AppendOnlyModel)]
        self.assertGreaterEqual(len(history), 10)
        for model in history:
            self.assertIn(
                f'REVOKE UPDATE, DELETE, TRUNCATE ON "{model._meta.db_table}" FROM "faoj_app"', sql
            )
        self.assertIn(
            'REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON "django_migrations" FROM "faoj_app"', sql
        )
        # Grants come first, revokes after them, so a re-run never re-opens history.
        first_revoke = next(i for i, s in enumerate(sql) if s.startswith("REVOKE"))
        self.assertTrue(all(s.startswith("GRANT") for s in sql[:first_revoke]))

    def test_role_names_are_checked(self):
        with self.assertRaises(CommandError):
            apply_db_grants.statements('app"; DROP TABLE x; --')


class OpsMetricsTests(TestCase):
    def test_metrics_are_ages_and_counts_only(self):
        WebhookInbox.objects.create(
            webhook_event_id="evt-m12",
            event_type="message",
            source_type="user",
            source_user_id="U" + "f" * 32,
            payload={"text": "ข้อความจำลอง"},
            payload_hash="x" * 64,
            occurred_at=timezone.now(),
            received_at=timezone.now() - timedelta(minutes=7),
        )
        values = ops_metrics.collect()
        self.assertGreaterEqual(values["inbox_pending_oldest_seconds"], 7 * 60 - 5)
        for name, value in values.items():
            self.assertIsInstance(value, int, name)
        printed = json.dumps(values)
        self.assertNotIn("U" + "f" * 32, printed)
        self.assertNotIn("ข้อความ", printed)


class VerifyRestoreTests(WebTestCase):
    def test_missing_and_changed_files_fail_the_drill(self):
        case = self.submit()
        with tempfile.TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            from operations import attachments

            folder = attachments.storage_dir()
            folder.mkdir(parents=True, exist_ok=True)
            good, changed = b"%PDF-1.4 kept", b"%PDF-1.4 original"
            (folder / "kept").write_bytes(good)
            (folder / "changed").write_bytes(b"%PDF-1.4 tampered")
            for name, data in (("kept", good), ("changed", changed), ("gone", good)):
                Attachment.objects.create(
                    case=case,
                    uploaded_by=self.intake,
                    declared_name=f"{name}.pdf",
                    content_type="application/pdf",
                    size=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    storage_name=name,
                    scan_status=Attachment.ScanStatus.CLEAN,
                )
            report = verify_restore.collect(int(timezone.now().timestamp()))
        self.assertEqual(report["attachments_checked"], 3)
        self.assertEqual(report["attachments_missing"], 1)
        self.assertEqual(report["attachments_mismatched"], 1)
        self.assertEqual(report["unapplied_migrations"], 0)
        self.assertFalse(report["ok"])


class SeedStagingTests(TestCase):
    def test_refused_in_production(self):
        with override_settings(APP_ENV="production"), self.assertRaises(CommandError):
            call_command("seed_staging")


class ReleaseManifestTests(SimpleTestCase):
    def test_manifest_pins_every_image_and_lists_migrations(self):
        module = load(ROOT / "deploy/release_manifest.py")
        db = "registry.example/faoj/db@sha256:" + "a" * 64
        manifest = module.manifest(ROOT, "abc123", db)
        for key in ("n8n_image", "clamav_image", "caddy_image", "backup_image"):
            self.assertRegex(manifest[key], PINNED, key)
        self.assertEqual(manifest["db_image"], db)
        files = sorted(p.stem for p in (ROOT / "operations/migrations").glob("[0-9]*.py"))
        self.assertEqual(manifest["migrations"], files)
        with self.assertRaises(SystemExit):
            module.main(["--db-image", "registry.example/faoj/db:latest"])


class HostStackTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.compose = yaml.safe_load((SERVER / "compose.yaml").read_text())
        cls.services = cls.compose["services"]

    def test_images_are_pinned_or_come_from_the_release(self):
        for name, service in self.services.items():
            image = service["image"]
            self.assertTrue(
                PINNED.search(image)
                or image in ("${APP_IMAGE:?release.env}", "${DB_IMAGE:?release.env}"),
                f"{name}: {image}",
            )

    def test_only_caddy_is_public_and_n8n_editor_is_local(self):
        for name, service in self.services.items():
            for port in service.get("ports", []):
                if name == "caddy":
                    self.assertRegex(port, r"^\$\{HTTPS?_PORT:-(80|443)\}:(80|443)$")
                else:
                    self.assertTrue(port.startswith("127.0.0.1:"), f"{name} publishes {port}")
        self.assertNotIn("ports", self.services["db"])

    def test_no_privilege_escalation_and_bounded_memory(self):
        long_running = ("caddy", "db", "web", "worker", "n8n", "clamav")
        for name, service in self.services.items():
            self.assertFalse(service.get("privileged", False), name)
            for volume in service.get("volumes", []):
                self.assertNotIn("docker.sock", volume, name)
            if name in long_running:
                self.assertIn("mem_limit", service, name)
        # pgBackRest's async workers need an init as PID 1 (rehearsal finding, M12).
        self.assertTrue(self.services["db"]["init"])

    def test_database_needs_tls_and_archives_within_the_rpo(self):
        command = " ".join(self.services["db"]["command"])
        self.assertIn("ssl=on", command)
        self.assertIn("hba_file=/etc/postgresql/pg_hba.conf", command)
        timeout = int(re.search(r"archive_timeout=(\d+)", command).group(1))
        self.assertLessEqual(timeout, 600)  # the WAL-lag alert fires at 600 s (E.9)
        rules = [
            line.split()
            for line in (SERVER / "db/pg_hba.conf").read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        for rule in rules:
            if rule[0] == "host":
                self.assertEqual(rule[-1], "reject")
            elif rule[0] == "local":
                self.assertEqual((rule[2], rule[3]), ("postgres", "peer"))
            else:
                self.assertEqual((rule[0], rule[-1]), ("hostssl", "scram-sha-256"))
        self.assertIn("sslmode=verify-full", (SERVER / "bootstrap-host.sh").read_text())

    def test_secrets_reach_only_the_services_that_need_them(self):
        def files(name):
            return " ".join(self.services[name].get("env_file", []))

        self.assertNotIn("db.secret", files("web") + files("worker") + files("n8n"))
        self.assertNotIn("backup.secret", files("web") + files("worker") + files("db"))
        self.assertIn("migrate.secret", files("migrate"))
        self.assertNotIn("migrate.secret", files("web") + files("worker"))

    def test_timers_call_existing_commands(self):
        script = (SERVER / "bin/faoj").read_text()
        commands = set(re.findall(r"^    ([a-z-]+)\)", script, re.M))
        for unit in (SERVER / "systemd").glob("*.service"):
            command = re.search(r"ExecStart=/usr/local/sbin/faoj (\S+)", unit.read_text()).group(1)
            self.assertIn(command, commands, unit.name)


class EdgeTests(SimpleTestCase):
    def test_internal_api_is_closed_before_the_proxy(self):
        caddy = (SERVER / "caddy/Caddyfile").read_text()
        # A bare `respond` loses to `handle` (Caddy's directive order): the rehearsal caught it.
        self.assertRegex(caddy, r"handle @internal \{\s*respond 404\s*\}")
        self.assertLess(caddy.index("handle @internal"), caddy.index("reverse_proxy web:8000"))
        self.assertIn("admin off", caddy)

    def test_maintenance_answers_503_with_the_fallback_channels(self):
        caddy = (SERVER / "caddy/Caddyfile").read_text()
        self.assertIn("try_files /maintenance", caddy)
        self.assertIn("status 503", caddy)
        self.assertRegex(caddy, r"@write not method GET HEAD\s+respond @write .* 503")
        page = (SERVER / "caddy/maintenance/index.html").read_text()
        self.assertIn('env "FALLBACK_PHONE"', page)
        for topic in ("METER", "EQUIPMENT", "USAGE"):  # ADR 0008: a number per topic
            self.assertIn(f'env "FALLBACK_PHONE_{topic}"', page)
        self.assertIn('env "FALLBACK_FORM_URL"', page)


class BudgetTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.budget = json.loads((SERVER / "oci/budget.json").read_text())
        cls.hosts = json.loads((OCI / "hosts.auto.tfvars.json").read_text())["hosts"]

    def test_terraform_hosts_match_the_budget(self):
        credit = self.budget["credit"]
        plans = self.budget["instances"] | credit["instances"]
        self.assertEqual(set(plans), set(self.hosts))
        for name, plan in plans.items():
            host = self.hosts[name]
            self.assertEqual(
                (host.get("shape", "VM.Standard.A1.Flex"), host["ocpus"], host["memory_gb"]),
                (plan.get("shape", "VM.Standard.A1.Flex"), plan["ocpus"], plan["memory_gb"]),
                name,
            )
            self.assertEqual((host["boot_gb"], host["data_gb"]), (plan["boot_gb"], plan["data_gb"]))
        a1 = [
            h
            for h in self.hosts.values()
            if h.get("shape", "VM.Standard.A1.Flex") in self.budget["allowed_shapes"]
        ]
        paid = [h for h in self.hosts.values() if h not in a1]
        self.assertTrue(all(h["shape"] in credit["shapes"] for h in paid))
        # ADR 0015: staging runs on the Mac and the A1 hosts are parked (volumes kept, no VM).
        running = {n for n, h in self.hosts.items() if h.get("instance", True)}
        self.assertEqual(running, set(credit["instances"]))
        self.assertLessEqual(sum(h["ocpus"] for h in a1), self.budget["a1_ocpus"])
        self.assertLessEqual(sum(h["memory_gb"] for h in a1), self.budget["a1_memory_gb"])
        self.assertLessEqual(
            sum(h["boot_gb"] + h["data_gb"] for h in a1), self.budget["block_storage_gb"]
        )
        self.assertLessEqual(sum(h["ocpus"] for h in paid), credit["caps"]["ocpus"])
        self.assertLessEqual(sum(h["memory_gb"] for h in paid), credit["caps"]["memory_gb"])
        self.assertLessEqual(
            sum(h["boot_gb"] + h["data_gb"] for h in paid), credit["caps"]["block_gb"]
        )

    def test_terraform_credit_defaults_repeat_the_budget(self):
        credit = self.budget["credit"]
        variables = (OCI / "variables.tf").read_text()
        self.assertIn(f'default     = "{credit["until"]}"', variables)
        self.assertIn(f"default     = {credit['amount_sgd']}", variables)
        self.assertIn(f'default     = "{credit["move_by"]}"', variables)
        caps = credit["caps"]
        self.assertIn(
            f"default = {{ ocpus = {caps['ocpus']}, memory_gb = {caps['memory_gb']}, "
            f"block_gb = {caps['block_gb']} }}",
            variables,
        )

    def test_quota_policy_repeats_the_budget(self):
        governance = (OCI / "governance.tf").read_text()
        self.assertIn(f"standard-a1-core-count to {self.budget['a1_ocpus']}", governance)
        self.assertIn(f"standard-a1-memory-count to {self.budget['a1_memory_gb']}", governance)
        # "zero" also clears the regional counts; without these the first apply was refused.
        self.assertIn(f"standard-a1-core-regional-count to {self.budget['a1_ocpus']}", governance)
        self.assertIn(
            f"standard-a1-memory-regional-count to {self.budget['a1_memory_gb']}", governance
        )
        self.assertIn(
            "depends_on = [oci_limits_quota.always_free]", (OCI / "compute.tf").read_text()
        )
        self.assertIn(
            f"total-storage-gb to ${{{self.budget['block_storage_gb']} + (local.credit_active",
            governance,
        )

    def test_only_web_ports_are_public(self):
        network = (OCI / "network.tf").read_text()
        public = re.findall(r'(\w+)\s*=\s*\{ port = (\d+), source = "0\.0\.0\.0/0" \}', network)
        self.assertEqual(sorted(public), [("http", "80"), ("https", "443")])
        self.assertIn("ssh   = { port = 22, source = oci_core_subnet.public.cidr_block }", network)
        self.assertIn("security_list_ids = [oci_core_security_list.egress_only.id]", network)

    def test_quota_check_flags_over_and_near(self):
        check = load(SERVER / "oci/quota_check.py")
        usage = {
            "shapes": ["VM.Standard.A1.Flex"] * 2,
            "a1_ocpus": 2,
            "a1_memory_gb": 12,
            "block_storage_gb": 200,
            "volume_backups": 4,
            "object_storage_gb": 9.5,
            "load_balancers": 0,
            "ocir_images": {"faoj/app": 5},
        }
        self.assertEqual(check.evaluate(usage, self.budget), [])
        usage |= {"a1_ocpus": 3, "object_storage_gb": 17, "ocir_images": {"faoj/app": 7}}
        usage["shapes"].append("VM.Standard.E4.Flex")
        after_credit = date.fromisoformat(self.budget["credit"]["until"])
        levels = sorted(level for level, _ in check.evaluate(usage, self.budget, after_credit))
        self.assertEqual(levels, ["near", "near", "over", "over"])

    def test_quota_check_allows_the_credit_host_until_the_credit_ends(self):
        check = load(SERVER / "oci/quota_check.py")
        until = date.fromisoformat(self.budget["credit"]["until"])
        usage = {
            "shapes": ["VM.Standard.E5.Flex"],
            "a1_ocpus": 0,
            "a1_memory_gb": 0,
            "credit_ocpus": 2,
            "credit_memory_gb": 16,
            "block_storage_gb": 150,
            "volume_backups": 1,
            "object_storage_gb": 2,
            "load_balancers": 0,
            "ocir_images": {},
        }
        self.assertEqual(check.evaluate(usage, self.budget, until - timedelta(days=10)), [])
        near = check.evaluate(usage, self.budget, until - timedelta(days=2))
        self.assertEqual([level for level, _ in near], ["near"])
        over = check.evaluate(usage, self.budget, until)
        self.assertIn(("over", "shape not in the budget: VM.Standard.E5.Flex"), over)
        usage |= {"credit_ocpus": 6, "block_storage_gb": 420}
        texts = [t for _, t in check.evaluate(usage, self.budget, until - timedelta(days=10))]
        self.assertIn("credit OCPUs: 6 > 4", texts)
        self.assertIn("block storage GB: 420 > 400", texts)


class PipelineTests(SimpleTestCase):
    def test_workflows_are_least_privilege_and_pinned(self):
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            text = path.read_text()
            data = yaml.safe_load(text)
            # CI also reads its own runs: main reuses the pull request run of the same tree.
            extra = {"actions": "read"} if path.name == "ci.yml" else {}
            self.assertEqual(data["permissions"], {"contents": "read", **extra}, path.name)
            self.assertNotIn("pull_request_target", text, path.name)
            for action in re.findall(r"uses:\s*(\S+)", text):
                self.assertRegex(action, r"@[0-9a-f]{40}$", f"{path.name}: {action}")

    def test_release_never_touches_a_host(self):
        release = (ROOT / ".github/workflows/release.yml").read_text()
        for forbidden in ("ssh ", "faoj deploy\n", "BASTION", "DATABASE_URL"):
            self.assertNotIn(forbidden, release)
        # Our own images are a blocking scan on both platforms; third-party ones only report.
        self.assertIn('scan "$app" "$p" 1', release)
        self.assertIn('scan "$DB_IMAGE" "$p" 1', release)
        self.assertIn('--exit-code "$3"', release)

    def test_images_go_to_artifact_registry_without_a_stored_key(self):
        # OCIR refused pushes on 11 Nov 2026; GitHub signs in through Workload Identity instead.
        for name in ("release.yml", "scheduled.yml"):
            text = (ROOT / ".github/workflows" / name).read_text()
            self.assertNotIn("secrets.", text, name)
            self.assertIn("vars.GAR_REGISTRY", text, name)
            self.assertIn("google-github-actions/auth@", text, name)
            for job_name, job in yaml.safe_load(text)["jobs"].items():
                if job.get("permissions"):
                    self.assertEqual(
                        job["permissions"], {"contents": "read", "id-token": "write"}, job_name
                    )

    def test_main_tag_moves_only_after_a_passing_release_from_main(self):
        release = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertIn(
            "github.event_name == 'workflow_run' || github.ref == 'refs/heads/main'", release
        )
        self.assertRegex(
            release,
            r'if \[ "\$status" = 0 \] && \[ "\$FROM_MAIN" = true \]; then\s+'
            r'docker buildx imagetools create -t "\$repo:main" "\$app"',
        )
        self.assertLess(release.index('-t "$repo:main"'), release.index('exit "$status"'))


class AutoDeployTests(SimpleTestCase):
    """Option ข: off until an operator switches staging on with a reason; never on production."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "env").mkdir()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"  # any call is recorded and fails: these paths need no Docker
        docker.write_text('#!/bin/sh\necho "$*" >> "$FAOJ_ROOT/docker.log"\nexit 1\n')
        docker.chmod(0o755)
        self.path = f"{bin_dir}:/usr/bin:/bin"

    def faoj(self, env, *args):
        (self.root / "env/compose.env").write_text(f"FAOJ_ENV={env}\nPUBLIC_HOST=example.test\n")
        return subprocess.run(
            ["bash", str(SERVER / "bin/faoj"), *args],
            env={"PATH": self.path, "FAOJ_ROOT": str(self.root), "USER": "operator1"},
            capture_output=True,
            text=True,
            check=False,
        )

    def test_deploy_clears_old_images_before_it_pulls(self):
        # 1 Nov 2026: old images filled the root disk, which the FAOJ_ROOT check never sees.
        for name in ("django.env", "app.secret.env", "migrate.secret.env", "db.secret.env"):
            (self.root / "env" / name).write_text("")
        (self.root / "env/n8n.secret.env").write_text("")
        (self.root / "tls/db").mkdir(parents=True)
        (self.root / "tls/db/server.key").write_text("")
        # The disk checks read this runner's /tmp, often under 15% free on CI: report room instead.
        python = self.root / "bin/python3"
        python.write_text(
            '#!/bin/sh\ncase "$*" in *disk_usage*) echo 50;; *) exec /usr/bin/python3 "$@";; esac\n'
        )
        python.chmod(0o755)
        result = self.faoj("staging", "deploy", "registry.example/app@sha256:" + "a" * 64)
        self.assertIn("could not remove old images", result.stderr)
        calls = (self.root / "docker.log").read_text().splitlines()
        self.assertEqual(calls, ["image prune -af --filter until=24h"])  # stopped before any pull

    def test_staging_is_off_until_switched_on_with_a_reason(self):
        self.assertTrue(self.faoj("staging", "auto-deploy", "status").stdout.startswith("off"))
        check = self.faoj("staging", "auto-deploy", "check")
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertFalse((self.root / "docker.log").exists())  # off: the timer pulls nothing

        self.assertNotEqual(self.faoj("staging", "auto-deploy", "on").returncode, 0)
        self.assertEqual(self.faoj("staging", "auto-deploy", "on", "UAT done").returncode, 0)
        status = self.faoj("staging", "auto-deploy", "status").stdout
        self.assertIn("by operator1: UAT done", status)
        self.assertEqual(self.faoj("staging", "auto-deploy", "off", "UAT round 2").returncode, 0)
        self.assertTrue(self.faoj("staging", "auto-deploy", "status").stdout.startswith("off"))
        log = (self.root / "releases/auto-deploy.log").read_text().splitlines()
        self.assertEqual(
            [line.split("\t")[1:] for line in log],
            [["operator1", "on", "UAT done"], ["operator1", "off", "UAT round 2"]],
        )

    def test_production_never_follows_the_tag(self):
        result = self.faoj("production", "auto-deploy", "on", "faster releases")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("production uses 'faoj promote'", result.stderr)
        self.assertFalse((self.root / "run/auto-deploy").exists())

    def test_promote_is_production_only_and_needs_an_approval(self):
        result = self.faoj("staging", "promote", "--approval", "owner 12 Oct")
        self.assertIn("promote is for production", result.stderr)
        result = self.faoj("production", "promote")
        self.assertIn("promote needs --approval", result.stderr)
        result = self.faoj("production", "promote", "--approval", "owner 12 Oct")
        self.assertIn("no release yet", result.stderr)
        self.assertFalse((self.root / "docker.log").exists())

    def test_bootstrap_enables_the_timer_on_staging_only(self):
        bootstrap = (SERVER / "bootstrap-host.sh").read_text()
        timers = dict(
            re.findall(r'^\s+(production|staging)\) timers="\$timers ([^"]+)"', bootstrap, re.M)
        )
        self.assertIn("faoj-auto-deploy.timer", timers["staging"])
        self.assertNotIn("faoj-auto-deploy.timer", timers["production"])


class ReleaseClassTests(SimpleTestCase):
    """ADR 0015 Q6: a release with a one-way migration or a PostgreSQL/n8n jump is major."""

    def setUp(self):
        self.module = load(SERVER / "release_class.py")
        self.base = {
            "migrations": ["0001_a"],
            "unsafe_migrations": [],
            "postgres_major": "17",
            "n8n_major": "2",
        }

    def test_one_way_operations_are_found_in_migration_files(self):
        unsafe = self.module.unsafe
        self.assertTrue(unsafe("migrations.RemoveField(model_name='x', name='y')"))
        self.assertTrue(unsafe("migrations.RunPython(backfill)"))
        self.assertTrue(unsafe("migrations.RunSQL('drop table x')"))
        self.assertFalse(unsafe("migrations.RunPython(backfill, migrations.RunPython.noop)"))
        self.assertFalse(unsafe("migrations.RunSQL('select 1', reverse_sql='select 1')"))
        self.assertFalse(unsafe("migrations.AddField(model_name='x', name='z')"))
        manifest = load(ROOT / "deploy/release_manifest.py").manifest(ROOT, "abc", "")
        self.assertEqual(manifest["postgres_major"], "17")
        self.assertEqual(manifest["unsafe_migrations"], [])  # 0028/0029 have a reverse (noop)

    def test_minor_and_major(self):
        classify = self.module.classify
        new = dict(self.base, migrations=["0001_a", "0002_b"])
        self.assertEqual(classify(new, self.base), ("minor", []))
        kind, why = classify(dict(new, unsafe_migrations=["0002_b"]), self.base)
        self.assertEqual(kind, "major")
        self.assertIn("0002_b", why[0])
        self.assertEqual(classify(dict(self.base, postgres_major="18"), self.base)[0], "major")
        self.assertEqual(classify(dict(self.base, migrations=[]), self.base)[0], "major")
        legacy = {"migrations": ["0001_a", "0002_b"]}  # built before the check existed
        self.assertEqual(classify(legacy, self.base)[0], "major")


class AutoPromoteTests(SimpleTestCase):
    """ADR 0015 Q20: production takes minor releases by itself only when switched on."""

    setUp = AutoDeployTests.setUp
    faoj = AutoDeployTests.faoj

    def test_production_only_off_until_switched_on_and_check_does_nothing_when_off(self):
        self.assertIn(
            "auto-promote is for production", self.faoj("staging", "auto-promote", "on", "x").stderr
        )
        self.assertTrue(self.faoj("production", "auto-promote", "status").stdout.startswith("off"))
        self.assertEqual(self.faoj("production", "auto-promote", "check").returncode, 0)
        self.assertFalse((self.root / "docker.log").exists())
        self.assertNotEqual(self.faoj("production", "auto-promote", "on").returncode, 0)
        self.assertEqual(
            self.faoj("production", "auto-promote", "on", "minor fixes at night").returncode, 0
        )
        status = self.faoj("production", "auto-promote", "status").stdout
        self.assertIn("by operator1: minor fixes at night", status)
        self.assertIn("window 18-07", status)

    def test_bootstrap_enables_the_promote_timer_on_production_only(self):
        bootstrap = (SERVER / "bootstrap-host.sh").read_text()
        timers = dict(
            re.findall(r'^\s+(production|staging)\) timers="\$timers ([^"]+)"', bootstrap, re.M)
        )
        self.assertIn("faoj-auto-promote.timer", timers["production"])
        self.assertNotIn("faoj-auto-promote.timer", timers["staging"])
