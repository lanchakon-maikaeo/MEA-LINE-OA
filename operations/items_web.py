"""รายการอุปกรณ์ (user, 23 Oct 2569): the admin page for each item's LINE menu group and SAP
material code, moved out of the registry settings so it exists without the registry menu.
Every change is audited (registry.set_line_group, set_official_code); a reason is optional.
"""

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render

from operations import registry
from operations.models import EquipmentType
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
            "items": EquipmentType.objects.order_by("code"),
            "line_groups": EquipmentType.LineGroup.choices,
        },
    )
