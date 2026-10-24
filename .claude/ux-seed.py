# Synthetic cases for the local UX preview (mea_ci only). Run:
#   .claude/ux-preview.sh manage shell -c "exec(open('.claude/ux-seed.py').read())"
import random
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone

from operations import services
from operations.contracts import Actor
from operations.models import Case, CaseTriage, District, EquipmentType, Requester, RoleGrant

assert connection.settings_dict["NAME"] == "mea_ci", "preview seed runs on mea_ci only"
rng = random.Random(15)
seeder = get_user_model().objects.get(username="sim-seed")
districts = list(District.objects.all())
items = list(EquipmentType.objects.all())
people = []
for n in range(1, 15):
    r, _ = Requester.objects.get_or_create(
        display_name=f"ผู้แจ้งจำลอง {n:02d}",
        district=rng.choice(districts),
        defaults={"approved_by": seeder},
    )
    people.append(r)
staff = {}
for g in RoleGrant.objects.select_related("user", "division").filter(role="staff"):
    if g.division_id:
        staff.setdefault(g.division_id, g.user)


def act(user, case, action, payload=None):
    try:
        services.perform_action(
            Actor.staff(user),
            case.pk,
            action,
            expected_revision=Case.objects.get(pk=case.pk).revision,
            idempotency_key=f"ux-{uuid.uuid4().hex}",
            payload=payload,
        )
        return True
    except Exception:  # a guard refused it: the case simply stays where it is
        return False


def payload(kind):
    item = rng.choice(items)
    serial = [f"SN{rng.randint(100000, 999999)}"] if item.is_serialized else []
    if kind == "withdrawal":
        return {
            "kind": kind,
            "purpose": "ใช้งานหน้างานประจำสัปดาห์",
            "lines": [{"item_code": item.code, "quantity": str(rng.randint(1, 6))}],
        }
    if kind == "return":
        return {
            "kind": kind,
            "condition": "สภาพใช้งานได้",
            "reason": "เหลือจากงาน",
            "lines": [{"item_code": item.code, "quantity": "1", "serials": serial}],
        }
    return {
        "kind": "problem",
        "description": rng.choice(
            [
                "มิเตอร์อ่านค่าไม่ขึ้นหลังติดตั้ง",
                "หม้อแปลงมีเสียงดังผิดปกติ",
                "สายไฟอุปกรณ์ชำรุด",
                "เครื่องมือวัดค่าคลาดเคลื่อน",
                "ขอคำแนะนำการใช้อุปกรณ์ใหม่",
            ]
        ),
        "lines": [{"item_code": item.code, "serials": serial}],
    }


now = timezone.now()
made = 0
for i in range(70):
    kind = rng.choice(["withdrawal", "withdrawal", "return", "problem", "problem"])
    try:
        result = services.submit_case(
            Actor.for_requester(rng.choice(people)),
            idempotency_key=f"ux-seed-{i}",
            payload=payload(kind),
        )
    except Exception as exc:
        print("skip", i, type(exc).__name__, str(exc)[:80])
        continue
    case = Case.objects.get(pk=result.case_id)
    made += 1
    user = staff.get(case.owner_division_id)
    roll = rng.random()
    if user and roll > 0.2:
        act(user, case, "start_triage")
        if roll > 0.35:
            triage = CaseTriage.objects.filter(case=case).first()
            if triage and triage.suggested_priority:
                act(user, case, "set_priority", {"priority": triage.suggested_priority})
            if roll > 0.9:
                act(user, case, "decline", {"reason": "ข้อมูลไม่พอ ขอให้แจ้งใหม่"})
            elif roll > 0.82:
                act(user, case, "cancel", {"reason": "ผู้แจ้งขอยกเลิก"})
            elif roll > 0.72:
                act(user, case, "request_info", {"reason": "ขอรูปถ่ายเพิ่ม"})
            elif roll > 0.5:
                act(user, case, "complete_triage")
    # Spread the history over the last 30 days so the overview has a trend to show.
    back = timedelta(days=rng.randint(0, 29), hours=rng.randint(0, 9))
    Case.objects.filter(pk=case.pk).update(submitted_at=now - back, updated_at=now - back / 2)
    Case.objects.filter(pk=case.pk, ended_at__isnull=False).update(ended_at=now - back / 2)
print("cases made", made, "total", Case.objects.count())
