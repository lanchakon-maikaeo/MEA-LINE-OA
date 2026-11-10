"""ผังหน่วยงาน page (ADR 0031): admins only see and change it (Q17). One card per กอง, แผนก and
กลุ่มงาน shows its people, the งานที่หน่วยรับ with their ผู้รับผิดชอบประจำงาน, and its
ผู้รับแจ้ง. (ADR 0042 took off "ลองดูว่าเด้งไปใคร" and the page's explanations.)

ADR 0041: a tree of the people registered in LINE by the roster's สายงาน › ฝ่าย › กอง › แผนก ›
กลุ่มงาน; it grows as people register. ADR 0044 took the cards and the ยังไม่ได้จัดหน่วย list off:
a click on a person opens their card (unit, ตำแหน่ง, role, งานประจำ, alerts, and the web account
to merge), a click on a unit opens its card, and a unit outside ฝอจ. only takes a ชื่อย่อ."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
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
    cards = {}  # every unit card by key, with the กอง it belongs to
    for d in chart:
        cards[d["key"]] = (d, d)
        for section in d["sections"]:
            cards[section["key"]] = (section, d)
            for group in section["groups"]:
                cards[group["key"]] = (group, d)
    paths = roster.unit_paths()
    return render(
        request,
        "operations/admin_org.html",
        {
            "kinds": list(units.KIND_SHORT.items()),
            "groups": units.group_choices(),
            "owner_slots": range(MAX_OWNERS),
            "tree": _tree(cards),
            "unit_tree": _unit_tree(paths),
            "positions": sorted(
                set(
                    Employee.objects.filter(is_active=True, is_synthetic=False)
                    .exclude(position="")
                    .values_list("position", flat=True)
                )
            ),
            "accounts": _web_accounts(),
            "picks": [(r, identity.role_label(r)) for r in identity.PICKS],
        },
    )


def _unit_tree(paths):
    """ฝ่าย › กอง › แผนก › กลุ่มงาน of the file as nested names, for the card's four pickers."""
    tree = {}
    for row in paths.values():
        node = tree
        for field in roster.LEVELS:
            if row[field]:
                node = node.setdefault(row[field], {})
    return tree


def _web_accounts():
    """Web accounts a person's LINE may merge into (ADR 0044): active, made by an admin, and not
    yet tied to an employee."""
    return [
        (p.user_id, f"{p.display_name} ({p.user.username})")
        for p in StaffProfile.objects.filter(user__is_active=True, employee_id="")
        .exclude(user__username__startswith=identity.LINE_ACCOUNT)
        .exclude(user__username=identity.SYSTEM_USERNAME)
        .select_related("user")
        .order_by("display_name")
    ]


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


def _tree(cards):
    """The registered people (a LINE OA user tied to the roster) by their roster path. Empty
    levels are skipped and a level named like its parent (สายงาน รผง. › ฝ่าย รผง.) is one node.
    Only units with someone in them show (ADR 0050 Q3): a unit of the system carries its card,
    one outside it its roster path, for its ชื่อย่อ (ADR 0044)."""
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
    profiles = {
        p.employee_id: p
        for p in StaffProfile.objects.filter(
            employee_id__in=[r.employee.employee_id for r in requesters], user__is_active=True
        ).select_related("user")
    }
    roles = dict(
        RoleGrant.objects.filter(
            user_id__in=[p.user_id for p in profiles.values()], revoked_at__isnull=True
        )
        .exclude(role=Role.SYSTEM_SUPPORT)
        .values_list("user_id", "role")
    )
    names = roster.unit_names()
    serial = iter(range(1, 1_000_000))
    anchors = {}

    def node_of(label):
        return {"label": label, "children": {}, "people": [], "count": 0, "id": next(serial)}

    def child(node, label, anchor="", path=""):
        if anchor in anchors:
            return anchors[anchor]
        if label not in node["children"]:
            card = cards.get(anchor)
            node["children"][label] = {
                **node_of(label),
                "card": card[0] if card else None,
                "division": card[1] if card else None,
                "path": "",
                "short": "",
            }
            if card:
                anchors[anchor] = node["children"][label]
        found = node["children"][label]
        if path and not found["card"] and not found["path"]:
            found["path"], found["short"] = path, names.get(path, "")
        return found

    root = node_of(ROOT)

    groups = {}
    for requester in sorted(requesters, key=lambda r: r.display_name):
        employee = requester.employee
        division = employee.division
        steps = [(employee.line_of_work, "", None), (employee.department, "", "department")]
        if division is not None:
            steps.append((f"{units.short(division)} : {division.name}", f"d{division.pk}", "d"))
            hit = next(
                (
                    x
                    for x in division.sections.filter(is_active=True)
                    if roster.same_unit(employee.section_name, x.name)
                ),
                None,
            )
            steps.append((employee.section_name, f"s{hit.pk}" if hit else "", "section_name"))
            if hit is not None and hit.pk not in groups:
                groups[hit.pk] = list(WorkGroup.objects.filter(section=hit, is_active=True))
            group = next(
                (
                    g
                    for g in groups.get(hit.pk if hit else None, [])
                    if roster.same_unit(employee.work_group, g.name)
                ),
                None,
            )
            steps.append((employee.work_group, f"g{group.pk}" if group else "", "work_group"))
        else:
            steps += [
                (employee.division_name, "", "division_name"),
                (employee.section_name, "", "section_name"),
                (employee.work_group, "", "work_group"),
            ]
        node, seen, path = root, [ROOT], []
        root["count"] += 1
        for label, anchor, field in steps:
            if field == "d":
                path.append(employee.division_name or division.name)
            elif field and label:
                path.append(label)
            # Only a unit outside every กอง of the system takes a ชื่อย่อ by its roster path.
            named = " › ".join(path) if field and division is None else ""
            if not label:
                continue
            if label == seen[-1]:  # one node for both levels; it is named by the lower one
                if named and node is not root and not node["card"] and not node["path"]:
                    node["path"], node["short"] = named, names.get(named, "")
                continue
            seen.append(label)
            node = child(node, label, anchor, named)
            node["count"] += 1
        profile = profiles.get(employee.employee_id)
        role = roles.get(profile.user_id if profile else None, identity.EMPLOYEE)
        node["people"].append(_person(requester, employee, profile, role, cards, division is None))

    def listed(node, depth=0, key=""):
        node["key"], node["open"], node["depth"] = key, False, depth
        node["children"] = [
            listed(c, depth + 1, f"{key}›{c['label']}") for c in node["children"].values()
        ]
        # ฝอจ. and the units of the system take one colour on the chart.
        node["home"] = bool(node.get("card")) or any(c["home"] for c in node["children"])
        return node

    listed(root)
    # ADR 0050 Q4, Q5: the page opens on ผู้ว่าการ › สายงาน › ฝ่าย with one branch open per
    # level, so of the สายงาน only the one with the most people shows its ฝ่าย.
    if root["children"]:
        max(root["children"], key=lambda c: c["count"])["open"] = True
    return root


def _person(requester, employee, profile, role, cards, outside):
    """One person's card: the four unit levels, ตำแหน่ง, role and, for an account in a กอง of
    the system, the งานประจำ and alerts of the units they sit in."""
    uid = profile.user_id if profile else None
    chain = []
    if profile is not None and profile.division_id:
        keys = [f"d{profile.division_id}"]
        if profile.section_id:
            keys.append(f"s{profile.section_id}")
        if profile.work_group_id:
            keys.append(f"g{profile.work_group_id}")
        chain = [cards[k] for k in keys if k in cards]
    works, alerts, koj = [], [], []
    for card, division in chain:
        if all(p["user"].pk != uid for p in division["everyone"]):
            continue  # no role in the กอง: no งานประจำ, no alerts (units.can_act)
        for w in card["works"]:
            ids = w["owner_ids"]
            choice = "main" if ids[:1] == [uid] else "backup" if uid in ids else ""
            works.append({**w, "unit": card["unit"].label, "choice": choice})
        if any(p["user"].pk == uid for p in division["eligible_line"]):
            alerts.append(
                {"key": card["key"], "unit": card["unit"].label, "on": uid in card["receiver_ids"]}
            )
            if card["koj"] and all(k["group"] != card["koj"]["group"] for k in koj):
                koj.append({**card["koj"], "on": uid in card["koj"]["ids"]})
    return {
        "employee": employee,
        "name": requester.display_name,
        "role": role,
        "role_label": identity.role_label(role),
        "in_picks": role in identity.PICKS,
        "outside": outside,
        "levels": [getattr(employee, f) for f in roster.LEVELS],
        "user_id": uid,
        "account": (
            profile.user.username
            if profile and not profile.user.username.startswith(identity.LINE_ACCOUNT)
            else ""
        ),
        "works": works,
        "alerts": alerts,
        "koj": koj,
    }


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


# --- Changing it --------------------------------------------------------------------------------


def _act(request):
    data, actor = request.POST, request.user
    action, reason = data.get("action", ""), data.get("reason", "")
    anchor = "#org-tree"
    try:
        if action == "unit_name":
            roster.set_unit_name(actor, data.get("path"), data.get("value"), reason)
            messages.success(request, "บันทึกชื่อย่อแล้ว")
        elif action == "short_name":
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
            _set_person(request, get_object_or_404(Employee, pk=data.get("employee")))
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return anchor


@transaction.atomic
def _set_person(request, employee):
    """ADR 0041, 0044: the person's card changes their unit, ตำแหน่ง, role, งานประจำ and alerts,
    and may merge their LINE into a web account. Only what changed is saved."""
    data, actor, reason = request.POST, request.user, request.POST.get("reason", "")
    changed = []
    path = " › ".join(
        v for v in (" ".join(data.get(f"unit-{i}", "").split()) for i in range(4)) if v
    )
    if path and path != roster.path_of(employee):
        roster.move_employee(actor, employee, path, reason)
        changed.append(f"ย้ายไปยัง {path}")
    position = data.get("position")
    if position is not None and " ".join(position.split()) != employee.position:
        roster.set_position(actor, employee, position, reason)
        changed.append(f"ตำแหน่ง {' '.join(position.split()) or 'ว่าง'}")
    requester = Requester.objects.filter(
        employee=employee, status=Requester.Status.ACTIVE, is_anonymous=False
    ).first()
    profile = StaffProfile.objects.filter(
        employee_id=employee.employee_id, user__is_active=True
    ).first()
    role = data.get("role", "")
    if role and requester is not None:
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
    if profile is not None:
        changed += _duties(actor, profile.user_id, data, reason)
    account = data.get("account", "")
    if account.isdigit():
        target = get_object_or_404(StaffProfile, user_id=int(account)).user
        identity.merge_accounts(actor, employee, target, reason)
        changed.append(f"รวมบัญชีกับ {target.username}")
    name = employee.name or employee.employee_id
    messages.success(request, f"{name}: {' · '.join(changed)}" if changed else "ไม่มีการเปลี่ยนแปลง")


def _duties(actor, uid, data, reason):
    """งานประจำ (ไม่รับ, หลัก or สำรอง) and alerts from the person's card, each only if changed."""
    changed = []
    for key in data.getlist("works"):
        work = UnitWork.objects.filter(pk=key, is_active=True).first() if key.isdigit() else None
        if work is None:
            continue
        owners = list(work.owners.order_by("order", "pk").values_list("user_id", flat=True))
        now = "main" if owners[:1] == [uid] else "backup" if uid in owners else ""
        wanted = data.get(f"work-{key}", "")
        if wanted == now:
            continue
        rest = [o for o in owners if o != uid]
        new = [uid, *rest] if wanted == "main" else [*rest, uid] if wanted == "backup" else rest
        if len(new) > MAX_OWNERS:
            raise ValidationError(f"งานหนึ่งมีผู้รับผิดชอบได้ไม่เกิน {MAX_OWNERS} คน")
        units.set_owners(actor, work, new, reason)
        changed.append(f"งานประจำ {units.work_label(work.kind, work.group)}")
    for key in data.getlist("alerts"):
        unit = units.unit_from_key(key)
        ids = list(unit.rows(UnitRecipient).values_list("user_id", flat=True))
        wanted = data.get(f"alert-{key}") == "1"
        if wanted != (uid in ids):
            ids = [*ids, uid] if wanted else [i for i in ids if i != uid]
            units.set_recipients(actor, unit, ids, reason)
            changed.append(f"ผู้รับแจ้งเรื่องสำคัญของ {unit.label}")
    for group in data.getlist("koj"):
        ids = list(KojRecipient.objects.filter(group=group).values_list("user_id", flat=True))
        wanted = data.get(f"koj-{group}") == "1"
        if wanted != (uid in ids):
            ids = [*ids, uid] if wanted else [i for i in ids if i != uid]
            koj_tracking.set_group(actor, group, ids)
            changed.append("ผู้รับแจ้งงานติดตาม กอจ.")
    return changed


def _pk(key, kind):
    key = key or ""
    if key[:1] != kind or not key[1:].isdigit():
        raise ValidationError("ไม่พบหน่วยนี้")
    return int(key[1:])
