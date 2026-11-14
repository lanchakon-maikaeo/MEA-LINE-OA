"""The LINE mockup is recorded from the real chat, so a changed chat must fail here, not when
someone next builds the page: seven of its scenarios had gone stale unnoticed (13 พ.ย. 2569).

Acceptance → tests: every scenario records to its end on the seeded structure."""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from operations import line_flows, line_mockup, options
from operations.models import Division, LineFlow, Role, SystemOption
from operations.tests.test_m02_cases import make_staff


class LineMockupTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # The structure and the published flow of the Meter, CT, VT tile, as a release has them.
        call_command("load_org", stdout=StringIO())
        Division.objects.filter(code="กองอุปกรณ์ฯ").update(code="กอจ.")  # its code since 29 ต.ค.
        call_command("seed_line_flows", stdout=StringIO())
        admin = make_staff("mockup-flow-admin", (Role.ADMIN, None))
        flow = LineFlow.objects.get(key="meter-ct-vt")
        line_flows.publish(admin, line_flows.draft_of(flow), "ตามข้อสรุป กคว.")
        SystemOption.objects.update_or_create(key=options.SCREENING, defaults={"value": "off"})

    def test_every_scenario_still_matches_the_chat(self):
        data = line_mockup.record()
        played = [scenario["key"] for scenario in data["scenarios"]]
        expected = [s.key for s in line_mockup.SCENARIOS if s.key not in data["skipped"]]
        self.assertEqual(played, expected)
        self.assertLessEqual(set(data["skipped"]), {"typed"})  # needs an active AI version
        for scenario in data["scenarios"]:
            self.assertGreater(len(scenario["steps"]), 2, scenario["key"])
