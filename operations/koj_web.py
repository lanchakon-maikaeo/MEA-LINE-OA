"""Private sources and manual refresh for กอจ.; recipient grants live on staff accounts."""

import csv
from io import StringIO

from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods, require_POST

from operations import identity
from operations import koj_tracking as tracking
from operations.models import District, KojHistory, KojRecord, KojSource
from operations.views import admin_required


class SourceForm(forms.ModelForm):
    completed = forms.CharField(
        label="สถานะที่ถือว่าจบงาน (บรรทัดละหนึ่งสถานะ)",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    district_aliases = forms.CharField(
        label="จับคู่เขตจากต้นทาง",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="บรรทัดละ ค่าจากชีท=ชื่อเขตในระบบ หากต้นทางใช้รหัสเขตตรงกับระบบอยู่แล้ว เว้นว่างได้",
    )
    equipment_aliases = forms.CharField(
        label="จับคู่ชนิดอุปกรณ์จากต้นทาง",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="บรรทัดละ ค่าจากชีท=ชื่ออุปกรณ์ เช่น RMU=RMU และ LBS=LBS",
    )

    class Meta:
        model = KojSource
        fields = [
            "name",
            "enabled",
            "provider",
            "service",
            "default_equipment",
            "spreadsheet_id",
            "sheet_name",
            "contact",
        ]
        labels = {
            "name": "ชื่อแหล่งข้อมูล",
            "enabled": "เปิดใช้งาน",
            "provider": "วิธีรับข้อมูล",
            "service": "บริการ",
            "default_equipment": "ชนิดอุปกรณ์เมื่อไม่ได้จับคู่คอลัมน์",
            "spreadsheet_id": "รหัสชีท (จากลิงก์ Google Sheets)",
            "sheet_name": "ชื่อแท็บ",
            "contact": "ช่องทางติดต่อเมื่อค้นไม่พบ",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["service"] = forms.ChoiceField(label="บริการ", choices=tracking.SERVICES.items())
        self.fields["default_equipment"] = forms.ChoiceField(
            label="ชนิดอุปกรณ์เมื่อไม่ได้จับคู่คอลัมน์", choices=tracking.EQUIPMENT.items()
        )
        defaults = {"reference": "WO/WBS/เลขรับ/ใบแจ้งซ่อม", "status": "สถานะงาน"}
        for key, label in tracking.COLUMNS.items():
            self.fields[f"column_{key}"] = forms.CharField(
                label=f"ชื่อคอลัมน์: {label}",
                required=key in tracking.REQUIRED,
                initial=self.instance.columns.get(key, defaults.get(key, "")),
                max_length=200,
            )
        self.fields["completed"].initial = "\n".join(self.instance.completed_statuses)
        self.fields["district_aliases"].initial = "\n".join(
            f"{k}={v}" for k, v in self.instance.district_map.items()
        )
        self.fields["equipment_aliases"].initial = "\n".join(
            f"{k}={tracking.EQUIPMENT.get(v, v)}" for k, v in self.instance.equipment_map.items()
        )

    def _aliases(self, field, choices):
        result = {}
        for line in self.cleaned_data.get(field, "").splitlines():
            if not line.strip():
                continue
            source, sep, target = line.partition("=")
            source, target = source.strip(), target.strip()
            if not sep or not source or target not in choices or source in result:
                raise ValidationError("ตรวจตารางจับคู่: ใช้ ค่าต้นทาง=ค่าในระบบ และห้ามค่าต้นทางซ้ำ")
            result[source] = choices[target]
        return result

    def clean(self):
        values = super().clean()
        if self.errors:
            return values
        source = self.instance
        for key in self.Meta.fields:
            setattr(source, key, values[key])
        source.columns = {
            k: values[f"column_{k}"].strip() for k in tracking.COLUMNS if values.get(f"column_{k}")
        }
        districts = {}
        for d in District.objects.filter(is_active=True):
            districts[d.code], districts[d.name] = d.code, d.code
        source.district_map = self._aliases("district_aliases", districts)
        equipment = {
            **{v: k for k, v in tracking.EQUIPMENT.items()},
            **{k: k for k in tracking.EQUIPMENT},
        }
        source.equipment_map = self._aliases("equipment_aliases", equipment)
        source.completed_statuses = list(
            dict.fromkeys(v.strip() for v in values["completed"].splitlines() if v.strip())
        )
        tracking.check_source(source)
        return values


@admin_required
@require_http_methods(["GET", "POST"])
def sources(request):
    # 28 ต.ค. 2569: the sources page sits in the admin menu only (ADR 0026).
    selected = (
        get_object_or_404(KojSource, pk=request.GET["edit"])
        if request.GET.get("edit", "").isdigit()
        else None
    )
    form = SourceForm(instance=selected)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "save":
            pk = request.POST.get("source", "")
            if pk and not pk.isdigit():
                raise Http404
            selected = get_object_or_404(KojSource, pk=pk) if pk.isdigit() else None
            previous = tracking._config(selected) if selected else None
            form = SourceForm(request.POST, instance=selected)
            if form.is_valid():
                with transaction.atomic():
                    source = form.save(commit=False)
                    fields = [
                        *SourceForm.Meta.fields,
                        "columns",
                        "district_map",
                        "equipment_map",
                        "completed_statuses",
                        "updated_at",
                    ]
                    source.save(update_fields=fields if source.pk else None)
                    if previous is not None and previous != tracking._config(source):
                        source.records.update(present=False)
                        KojSource.objects.filter(pk=source.pk).update(
                            last_error="ตั้งค่าใหม่แล้ว รออ่านข้อมูลรอบถัดไป"
                        )
                identity.audit(request.user, "koj.source_saved", source)
                messages.success(request, "บันทึกแหล่งข้อมูล กอจ. แล้ว")
                return redirect("koj-sources")
        elif action in {"refresh", "import"}:
            pk = request.POST.get("source", "")
            if not pk.isdigit():
                raise Http404
            source = get_object_or_404(KojSource, pk=int(pk))
            try:
                if action == "refresh":
                    tracking.request_refresh(request.user, source)
                    messages.success(request, "สั่งอ่านใหม่แล้ว ดูผลที่เวลาข้อมูลล่าสุด")
                else:
                    upload = request.FILES.get("file")
                    if source.provider != "csv" or upload is None or upload.size > 20_000_000:
                        raise ValidationError("เลือกแหล่ง CSV และไฟล์ UTF-8 ไม่เกิน 20 MB")
                    matrix = list(csv.reader(StringIO(upload.read().decode("utf-8-sig"))))
                    count = tracking.apply_snapshot(source.pk, matrix)
                    identity.audit(request.user, "koj.csv_imported", source, count=count)
                    messages.success(request, f"นำเข้า {count} แถวแล้ว")
            except (ValidationError, UnicodeError, csv.Error) as exc:
                messages.error(
                    request,
                    "; ".join(exc.messages)
                    if isinstance(exc, ValidationError)
                    else "ไฟล์ CSV ไม่ถูกต้อง",
                )
            return redirect("koj-sources")
    return render(
        request,
        "operations/koj_sources.html",
        {
            "sources": KojSource.objects.all(),
            "form": form,
            "selected": selected,
            "can_configure": True,
            "equipment": tracking.EQUIPMENT,
            "services": tracking.SERVICES,
            "unmapped": KojRecord.objects.filter(present=True, district=None).count(),
        },
    )


@admin_required
@require_POST
def recipients(request, pk):
    user = get_object_or_404(get_user_model(), pk=pk)
    try:
        tracking.set_recipients(request.user, user, request.POST.getlist("koj_groups"))
        messages.success(request, "บันทึกชุดรับแจ้ง กอจ. แล้ว")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    back = request.POST.get("next", "")
    if back and url_has_allowed_host_and_scheme(back, {request.get_host()}):
        return redirect(back)
    return redirect("admin-user-detail", pk=user.pk)


@admin_required
def history(request, pk):
    source = get_object_or_404(KojSource, pk=pk)
    records = source.records.order_by("reference_key", "pk")
    query = request.GET.get("q", "").strip()
    if query:
        try:
            records = records.filter(reference_key__startswith=tracking.reference_key(query))
        except ValidationError:
            records = records.none()
    from django.core.paginator import Paginator

    page = Paginator(records, 30).get_page(request.GET.get("page"))
    return render(
        request,
        "operations/koj_history.html",
        {
            "source": source,
            "page": page,
            "q": query,
            "history": KojHistory.objects.filter(record__in=page.object_list)
            .select_related("record")
            .order_by("-observed_at", "-pk")[:150],
        },
    )
