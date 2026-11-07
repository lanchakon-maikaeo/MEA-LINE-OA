"""Who a staff or order alert reaches (ADR 0050 Q6–Q8, Q13, Q15).

Each kind on the Push Message page under ถึงเจ้าหน้าที่ and งานสั่ง goes to the แอดมินฝ่าย on
duty (every แอดมินฝ่าย when nobody is). The people a kind went to before this, such as the
assigned ผู้รับผิดชอบ or the แอดมินกอง, still get it when that kind's switch for them is on.
Everyone else sees the work when they open the web or the LINE menu. The person who did the step
is left out unless the self switch is on. LINE and email each have their own switch per kind;
the people are the same for both.
"""

from operations import duty, options


def _without(people, actor_id):
    if actor_id is None or options.get(options.PUSH_SELF) == "on":
        return people
    return [u for u in people if u.pk != actor_id]


def originals(kind, users, actor_id=None):
    """The kind's own recipients, when its switch for them is on."""
    users = [u for u in users if u is not None]
    if kind in options.ROUTED and not options.push_on(kind, options.original(kind)):
        return []
    return _without(users, actor_id)


def on_duty(kind, actor_id=None, email=False):
    """The แอดมินฝ่าย on duty, when this kind goes to them."""
    if kind not in options.ROUTED or not options.push_on(kind, options.DUTY):
        return []
    return _without(duty.receivers(email=email), actor_id)
