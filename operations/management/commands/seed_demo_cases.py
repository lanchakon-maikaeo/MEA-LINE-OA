import json

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction

from operations.contracts import Actor
from operations.management.demo import DemoCommand
from operations.models import District, EquipmentType, Requester
from operations.services import submit_case

DIVISIBLE_UNITS = {"เมตร"}
SEED_USERNAME = "sim-seed"


class Command(DemoCommand):
    help = "Create synthetic items, requesters and example cases (local/CI demo DB only)."

    def add_arguments(self, parser):
        parser.add_argument("--source", default=str(settings.BASE_DIR / "synthetic-data.json"))

    @transaction.atomic
    def handle_demo(self, *args, source, **options):
        with open(source, encoding="utf-8") as stream:
            data = json.load(stream)
        for row in data["items"]:
            EquipmentType.objects.update_or_create(
                code=row["id"],
                defaults={
                    "name": row["name"],
                    "category": row["category"],
                    "unit": row["unit"],
                    "is_serialized": row["serialized"],
                    "is_divisible": row.get("divisible", row["unit"] in DIVISIBLE_UNITS),
                    "is_synthetic": True,
                },
            )
        # Synthetic approvals need an approver; this account cannot log in.
        seeder, created = get_user_model().objects.get_or_create(
            username=SEED_USERNAME, defaults={"is_active": False}
        )
        if created:
            seeder.set_unusable_password()
            seeder.save(update_fields=["password"])
        for row in data["example_cases"]:
            district = District.objects.get(code=row["district_id"])
            requester, _ = Requester.objects.get_or_create(
                display_name=f"ผู้แจ้งจำลอง {district.code}",
                district=district,
                defaults={"approved_by": seeder},
            )
            # Same key every run: a re-run replays the original submission.
            submit_case(
                Actor.for_requester(requester),
                idempotency_key=f"seed-{row['id']}",
                payload=_payload(row),
            )
        self.stdout.write(
            f"Demo items: {len(data['items'])}, cases: {len(data['example_cases'])} ensured."
        )


def _payload(row):
    if row["type"] == "withdrawal":
        return {
            "kind": "withdrawal",
            "purpose": row["message"],
            "lines": [
                {"item_code": line["item_id"], "quantity": str(line["requested"])}
                for line in row["lines"]
            ],
        }
    if row["type"] == "return":
        return {
            "kind": "return",
            "condition": "ตามที่แจ้งในข้อความ",
            "reason": row["message"],
            "source_reference": row.get("source_document", ""),
            "lines": [
                {
                    "item_code": line["item_id"],
                    "quantity": str(line["requested_return"]),
                    "serials": line.get("requested_serials", []),
                }
                for line in row["lines"]
            ],
        }
    return {
        "kind": "problem",
        "description": row["message"],
        "lines": [{"item_code": row["item_id"], "serials": [row["serial"]]}]
        if row.get("item_id")
        else [],
    }
