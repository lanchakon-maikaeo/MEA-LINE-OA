"""รายการอุปกรณ์ (user, 23 Oct 2569): the admin page for each item's LINE menu group and SAP
material code (moved out of the registry settings so it exists without the registry menu), and
the fields the LINE chat asks when a line is typed (typed_lines): labels, which are required,
fields of the admin's own and each group's unit. Every change is audited; a reason is optional.
"""

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render

from operations import registry, typed_lines
from operations.models import EntryField, EquipmentType
from operations.views import admin_required


def _messages(request, exc):
    for text in getattr(exc, "messages", None) or [str(exc)]:
        messages.error(request, text)


@admin_required
def admin_items(request):
    if request.method == "POST":
        post = request.POST
        try:
            if post.get("action") == "codes":
                reason = (post.get("reason") or "").strip()
                with transaction.atomic():
                    for item in EquipmentType.objects.order_by("code"):
                        value = post.get(f"code-{item.pk}")
                        if value is not None and value.strip() != item.official_code:
                            registry.set_official_code(request.user, item, value, reason)
                        group = post.get(f"group-{item.pk}")
                        if group is not None:
                            registry.set_line_group(request.user, item, group, reason)
            elif post.get("action") == "fields":
                _save_fields(request.user, post)
            elif post.get("action") == "add_field":
                typed_lines.add_field(
                    request.user,
                    post.get("group"),
                    post.get("label"),
                    post.get("required") == "1",
                    post.getlist("kinds"),
                    post.get("reason", ""),
                )
            elif post.get("action") == "codes_file" and request.FILES.get("file"):
                upload = request.FILES["file"]
                count = registry.import_official_codes(
                    request.user, upload.name, upload.read(registry.MAX_BYTES + 1)
                )
                messages.success(request, f"ผูกรหัสพัสดุ SAP {count} รายการแล้ว")
                return redirect("admin-items")
            else:
                raise Http404
        except (ValidationError, PermissionDenied) as exc:
            _messages(request, exc)
        else:
            messages.success(request, "บันทึกแล้ว")
        return redirect("admin-items")
    return render(
        request,
        "operations/admin_items.html",
        {
            "items": EquipmentType.objects.filter(entry_group="").order_by("code"),
            "line_groups": [
                c for c in EquipmentType.LineGroup.choices if c[0] in EquipmentType.MENU_GROUPS
            ],
            "groups": _groups(),
            "kinds": [
                ("withdrawal", "เบิก"),
                ("return", "ส่งคืน"),
                ("problem", "แจ้งปัญหา"),
            ],
        },
    )


def _groups():
    shared = {i.entry_group: i for i in EquipmentType.objects.exclude(entry_group="")}
    rows = []
    for group, label in EquipmentType.LineGroup.choices:
        fields = EntryField.objects.filter(group=group).order_by("position", "pk")
        rows.append(
            {
                "key": group,
                "label": label,
                "item": shared.get(group),
                "fields": [
                    {"field": f, "fixed": f.is_base and f.key in typed_lines.STRUCTURAL}
                    for f in fields
                ],
            }
        )
    return rows


@transaction.atomic
def _save_fields(actor, post):
    reason = post.get("reason", "")
    for field in EntryField.objects.order_by("pk"):
        if f"label-{field.pk}" not in post:
            continue
        typed_lines.update_field(
            actor,
            field,
            post.get(f"label-{field.pk}"),
            post.get(f"required-{field.pk}") == "1",
            post.getlist(f"kinds-{field.pk}"),
            post.get(f"position-{field.pk}") or field.position,
            post.get(f"active-{field.pk}") == "1",
            reason,
        )
    for group, _ in EquipmentType.LineGroup.choices:
        if f"unit-{group}" in post:
            typed_lines.set_unit(actor, group, post.get(f"unit-{group}"), reason)
