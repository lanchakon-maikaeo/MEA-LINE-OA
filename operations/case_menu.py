"""สิ่งที่ทำได้ (ADR 0055): the steps a person may take on a case, as one list that the web's
case page and the LINE card both show, with the same names, order and groups.

The web's offers (web_actions.available) and its appointment and warehouse steps
(warehouse_web.case_steps) meet here, in the order of ADR 0051 Q8: รับงาน while it waits, an
appointment the requester waits on, the next warehouse stage, the main step, the rest, and last
the steps that cannot be undone. LINE groups the same list as ทำต่อ, อื่น ๆ, ทำในเว็บ and
ย้อนกลับไม่ได้; what one channel shows the other does too.
"""

from dataclasses import dataclass

from django.urls import reverse


@dataclass(frozen=True)
class Item:
    kind: str  # "action", "stage" or "appointment"
    name: str  # the action, the warehouse stage, or the appointment's id
    label: str
    url: str  # its form on the web
    irreversible: bool = False
    line: bool = True  # False: its form needs the web (item rows, ticks, a long list)


@dataclass
class Menu:
    items: list
    offers: list  # web_actions.available, for the hints
    st: object
    panel: dict
    lead: bool = True  # the first item is the step to do next (a main step, or a stage)

    @property
    def first(self):
        """ทำต่อ: the one step to do next, never one that cannot be undone."""
        if self.lead and self.items and not self.items[0].irreversible:
            return self.items[0]
        return None

    @property
    def more(self):
        return [i for i in self.items[1 if self.first else 0 :] if not i.irreversible]

    @property
    def irreversible(self):
        return [i for i in self.items if i.irreversible]

    @property
    def irreversible_names(self):
        return ", ".join(i.label for i in self.irreversible)

    def find(self, kind, name):
        return next((i for i in self.items if i.kind == kind and i.name == str(name)), None)


def build(user, case, st=None, panel=None, line=False):
    """The menu of `user` for `case`; `line` also works out which steps chat can ask."""
    from operations import warehouse_web, web_actions

    st = st or web_actions.situation(user, case)
    offers = web_actions.available(user, case, st)
    panel = warehouse_web.case_panel(user, case) if panel is None else panel
    steps = warehouse_web.case_steps(case, panel)
    if web_actions._awaits_me(st):
        # ADR 0049 Q9, ADR 0055 Q6: no warehouse stage or นัดรับของ before รับงาน; the
        # โน้ตบุ๊กและหัวอ่าน appointment keeps its flow as it was.
        steps = [s for s in steps if "appointment" in s and not s["appointment"].calendar.is_pickup]
    head = [o for o in offers if o.primary and (o.name == "accept_assignment" or not steps)]
    rest = [o for o in offers if o not in head]  # already the irreversible ones last
    items = [
        *(_offer(user, case, st, o, line) for o in head),
        *(_step(case, s) for s in steps),
        *(_offer(user, case, st, o, line) for o in rest),
    ]
    return Menu(items, offers, st, panel, lead=bool(head or steps))


def _offer(user, case, st, offer, line):
    url = reverse("case-action", args=[case.number, offer.name])
    chat = True
    if line:
        from operations import staff_work

        chat = staff_work.chat_fields(user, case, offer.name, st) is not None
    return Item("action", offer.name, offer.label, url, offer.irreversible, chat)


def _step(case, step):
    if "appointment" in step:
        a = step["appointment"]
        # ADR 0055: นัดรับของ is answered in LINE; โน้ตบุ๊กและหัวอ่าน stays on the web as before.
        return Item("appointment", str(a.pk), step["label"], step["url"], line=a.calendar.is_pickup)
    return Item("stage", step["stage"], step["label"], step["url"])
