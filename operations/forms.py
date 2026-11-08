from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model

from operations.models import (
    AIConfigVersion,
    AICredential,
    AIProfile,
    AuthorityGrant,
    District,
    Division,
    EquipmentType,
    Section,
    WorkGroup,
)

REASON = forms.CharField(label="เหตุผล", max_length=500, widget=forms.TextInput, required=False)


class StaffCreateForm(forms.Form):
    username = forms.CharField(label="ชื่อผู้ใช้", max_length=150)
    display_name = forms.CharField(label="ชื่อ-นามสกุล", max_length=200)
    email = forms.EmailField(label="อีเมล", required=False)


class GrantRoleForm(forms.Form):
    """The one role of a person (ADR 0041); a กอง role without a กอง takes their สังกัด, and a
    แอดมินแผนก/กลุ่มงาน without a unit takes the สังกัด's (ADR 0052 Q7)."""

    role = forms.ChoiceField(label="บทบาท")
    division = forms.ModelChoiceField(
        label="กอง (เว้นว่าง = กองในสังกัด)",
        queryset=Division.objects.filter(is_active=True),
        required=False,
    )
    unit = forms.ChoiceField(
        label="แผนกหรือกลุ่มงาน (แอดมินแผนก/กลุ่มงาน เว้นว่าง = หน่วยในสังกัด)", required=False
    )
    reason = forms.CharField(label="เหตุผล", max_length=500, required=False)

    def __init__(self, *args, **kwargs):
        from operations import identity

        super().__init__(*args, **kwargs)
        self.fields["role"].choices = [("", "เลือกบทบาท")] + [
            (r, identity.role_label(r)) for r in identity.PICKS
        ]
        units = [("", "หน่วยในสังกัด")]
        for section in Section.objects.filter(is_active=True, division__is_active=True).order_by(
            "division__code", "order", "name"
        ):
            units.append((f"s{section.pk}", f"{section.division.code} › {section.name}"))
            for group in section.work_groups.filter(is_active=True).order_by("order", "name"):
                units.append(
                    (f"g{group.pk}", f"{section.division.code} › {section.name} › {group.name}")
                )
        self.fields["unit"].choices = units

    def clean(self):
        """The unit key (s<pk>, g<pk>) becomes the section or work_group the role takes."""
        data = super().clean()
        key = data.pop("unit", "") or ""
        data["section"] = data["work_group"] = None
        if key.startswith("s"):
            data["section"] = Section.objects.filter(pk=key[1:], is_active=True).first()
        elif key.startswith("g"):
            data["work_group"] = WorkGroup.objects.filter(pk=key[1:], is_active=True).first()
        return data


class ReasonForm(forms.Form):
    reason = REASON


class ChangeDivisionForm(forms.Form):
    from_division = forms.ModelChoiceField(label="จากกอง", queryset=Division.objects.all())
    to_division = forms.ModelChoiceField(
        label="ไปกอง", queryset=Division.objects.filter(is_active=True)
    )
    reason = REASON


class DivisionCreateForm(forms.Form):
    code = forms.CharField(label="รหัส", max_length=32)
    name = forms.CharField(label="ชื่อ", max_length=200)
    kind = forms.ChoiceField(label="ประเภท", choices=Division.Kind.choices)


class RequesterCreateForm(forms.Form):
    display_name = forms.CharField(label="ชื่อ-นามสกุลผู้แจ้ง", max_length=200)
    district = forms.ModelChoiceField(
        label="หน่วยงาน (เขต/ฝ่าย)",
        queryset=District.objects.filter(is_active=True),
        to_field_name="code",
    )
    reason = forms.CharField(label="เหตุผล (เช่น ไม่ใช้ LINE)", max_length=500, required=False)


class AICredentialForm(forms.Form):
    name = forms.CharField(label="ชื่อ (เช่น thaillm)", max_length=64, initial="thaillm")
    base_url = forms.CharField(
        label="Base URL", max_length=200, initial="https://thaillm.or.th/api/v1"
    )
    api_key = forms.CharField(
        label="API key (กรอกใหม่เท่านั้น ระบบไม่แสดงค่าเดิม)",
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "off"}),
    )
    reason = forms.CharField(label="เหตุผล", max_length=500, required=False)


class AIProfileForm(forms.Form):
    credential = forms.ModelChoiceField(label="Credential", queryset=AICredential.objects.all())
    model_id = forms.CharField(label="Model ID ตามผู้ให้บริการ", max_length=100)
    timeout_seconds = forms.IntegerField(
        label="Timeout (วินาที)", min_value=2, max_value=30, initial=10
    )
    max_tokens = forms.IntegerField(label="Max tokens", min_value=64, max_value=2048, initial=800)
    temperature = forms.DecimalField(
        label="Temperature", min_value=0, max_value=1, decimal_places=2, initial="0.10"
    )
    use_json_schema = forms.BooleanField(
        label=(
            "ส่ง JSON schema ให้ผู้ให้บริการ (response_format) ถ้าผู้ให้บริการไม่บังคับ "
            "เช่น OpenTyphoon ระบบใส่ schema เดียวกันใน prompt ด้วย และตรวจคำตอบทุกครั้ง"
        ),
        required=False,
        initial=True,
    )


class AIPresetForm(forms.Form):
    task = forms.ChoiceField(
        label="งานของ AI",
        choices=[
            (t, AIConfigVersion.Task(t).label)
            for t in (AIConfigVersion.Task.EXTRACT, AIConfigVersion.Task.ANSWER)
        ],
        initial=AIConfigVersion.Task.EXTRACT,
        required=False,
        widget=forms.RadioSelect,
    )
    pipeline = forms.ChoiceField(
        label="รูปแบบการทำงานรอบโมเดล",
        choices=AIConfigVersion.Pipeline.choices,
        initial=AIConfigVersion.Pipeline.HARNESS,
        widget=forms.RadioSelect,
    )
    use_json_schema = AIProfileForm.base_fields["use_json_schema"]
    typed_fields = forms.BooleanField(
        label="อ่านเลขใบเบิก/ใบส่งคืน SAP และช่องที่พิมพ์ของมิเตอร์ CT VT (งานสกัดร่างเท่านั้น)",
        initial=True,
        required=False,
    )
    menu_fields = forms.BooleanField(
        label="อ่านว่าข้อความเป็นเรื่องของกองไหนและหัวข้อไหน เครื่องวัดและเขต หมายเลขอุปกรณ์ "
        "และงานซ่อม (ต้องอ่านช่องที่พิมพ์ด้วย)",
        initial=True,
        required=False,
    )
    slip_facts = forms.BooleanField(
        label="รู้ว่า DED Center ติดตามใบเบิกและใบส่งคืนที่ออกใน SAP เบิกของไม่ได้ และไม่มียอดคลัง "
        "(ADR 0032 ต้องอ่านกองและหัวข้อด้วย ประเมินด้วย dialogue-v2)",
        initial=True,
        required=False,
    )


class AIVersionForm(forms.Form):
    task = forms.ChoiceField(
        label="งานของ AI (แต่ละงานมีเกณฑ์ประเมินและรุ่นที่ใช้งานของตัวเอง)",
        choices=AIConfigVersion.Task.choices,
        initial=AIConfigVersion.Task.EXTRACT,
        required=False,
        widget=forms.RadioSelect,
    )
    primary = forms.ModelChoiceField(label="โมเดลหลัก", queryset=AIProfile.objects.all())
    fallback_1 = forms.ModelChoiceField(
        label="สำรอง 1", queryset=AIProfile.objects.all(), required=False
    )
    fallback_2 = forms.ModelChoiceField(
        label="สำรอง 2", queryset=AIProfile.objects.all(), required=False
    )
    fallback_3 = forms.ModelChoiceField(
        label="สำรอง 3", queryset=AIProfile.objects.all(), required=False
    )
    few_shot = forms.IntegerField(
        label="จำนวนตัวอย่างที่ค้นมาใส่ prompt (0 = ไม่ใช้)", min_value=0, max_value=5, initial=0
    )
    pipeline = forms.ChoiceField(
        label="รูปแบบการทำงานรอบโมเดล",
        choices=AIConfigVersion.Pipeline.choices,
        initial=AIConfigVersion.Pipeline.SINGLE,
        widget=forms.RadioSelect,
    )
    screen_profile = forms.ModelChoiceField(
        label="โมเดลคัดกรองข้อความสั่ง AI ก่อนสกัด (harness เท่านั้น ว่าง = โมเดลหลัก)",
        queryset=AIProfile.objects.all(),
        required=False,
    )
    typed_fields = AIPresetForm.base_fields["typed_fields"]
    menu_fields = AIPresetForm.base_fields["menu_fields"]
    slip_facts = AIPresetForm.base_fields["slip_facts"]
    note = forms.CharField(label="หมายเหตุ", max_length=500, required=False)

    def profiles(self):
        data = self.cleaned_data
        return [
            p
            for p in (data["primary"], data["fallback_1"], data["fallback_2"], data["fallback_3"])
            if p
        ]


DATETIME_LOCAL = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"]


class PolicyActivateForm(forms.Form):
    approved_by_name = forms.CharField(label="ผู้รับรองกติกา (เจ้าของกติกา)", max_length=200)
    approval_reference = forms.CharField(label="เลขเอกสารหรืออ้างอิงการรับรอง", max_length=200)
    reason = forms.CharField(label="เหตุผล", max_length=500, required=False)


class AuthorityGrantForm(forms.Form):
    user = forms.ModelChoiceField(
        label="ผู้ได้รับอำนาจ",
        queryset=get_user_model()
        .objects.filter(is_active=True, staff_profile__isnull=False)
        .order_by("username"),
    )
    activity = forms.ChoiceField(label="กิจกรรม", choices=AuthorityGrant.Activity.choices)
    division = forms.ModelChoiceField(
        label="ขอบเขตกอง (ว่าง = ทุกกอง)",
        queryset=Division.objects.filter(is_active=True),
        required=False,
    )
    # M07 Q2: approval/allocation may narrow to a category or an item; a cap needs an item.
    category = forms.ChoiceField(
        label="หมวดของ (อนุมัติเบิก/จัดสรร/รับรองสภาพเท่านั้น; ว่าง = ทุกหมวด)",
        choices=[("", "ทุกหมวด"), *EquipmentType.Category.choices],
        required=False,
    )
    item = forms.ModelChoiceField(
        label="รายการของ (ว่าง = ทุกรายการ)",
        queryset=EquipmentType.objects.filter(is_active=True),
        required=False,
    )
    max_quantity = forms.DecimalField(
        label="จำนวนอนุมัติสูงสุดต่อรายการ (ต้องระบุรายการของ)",
        required=False,
        min_value=Decimal("0.01"),
        max_digits=12,
        decimal_places=2,
    )
    valid_from = forms.DateTimeField(
        label="เริ่มมีผล (ว่าง = ตอนนี้)",
        required=False,
        input_formats=DATETIME_LOCAL,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    valid_until = forms.DateTimeField(
        label="สิ้นสุด (ว่าง = ไม่กำหนด)",
        required=False,
        input_formats=DATETIME_LOCAL,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    approval_reference = forms.CharField(label="ผู้รับรองหรือเลขเอกสารอำนาจ", max_length=200)
    reason = forms.CharField(label="เหตุผล", max_length=500, required=False)


class InventoryAdjustForm(forms.Form):
    """An admin correction of the fake store (M07 Q18), like a registry correction document."""

    item = forms.ModelChoiceField(
        label="รายการของ", queryset=EquipmentType.objects.filter(is_active=True)
    )
    bucket = forms.ChoiceField(
        label="ยอดที่ปรับ",
        choices=[
            ("available", "พร้อมจ่าย"),
            ("awaiting_inspection", "รอตรวจ"),
            ("awaiting_repair", "รอซ่อม"),
        ],
        initial="available",
        required=False,
    )
    delta = forms.DecimalField(
        label="เปลี่ยนยอดพร้อมจ่าย (+/-)", max_digits=12, decimal_places=2, initial=0
    )
    add_serials = forms.CharField(
        label="เพิ่มเลขเครื่อง (คั่นด้วยเว้นวรรค)", required=False, max_length=2000
    )
    remove_serials = forms.CharField(
        label="นำเลขเครื่องออก (คั่นด้วยเว้นวรรค)", required=False, max_length=2000
    )
    reason = forms.CharField(label="เหตุผล/เลขเอกสารอ้างอิง", max_length=500, required=False)


class SnapshotImportForm(forms.Form):
    """Registry figures for manual_reference mode (M07 Q10)."""

    rows = forms.CharField(
        label='ยอดเป็น JSON เช่น [{"item_id": "FUSE_SYN_A", "available": 40}]',
        widget=forms.Textarea(attrs={"rows": 6}),
    )
    as_of = forms.DateTimeField(
        label="ยอด ณ เวลา (as_of)",
        input_formats=DATETIME_LOCAL,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    source = forms.CharField(label="แหล่งยอด (ระบบ/เอกสาร)", max_length=200)
    is_synthetic = forms.BooleanField(label="เป็นข้อมูลจำลอง", required=False)
