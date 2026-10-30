"""ชีทที่ระบบอ่าน (ADR 0033): one page for both kinds of sheet the chat reads, grouped by the
unit that owns them, and the ผู้ดูแลชีท an admin names for each unit.

29 ต.ค. 2569, the user: the old two pages were hard to find and to use ("ใช้ยาก ไม่ user
friendly"). A sheet's form starts with the link, the tab and on/off; ทดสอบอ่าน shows the first
rows (or the tabs, when the tab is wrong) before anything is saved.

The same day ("ไม่เอาการตั้งค่าที่เป็นลักษณะแต่ละชีทมาปนกับชีทอื่น"): a ค้นสถานะ sheet's form
asks what the chat user types and then shows that แบบชีท's columns only, and ทดสอบอ่าน shows the
line the chat would answer. A กอจ. sheet shows the link and tab for Google Sheets only, and a CSV
sheet takes its file with บันทึก. The other choice's part of the form is hidden and disabled
(data-when, app.js), so it is neither checked nor sent.
"""

import csv
from io import StringIO

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from operations import identity, koj_sheets, sheet_access, sheet_lookup, units
from operations import koj_tracking as tracking
from operations.koj_web import SourceForm
from operations.models import (
    KojRecord,
    KojSource,
    LineFlowVersion,
    SheetEditor,
    SheetSource,
)

KINDS = {"lookup": "ค้นสถานะในแชต", "koj": "ติดตามงาน กอจ."}
# แบบชีท of a ค้นสถานะ sheet, as the one who looks after it would put it: what the chat user types.
TYPES = (
    (
        SheetSource.Match.DIGITS,
        "เลขเรื่องที่ได้ตอนแจ้ง",
        "เช่น เลข UI ของ SMG แชตตอบสถานะของเลขนั้นทุกครั้งที่แจ้งไว้",
    ),
    (
        SheetSource.Match.CODE,
        "หมายเลขที่มีตัวอักษร",
        "เช่น หมายเลขอุปกรณ์ NC801 หรือเลขงานซ่อมของ ซจ. แชตตอบแถวล่าสุดของเลขนั้น"
        " ถ้าตั้งคอลัมน์ ฟข. ผู้ใช้จะเห็นเฉพาะแถวของ ฟข. ตัวเอง",
    ),
)
SEARCHED_BY = {value: name for value, name, _ in TYPES}
PREVIEW_ROWS = 3
# The KojSource fields in the order shown: the basics, the Google Sheets part, the end; the rest
# (columns, matching) fold under ตั้งค่าคอลัมน์.
KOJ_BASIC = ("name", "service", "provider")
KOJ_GOOGLE = ("spreadsheet_id", "sheet_name")
KOJ_END = ("contact", "enabled")
KOJ_SAVED = (
    *SourceForm.Meta.fields,
    "columns",
    "district_map",
    "equipment_map",
    "completed_statuses",
    "division",
    "section",
    "work_group",
)


@login_required
@require_http_methods(["GET", "HEAD", "POST"])
def page(request):
    user = request.user
    if not sheet_access.may_open(user):
        raise PermissionDenied
    admin = identity.staff_scope(user).is_admin
    form = None
    if request.method == "POST":
        do, kind = request.POST.get("do", ""), request.POST.get("kind", "")
        if do in {"grant", "revoke"}:
            return _editors(request, do)
        if kind == "lookup":
            result = _lookup_post(request, do)
        elif kind == "koj":
            result = _koj_post(request, do)
        else:
            raise Http404
        if not isinstance(result, dict):
            return result
        form = result
    else:
        form = _form_from_query(request)
    return render(request, "operations/sheets.html", _context(user, admin, form))


def _lookup_source(user, key):
    if not key:
        return None
    source = get_object_or_404(SheetSource, key=key)
    sheet_access.require(user, source)
    return source


def _koj_source(user, pk):
    if not pk:
        return None
    if not str(pk).isdigit():
        raise Http404
    source = get_object_or_404(KojSource, pk=pk)
    sheet_access.require(user, source)
    return source


def _form_from_query(request):
    """?lookup=<key>, ?koj=<pk> or ?new=lookup|koj opens one sheet's form."""
    user = request.user
    if request.GET.get("lookup"):
        source = _lookup_source(user, request.GET["lookup"])
        return {"kind": "lookup", "source": source, "draft": source}
    if request.GET.get("koj"):
        source = _koj_source(user, request.GET["koj"])
        return {"kind": "koj", "source": source, "koj_form": SourceForm(instance=source)}
    if request.GET.get("new") == "lookup":
        draft = SheetSource(first_row=2, number_label="")
        return {"kind": "lookup", "source": None, "draft": draft}
    if request.GET.get("new") == "koj":
        return {"kind": "koj", "source": None, "koj_form": SourceForm(instance=KojSource())}
    return None


def _lookup_post(request, do):
    user, data = request.user, request.POST
    source = _lookup_source(user, data.get("source", ""))
    if do == "refresh" and source is not None:
        sheet_lookup.request_refresh(user, source)
        messages.success(request, f"สั่งอ่าน {source.name} ใหม่แล้ว")
        return redirect("sheets")
    if do not in {"save", "test"}:
        raise Http404
    form = {"kind": "lookup", "source": source}
    try:
        if do == "save":
            saved = sheet_lookup.save_source(user, source, data, data.get("reason"))
            messages.success(request, f"บันทึก {saved.name} แล้ว ระบบกำลังอ่านชีทใหม่")
            return redirect("sheets")
        draft = sheet_lookup.build_source(user, _fresh(source), data)
        form["draft"] = draft
        form["preview"] = _preview_lookup(draft)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        form["draft"] = _keep_typed(user, source, data)
    return form


def _fresh(source):
    return SheetSource.objects.get(pk=source.pk) if source else None


def _keep_typed(user, source, data):
    """The form again as typed, when what was typed does not pass the checks."""
    draft = _fresh(source) or SheetSource(first_row=2, number_label="")
    for name in sheet_lookup.FIELDS:
        if name in data:
            setattr(draft, name, data.get(name))
    if data.get("match") in SheetSource.Match.values:
        draft.match = data["match"]
    draft.spreadsheet_id = data.get("link", draft.spreadsheet_id)
    draft.enabled = bool(data.get("enabled"))
    draft.unit_key = data.get("unit", "")
    return draft


def _preview_lookup(source):
    wanted = sheet_lookup.read_list(source)
    columns = [c for c, _ in wanted]
    try:
        lists = koj_sheets.read_columns(
            source.spreadsheet_id,
            source.sheet_name,
            columns,
            1,
            source.first_row + PREVIEW_ROWS - 1,
        )
    except ValidationError as exc:
        return _preview_failed(source.spreadsheet_id, exc)
    height = max((len(c) for c in lists), default=0)
    rows = [
        {"number": i + 1, "cells": [c[i] if i < len(c) else "" for c in lists]}
        for i in range(height)
    ]
    data = [dict(zip(columns, r["cells"], strict=True)) for r in rows[source.first_row - 1 :]]
    return {
        "columns": [f"{what} ({column})" for column, what in wanted],
        "rows": rows,
        "first_row": source.first_row,
        "answer": next((said for row in data if (said := sheet_lookup.sample(source, row))), ""),
    }


def _preview_failed(spreadsheet_id, exc):
    try:
        tabs = koj_sheets.tab_names(spreadsheet_id)
    except ValidationError:
        tabs = []
    return {"error": " ".join(exc.messages), "tabs": tabs}


def _koj_post(request, do):
    user, data = request.user, request.POST
    selected = _koj_source(user, data.get("source", ""))
    if do == "refresh" and selected is not None:
        try:
            tracking.request_refresh(user, selected)
            messages.success(request, "สั่งอ่านใหม่แล้ว ดูผลที่เวลาข้อมูลล่าสุด")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        return redirect("sheets")
    if do == "import" and selected is not None:
        _koj_import(request, selected)
        return redirect("sheets")
    if do not in {"save", "test"}:
        raise Http404
    if selected is None and not sheet_access.may_open(user):
        raise PermissionDenied
    previous = tracking._config(selected) if selected else None
    koj_form = SourceForm(data, instance=selected or KojSource())
    form = {"kind": "koj", "source": selected, "koj_form": koj_form}
    if not koj_form.is_valid():
        return form
    source = koj_form.instance
    try:
        sheet_access.place(user, source, data.get("unit") or "")
        if do == "test":
            form["preview"] = _preview_koj(source)
            return form
        reason = (data.get("reason") or "").strip()
        if not reason:
            raise ValidationError("ใส่เหตุผลก่อนบันทึก")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return form
    with transaction.atomic():
        # A known sheet keeps the worker's read results (last_synced_at, last_error, ...).
        source.save(update_fields=[*KOJ_SAVED, "updated_at"] if source.pk else None)
        if previous is not None and previous != tracking._config(source):
            source.records.update(present=False)
            KojSource.objects.filter(pk=source.pk).update(last_error="ตั้งค่าใหม่แล้ว รออ่านข้อมูลรอบถัดไป")
        identity.audit(user, "koj.source_saved", source, reason[:300])
    messages.success(request, f"บันทึก {source.name} แล้ว")
    if source.provider == "csv" and request.FILES.get("file"):
        _koj_import(request, source)
    return redirect("sheets")


def _preview_koj(source):
    if source.provider != "google":
        return {"error": "ทดสอบอ่านใช้กับ Google Sheets แหล่ง CSV ให้นำเข้าไฟล์", "tabs": []}
    try:
        matrix = koj_sheets.fetch(source)
    except ValidationError as exc:
        return _preview_failed(source.spreadsheet_id, exc)
    head = matrix[0] if matrix else []
    wanted = [name for name in source.columns.values() if name]
    picked = [i for i, name in enumerate(head) if name in wanted]
    missing = [name for name in wanted if name not in head]
    rows = [
        {"number": n + 1, "cells": [row[i] if i < len(row) else "" for i in picked]}
        for n, row in enumerate(matrix[: 1 + PREVIEW_ROWS])
    ]
    return {"columns": [head[i] for i in picked], "rows": rows, "first_row": 2, "missing": missing}


def _koj_import(request, source):
    upload = request.FILES.get("file")
    try:
        if source.provider != "csv" or upload is None or upload.size > 20_000_000:
            raise ValidationError("เลือกแหล่ง CSV และไฟล์ UTF-8 ไม่เกิน 20 MB")
        matrix = list(csv.reader(StringIO(upload.read().decode("utf-8-sig"))))
        count = tracking.apply_snapshot(source.pk, matrix)
        identity.audit(request.user, "koj.csv_imported", source, count=count)
        messages.success(request, f"นำเข้า {count} แถวแล้ว")
    except (ValidationError, UnicodeError, csv.Error) as exc:
        messages.error(
            request,
            " ".join(exc.messages) if isinstance(exc, ValidationError) else "ไฟล์ CSV ไม่ถูกต้อง",
        )


def _editors(request, do):
    if not identity.staff_scope(request.user).is_admin:
        raise PermissionDenied
    try:
        if do == "grant":
            sheet_access.grant(
                request.user,
                request.POST.get("person", ""),
                request.POST.get("unit", ""),
                request.POST.get("reason"),
            )
            messages.success(request, "เพิ่มผู้ดูแลชีทแล้ว")
        else:
            sheet_access.revoke(
                request.user, request.POST.get("editor", ""), request.POST.get("reason")
            )
            messages.success(request, "ถอนสิทธิ์ผู้ดูแลชีทแล้ว")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect(reverse("sheets") + "#editors")


def _flows_using():
    """{sheet key: [flow names]} of the published LINE flows whose ค้นในชีท boxes read it."""
    found = {}
    for version in LineFlowVersion.objects.filter(
        status=LineFlowVersion.Status.PUBLISHED
    ).select_related("flow"):
        for node in (version.graph or {}).get("nodes", []):
            if node.get("kind") == "sheet_lookup":
                key = (node.get("config") or {}).get("source", "")
                names = found.setdefault(key, [])
                if version.flow.name not in names:
                    names.append(version.flow.name)
    return found


def _context(user, admin, form):
    flows = _flows_using()
    groups = {}

    def add(source, kind):
        unit = sheet_access.unit_of(source)
        key = unit.key if unit else ""
        group = groups.setdefault(
            key, {"label": unit.path if unit else "ยังไม่ระบุหน่วย", "sheets": []}
        )
        group["sheets"].append(
            {
                "kind": kind,
                "kind_label": KINDS[kind],
                "source": source,
                "what": (
                    tracking.SERVICES.get(source.service, source.service)
                    if kind == "koj"
                    else "ใช้ใน " + ", ".join(flows[source.key])
                    if source.key in flows
                    else "ยังไม่มี flow LINE ไหนใช้"
                ),
                "edit": f"?{kind}={source.key if kind == 'lookup' else source.pk}",
                "searched_by": SEARCHED_BY.get(source.match, "") if kind == "lookup" else "",
            }
        )

    related = ("division", "section", "work_group")
    for source in SheetSource.objects.select_related(*related):
        if sheet_access.can_edit(user, source):
            add(source, "lookup")
    for source in KojSource.objects.select_related(*related):
        if sheet_access.can_edit(user, source):
            add(source, "koj")
    options = sheet_access.unit_options(user)
    if form is not None:
        form["unit"] = _unit_key(form)
        form["units"] = options
        form["kind_label"] = KINDS[form["kind"]]
        if form["kind"] == "koj":
            bound = form["koj_form"]
            form["provider"] = bound["provider"].value() or "google"
            form["basic"] = [bound[n] for n in KOJ_BASIC]
            form["google"] = [bound[n] for n in KOJ_GOOGLE]
            form["end"] = [bound[n] for n in KOJ_END]
            shown = {*KOJ_BASIC, *KOJ_GOOGLE, *KOJ_END}
            form["advanced"] = [f for f in bound if f.name not in shown]
    context = {
        "groups": [groups[k] for k in sorted(groups, key=lambda k: (k != "", groups[k]["label"]))],
        "form": form,
        "admin": admin,
        "reader": koj_sheets.reader_email(),
        "every": int(sheet_lookup.EVERY.total_seconds() // 60),
        "types": TYPES,
        "unmapped": KojRecord.objects.filter(present=True, district=None).count(),
    }
    if admin:
        context["editors"] = [
            {"row": e, "unit": units.Unit.of(e.division, e.section, e.work_group).path}
            for e in SheetEditor.objects.select_related(
                "user__staff_profile", "division", "section", "work_group", "added_by"
            )
        ]
        context["people"] = (
            get_user_model()
            .objects.filter(is_active=True, staff_profile__isnull=False)
            .select_related("staff_profile")
            .order_by("staff_profile__display_name")
        )
        context["all_units"] = [o for o in options if o[0]]
    return context


def _unit_key(form):
    draft = form.get("draft")
    if draft is not None and hasattr(draft, "unit_key"):
        return draft.unit_key
    source = draft if form["kind"] == "lookup" else form["koj_form"].instance
    unit = sheet_access.unit_of(source) if source is not None else None
    return unit.key if unit else ""
