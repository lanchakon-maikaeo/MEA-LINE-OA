"""ผังหน่วยงาน (ADR 0031): the สังกัด of each staff member, the งานที่หน่วยรับ of each unit, the
ผู้รับผิดชอบประจำงาน who takes a matching case at once, and the ผู้รับแจ้งของหน่วย who hear of an
important case on LINE. One page says which unit a case belongs to, who takes it, and whose LINE
it reaches; nothing about it lives in the policy any more.

A unit is a กอง, a แผนก or a กลุ่มงาน. Only admins see and change the chart (Q17); a staff member
sets their own "ไม่อยู่" date on their account page (Q21)."""

from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from operations import identity, workflow
from operations.models import (
    UNIT_ROLES,
    VIEWER_ROLES,
    Case,
    ChannelIdentity,
    Division,
    EquipmentType,
    Role,
    RoleGrant,
    Section,
    StaffProfile,
    UnitRecipient,
    UnitWork,
    UnitWorkOwner,
    WorkGroup,
)

User = get_user_model()
NA = "N/A"  # Q13: a level a person does not have, shown in every place
KIND_SHORT = {  # ADR 0032: the short names LINE uses
    Case.Kind.WITHDRAWAL: "เบิก",
    Case.Kind.RETURN: "ส่งคืน",
    Case.Kind.PROBLEM: "ปัญหา/ข้อเสนอแนะ",
}


def group_choices():
    """What a งานที่หน่วยรับ names: the LINE groups of goods, then the topics of a case."""
    from operations import flow_runner  # local: the flow runner builds on the cases

    out = dict(EquipmentType.LineGroup.choices)
    for value, label in flow_runner.GROUP_LABELS.items():  # ADR 0034: one per กอจ. button
        out.setdefault(value, label)
    for value, label in Case.Topic.choices:
        out.setdefault(value, label)
    return list(out.items())


def work_label(kind, group):
    return f"{KIND_SHORT.get(kind, kind)} · {dict(group_choices()).get(group, group)}"


# --- Units ------------------------------------------------------------------------------------


def short(obj):
    """ชื่อย่อ of a unit, else its full name (Q3)."""
    if obj is None:
        return NA
    if isinstance(obj, Division):
        return obj.short_name or obj.code
    return obj.code or obj.name


@dataclass(frozen=True)
class Unit:
    division: Division
    section: Section | None = None
    group: WorkGroup | None = None

    @classmethod
    def of(cls, division=None, section=None, group=None):
        if group is not None:
            section = group.section
        if section is not None:
            division = section.division
        return cls(division, section, group) if division is not None else None

    @property
    def key(self):
        if self.group:
            return f"g{self.group.pk}"
        return f"s{self.section.pk}" if self.section else f"d{self.division.pk}"

    @property
    def level(self):
        return "กลุ่มงาน" if self.group else ("แผนก" if self.section else "กอง")

    @property
    def name(self):
        return (self.group or self.section or self.division).name

    @property
    def label(self):
        return short(self.group or self.section or self.division)

    @property
    def path(self):
        return " › ".join(
            short(x) for x in (self.division, self.section, self.group) if x is not None
        )

    def parent(self):
        if self.group:
            return Unit(self.division, self.section)
        return Unit(self.division) if self.section else None

    def chain(self):
        """This unit, then the bigger ones up to the กอง."""
        unit = self
        while unit is not None:
            yield unit
            unit = unit.parent()

    def rows(self, model):
        return model.objects.filter(
            division=self.division, section=self.section, work_group=self.group
        )


def unit_from_key(key):
    """ "d3", "s5" or "g7" (a กอง, แผนก or กลุ่มงาน) as a Unit."""
    key = key or ""
    kind, pk = key[:1], key[1:]
    if not pk.isdigit():
        _missing()
    if kind == "g":
        found = WorkGroup.objects.select_related("section__division").filter(pk=pk).first()
        return Unit.of(group=found) if found else _missing()
    if kind == "s":
        found = Section.objects.select_related("division").filter(pk=pk).first()
        return Unit.of(section=found) if found else _missing()
    if kind == "d":
        found = Division.objects.filter(pk=pk).first()
        return Unit.of(division=found) if found else _missing()
    return _missing()


def _missing():
    raise ValidationError("ไม่พบหน่วยนี้")


def unit_of(profile):
    """The สังกัด of a staff profile as a Unit, or None (N/A all the way)."""
    if profile is None:
        return None
    return Unit.of(profile.division, profile.section, profile.work_group)


def path_labels(profile):
    """Q13: three slots, กอง › แผนก › กลุ่มงาน, each N/A when missing."""
    unit = unit_of(profile)
    if unit is None:
        return [NA, NA, NA]
    return [short(unit.division), short(unit.section), short(unit.group)]


def path_names(profile):
    """The full names behind the three slots, for a tooltip."""
    unit = unit_of(profile)
    if unit is None:
        return [NA, NA, NA]
    return [x.name if x else NA for x in (unit.division, unit.section, unit.group)]


# --- Who can take or hear of a case -----------------------------------------------------------


def has_line(user):
    return ChannelIdentity.objects.filter(staff_user=user, is_following=True).exists()


def can_act(user, division_id):
    """Holds a working role in the กอง; ADR 0052: a แอดมินแผนก/กลุ่มงาน of it counts too."""
    if user is None or not user.is_active:
        return False
    scope = identity.staff_scope(user)
    return division_id in scope.division_roles or division_id in scope.unit_divisions


def in_reach(scope, person):
    """ADR 0052 Q6: a แอดมินแผนก/กลุ่มงาน hands work only to people of their unit."""
    if not scope.unit_only:
        return True
    profile = getattr(person, "staff_profile", None)
    return bool(profile) and (
        profile.section_id in scope.sections or profile.work_group_id in scope.work_groups
    )


def heads_only(scope, case):
    """ADR 0054 Q4: a แอดมินฝ่าย (and แอดมินระบบ) hands a case to the owner กอง's แอดมินกอง,
    while the กอง has one."""
    return scope.is_department_supervisor and (
        RoleGrant.objects.filter(
            division_id=case.owner_division_id,
            role=Role.SUPERVISOR,
            revoked_at__isnull=True,
            user__is_active=True,
        ).exists()
    )


def assignable(scope, case):
    """ADR 0054 Q4, Q5: who this person may make the case's ผู้รับผิดชอบ, in the order the web
    and LINE both show. A แอดมินฝ่าย sees the owner กอง's แอดมินกอง (everyone in the กอง when it
    has none); everyone else sees the กอง's people, the แอดมินแผนก and แอดมินกลุ่มงาน first and
    those of the case's แผนก ahead of the rest; a unit admin sees only their unit (ADR 0052 Q6).
    Nobody assigns work to a viewer (ADR 0039)."""
    grants = (
        RoleGrant.objects.filter(
            division_id=case.owner_division_id, revoked_at__isnull=True, user__is_active=True
        )
        .exclude(role__in=VIEWER_ROLES)
        .select_related("user__staff_profile", "work_group")
    )
    grants = [g for g in grants if in_reach(scope, g.user)]
    if heads_only(scope, case):
        grants = [g for g in grants if g.role == Role.SUPERVISOR]
    best = {}
    for g in grants:
        profile = getattr(g.user, "staff_profile", None)
        sections = {
            profile.section_id if profile else None,
            g.section_id,
            g.work_group.section_id if g.work_group else None,
        }
        ours = case.section_id is not None and case.section_id in sections
        key = (g.role not in UNIT_ROLES, not ours, display(g.user), g.user_id)
        if g.user_id not in best or key < best[g.user_id][0]:
            best[g.user_id] = (key, g.user)
    return [user for _, user in sorted(best.values(), key=lambda pair: pair[0])]


def case_groups(case):
    """The groups a case names: each line's LINE group and category, and the topic."""
    groups = set()
    for line in case.lines.select_related("item"):
        groups.update({line.item.line_group, line.item.category})
    if case.topic:
        groups.add(case.topic)
    # A tracking case has no lines: the group its button set says it (ADR 0034).
    groups.add(str((case.intake_data or {}).get("group", "")))
    return groups - {""}


def match(division_id, kind, groups):
    """Q8, Q23: the first active งานที่หน่วยรับ of the division, in the admin's order."""
    works = UnitWork.objects.filter(division_id=division_id, kind=kind, is_active=True)
    for work in works.select_related("division", "section", "work_group__section").order_by(
        "order", "pk"
    ):
        if work.group in groups:
            return work
    return None


def match_case(case, division_id=None):
    return match(division_id or case.owner_division_id, case.kind, case_groups(case))


def unit_for(case, division_id=None):
    """Q14, ADR 0052 Q4: the แผนก and กลุ่มงาน of the unit that takes the case's work, if any."""
    work = match_case(case, division_id)
    section = work and (work.work_group.section if work.work_group else work.section)
    if not (section and section.is_active):
        return None, None
    group = work.work_group
    return section, group if group and group.is_active else None


def section_for(case, division_id=None):
    return unit_for(case, division_id)[0]


def place(case, division_id=None):
    """Set the case's แผนก and กลุ่มงาน from the unit cards (the caller saves)."""
    case.section, case.work_group = unit_for(case, division_id)


def group_in(case, section):
    """ADR 0052 Q20: the กลุ่มงาน of the first unit card inside `section` the case matches;
    None when that card is the แผนก's own or nothing there matches."""
    if section is None:
        return None
    groups = case_groups(case)
    works = UnitWork.objects.filter(
        Q(section=section) | Q(work_group__section=section),
        division_id=case.owner_division_id,
        kind=case.kind,
        is_active=True,
    ).select_related("work_group")
    for work in works.order_by("order", "pk"):
        if work.group in groups:
            group = work.work_group
            return group if group and group.is_active else None
    return None


def follow_person(case, person):
    """ADR 0052 Q18: a case with no แผนก takes the สังกัด of its new ผู้รับผิดชอบ, when that
    person sits in a แผนก of the case's กอง (the caller saves). Returns whether it moved."""
    if case.section_id is not None or person is None:
        return False
    profile = (
        StaffProfile.objects.filter(user=person, division_id=case.owner_division_id)
        .select_related("section", "work_group__section")
        .first()
    )
    group = profile.work_group if profile and profile.work_group else None
    group = group if group and group.is_active else None
    section = group.section if group else profile and profile.section
    if not (section and section.is_active and section.division_id == case.owner_division_id):
        return False
    case.section, case.work_group = section, group
    return True


@dataclass
class Pick:
    """Who takes the work and why, for the case and for the admin's preview (Q10)."""

    work: UnitWork | None = None
    person: object = None
    notes: list = field(default_factory=list)


def responsible(work, skip=()):
    """Q21: the first ผู้รับผิดชอบประจำงาน who can act; the others are noted. ADR 0054 Q13: nobody
    is skipped for being away any more."""
    pick = Pick(work=work)
    if work is None:
        return pick
    owners = work.owners.select_related("user__staff_profile").order_by("order", "pk")
    if not owners:
        pick.notes.append("ไม่ได้ตั้งผู้รับผิดชอบประจำงาน เรื่องรอคนในกองกดรับงาน")
        return pick
    for owner in owners:
        user, name = owner.user, display(owner.user)
        if user.pk in skip:
            continue
        if not can_act(user, work.division_id):
            pick.notes.append(f"{name} ไม่มีสิทธิ์ในกองนี้แล้ว จึงข้ามไป")
            continue
        pick.person = user
        return pick
    pick.notes.append("ผู้รับผิดชอบที่ตั้งไว้ทำงานนี้ไม่ได้ทุกคน เรื่องรอคนในกองกดรับงาน")
    return pick


def start_unit(division, work, section=None):
    if work is not None:
        return Unit.of(work.division, work.section, work.work_group)
    if section is not None and section.division_id == division.pk:
        return Unit.of(section=section)
    return Unit.of(division=division)


def line_people(division, work, assignee=None, section=None, with_reasons=False):
    """Q15, Q25: the people told of an important case on LINE, smallest first: the responsible
    person, the ผู้รับแจ้งเรื่องสำคัญ of the unit and then of the bigger units, then the
    Supervisors of the กอง. Each step counts only people with LINE linked. None means the
    caller keeps the old last step (everyone told)."""
    if assignee is not None and assignee.is_active and has_line(assignee):
        found = [(assignee, "ผู้รับผิดชอบเรื่อง")]
        return found if with_reasons else [u for u, _ in found]
    skipped = []
    for unit in start_unit(division, work, section).chain():
        users = [
            r.user
            for r in unit.rows(UnitRecipient).select_related("user__staff_profile")
            if r.user.is_active and has_line(r.user)
        ]
        if users:
            reason = f"ผู้รับแจ้งเรื่องสำคัญของ {unit.path}"
            if skipped:
                reason += f" (ไม่มีคนใน {' และ '.join(skipped)} ที่ผูก LINE)"
            found = [(u, reason) for u in users]
            return found if with_reasons else users
        skipped.append(unit.path)
    heads = [
        g.user
        for g in RoleGrant.objects.filter(
            division=division, role=Role.SUPERVISOR, revoked_at__isnull=True, user__is_active=True
        ).select_related("user__staff_profile")
        if has_line(g.user)
    ]
    found = [(u, f"แอดมินของ {short(division)} (ยังไม่ได้เลือกผู้รับแจ้ง)") for u in heads]
    return (found if with_reasons else heads) or None


# --- Assigning cases ------------------------------------------------------------------------


def _set(case, person, why):
    """Hand the case to `person` (or nobody) as the system, with its own timeline entry."""
    from operations import services  # local: services calls this module while it submits

    previous = case.assignee
    if (previous.pk if previous else None) == (person.pk if person else None):
        return False
    case.assignee = person
    case.auto_assigned = person is not None
    case.accepted_by = case.accepted_at = None  # ADR 0049: the new person presses รับงาน
    follow_person(case, person)  # ADR 0052 Q18: a กอง-level card leaves no แผนก
    case.revision += 1
    case.updated_at = timezone.now()
    case.save(
        update_fields=[
            "section",
            "work_group",
            "assignee",
            "auto_assigned",
            "accepted_by",
            "accepted_at",
            "revision",
            "updated_at",
        ]
    )
    services.record_system(
        case,
        "assign",
        why,
        {
            "assignee_id": person.pk if person else None,
            "previous_assignee_id": previous.pk if previous else None,
            "assignee_name": display(person) if person else "",
            "auto": True,
        },
    )
    return True


@transaction.atomic
def assign_new(case):
    """Q20: a new case (still awaiting approval) goes to the ผู้รับผิดชอบประจำงาน at once, and
    Q22: so does a case a new กอง accepted (it starts with nobody)."""
    case = Case.objects.select_for_update().get(pk=case.pk)
    if case.status in workflow.TERMINAL or case.assignee_id:
        return case
    work = match_case(case)
    if work is None:
        return case
    case.unit_work = work
    case.save(update_fields=["unit_work"])
    pick = responsible(work)
    if pick.person is not None:
        unit = Unit.of(work.division, work.section, work.work_group)
        _set(
            case, pick.person, f"ตามงานที่หน่วยรับ {work_label(work.kind, work.group)} ของ {unit.path}"
        )
    return case


after_owner_change = assign_new


@transaction.atomic
def after_section_change(case):
    """Q24: a case the system assigned follows its new แผนก, when that แผนก named somebody."""
    case = Case.objects.select_for_update().get(pk=case.pk)
    if not case.auto_assigned or case.section_id is None:
        return
    works = UnitWork.objects.filter(
        division_id=case.owner_division_id, kind=case.kind, is_active=True
    ).filter(section=case.section) | UnitWork.objects.filter(
        division_id=case.owner_division_id,
        kind=case.kind,
        is_active=True,
        work_group__section=case.section,
    )
    groups = case_groups(case)
    for work in works.order_by("order", "pk"):
        if work.group not in groups:
            continue
        pick = responsible(work)
        if pick.person is not None:
            case.unit_work = work
            case.save(update_fields=["unit_work"])
            _set(case, pick.person, f"เปลี่ยนแผนกเป็น {short(case.section)}")
        return


def _open_auto(**filters):
    return (
        Case.objects.select_for_update(of=("self",))
        .filter(auto_assigned=True, status__in=workflow.OPEN, **filters)
        .select_related("unit_work", "assignee__staff_profile")
        .order_by("pk")
    )


def release(user, why):
    """Q24: the open cases the system gave this person move to the next one who can take them.
    A person who can still act keeps a case nobody else can take; one who cannot gives it up."""
    moved = 0
    for case in _open_auto(assignee=user):
        work = case.unit_work or match_case(case)
        pick = responsible(work, skip={user.pk})
        if pick.person is not None:
            moved += _set(case, pick.person, why)
        elif not can_act(user, case.owner_division_id):
            moved += _set(case, None, why)
    return moved


def refollow(work, why):
    """Q24: the admin changed who takes this work; its open system-assigned cases follow."""
    moved = 0
    for case in _open_auto(unit_work=work):
        pick = responsible(work)
        if pick.person is not None:
            moved += _set(case, pick.person, why)
    return moved


# --- Admin changes (Q17: admins only) ---------------------------------------------------------


def _admin(actor):
    identity.require_admin(actor)


@transaction.atomic
def set_short_name(actor, unit, value, reason=""):
    _admin(actor)
    value = (value or "").strip()[:32]
    target = unit.group or unit.section or unit.division
    attr = "short_name" if isinstance(target, Division) else "code"
    previous = getattr(target, attr)
    if previous == value:
        raise ValidationError("ชื่อย่อนี้ตั้งไว้แล้ว")
    setattr(target, attr, value)
    target.save(update_fields=[attr])
    identity.audit(actor, "unit.short_name", target, reason, previous=previous, value=value)


@transaction.atomic
def create_group(actor, section, name, code="", reason=""):
    """Q11, Q13: only an admin makes a กลุ่มงาน; staff of the แผนก whose roster row names it
    move in at once."""
    _admin(actor)
    name = (name or "").strip()[:200]
    if not name:
        raise ValidationError("ตั้งชื่อกลุ่มงาน")
    if WorkGroup.objects.filter(section=section, name=name).exists():
        raise ValidationError("แผนกนี้มีกลุ่มงานชื่อนี้แล้ว")
    order = (WorkGroup.objects.filter(section=section).aggregate(m=Max("order"))["m"] or 0) + 1
    group = WorkGroup.objects.create(
        section=section, name=name, code=(code or "").strip()[:32], order=order
    )
    identity.audit(actor, "unit.group_created", group, reason, section=section.name, name=name)
    joined = 0
    for profile in StaffProfile.objects.filter(section=section, work_group__isnull=True):
        employee = roster_row(profile)
        if employee and employee.work_group.strip() == name:
            profile.work_group = group
            profile.save(update_fields=["work_group"])
            joined += 1
    return group, joined


@transaction.atomic
def close_group(actor, group, reason=""):
    """A กลุ่มงาน that ends: its people stay in the แผนก, its work and receivers go with it."""
    _admin(actor)
    StaffProfile.objects.filter(work_group=group).update(work_group=None)
    UnitWork.objects.filter(work_group=group).update(is_active=False)
    UnitRecipient.objects.filter(work_group=group).delete()
    group.is_active = False
    group.save(update_fields=["is_active"])
    identity.audit(actor, "unit.group_closed", group, reason)


def roster_row(profile):
    from operations.models import Employee

    if not profile.employee_id:
        return None
    return Employee.objects.filter(employee_id=profile.employee_id, is_active=True).first()


def _check_member(user, unit):
    held = (
        RoleGrant.objects.filter(user=user, division=unit.division, revoked_at__isnull=True)
        .exclude(role__in=VIEWER_ROLES)
        .exists()
    )
    if not held:
        raise ValidationError(f"บัญชีนี้ไม่มีบทบาทใน {short(unit.division)} จึงย้ายเข้าไม่ได้")


@transaction.atomic
def move_person(actor, user, unit, reason=""):
    """Q5: an admin sets the สังกัด of a person (a unit of a กอง where they hold a role, or none)."""
    _admin(actor)
    profile = StaffProfile.objects.select_for_update().get(user=user)
    if unit is not None:
        _check_member(user, unit)
    before = unit_of(profile)
    if (before.key if before else None) == (unit.key if unit else None):
        raise ValidationError("บัญชีนี้อยู่หน่วยนี้แล้ว")
    profile.division = unit.division if unit else None
    profile.section = unit.section if unit else None
    profile.work_group = unit.group if unit else None
    profile.roster_row = None  # off the chart until the next import, so the label shows the move
    profile.save(update_fields=["division", "section", "work_group", "roster_row"])
    identity.audit(
        actor,
        "staff.unit_changed",
        user,
        reason,
        unit=unit.path if unit else NA,
        previous=before.path if before else NA,
    )


def bind_from_roster(user, employee):
    """The สังกัด the roster names (1 Nov 2569 Q3: the file wins on every import; an admin's move
    lasts until the next one). A level the system does not know stays N/A."""
    profile = StaffProfile.objects.filter(user=user).first()
    if profile is None:
        return
    division, section, group = employee.division, None, None
    if division is not None:
        section = _find(
            Section.objects.filter(division=division, is_active=True), employee.section_name
        )
    if section is not None:
        group = _find(
            WorkGroup.objects.filter(section=section, is_active=True), employee.work_group
        )
    now = (division, section, group, employee)
    if (profile.division, profile.section, profile.work_group, profile.roster_row) != now:
        profile.division, profile.section, profile.work_group, profile.roster_row = now
        profile.save(update_fields=["division", "section", "work_group", "roster_row"])


def _find(rows, text):
    from operations import roster  # local: roster builds on units through identity

    text = (text or "").strip()
    if not text:
        return None
    return next((u for u in rows if roster.same_unit(text, u.name) or text == u.code), None)


@transaction.atomic
def add_work(actor, unit, kind, group, reason=""):
    """Q8: the unit takes this kind of case with this group. A second unit of the same กอง with
    the same work is allowed and warned about; the admin's order decides."""
    _admin(actor)
    if kind not in KIND_SHORT or group not in dict(group_choices()):
        raise ValidationError("เลือกประเภทเรื่องและกลุ่มของ")
    if unit.rows(UnitWork).filter(kind=kind, group=group, is_active=True).exists():
        raise ValidationError("หน่วยนี้รับงานนี้อยู่แล้ว")
    others = list(
        UnitWork.objects.filter(
            division=unit.division, kind=kind, group=group, is_active=True
        ).select_related("division", "section", "work_group__section")
    )
    order = (
        UnitWork.objects.filter(division=unit.division).aggregate(m=Max("order"))["m"] or 0
    ) + 1
    work = UnitWork.objects.create(
        division=unit.division,
        section=unit.section,
        work_group=unit.group,
        kind=kind,
        group=group,
        order=order,
    )
    identity.audit(
        actor, "unit.work_added", work, reason, unit=unit.path, work=work_label(kind, group)
    )
    return work, [Unit.of(o.division, o.section, o.work_group).path for o in others]


@transaction.atomic
def remove_work(actor, work, reason=""):
    """Q22: open cases keep their person; new cases look at the work left."""
    _admin(actor)
    work.is_active = False
    work.save(update_fields=["is_active"])
    identity.audit(actor, "unit.work_removed", work, reason, work=work_label(work.kind, work.group))


@transaction.atomic
def move_work(actor, work, step):
    """Swap with the neighbour above (-1) or below (+1) in the กอง's order."""
    _admin(actor)
    works = list(
        UnitWork.objects.select_for_update()
        .filter(division=work.division, is_active=True)
        .order_by("order", "pk")
    )
    at = next(i for i, w in enumerate(works) if w.pk == work.pk)
    other = at + step
    if not 0 <= other < len(works):
        return
    works[at], works[other] = works[other], works[at]
    for number, row in enumerate(works, start=1):
        if row.order != number:
            row.order = number
            row.save(update_fields=["order"])
    identity.audit(actor, "unit.work_moved", work, step=step)


@transaction.atomic
def set_owners(actor, work, user_ids, reason=""):
    """Q16, Q21: the ordered ผู้รับผิดชอบประจำงาน; open system-assigned cases follow (Q24)."""
    _admin(actor)
    users = []
    for pk in user_ids:
        user = User.objects.filter(pk=pk, is_active=True).first() if str(pk).isdigit() else None
        if user is None or user in users:
            continue
        if not can_act(user, work.division_id):
            raise ValidationError(f"{display(user)} ไม่มีสิทธิ์ใน {short(work.division)}")
        users.append(user)
    before = [o.user_id for o in work.owners.order_by("order", "pk")]
    if before == [u.pk for u in users]:
        raise ValidationError("ผู้รับผิดชอบยังเหมือนเดิม")
    work.owners.all().delete()
    for number, user in enumerate(users, start=1):
        UnitWorkOwner.objects.create(work=work, user=user, order=number)
    identity.audit(
        actor,
        "unit.owners_set",
        work,
        reason,
        work=work_label(work.kind, work.group),
        owners=[display(u) for u in users],
    )
    names = ", ".join(display(u) for u in users) or "ไม่มีใคร"
    return refollow(work, f"แอดมินเปลี่ยนผู้รับผิดชอบประจำงานเป็น {names}")


@transaction.atomic
def set_recipients(actor, unit, user_ids, reason=""):
    """Q12, Q25: the ผู้รับแจ้งเรื่องสำคัญ of one unit; each needs LINE and a role in the กอง."""
    _admin(actor)
    wanted = []
    for pk in user_ids:
        user = User.objects.filter(pk=pk, is_active=True).first() if str(pk).isdigit() else None
        if user is None or user in wanted:
            continue
        if not can_act(user, unit.division.pk):
            raise ValidationError(f"{display(user)} ไม่มีสิทธิ์ใน {short(unit.division)}")
        if not has_line(user):
            raise ValidationError(f"{display(user)} ยังไม่ได้ผูก LINE จึงรับแจ้งไม่ได้")
        wanted.append(user)
    unit.rows(UnitRecipient).exclude(user__in=wanted).delete()
    have = set(unit.rows(UnitRecipient).values_list("user_id", flat=True))
    for user in wanted:
        if user.pk not in have:
            UnitRecipient.objects.create(
                division=unit.division,
                section=unit.section,
                work_group=unit.group,
                user=user,
                added_by=actor,
            )
    identity.audit(
        actor,
        "unit.recipients_set",
        unit.group or unit.section or unit.division,
        reason,
        unit=unit.path,
        people=[display(u) for u in wanted],
    )


# --- Display ----------------------------------------------------------------------------------


def display(user):
    profile = getattr(user, "staff_profile", None)
    return profile.display_name if profile else user.username


def thai_date(value):
    from operations.calendars import MONTHS

    return f"{value.day} {MONTHS[value.month - 1]} {value.year + 543}"
