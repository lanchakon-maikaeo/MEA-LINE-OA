"""งานสั่ง on the web (16 Oct 2569 Q14, Q15): the list with its tabs, a new order for
Supervisors, and each order's page with its steps, files and timeline. LINE gets the same steps
in round 2 B; this page is also where a LINE message links to."""

from datetime import date

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import FileResponse, Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from operations import attachments, identity, queues, uploads, work_orders
from operations.models import Attachment, WorkOrder

User = get_user_model()
S = WorkOrder.Status
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "application/pdf": "pdf"}
JOURNEY = (
    (S.ASSIGNED, "มอบหมาย"),
    (S.ACKNOWLEDGED, "รับทราบ"),
    (S.IN_PROGRESS, "กำลังทำ"),
    (S.DONE, "เสร็จ"),
    (S.CLOSED, "ปิดงาน"),
)


def _require_access(request):
    if not identity.staff_scope(request.user).may_read:  # ADR 0039: viewers read งานสั่ง
        raise Http404


def _order(request, number):
    order = (
        work_orders.visible(request.user)
        .select_related("division", "assignee__staff_profile", "issued_by__staff_profile")
        .filter(number=number)
        .first()
    )
    if order is None:
        raise Http404
    return order


def _targets(user):
    """Where a Supervisor may send work: each division they supervise, whole or one person,
    as (division, [(value, label), ...]) for one grouped select."""
    groups = []
    for division in work_orders.issuable(user):
        choices = [(f"d:{division.pk}", f"{division.code} ทั้งกอง")]
        choices += [
            (f"u:{division.pk}:{person.pk}", f"{division.code} · {work_orders.name_of(person)}")
            for person in work_orders.members(division.pk)
        ]
        groups.append((division, choices))
    return groups


def _read_target(value, user):
    """'d:<division>' for the whole division or 'u:<division>:<user>' for one person."""
    parts = (value or "").split(":")
    allowed = {d.pk: d for d in work_orders.issuable(user)}
    try:
        division = allowed.get(int(parts[1])) if len(parts) >= 2 else None
        person = User.objects.filter(pk=int(parts[2])).first() if len(parts) == 3 else None
    except ValueError:
        division = person = None
    if division is None or parts[0] not in {"d", "u"} or (parts[0] == "u" and person is None):
        raise ValidationError("เลือกว่าจะสั่งถึงกองไหนหรือใคร")
    return division, person


def _read_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError("วันกำหนดไม่ถูกต้อง") from exc


def _journey(order):
    """Reached steps are done, the next is current; a send-back or cancel stops it there."""
    values = [value for value, _ in JOURNEY]
    if order.status in values:
        reached = values.index(order.status)
    else:
        reached = 3 if order.done_at else 1 if order.acknowledged_at else 0
    stopped = order.get_status_display() if order.status in {S.SENT_BACK, S.CANCELLED} else ""
    steps = []
    for index, (_, label) in enumerate(JOURNEY):
        if index <= reached:
            state = "done"
        elif index == reached + 1:
            state = "stopped" if stopped else "current"
        else:
            state = "todo"
        note = stopped if state == "stopped" else ""
        steps.append({"label": label, "state": state, "note": note})
    return steps


TUCKED = {"send_back", "cancel"}  # need a reason; kept under ตัวเลือกอื่น
SAID = {
    "acknowledge": "รับทราบงานแล้ว",
    "start": "เริ่มทำงานแล้ว",
    "done": "บันทึกว่าทำเสร็จแล้ว ผู้สั่งจะได้รับข้อความ",
    "send_back": "ส่งงานกลับผู้สั่งแล้ว",
    "assign_person": "มอบให้คนในกองแล้ว",
    "reassign": "สั่งงานใหม่แล้ว",
    "close": "ปิดงานแล้ว",
    "cancel": "ยกเลิกงานแล้ว",
}


def _offers(order, steps):
    """The steps as buttons: the first plain one is the main button, and send-back and cancel
    wait under ตัวเลือกอื่น."""
    offers = []
    for name, label in steps:
        if name == "assign_person" and order.assignee_id:
            label = "เปลี่ยนคนทำ"
        offers.append(
            {
                "name": name,
                "label": label,
                "needs": work_orders.STEPS[name][3],
                "tucked": name in TUCKED,
                "primary": False,
            }
        )
    plain = [o for o in offers if not o["tucked"]]
    if plain and not (plain[0]["name"] == "assign_person" and order.assignee_id):
        plain[0]["primary"] = True
    return offers


@login_required
@require_safe
def orders(request):
    _require_access(request)
    scope = identity.staff_scope(request.user)
    if scope.is_admin or scope.is_support:
        target = "queue-all"
        args = []
    else:
        divisions = queues.own_divisions(scope)
        if not divisions:
            return redirect("console")
        target = "division-detail"
        args = [divisions[0].code]
    tab = {"mine": "mine", "ended": "closed"}.get(request.GET.get("tab"), "host")
    return redirect(f"{reverse(target, args=args)}?tab={tab}")


@login_required
@require_http_methods(["GET", "HEAD", "POST"])
def order_new(request):
    _require_access(request)
    if not work_orders.may_issue(request.user):
        raise PermissionDenied
    values = request.POST if request.method == "POST" else {"target": request.GET.get("to", "")}
    errors = []
    if request.method == "POST":
        files = request.FILES.getlist("files")
        errors = uploads.check(files) if files else []
        if not errors:
            try:
                with transaction.atomic():
                    division, person = _read_target(request.POST.get("target"), request.user)
                    order = work_orders.create(
                        request.user,
                        request.POST.get("title", ""),
                        division,
                        person,
                        detail=request.POST.get("detail", ""),
                        due_on=_read_date(request.POST.get("due_on")),
                        urgent=request.POST.get("urgent") == "1",
                        channel=WorkOrder.Channel.WEB,
                    )
                    if files:
                        uploads.store(request.user, None, files, work_order=order)
            except ValidationError as exc:
                errors = exc.messages
            else:
                messages.success(request, f"สั่งงาน {order.number} แล้ว")
                return redirect("order-detail", number=order.number)
    return render(
        request,
        "operations/order_new.html",
        {
            "targets": _targets(request.user),
            "values": values,
            "errors": errors,
            "today": timezone.localdate().isoformat(),
        },
    )


@login_required
@require_safe
def order_detail(request, number):
    _require_access(request)
    order = _order(request, number)
    steps = work_orders.steps_for(request.user, order)
    scope = identity.staff_scope(request.user)
    names = {name for name, _ in steps}
    offers = _offers(order, steps)
    may_attach = order.status not in WorkOrder.ENDED and (
        work_orders.is_doer(request.user, order, scope)
        or work_orders.is_supervisor(request.user, order, scope)
    )
    return render(
        request,
        "operations/order_detail.html",
        {
            "order": order,
            "steps": offers,
            "tucked": any(o["tucked"] for o in offers),
            "people": work_orders.members(order.division_id) if "assign_person" in names else [],
            "targets": _targets(request.user) if "reassign" in names else [],
            "files": order.attachments.order_by("created_at"),
            "timeline": work_orders.timeline(order),
            "journey": _journey(order),
            "may_attach": may_attach,
            "late": order.due_on is not None
            and order.status in WorkOrder.OPEN
            and order.due_on < timezone.localdate(),
            "today": timezone.localdate().isoformat(),
        },
    )


@login_required
@require_POST
def order_action(request, number, step):
    _require_access(request)
    order = _order(request, number)
    data = request.POST
    try:
        person = None
        division = None
        if step == "assign_person":
            person = User.objects.filter(pk=int(data.get("person") or 0)).first()
        if step == "reassign":
            division, person = _read_target(data.get("target"), request.user)
        work_orders.act(
            request.user,
            order,
            step,
            reason=data.get("reason", ""),
            result=data.get("result", ""),
            assignee=person,
            division=division,
            due_on=_read_date(data.get("due_on")),
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    except PermissionDenied:
        messages.error(request, "ทำขั้นนี้ไม่ได้ในตอนนี้ อาจมีคนทำไปก่อนแล้ว")
    except ValueError:
        messages.error(request, "ข้อมูลไม่ถูกต้อง")
    else:
        messages.success(request, SAID[step])
    return redirect("order-detail", number=order.number)


@login_required
@require_POST
def order_upload(request, number):
    _require_access(request)
    order = _order(request, number)
    scope = identity.staff_scope(request.user)
    if order.status in WorkOrder.ENDED or not (
        work_orders.is_doer(request.user, order, scope)
        or work_orders.is_supervisor(request.user, order, scope)
    ):
        raise PermissionDenied
    files = request.FILES.getlist("files")
    problems = uploads.check(files) if files else ["เลือกไฟล์ก่อน"]
    if problems:
        for problem in problems:
            messages.error(request, problem)
    else:
        note = (request.POST.get("note") or "").strip()[:200]
        stored = uploads.store(request.user, None, files, note=note, work_order=order)
        messages.success(request, f"แนบไฟล์แล้ว {len(stored)} ไฟล์ (รอสแกนไวรัสก่อนเปิดได้)")
    return redirect("order-detail", number=order.number)


@login_required
@require_safe
def order_file(request, number, pk):
    """Scanned-clean files of an order the viewer may see; everything else is 404."""
    _require_access(request)
    order = _order(request, number)
    attachment = order.attachments.filter(pk=pk, scan_status=Attachment.ScanStatus.CLEAN).first()
    if attachment is None:
        raise Http404
    path = attachments.storage_dir() / attachment.storage_name
    if not attachment.storage_name or not path.is_file():
        raise Http404
    position = list(order.attachments.values_list("pk", flat=True)).index(attachment.pk)
    filename = f"{order.number}-{position + 1}.{EXTENSIONS[attachment.content_type]}"
    response = FileResponse(
        path.open("rb"), as_attachment=True, filename=filename, content_type=attachment.content_type
    )
    response["Cache-Control"] = "private, no-store"
    return response
