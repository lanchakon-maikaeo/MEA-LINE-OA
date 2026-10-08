from django.db import transaction

from operations import policy
from operations.management.demo import DemoCommand


class Command(DemoCommand):
    help = (
        "Import and activate the newest synthetic DEMO-PE routing/priority policy (local/CI "
        "demo DB only). Real policies come from the rule owner through import_policy (G02)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--source", default=str(policy.DEMO_POLICY))

    @transaction.atomic
    def handle_demo(self, *args, source, **options):
        version = policy.import_policy(policy.load_document(source))
        active = policy.active_policy()
        # The newest synthetic demo replaces an older synthetic demo; real policies stay.
        if active is None or (active.is_synthetic and active.code < version.code):
            policy.activate_policy(
                None,
                version,
                "ข้อมูลจำลอง (ผู้ใช้อนุญาตให้สร้างกองจำลอง)",
                "PROJECT.md B.3-B.4 / priority-engine-demo.json",
                "seed demo policy",
            )
        self.stdout.write(f"Policy {version.code}: {policy.active_policy() or 'none active'}")
