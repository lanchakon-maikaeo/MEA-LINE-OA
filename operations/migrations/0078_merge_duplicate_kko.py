"""กคอ. twice (ADR 0030): the row of the code before 15 Oct 2569 (กองเครื่องมือฯ) and the row the
real structure loaded (กคอ.) are one division. Everything that points at the old row moves to
กคอ., a แผนก with the same name becomes the one กคอ. already has, and the old row is switched off,
never deleted. A role or emergency contact กคอ. already has is ended on the old row instead.
Nothing happens where only one of the two rows exists."""

from django.db import migrations
from django.utils import timezone

OLD, NEW = "กองเครื่องมือฯ", "กคอ."
REASON = "รวมกอง กองเครื่องมือฯ เข้า กคอ. (ADR 0030)"


def _pointers(apps, target):
    """(model, field name) for every foreign key of this app to `target`."""
    found = []
    for model in apps.get_app_config("operations").get_models():
        for field in model._meta.fields:
            if field.is_relation and field.related_model is target:
                found.append((model, field.name))
    return found


def merge(apps, schema_editor):
    Division = apps.get_model("operations", "Division")
    Section = apps.get_model("operations", "Section")
    old = Division.objects.filter(code=OLD).first()
    new = Division.objects.filter(code=NEW).first()
    if old is None or new is None or old.pk == new.pk:
        return
    now = timezone.now()

    # แผนก: same name → the one กคอ. has; otherwise the section moves with its people.
    for section in Section.objects.filter(division=old):
        twin = Section.objects.filter(division=new, name=section.name).first()
        if twin is None:
            section.division = new
            section.save(update_fields=["division"])
            continue
        for model, name in _pointers(apps, Section):
            if model is not Section:
                model.objects.filter(**{name: section}).update(**{name: twin})

    RoleGrant = apps.get_model("operations", "RoleGrant")
    for grant in RoleGrant.objects.filter(division=old, revoked_at__isnull=True):
        held = RoleGrant.objects.filter(
            division=new, user_id=grant.user_id, role=grant.role, revoked_at__isnull=True
        ).exists()
        if held:
            grant.revoked_at = now
            grant.reason = REASON
            grant.save(update_fields=["revoked_at", "reason"])
        else:
            grant.division = new
            grant.save(update_fields=["division"])

    Contact = apps.get_model("operations", "EmergencyContact")
    for contact in Contact.objects.filter(division=old):
        if Contact.objects.filter(division=new, user_id=contact.user_id).exists():
            contact.delete()
        else:
            contact.division = new
            contact.save(update_fields=["division"])

    Schedule = apps.get_model("operations", "DivisionSchedule")
    if Schedule.objects.filter(division=new).exists():
        Schedule.objects.filter(division=old).delete()  # กคอ. keeps its own table

    ChildWork = apps.get_model("operations", "ChildWork")
    done = {(Section, "division"), (Contact, "division"), (RoleGrant, "division")}
    for model, name in _pointers(apps, Division):
        if (model, name) in done:
            continue  # above; ended role grants stay on the old row as history
        rows = model.objects.filter(**{name: old})
        if model is ChildWork:
            # One open child work per case and division: an open one stays where it is.
            rows = rows.exclude(case__in=ChildWork.objects.filter(division=new).values("case"))
        rows.update(**{name: new})

    old.is_active = False
    old.save(update_fields=["is_active"])


class Migration(migrations.Migration):
    dependencies = [("operations", "0077_problem_tile_division_pick")]

    operations = [migrations.RunPython(merge, migrations.RunPython.noop)]
