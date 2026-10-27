"""นำ flow ร่างแรกและ requirement ของกองเข้าระบบ (ADR 0023, grill กคว. 27 ต.ค. 2569).

Safe to run again: a flow that already has a version keeps it, and a requirement whose text the
division already has is not added twice.
"""

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from operations import identity, line_flows
from operations.models import Division, DivisionRequirement, LineFlow, LineFlowVersion

FIXTURES = Path(settings.BASE_DIR) / "operations" / "fixtures"
FLOWS = ("line_flow_meter_ct_vt_v1.json",)
REQUIREMENTS = "division_requirements_v1.json"


class Command(BaseCommand):
    help = "Load the first LINE flow drafts and the divisions' requirements."

    @transaction.atomic
    def handle(self, *args, **options):
        actor = identity.system_user()
        for name in FLOWS:
            data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
            spec = data["flow"]
            flow, _ = LineFlow.objects.get_or_create(
                key=spec["key"],
                defaults={
                    "name": spec["name"],
                    "division": Division.objects.filter(code=spec["division"]).first(),
                },
            )
            if flow.versions.exists():
                self.stdout.write(f"{flow.key}: มีรุ่นอยู่แล้ว ไม่เปลี่ยน")
                continue
            graph = line_flows.clean_graph(data["graph"])
            LineFlowVersion.objects.create(
                flow=flow,
                number=1,
                graph=graph,
                checks=line_flows.check(graph),
                note=data["note"],
                created_by=actor,
                updated_by=actor,
            )
            self.stdout.write(f"{flow.key}: สร้างร่างรุ่น 1")
        data = json.loads((FIXTURES / REQUIREMENTS).read_text(encoding="utf-8"))
        added = 0
        for row in data["requirements"]:
            division = Division.objects.filter(code=row["division"]).first()
            if division is None:
                self.stdout.write(f"ไม่พบกอง {row['division']} ข้าม")
                continue
            if DivisionRequirement.objects.filter(division=division, text=row["text"]).exists():
                continue
            DivisionRequirement.objects.create(
                division=division,
                topic=row["topic"],
                text=row["text"],
                status=row["status"],
                priority=row["priority"],
                remark=row["remark"],
                flow=LineFlow.objects.filter(key=row["flow"]).first() if row["flow"] else None,
                node_id=row["node"],
            )
            added += 1
        self.stdout.write(f"requirement เพิ่ม {added} ข้อ")
