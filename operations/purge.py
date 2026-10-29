"""ล้างข้อมูลจำลอง (15 ต.ค. 2569 Q1–Q4, Q33, Q37): production carried the demo world over from
the Mac staging. This removes it, keeps the cases the owner picks as labelled examples, and turns
the demo staff accounts off. `plan()` changes nothing; `apply()` does it in one transaction.

- Cases: those in a synthetic division, and those submitted before the cutoff (go-live), except
  the kept examples. Every row that refers to a removed row goes with it; a reference that may be
  empty (SET_NULL) is emptied instead. Audit events stay: they name rows by id, not by key.
- Synthetic divisions go the same way. People-side rows (requesters, LINE identities, staff
  profiles, employees) are never removed by that cascade: a nullable reference to them is emptied,
  any other blocks the plan.
- Synthetic requesters, employees, authority grants, policy versions (not active) and equipment
  types with their fake stock: removed only when nothing that stays still refers to them.
- Demo staff accounts (seed prefixes, or a role in a synthetic division): turned off with their
  roles revoked, never deleted, because the audit trail names them.
- The bot's "contact" answer while it still carries the placeholder phone numbers: retired.

History tables are append-only for the app's database role (apply_db_grants), so this runs as the
migration (owner) role, like migrate.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, models, transaction
from django.db.models import Q

from operations import answers, attachments, identity
from operations.models import (
    Attachment,
    AuthorityGrant,
    Case,
    CaseNumberCounter,
    ChannelIdentity,
    ChatAnswer,
    Division,
    Employee,
    EquipmentType,
    FakeSerial,
    FakeStock,
    PolicyVersion,
    Requester,
    RoleGrant,
    StaffProfile,
)

GO_LIVE = datetime(2026, 10, 14, 21, 54, tzinfo=ZoneInfo("Asia/Bangkok"))  # ADR 0015 cutover
SEED_PREFIXES = ("e2e-", "org-demo", "sim-", "demo")
PLACEHOLDER_PHONES = "เบอร์จำลอง"
SYNTHETIC_NAME = "จำลอง"
MAX_EXAMPLES = 2


def _people():
    """Rows the cascade never removes: a nullable reference to them is emptied instead."""
    return {Requester, ChannelIdentity, StaffProfile, Employee, get_user_model()}


def _references(model):
    """(referring model, its foreign key) for every relation that points at `model`."""
    for rel in model._meta.get_fields(include_hidden=True):
        if rel.auto_created and not rel.concrete and (rel.one_to_many or rel.one_to_one):
            yield rel.related_model, rel.field


@dataclass
class Closure:
    delete: dict = field(default_factory=lambda: defaultdict(set))
    empty: dict = field(default_factory=lambda: defaultdict(set))  # (model, fk name) → pks
    blocked: list = field(default_factory=list)

    def count(self):
        return sum(len(pks) for pks in self.delete.values())


def closure(roots, already=None):
    """Every row that refers to the roots, transitively. `already` (a Closure) is going anyway."""
    out = Closure()
    gone = already.delete if already else {}
    people = _people()
    queue = [(model, set(pks)) for model, pks in roots.items() if pks]
    while queue:
        model, pks = queue.pop()
        new = pks - out.delete[model] - gone.get(model, set())
        if not new:
            continue
        out.delete[model] |= new
        for related, fk in _references(model):
            rows = set(
                related._base_manager.filter(**{f"{fk.name}__in": new}).values_list("pk", flat=True)
            ) - gone.get(related, set())
            if not rows:
                continue
            if fk.remote_field.on_delete is models.SET_NULL:
                out.empty[(related, fk.name)] |= rows
            elif related in people:
                if fk.null:
                    out.empty[(related, fk.name)] |= rows
                else:
                    out.blocked.append(f"{related.__name__}.{fk.name} → {model.__name__}")
            else:
                queue.append((related, rows))
    return out


def _merge(into, part):
    for model, pks in part.delete.items():
        into.delete[model] |= pks
    for key, pks in part.empty.items():
        into.empty[key] |= pks
    into.blocked += part.blocked


@dataclass
class Plan:
    keep: list
    cases: list
    users: list
    rows: Closure
    left: list  # (what, why) synthetic rows that stay because something kept refers to them
    contact: ChatAnswer | None

    def summary(self):
        """{model name: rows} removed, most first."""
        counts = {m.__name__: len(p) for m, p in self.rows.delete.items() if p}
        return dict(sorted(counts.items(), key=lambda item: -item[1]))


def candidates(before=GO_LIVE):
    """Cases in real divisions from before go-live: what the owner picks the examples from."""
    return (
        Case.objects.filter(submitted_at__lt=before, owner_division__is_synthetic=False)
        .select_related("owner_division")
        .order_by("kind", "submitted_at")
    )


def demo_users():
    synthetic_roles = RoleGrant.objects.filter(
        revoked_at__isnull=True, division__is_synthetic=True
    ).values("user_id")
    prefixes = Q()
    for prefix in SEED_PREFIXES:
        prefixes |= Q(username__startswith=prefix)
    return (
        get_user_model().objects.filter(prefixes | Q(pk__in=synthetic_roles)).order_by("username")
    )


def plan(keep_numbers=(), before=GO_LIVE, keep_users=()):
    keep_numbers = list(dict.fromkeys(n.strip() for n in keep_numbers if n.strip()))
    if len(keep_numbers) > MAX_EXAMPLES:
        raise ValidationError(f"เก็บเรื่องตัวอย่างได้ไม่เกิน {MAX_EXAMPLES} เรื่อง")
    keep = list(Case.objects.filter(number__in=keep_numbers).select_related("owner_division"))
    missing = set(keep_numbers) - {c.number for c in keep}
    if missing:
        raise ValidationError(f"ไม่พบเรื่อง {', '.join(sorted(missing))}")
    if any(c.owner_division.is_synthetic for c in keep):
        raise ValidationError("เรื่องตัวอย่างต้องอยู่ในกองจริง กองจำลองจะถูกลบทั้งกอง")
    kept = {c.pk for c in keep}
    cases = list(
        Case.objects.filter(Q(owner_division__is_synthetic=True) | Q(submitted_at__lt=before))
        .exclude(pk__in=kept)
        .select_related("owner_division")
        .order_by("submitted_at")
    )
    divisions = set(Division.objects.filter(is_synthetic=True).values_list("pk", flat=True))
    rows = closure({Case: {c.pk for c in cases}, Division: divisions})
    problems = _problems(rows, kept)
    if problems:
        raise ValidationError("ลบไม่ได้: " + "; ".join(problems))

    # The rest goes only if nothing that stays refers to it.
    left = []
    secondary = [
        ("ผู้ใช้จำลอง", Requester, Requester.objects.filter(display_name__contains=SYNTHETIC_NAME)),
        ("พนักงานจำลอง", Employee, Employee.objects.filter(is_synthetic=True)),
        ("อำนาจจำลอง", AuthorityGrant, AuthorityGrant.objects.filter(is_synthetic=True)),
        (
            "กติกาจำลอง",
            PolicyVersion,
            PolicyVersion.objects.filter(is_synthetic=True).exclude(status="active"),
        ),
        ("คลังจำลอง", FakeSerial, FakeSerial.objects.all()),
        ("คลังจำลอง", FakeStock, FakeStock.objects.all()),
        ("ของจำลอง", EquipmentType, EquipmentType.objects.filter(is_synthetic=True)),
    ]
    for label, model, queryset in secondary:
        for pk in queryset.values_list("pk", flat=True):
            if pk in rows.delete.get(model, set()):
                continue
            roots = {model: {pk}}
            if model is Requester:  # its own LINE identities go with it
                roots[ChannelIdentity] = set(
                    ChannelIdentity.objects.filter(requester_id=pk).values_list("pk", flat=True)
                )
            part = closure(roots, already=rows)
            why = _why_it_stays(part, kept)
            if why:
                left.append((f"{label} #{pk}", why))
                continue
            _merge(rows, part)

    keep_users = {u.strip() for u in keep_users if u.strip()}
    users = [u for u in demo_users() if u.username not in keep_users]
    contact = ChatAnswer.objects.filter(
        key="contact", status=ChatAnswer.Status.APPROVED, text__contains=PLACEHOLDER_PHONES
    ).first()
    return Plan(keep, cases, users, rows, left, contact)


def _why_it_stays(part, kept):
    """One short reason a synthetic row stays, or "" when it can go."""
    if _problems(part, kept):
        return "ใช้กับเรื่องตัวอย่าง" if not part.blocked else "; ".join(_problems(part, kept))
    if part.delete.get(Case) or part.delete.get(Division):
        return "มีเรื่องหรือกองที่ยังใช้อยู่อ้างถึง"
    return ""


def _problems(rows, kept):
    """Why a removal cannot go ahead: a people row would go by cascade, or a kept example would
    lose a row that belongs to it."""
    out = [f"{b} (ข้อมูลบุคคล)" for b in sorted(set(rows.blocked))]
    if rows.delete.get(Case, set()) & kept:
        out.append("เรื่องตัวอย่างอยู่ในสิ่งที่จะลบ")
    for model, pks in rows.delete.items():
        for fk in model._meta.concrete_fields:
            if pks and fk.is_relation and fk.related_model is Case:
                if model._base_manager.filter(pk__in=pks, **{f"{fk.name}__in": kept}).exists():
                    out.append(f"{model.__name__} ของเรื่องตัวอย่างจะถูกลบไปด้วย")
    return out


def plan_restart():
    """ล้างผู้แจ้งและเรื่องทั้งหมด (29 Oct 2569): the move to LINE OA @705ttrga gives everyone a
    new LINE user ID, and the user chose to start over. Every case, requester and LINE account
    goes with what refers to them; staff accounts, employees and divisions stay."""
    roots = {
        Case: set(Case.objects.values_list("pk", flat=True)),
        Requester: set(Requester.objects.values_list("pk", flat=True)),
        ChannelIdentity: set(ChannelIdentity.objects.values_list("pk", flat=True)),
    }
    rows = closure(roots)
    problems = _problems(rows, set())
    if problems:
        raise ValidationError("ลบไม่ได้: " + "; ".join(problems))
    cases = list(Case.objects.select_related("owner_division").order_by("submitted_at"))
    return Plan([], cases, [], rows, [], None)


@transaction.atomic
def restart(actor, the_plan, reason, reset_numbers=False):
    removed = apply(actor, the_plan, reason, action="line_data.restarted")
    if reset_numbers:
        CaseNumberCounter.objects.all().delete()
    return removed


@transaction.atomic
def apply(actor, the_plan, reason, action="demo_data.purged"):
    """Remove what the plan lists, label the examples, turn the demo accounts off."""
    identity.require_admin(actor)
    reason = identity._require_reason(reason)
    for user in the_plan.users:
        if user.pk == actor.pk:
            continue
        for grant in RoleGrant.objects.filter(user=user, revoked_at__isnull=True):
            identity.revoke_role(actor, grant, reason)
        if user.is_active:
            identity.set_staff_active(actor, user, False, reason)
    files = list(
        Attachment.objects.filter(pk__in=the_plan.rows.delete.get(Attachment, set()))
        .exclude(storage_name="")
        .values_list("storage_name", flat=True)
    )
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL DEFERRED")
    for (model, name), pks in the_plan.rows.empty.items():
        pks = pks - the_plan.rows.delete.get(model, set())
        if pks:
            model._base_manager.filter(pk__in=pks).update(**{name: None})
    for model, pks in the_plan.rows.delete.items():
        if pks:
            model._base_manager.filter(pk__in=pks)._raw_delete(connection.alias)
    unlinked = the_plan.rows.empty.get((Requester, "employee"), set())
    Requester.objects.filter(pk__in=unlinked).update(employee_link="")
    Case.objects.filter(pk__in=[c.pk for c in the_plan.keep]).update(is_example=True)
    if the_plan.contact is not None:
        answers.retire(actor, the_plan.contact.key, reason)
    identity.audit(
        actor,
        action,
        actor,
        reason,
        removed=the_plan.summary(),
        examples=[c.number for c in the_plan.keep],
        accounts_off=[u.username for u in the_plan.users if u.pk != actor.pk],
    )
    folder = attachments.storage_dir()
    transaction.on_commit(lambda: [(folder / name).unlink(missing_ok=True) for name in files])
    return the_plan.summary()
