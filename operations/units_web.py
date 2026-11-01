"""ผังหน่วยงาน page (ADR 0031): admins only see and change it (Q17). One card per กอง, แผนก and
กลุ่มงาน shows its people, the งานที่หน่วยรับ with their ผู้รับผิดชอบประจำงาน, and its
ผู้รับแจ้ง. (ADR 0042 took off "ลองดูว่าเด้งไปใคร" and the page's explanations.)

ADR 0041: above the cards, a tree of the people registered in LINE by the roster's สายงาน ›
ฝ่าย › กอง › แผนก › กลุ่มงาน; it grows as people register, and a click on a person changes
their unit or role."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from operations import identity, koj_tracking, roster, units
from operations.koj_models import KojRecipient
from operations.models import (
    VIEWER_ROLES,
    ChannelIdentity,
    Division,
    Employee,
    Requester,
    Role,
    RoleGrant,
    Section,
    StaffProfile,
    UnitRecipient,
    UnitWork,
    WorkGroup,
)
from operations.views import admin_required

MAX_OWNERS = 3  # คนหลัก and two backups fit one card; more can be added once really needed


@admin_required
@require_http_methods(["GET", "HEAD", "POST"])
def admin_page(request):
    if request.method == "POST":
        anchor = _act(request)
        return redirect(f"{request.path}{anchor}")
    divisions = list(
        Division.objects.filter(is_active=True, kind=Division.Kind.DIVISION).order_by("code")
    )
    chart = _chart(divisions)
    return render(
        request,
        "operations/admin_org.html",
        {
            "chart": chart,
            "divisions": divisions,
            "kinds": list(units.KIND_SHORT.items()),
            "groups": units.group_choices(),
            "unplaced": _unplaced(),
            "owner_slots": range(MAX_OWNERS),
            "tree": _tree(),
            "paths": roster.unit_paths(),
            "picks": [(r, identity.role_label(r)) for r in identity.PICKS],
        },
    )


# --- Reading the chart ------------------------------------------------------------------------


def _chart(divisions):
    line_ids = set(
        ChannelIdentity.objects.filter(staff_user__isnull=False, is_following=True).values_list(
            "staff_user_id", flat=True
        )
    )
    members = {}
    for grant in (
        RoleGrant.objects.filter(
            revoked_at__isnull=True, division__in=divisions, user__is_active=True
        )
        .exclude(role__in=VIEWER_ROLES)  # ADR 0039: viewers take no work
        .select_related("user__staff_profile")
    ):
        members.setdefault(grant.division_id, {})[grant.user_id] = grant.user
    profiles = {
        p.user_id: p
        for p in StaffProfile.objects.filter(user__is_active=True).select_related(
            "division", "section", "work_group"
        )
    }
    works = {}
    for work in (
        UnitWork.objects.filter(is_active=True, division__in=divisions)
        .prefetch_related("owners__user__staff_profile")
        .order_by("order", "pk")
    ):
        works.setdefault(_key(work.division_id, work.section_id, work.work_group_id), []).append(
            work
        )
    receivers = {}
    for row in UnitRecipient.objects.select_related("user__staff_profile"):
        receivers.setdefault(_key(row.division_id, row.section_id, row.work_group_id), []).append(
            row.user
        )
    koj = {}
    for row in KojRecipient.objects.select_related("user__staff_profile"):
        koj.setdefault(row.group, []).append(row.user)
    koj_units = koj_tracking.unit_groups()

    def person(user):
        profile = profiles.get(user.pk)
        return {
            "user": user,
            "name": units.display(user),
            "line": user.pk in line_ids,
            "away": units.away_until(user),
            "path": units.path_labels(profile),
        }

    def card(unit, people_ids, eligible):
        key = unit.key
        group = koj_units.get(key)
        return {
            "unit": unit,
            "key": key,
            "people": [person(eligible[i]) for i in people_ids if i in eligible],
            "works": [
                {
                    "work": w,
                    "label": units.work_label(w.kind, w.group),
                    "owners": [units.display(o.user) for o in w.owners.all()],
                    "owner_ids": [o.user_id for o in w.owners.all()],
                }
                for w in works.get(key, [])
            ],
            "receivers": [units.display(u) for u in receivers.get(key, [])],
            "receiver_ids": {u.pk for u in receivers.get(key, [])},
            "koj": (
                {
                    "group": group,
                    "label": koj_tracking.GROUPS[group],
                    "names": [units.display(u) for u in koj.get(group, [])],
                    "ids": {u.pk for u in koj.get(group, [])},
                }
                if group
                else None
            ),
        }

    out = []
    for division in divisions:
        eligible = members.get(division.pk, {})
        placed = {
            uid: profiles[uid] for uid in eligible if uid in profiles and profiles[uid].division_id
        }

        def at(section=None, group=None, placed=placed, division=division):
            return [
                uid
                for uid, p in placed.items()
                if p.division_id == division.pk
                and p.section_id == (section.pk if section else None)
                and p.work_group_id == (group.pk if group else None)
            ]

        sections = []
        for section in division.sections.filter(is_active=True).order_by("order", "name"):
            groups = [
                card(units.Unit.of(group=g), at(section, g), eligible)
                for g in WorkGroup.objects.filter(section=section, is_active=True).order_by(
                    "order", "name"
                )
            ]
            sections.append(
                {**card(units.Unit.of(section=section), at(section), eligible), "groups": groups}
            )
        everyone = sorted((person(u) for u in eligible.values()), key=lambda p: p["name"])
        out.append(
            {
                **card(units.Unit.of(division=division), at(), eligible),
                "sections": sections,
                "everyone": everyone,
                "eligible_line": [p for p in everyone if p["line"]],
                "units": _unit_options(division),
                "work_count": sum(
                    len(v) for k, v in works.items() if v and v[0].division_id == division.pk
                ),
            }
        )
    return out


ROOT = "ผู้ว่าการ"


def _tree():
    """The registered people (a LINE OA user tied to the roster) by their roster path. Empty
    levels are skipped and a level named like its parent (สายงาน รผง. › ฝ่าย รผง.) is one node.
    A system กอง or แผนก links to its card below."""
    link = Requester.EmployeeLink
    requesters = (
        Requester.objects.filter(
            status=Requester.Status.ACTIVE,
            is_anonymous=False,
            employee_link__in=[link.VERIFIED, link.ADMIN],
            channelidentity__status=ChannelIdentity.Status.ACTIVE,
        )
        .select_related("employee__division")
        .distinct()
    )
    users = dict(
        StaffProfile.objects.filter(
            employee_id__in=[r.employee.employee_id for r in requesters], user__is_active=True
        ).values_list("employee_id", "user_id")
    )
    roles = dict(
        RoleGrant.objects.filter(user_id__in=users.values(), revoked_at__isnull=True)
        .exclude(role=Role.SYSTEM_SUPPORT)
        .values_list("user_id", "role")
    )
    sections = {}
    root = {"label": ROOT, "children": {}, "people": [], "anchor": "", "count": 0}
    for requester in sorted(requesters, key=lambda r: r.display_name):
        employee = requester.employee
        node, names = root, [ROOT]
        steps = [(employee.line_of_work, ""), (employee.department, "")]
        division = employee.division
        if division is not None:
            steps.append((f"{units.short(division)} : {division.name}", f"d{division.pk}"))
            if division.pk not in sections:
                sections[division.pk] = list(division.sections.filter(is_active=True))
            hit = next(
                (
                    x
                    for x in sections[division.pk]
                    if roster.same_unit(employee.section_name, x.name)
                ),
                None,
            )
            steps.append((employee.section_name, f"s{hit.pk}" if hit else ""))
        else:
            steps += [(employee.division_name, ""), (employee.section_name, "")]
        steps.append((employee.work_group, ""))
        node["count"] += 1
        for label, anchor in steps:
            if not label or label == names[-1]:
                continue
            names.append(label)
            node = node["children"].setdefault(
                label, {"label": label, "children": {}, "people": [], "anchor": anchor, "count": 0}
            )
            node["count"] += 1
        role = roles.get(users.get(employee.employee_id), identity.EMPLOYEE)
        node["people"].append(
            {
                "employee": employee,
                "name": requester.display_name,
                "role": role,
                "role_label": identity.role_label(role),
                "in_picks": role in identity.PICKS,
                "path": roster.path_of(employee),
            }
        )

    def listed(node):
        node["children"] = [listed(c) for c in node["children"].values()]
        return node

    root = listed(root)
    root["top"] = {"people": root["people"], "children": []}  # people at the root itself
    return root


def _key(division_id, section_id, group_id):
    if group_id:
        return f"g{group_id}"
    return f"s{section_id}" if section_id else f"d{division_id}"


def _unit_options(division):
    """(key, label) of every unit in the กอง, for the move and add forms."""
    rows = [(f"d{division.pk}", f"{units.short(division)} (ระดับกอง)")]
    for section in division.sections.filter(is_active=True).order_by("order", "name"):
        rows.append((f"s{section.pk}", f"{units.short(division)} › {units.short(section)}"))
        for group in WorkGroup.objects.filter(section=section, is_active=True).order_by(
            "order", "name"
        ):
            rows.append(
                (
                    f"g{group.pk}",
                    f"{units.short(division)} › {units.short(section)} › {units.short(group)}",
                )
            )
    return rows


def _unplaced():
    """Active staff of a กอง with no กอง in their สังกัด, with what the roster says about them.
    System-level accounts (admins) belong to no กอง and stay N/A without being listed."""
    rows = []
    for profile in (
        StaffProfile.objects.filter(user__is_active=True, division__isnull=True)
        .select_related("user")
        .order_by("display_name")
    ):
        divisions = list(
            Division.objects.filter(
                pk__in=RoleGrant.objects.filter(
                    user=profile.user, revoked_at__isnull=True, division__isnull=False
                ).values("division_id"),
                is_active=True,
                kind=Division.Kind.DIVISION,
            ).order_by("code")
        )
        if not divisions:
            continue
        employee = units.roster_row(profile)
        rows.append(
            {
                "profile": profile,
                "roster": " · ".join(
                    x
                    for x in (
                        employee.division_name if employee else "",
                        employee.section_name if employee else "",
                        employee.work_group if employee else "",
                    )
                    if x
                ),
                "options": [option for d in divisions for option in _unit_options(d)],
            }
        )
    return rows


# --- Changing it --------------------------------------------------------------------------------


def _act(request):
    data, actor = request.POST, request.user
    action, reason = data.get("action", ""), data.get("reason", "")
    anchor = f"#u-{data.get('unit')}" if data.get("unit") else ""
    try:
        if action == "short_name":
            units.set_short_name(
                actor, units.unit_from_key(data.get("unit")), data.get("value"), reason
            )
            messages.success(request, "บันทึกชื่อย่อแล้ว")
        elif action == "add_group":
            section = get_object_or_404(Section, pk=_pk(data.get("unit"), "s"))
            group, joined = units.create_group(
                actor, section, data.get("name"), data.get("code"), reason
            )
            extra = f" ย้ายคนที่ทะเบียนระบุกลุ่มงานนี้เข้าไป {joined} คน" if joined else ""
            messages.success(request, f"เพิ่มกลุ่มงาน {group.name} แล้ว{extra}")
        elif action == "close_group":
            group = get_object_or_404(WorkGroup, pk=_pk(data.get("unit"), "g"))
            units.close_group(actor, group, reason)
            messages.success(request, f"ปิดกลุ่มงาน {group.name} แล้ว คนในกลุ่มยังอยู่ในแผนกเดิม")
        elif action == "move_person":
            profile = get_object_or_404(StaffProfile, user_id=data.get("user"))
            target = data.get("target")
            unit = units.unit_from_key(target) if target else None
            units.move_person(actor, profile.user, unit, reason)
            messages.success(
                request, f"ย้าย {profile.display_name} ไป {unit.path if unit else units.NA} แล้ว"
            )
        elif action == "add_work":
            unit = units.unit_from_key(data.get("unit"))
            work, others = units.add_work(actor, unit, data.get("kind"), data.get("group"), reason)
            label = units.work_label(work.kind, work.group)
            if others:
                messages.warning(
                    request,
                    f"{label} มีหน่วยรับอยู่แล้ว ({', '.join(others)}) ระบบเลือกหน่วยที่อยู่ลำดับบนกว่า"
                    " ปรับลำดับได้ที่ปุ่มขึ้นและลง",
                )
            else:
                messages.success(request, f"{unit.path} รับงาน {label} แล้ว")
        elif action in ("remove_work", "work_up", "work_down", "owners"):
            work = get_object_or_404(UnitWork, pk=data.get("work"), is_active=True)
            if action == "remove_work":
                units.remove_work(actor, work, reason)
                messages.success(request, "เลิกรับงานนี้แล้ว เรื่องที่มีคนรับผิดชอบอยู่ยังอยู่กับคนเดิม")
            elif action == "owners":
                moved = units.set_owners(actor, work, data.getlist("owner"), reason)
                extra = f" ย้ายเรื่องที่ยังไม่จบให้คนใหม่ {moved} เรื่อง" if moved else ""
                messages.success(request, f"บันทึกผู้รับผิดชอบแล้ว{extra}")
            else:
                units.move_work(actor, work, -1 if action == "work_up" else 1)
        elif action == "receivers":
            units.set_recipients(
                actor, units.unit_from_key(data.get("unit")), data.getlist("person"), reason
            )
            messages.success(request, "บันทึกผู้รับแจ้งเรื่องสำคัญแล้ว")
        elif action == "koj":
            koj_tracking.set_group(actor, data.get("group"), data.getlist("person"))
            messages.success(request, "บันทึกผู้รับแจ้งงานติดตาม กอจ. แล้ว")
        elif action == "set_person":
            anchor = "#org-tree"
            _set_person(request, get_object_or_404(Employee, pk=data.get("employee")))
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return anchor


def _set_person(request, employee):
    """ADR 0041: a click on a person in the tree changes their unit, their role, or both."""
    data, actor, reason = request.POST, request.user, request.POST.get("reason", "")
    changed = []
    path = " ".join(data.get("path", "").split())
    if path and path != roster.path_of(employee):
        roster.move_employee(actor, employee, path, reason)
        changed.append(f"ย้ายไปยัง {path}")
    role = data.get("role", "")
    requester = Requester.objects.filter(
        employee=employee, status=Requester.Status.ACTIVE, is_anonymous=False
    ).first()
    if role and requester is not None:
        profile = StaffProfile.objects.filter(employee_id=employee.employee_id).first()
        held = (
            RoleGrant.objects.filter(user_id=profile.user_id, revoked_at__isnull=True)
            .exclude(role=Role.SYSTEM_SUPPORT)
            .values_list("role", flat=True)
            .first()
            if profile
            else None
        ) or identity.EMPLOYEE
        if role != held:
            identity.grant_requester_role(actor, requester, role, None, reason)
            changed.append(f"กำหนดบทบาทเป็น{identity.role_label(role)}")
    name = employee.name or employee.employee_id
    messages.success(request, f"{name}: {' · '.join(changed)}" if changed else "ไม่มีการเปลี่ยนแปลง")


def _pk(key, kind):
    key = key or ""
    if key[:1] != kind or not key[1:].isdigit():
        raise ValidationError("ไม่พบหน่วยนี้")
    return int(key[1:])
