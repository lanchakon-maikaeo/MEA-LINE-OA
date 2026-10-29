"""ตารางรถของกองจากไฟล์ (grill กคอ. 28 ต.ค. 2569 Q15, Q16, Q26, Q32).

Safe to run again: weekdays of an abbreviation already in the table, a contact or a stage name
staff set are kept; only what is missing is filled in. The truck becomes the division's default
way to send only when the schedule is new.
"""

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from operations.models import Calendar, Division, DivisionSchedule

FIXTURES = Path(settings.BASE_DIR) / "operations" / "fixtures"


class Command(BaseCommand):
    help = "Load a division's truck table (weekdays by ชื่อย่อ ฟข.) from a fixture file."

    def add_arguments(self, parser):
        parser.add_argument("file", nargs="?", default="te_delivery_kko_v1.json")

    @transaction.atomic
    def handle(self, *args, file, **options):
        data = json.loads((FIXTURES / file).read_text(encoding="utf-8"))
        division = Division.objects.filter(code=data["division"]).first()
        if division is None:
            self.stdout.write(f"ไม่พบกอง {data['division']} ข้าม")
            return
        schedule, created = DivisionSchedule.objects.get_or_create(
            division=division,
            defaults={
                "issue_weekdays": [0, 1, 2, 3, 4],
                "delivery": data.get("delivery", DivisionSchedule.Delivery.CHOOSE),
                "lead_days": data.get("lead_days", 0),
            },
        )
        trucks = dict(schedule.truck_days or {})
        added = 0
        for weekday, names in data["weekdays"].items():
            for name in names:
                days = trucks.setdefault(name, [])
                if int(weekday) not in days and not schedule.truck_days.get(name):
                    days.append(int(weekday))
                    added += 1
        schedule.truck_days = {k: sorted(v) for k, v in trucks.items()}
        if not schedule.contact:
            schedule.contact = data.get("phone", "")
        labels = dict(schedule.stage_labels or {})
        for kind, names in data.get("stage_labels", {}).items():
            labels[kind] = {**names, **labels.get(kind, {})}
        schedule.stage_labels = labels
        schedule.save()
        if data.get("calendar"):
            Calendar.objects.get_or_create(
                key=data["calendar"]["key"],
                defaults={"name": data["calendar"]["name"], "division": division},
            )
        state = "สร้างตารางใหม่" if created else "เติมตารางเดิม"
        self.stdout.write(f"{division.code}: {state} วันรถเพิ่ม {added} ช่อง")
