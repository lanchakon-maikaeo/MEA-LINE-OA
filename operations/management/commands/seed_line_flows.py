"""นำ flow ร่างแรกและ requirement ของกองเข้าระบบ (ADR 0023, grill กคว. 27 ต.ค. 2569).

Safe to run again: a flow that already has a version keeps it, and a requirement whose text the
division already has is not added twice. --replace-draft puts the fixture into a flow's draft
(nothing published changes); a flow without a tile gets the fixture's.
"""

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from operations import identity, line_flows
from operations.models import (
    Calendar,
    Division,
    DivisionRequirement,
    DivisionSchedule,
    LineFlow,
    LineFlowVersion,
    SheetSource,
)

FIXTURES = Path(settings.BASE_DIR) / "operations" / "fixtures"
FLOWS = (
    "line_flow_meter_ct_vt_v1.json",
    "line_flow_koj_v1.json",
    # 28 ต.ค. 2569: the other buttons of the rich menu, drawn as the chat asks today.
    "line_flow_kco_v1.json",
    "line_flow_smg_v2.json",  # grill เจษ 28 ต.ค. 2569 (ADR 0027)
    "line_flow_usage_v1.json",
    "line_flow_general_v1.json",
)
REQUIREMENTS = "division_requirements_v1.json"


class Command(BaseCommand):
    help = "Load the first LINE flow drafts and the divisions' requirements."

    def add_arguments(self, parser):
        parser.add_argument("--division", help="seed เฉพาะกองที่ระบุ เช่น กอจ. โดยไม่แตะร่างกองอื่น")
        parser.add_argument(
            "--replace-draft", action="store_true", help="put the fixture into existing drafts"
        )

    @transaction.atomic
    def handle(self, *args, **options):
        actor = identity.system_user()
        for name in FLOWS:
            data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
            spec = data["flow"]
            if options.get("division") and spec["division"] != options["division"]:
                continue
            taken = LineFlow.objects.filter(tile=spec.get("tile", "")).exclude(key=spec["key"])
            if spec.get("tile") and taken.exists():
                # An admin already made this button's flow in the web; keep theirs.
                self.stdout.write(f"{spec['key']}: ปุ่ม {spec['tile']} มี flow อยู่แล้ว ข้าม")
                continue
            flow, _ = LineFlow.objects.get_or_create(
                key=spec["key"],
                defaults={
                    "name": spec["name"],
                    "division": Division.objects.filter(code=spec["division"]).first(),
                },
            )
            if not flow.tile and spec.get("tile"):
                flow.tile = spec["tile"]
                flow.save(update_fields=["tile"])
            for spec in data.get("calendars", []):
                division = Division.objects.filter(code=spec["division"]).first()
                if division is None:
                    continue
                Calendar.objects.get_or_create(
                    key=spec["key"], defaults={"name": spec["name"], "division": division}
                )
                # กำหนดจ่ายและวันส่งรถ start empty; กคว. fills them in (Q17, Q30).
                DivisionSchedule.objects.get_or_create(
                    division=division, defaults={"issue_weekdays": [0, 1, 2, 3, 4]}
                )
            for sheet in data.get("sheets", []):
                # ADR 0027: created switched off; an admin turns it on once it reads.
                SheetSource.objects.get_or_create(
                    key=sheet["key"], defaults={**sheet, "updated_by": actor}
                )
            graph = line_flows.clean_graph(data["graph"])
            draft = line_flows.draft_of(flow)
            if options["replace_draft"] and draft is not None:
                draft.graph, draft.checks = graph, line_flows.check(graph)
                draft.note = data["note"]
                draft.revision += 1
                draft.updated_by = actor
                draft.save(
                    update_fields=[
                        "graph",
                        "checks",
                        "note",
                        "revision",
                        "updated_by",
                        "updated_at",
                    ]
                )
                self.stdout.write(f"{flow.key}: แทนร่างรุ่น {draft.number} ด้วยข้อสรุป")
                continue
            if flow.versions.exists():
                self.stdout.write(f"{flow.key}: มีรุ่นอยู่แล้ว ไม่เปลี่ยน")
                continue
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
            if options.get("division") and row["division"] != options["division"]:
                continue
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
        if options["replace_draft"]:
            # A new draft may rename boxes: requirements follow the fixture's links.
            for row in data["requirements"]:
                if options.get("division") and row["division"] != options["division"]:
                    continue
                updates = {"node_id": row["node"], "remark": row["remark"]}
                if row["division"] == "กอจ.":
                    updates.update(
                        status=row["status"], flow=LineFlow.objects.filter(key=row["flow"]).first()
                    )
                DivisionRequirement.objects.filter(
                    division__code=row["division"], text=row["text"]
                ).update(**updates)
