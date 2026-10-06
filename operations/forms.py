from django import forms

from operations.models import Division, Role

REASON = forms.CharField(label="เหตุผล", max_length=500, widget=forms.TextInput)


class StaffCreateForm(forms.Form):
    username = forms.CharField(label="ชื่อผู้ใช้", max_length=150)
    display_name = forms.CharField(label="ชื่อ-นามสกุล", max_length=200)
    email = forms.EmailField(label="อีเมล", required=False)


class GrantRoleForm(forms.Form):
    role = forms.ChoiceField(label="บทบาท", choices=Role.choices)
    division = forms.ModelChoiceField(
        label="กอง (เว้นว่างสำหรับบทบาทระดับระบบ)",
        queryset=Division.objects.filter(is_active=True),
        required=False,
    )
    reason = forms.CharField(label="เหตุผล", max_length=500, required=False)


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
