"""ADR 0060: no location anywhere, and a person's unit is the roster's unit.

Q2: the location sentence leaves the answers; Q1: the retired location option goes; Q7: stored
location messages lose their place; Q8: requesters tied to the roster move to its unit; Q9: the
groups keep only roster units. Each change is audited like the code would (actor: migration)."""

from django.db import migrations
from django.db.models import F

SENTENCE = " ต้องส่งตำแหน่งที่พบเรื่อง"
LOCATION_FIELDS = ("latitude", "longitude", "address", "title")


def forward(apps, schema_editor):
    AuditEvent = apps.get_model("operations", "AuditEvent")
    ChatAnswer = apps.get_model("operations", "ChatAnswer")
    SystemOption = apps.get_model("operations", "SystemOption")
    WebhookInbox = apps.get_model("operations", "WebhookInbox")
    Requester = apps.get_model("operations", "Requester")
    District = apps.get_model("operations", "District")
    BroadcastSegment = apps.get_model("operations", "BroadcastSegment")

    def audit(action, target, **data):
        AuditEvent.objects.create(
            actor=None,
            actor_label="migration 0112",
            action=action,
            target_type=target.__class__.__name__,
            target_id=str(target.pk),
            reason="ADR 0060",
            data=data,
        )

    for answer in ChatAnswer.objects.filter(text__contains=SENTENCE.strip()):
        answer.text = answer.text.replace(SENTENCE, "").replace(SENTENCE.strip(), "").strip()
        answer.save(update_fields=["text"])
        audit("chat_answer.location_removed", answer, key=answer.key)

    SystemOption.objects.filter(key="registration_location").delete()

    for row in WebhookInbox.objects.filter(payload__message__type="location"):
        message = {k: v for k, v in row.payload["message"].items() if k not in LOCATION_FIELDS}
        row.payload = {**row.payload, "message": message}
        row.save(update_fields=["payload"])

    for requester in (
        Requester.objects.filter(
            employee_link__in=["verified", "admin"], employee__district__is_active=True
        )
        .exclude(district=F("employee__district"))
        .select_related("employee")
    ):
        before = requester.district_id
        requester.district_id = requester.employee.district_id
        requester.save(update_fields=["district"])
        audit(
            "requester.unit_followed_roster",
            requester,
            from_district=before,
            to_district=requester.district_id,
        )

    roster = set(
        District.objects.filter(is_active=True, employee__is_active=True).values_list(
            "pk", flat=True
        )
    )
    if not roster:  # no roster imported (a new install): nothing to narrow to
        return
    for segment in BroadcastSegment.objects.all():
        chosen = segment.criteria.get("districts")
        if not chosen:
            continue
        kept = [pk for pk in chosen if pk in roster]
        if not kept or kept == chosen:  # a group of only full-name เขต stays valid as it is
            continue
        criteria = {**segment.criteria, "districts": kept}
        segment.criteria, segment.revision = criteria, segment.revision + 1
        segment.save(update_fields=["criteria", "revision"])
        audit("broadcast.segment_units_narrowed", segment, removed=len(chosen) - len(kept))


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0111_no_location"),
    ]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
