"""The form of a กอจ. sheet and its history; the sheet page is sheets_web (ADR 0033)."""

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, render

from operations import koj_tracking as tracking
from operations import sheet_access
from operations.models import District, KojHistory, KojSource


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
            "spreadsheet_id": "ลิงก์ Google Sheets (วางทั้งลิงก์ได้)",
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

    def clean_spreadsheet_id(self):
        return sheet_access.spreadsheet_id(self.cleaned_data.get("spreadsheet_id"))

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


@login_required
def history(request, pk):
    source = get_object_or_404(KojSource, pk=pk)
    sheet_access.require(request.user, source)  # ADR 0033: its ผู้ดูแลชีท, or an admin
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
