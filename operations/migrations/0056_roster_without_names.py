"""ทะเบียนพนักงานไม่มีชื่อ (user, 23 Oct 2569): names leave the roster.

Before the names are wiped, the ones the system copied from the roster become the employee label
(`พนักงาน <รหัส>`, employees.label): verified LINE OA users, their registration requests, the
staff accounts made from the roster and the internal requesters named after those accounts. A
name someone typed is kept: only an exact copy of the roster name changes. Links made from a
matching name are cleared, since nothing proved them. The audit trail is not rewritten.
"""

from django.db import migrations, models

LABEL = "พนักงาน {}"


def _same(a, b):
    return bool(b) and " ".join((a or "").split()) == " ".join(b.split())


def forward(apps, schema_editor):
    Employee = apps.get_model("operations", "Employee")
    rows = {
        pk: (eid, name)
        for pk, eid, name in Employee.objects.values_list("pk", "employee_id", "name")
    }
    convert(
        apps.get_model("operations", "Requester"),
        apps.get_model("operations", "RegistrationRequest"),
        apps.get_model("operations", "StaffProfile"),
        rows,
    )


def convert(Requester, Registration, StaffProfile, rows):
    """`rows`: {employee pk: (employee ID, roster name)}, read before the column goes."""
    by_id = {eid: name for eid, name in rows.values()}
    Requester.objects.filter(employee_link="name").update(employee=None, employee_link="")
    for requester in Requester.objects.filter(employee_link="verified").exclude(employee=None):
        eid, name = rows[requester.employee_id]
        if _same(requester.display_name, name):
            requester.display_name = LABEL.format(eid)
            requester.save(update_fields=["display_name"])
    for registration in Registration.objects.exclude(employee=None).exclude(verified_by=""):
        eid, name = rows[registration.employee_id]
        if _same(registration.submitted_name, name):
            registration.submitted_name = LABEL.format(eid)
            registration.save(update_fields=["submitted_name"])
    renamed = {}
    profiles = StaffProfile.objects.exclude(employee_id="").filter(
        user__username__startswith="line-employee-"
    )
    for profile in profiles:
        if _same(profile.display_name, by_id.get(profile.employee_id)):
            renamed[profile.display_name] = LABEL.format(profile.employee_id)
            profile.display_name = renamed[profile.display_name]
            profile.save(update_fields=["display_name"])
    # ผู้แจ้งภายใน carry the staff name and the division code: "<name> (<code>)".
    for requester in Requester.objects.filter(is_internal=True):
        name, _, rest = requester.display_name.rpartition(" (")
        if name in renamed and rest.endswith(")"):
            requester.display_name = f"{renamed[name]} ({rest}"
            requester.save(update_fields=["display_name"])


def wipe(apps, schema_editor):
    apps.get_model("operations", "Employee").objects.exclude(name="").update(name="")


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0055_work_order_channel"),
    ]

    operations = [
        migrations.RunPython(forward, migrations.RunPython.noop),
        # The column stays empty for one release (the previous release still reads it while
        # a deploy runs); a later release drops it.
        migrations.AlterField(
            model_name="employee",
            name="name",
            field=models.CharField(blank=True, db_default="", default="", max_length=200),
        ),
        migrations.RunPython(wipe, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="requester",
            name="employee_link",
            field=models.CharField(
                blank=True,
                choices=[
                    ("verified", "ยืนยันด้วยรหัสพนักงาน"),
                    ("admin", "แอดมินผูก"),
                    ("unlinked", "แอดมินยกเลิกการผูก"),
                ],
                max_length=16,
            ),
        ),
    ]
