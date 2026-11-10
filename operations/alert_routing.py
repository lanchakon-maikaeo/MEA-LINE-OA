"""Who a staff or order alert reaches (ADR 0050 Q6–Q8, Q13, Q15).

Each kind on the Push Message page under ถึงเจ้าหน้าที่ and งานสั่ง goes to the แอดมินฝ่าย on
duty (every แอดมินฝ่าย when nobody is). The people a kind went to before this, such as the
assigned ผู้รับผิดชอบ or the แอดมินกอง, still get it when that kind's switch for them is on.
Everyone else sees the work when they open the web or the LINE menu. The person who did the step
is left out unless the self switch is on. LINE and email each have their own switch per kind,
and since ADR 0054 Q9 every group of people has a LINE switch and an email switch of its own.
The kinds about a case may also reach the owner กอง's แอดมินกอง and the case's แอดมินแผนก or
แอดมินกลุ่มงาน (ADR 0054 Q10).
"""

from operations import duty, options


def _without(people, actor_id):
    if actor_id is None or options.get(options.PUSH_SELF) == "on":
        return people
    return [u for u in people if u.pk != actor_id]


def _on(kind, to, email):
    return options.push_on(kind, f"{to}:email" if email else to)


def originals_on(kind, email=False):
    """Whether the kind's own recipients are told on this channel."""
    return kind not in options.ROUTED or _on(kind, options.original(kind), email)


def originals(kind, users, actor_id=None, email=False):
    """The kind's own recipients, when its switch for them (on this channel) is on."""
    users = [u for u in users if u is not None]
    if not originals_on(kind, email):
        return []
    return _without(users, actor_id)


def on_duty(kind, actor_id=None, email=False):
    """The แอดมินฝ่าย on duty, when this kind goes to them on this channel."""
    if kind not in options.ROUTED or not _on(kind, options.DUTY, email):
        return []
    return _without(duty.receivers(email=email), actor_id)


def case_admins(kind, case, actor_id=None, email=False):
    """ADR 0054 Q10: the owner กอง's แอดมินกอง and the แอดมินแผนก of the case's แผนก with the
    แอดมินกลุ่มงาน of its กลุ่มงาน, each group while its switch on this channel is on. A case
    with no แผนก has nobody in the second group."""
    from operations.models import Role, RoleGrant
    from operations.staff_email import members  # local: staff_email plans through here

    if case is None or kind not in options.UNIT_KINDS:
        return []
    people = []
    if kind in options.HOSTED and _on(kind, options.HOST, email) and case.owner_division_id:
        people += members(case.owner_division_id, [Role.SUPERVISOR])
    if _on(kind, options.UNIT, email) and case.section_id:
        grants = RoleGrant.objects.filter(
            revoked_at__isnull=True, user__is_active=True
        ).select_related("user")
        units = grants.filter(role=Role.SECTION_ADMIN, section_id=case.section_id)
        if case.work_group_id:
            units = units | grants.filter(
                role=Role.WORK_GROUP_ADMIN, work_group_id=case.work_group_id
            )
        people += [g.user for g in units.order_by("user_id")]
    seen = set()
    once = [u for u in people if not (u.pk in seen or seen.add(u.pk))]
    return _without(once, actor_id)
