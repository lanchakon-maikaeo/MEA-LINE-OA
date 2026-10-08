"""Registry pages of the staff web (M11 Q6): ส่วนต่างทะเบียน, SAP uploads and settings.

Registry staff (the policy's registry unit, SIM-REG in the synthetic policy) see every
discrepancy and decide it; an admin sees everything read-only; another division sees only what
was sent to it for checking. Settings and official codes are admin-only.
"""

import csv
import io
from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST, require_safe

from operations import identity, registry, reports
from operations.models import EquipmentType, RegistryDiscrepancy, RegistryImport

D = RegistryDiscrepancy
TABS = [
    ("open", "รอตรวจ"),
    ("with_division", "รอกองตรวจ"),
    ("waiting_registry", "รอทะเบียนแก้"),
    ("resolved", "ปิดแล้ว (30 วัน)"),
    ("unreconciled", "ยังไม่ได้กระทบยอด"),
]
CLOSED_DAYS = 30


def _messages(request, exc):
    for text in getattr(exc, "messages", None) or [str(exc)]:
        messages.error(request, text)


def _tabs(user):
    if registry.may_open(user):
        return TABS
    return [("with_division", "รอกองตรวจ")]


def _rows(user, tab, kind, now):
    seen = registry.visible_discrepancies(user).select_related(
        "case", "record__division", "division", "record__recorded_by__staff_profile"
    )
    if kind in D.Kind.values:
        seen = seen.filter(kind=kind)
    if tab == "resolved":
        return list(
            seen.filter(
                status=D.Status.RESOLVED, resolved_at__gte=now - timedelta(days=CLOSED_DAYS)
            ).order_by("-resolved_at")[:200]
        )
    return list(seen.filter(status=tab).order_by("opened_at")[:200])


@login_required
@require_safe
def home(request):
    user, now = request.user, timezone.now()
    full = registry.may_open(user)
    if not full and not registry.visible_discrepancies(user).exists():
        raise Http404
    tabs = _tabs(user)
    keys = [key for key, _ in tabs]
    tab = request.GET.get("tab", keys[0])
    if tab not in keys:
        tab = keys[0]
    kind = request.GET.get("kind", "")
    counts = registry.counts(user)
    unreconciled = registry.unreconciled(now) if full else []
    context = {
        "tabs": [
            (key, label, len(unreconciled) if key == "unreconciled" else counts.get(key))
            for key, label in tabs
        ],
        "tab": tab,
        "kind": kind,
        "kinds": D.Kind.choices,
        "rows": [] if tab == "unreconciled" else _rows(user, tab, kind, now),
        "unreconciled": unreconciled if tab == "unreconciled" else [],
        "full": full,
        "decides": registry.is_registry_staff(user),
        "can_import": registry.may_import(user),
        "is_admin": identity.staff_scope(user).is_admin,
        "report": registry.report(now) if full else None,
        "imports": RegistryImport.objects.select_related("uploaded_by__staff_profile")[:8]
        if full
        else [],
        "configured": any((registry.config().movement_types or {}).values()),
        "now": now,
        "today": timezone.localdate(),
    }
    return render(request, "operations/registry.html", context)


def _discrepancy(request, pk):
    d = (
        registry.visible_discrepancies(request.user)
        .select_related("case", "record__division", "record__case", "division", "found_by")
        .filter(pk=pk)
        .first()
    )
    if d is None:
        raise Http404
    return d


@login_required
def discrepancy(request, pk):
    d = _discrepancy(request, pk)
    user = request.user
    if request.method == "POST":
        action = request.POST.get("action", "")
        post = request.POST
        try:
            if action == "fix_number":
                registry.fix_number(user, d.pk, post.get("number"), post.get("reason"))
            elif action == "send_to_division":
                registry.send_to_division(user, d.pk, post.get("note"))
            elif action == "answer":
                registry.answer(user, d.pk, post.get("answer"), post.get("note"))
            elif action == "wait_for_registry":
                registry.wait_for_registry(user, d.pk, post.get("reference"), post.get("reason"))
            elif action == "accept":
                registry.accept(user, d.pk, post.get("reason"))
            elif action == "add_evidence" and d.record_id:
                registry.add_evidence(user, d.record_id, request.FILES.getlist("files"))
            else:
                raise Http404
        except (ValidationError, PermissionDenied) as exc:
            _messages(request, exc)
        else:
            messages.success(request, "บันทึกแล้ว")
        return redirect("registry-discrepancy", pk=d.pk)
    if request.method not in ("GET", "HEAD"):
        return HttpResponse(status=405)
    record = d.record
    decides = registry.is_registry_staff(user)
    answers = d.status == D.Status.WITH_DIVISION and bool(
        identity.staff_scope(user).division_roles.get(d.division_id)
    )
    return render(
        request,
        "operations/registry_discrepancy.html",
        {
            "d": d,
            "record": record,
            "evidence": list(record.evidence.order_by("created_at")) if record else [],
            "decides": decides and d.status != D.Status.RESOLVED,
            "answers": answers,
            "answer_choices": D.Answer.choices,
            "can_add_evidence": record is not None
            and d.status != D.Status.RESOLVED
            and registry.may_add_evidence(user, record),
        },
    )


def _datetime(value):
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return timezone.make_aware(moment) if timezone.is_naive(moment) else moment


def _date(value):
    try:
        return datetime.fromisoformat(value).date()
    except (TypeError, ValueError):
        return None


@login_required
@require_POST
def upload(request, kind):
    if kind not in RegistryImport.Kind.values:
        raise Http404
    if not registry.may_import(request.user):
        raise PermissionDenied
    chosen = request.FILES.get("file")
    exported_at = _datetime(request.POST.get("exported_at"))
    if chosen is None or exported_at is None:
        messages.error(request, "เลือกไฟล์และระบุเวลาที่ส่งออกจาก SAP")
        return redirect("registry")
    data = chosen.read(registry.MAX_BYTES + 1)
    try:
        if kind == RegistryImport.Kind.DOCUMENTS:
            done = registry.import_documents(
                request.user,
                chosen.name,
                data,
                exported_at,
                _date(request.POST.get("period_from")),
                _date(request.POST.get("period_to")),
            )
            new = sum((done.summary.get("new") or {}).values())
            messages.success(
                request,
                f"นำเข้า {done.rows} บรรทัดแล้ว · ส่วนต่างใหม่ {new} · ปิดเองเพราะตรงแล้ว "
                f"{done.summary.get('resolved', 0)} · ข้ามประเภทที่ไม่ได้ตั้งค่า "
                f"{done.summary.get('ignored_rows', 0)} บรรทัด",
            )
        elif kind == RegistryImport.Kind.SERIALS:
            done = registry.import_serials(request.user, chosen.name, data, exported_at)
            messages.success(request, f"นำเข้าเลขเครื่อง {done.rows} เลขแล้ว")
        else:
            done = registry.import_balances(request.user, chosen.name, data, exported_at)
            unmapped = done.summary.get("unmapped") or []
            messages.success(
                request,
                f"นำเข้ายอดคงเหลือ {done.summary['items']} รายการแล้ว"
                + (f" · รหัสที่ยังไม่ผูกกับของ {len(unmapped)} รหัส" if unmapped else ""),
            )
    except (ValidationError, PermissionDenied) as exc:
        _messages(request, exc)
    return redirect("registry")


@login_required
def settings_page(request):
    if not identity.staff_scope(request.user).is_admin:
        raise PermissionDenied
    row = registry.config()
    if request.method == "POST":
        post = request.POST
        try:
            if post.get("action") == "config":
                registry.update_config(
                    request.user,
                    {kind: post.get(f"mt-{kind}", "") for kind in registry.MOVEMENT_KINDS},
                    {name: post.get(f"alias-{name}", "") for name in registry.DEFAULT_ALIASES},
                    post.get("storage_locations", ""),
                    post.get("reason", ""),
                )
            elif post.get("action") == "codes":
                reason = (post.get("reason") or "").strip()
                if not reason:
                    raise ValidationError("ระบุเหตุผลของการแก้รหัสทางการ")
                for item in EquipmentType.objects.order_by("code"):
                    value = post.get(f"code-{item.pk}")
                    if value is not None and value.strip() != item.official_code:
                        registry.set_official_code(request.user, item, value, reason)
            elif post.get("action") == "codes_file" and request.FILES.get("file"):
                upload_file = request.FILES["file"]
                count = registry.import_official_codes(
                    request.user, upload_file.name, upload_file.read(registry.MAX_BYTES + 1)
                )
                messages.success(request, f"ผูกรหัสทางการ {count} รายการแล้ว")
                return redirect("registry-settings")
            else:
                raise Http404
        except (ValidationError, PermissionDenied) as exc:
            _messages(request, exc)
        else:
            messages.success(request, "บันทึกการตั้งค่าแล้ว")
        return redirect("registry-settings")
    if request.method not in ("GET", "HEAD"):
        return HttpResponse(status=405)
    return render(
        request,
        "operations/registry_settings.html",
        {
            "row": row,
            "movement": [
                (kind, label, " ".join((row.movement_types or {}).get(kind, [])))
                for kind, label in [
                    ("issue", "จ่าย (เช่น 201, 261)"),
                    ("return", "รับคืน (เช่น 202, 262)"),
                    ("status", "เปลี่ยนสถานะของ/ผลตรวจ (เช่น 321, 344, 551)"),
                    ("reversal", "กลับรายการ"),
                ]
            ],
            "aliases": [
                (
                    name,
                    registry.FIELD_LABELS[name],
                    "\n".join((row.column_aliases or {}).get(name, [])),
                    ", ".join(registry.DEFAULT_ALIASES[name]),
                )
                for name in registry.DEFAULT_ALIASES
            ],
            "places": "\n".join(" / ".join(p for p in pair if p) for pair in row.storage_locations),
            "items": EquipmentType.objects.order_by("code"),
        },
    )


@login_required
@require_safe
def export(request):
    """ส่วนต่างทะเบียน as CSV (BOM, formula-safe, audited); no requester data."""
    if not registry.may_open(request.user):
        raise PermissionDenied
    rows = registry.visible_discrepancies(request.user).select_related(
        "case", "record__division", "division"
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "รหัส",
            "ชนิด",
            "สถานะ",
            "เลขเรื่อง",
            "เลขเอกสาร",
            "รายละเอียด",
            "กองที่บันทึก",
            "เปิดเมื่อ",
            "ผลการตัดสิน",
            "เหตุผล",
            "เลขเอกสารแก้ไข SAP",
        ]
    )
    count = 0
    for d in rows.order_by("opened_at")[:5000]:
        writer.writerow(
            [
                reports._cell(v)
                for v in (
                    d.pk,
                    d.get_kind_display(),
                    d.get_status_display(),
                    d.case.number if d.case else "",
                    d.document_number,
                    d.detail,
                    d.record.division.code if d.record else "",
                    timezone.localtime(d.opened_at).strftime("%Y-%m-%d %H:%M"),
                    d.get_resolution_display() if d.resolution else "",
                    d.resolution_reason,
                    d.registry_reference,
                )
            ]
        )
        count += 1
    identity.audit(request.user, "registry.exported", request.user, rows=count)
    response = HttpResponse("﻿" + buffer.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="registry-discrepancies.csv"'
    return response
