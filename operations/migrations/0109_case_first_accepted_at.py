"""ADR 0058 (owner, 16 พ.ย. 2569): รอรับงาน is a case nobody has pressed รับงาน on yet. The
current รับงาน is cleared on every new assignment, so the first one is kept apart. Old cases
take it from their history: รับงาน, taking the case (start_triage, claim), or assigning it to
yourself."""

from django.db import migrations, models

TAKES = ("start_triage", "claim", "accept_assignment")


def fill(apps, schema_editor):
    Case = apps.get_model("operations", "Case")
    Decision = apps.get_model("operations", "Decision")
    first = dict(Case.objects.filter(accepted_at__isnull=False).values_list("pk", "accepted_at"))
    rows = Decision.objects.filter(action__in=(*TAKES, "assign")).values_list(
        "case_id", "action", "actor_id", "data", "created_at"
    )
    for case_id, action, actor_id, data, at in rows:
        if action == "assign" and (data or {}).get("assignee_id") != actor_id:
            continue
        if case_id not in first or at < first[case_id]:
            first[case_id] = at
    for case_id, at in first.items():
        Case.objects.filter(pk=case_id).update(first_accepted_at=at)


class Migration(migrations.Migration):
    dependencies = [("operations", "0108_drop_the_tracking_number_hint")]
    operations = [
        migrations.AddField(
            model_name="case",
            name="first_accepted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(fill, migrations.RunPython.noop),
    ]
