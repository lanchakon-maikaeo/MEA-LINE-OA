"""ADR 0058 (grill 16 พ.ย. 2569): รอรับงาน is a case nobody has pressed รับงาน on since it came
in, while it is neither paused nor ended. Staff read it in red in place of the case's status, as
one tag on the web and in LINE; the LINE inbox opens on a red รอรับงาน tab, four tabs two a row.

Acceptance → tests: a case reads รอรับงาน until the first รับงาน and never again after a new
assignment, while its new ผู้รับผิดชอบ still has to press รับงาน (RuleTests); a paused case
shows its own status (RuleTests); the LINE inbox and the case card (LineTests) and the web list
and case page (WebTests) show it; old cases take the first รับงาน from their history
(BackfillTests).
"""

import importlib

from django.apps import apps
from django.test import Client
from django.urls import reverse

from operations import staff_inbox, workflow
from operations.models import Case, Decision
from operations.tests.test_m03_line import texts
from operations.tests.test_m28_accept_and_cards import HEAD, AcceptTestCase

fill = importlib.import_module("operations.migrations.0109_case_first_accepted_at").fill


def bubble(reply):
    return next(m for m in reply if m["type"] == "flex")["contents"]


def tabs(reply):
    body = bubble(reply)["body"]["contents"]
    rows = [n for n in body if (n.get("contents") or [{}])[0].get("type") == "button"]
    return [
        [(b["action"]["label"], b["style"], b.get("color")) for b in r["contents"]] for r in rows
    ]


def status_of(reply, number):
    for node in bubble(reply)["body"]["contents"]:
        cells = node.get("contents") or []
        if cells and cells[0].get("text") == number:
            return cells[-1]["text"], cells[-1]["color"]
    raise AssertionError(f"{number} not listed")


class RuleTests(AcceptTestCase):
    def test_a_case_is_untaken_until_the_first_accept_and_never_after(self):
        case = self.withdraw("4500070581")
        self.assertTrue(workflow.untaken(case))
        self.assertIn(case, staff_inbox.sources(self.head)[0])  # the default tab
        case = self.assign(case, self.kcw)
        self.assertEqual(workflow.shown_status(case), "รอรับงาน")
        self.assertNotIn(case, staff_inbox.sources(self.head, "unassigned")[0])
        case = self.act(self.kcw, case, "accept_assignment")
        self.assertIsNotNone(case.first_accepted_at)
        case = self.assign(case, self.other)
        self.assertTrue(case.awaits_acceptance)  # the new person still presses รับงาน
        self.assertEqual(workflow.shown_status(case), "กำลังดำเนินการ")
        self.assertNotIn(case, staff_inbox.sources(self.head)[0])

    def test_an_unassigned_new_case_waits_in_both_tabs(self):
        case = self.withdraw("4500070582")
        Case.objects.filter(pk=case.pk).update(assignee=None)
        self.assertIn(case, staff_inbox.sources(self.head, "untaken")[0])
        self.assertIn(case, staff_inbox.sources(self.head, "unassigned")[0])

    def test_a_case_waiting_for_the_division_reads_its_own_status(self):
        case = self.older_waiting("4500070590")  # รอรับเรื่อง: รับเรื่อง is not รับงาน
        self.assertEqual(workflow.shown_status(case), "รอรับเรื่อง")
        self.assertNotIn(case, staff_inbox.sources(self.head)[0])

    def test_a_paused_case_shows_its_own_status(self):
        case = self.assign(self.withdraw("4500070583"), self.kcw)
        case = self.act(self.other, case, "request_info", reason="ขอเลขเครื่อง")
        self.assertEqual(workflow.shown_status(case), "รอข้อมูลเพิ่ม")
        self.assertNotIn(case, staff_inbox.sources(self.head)[0])


class LineTests(AcceptTestCase):
    def test_the_inbox_opens_on_a_red_untaken_tab(self):
        case = self.assign(self.withdraw("4500070584"), self.kcw)
        reply = self.say(self.postback(HEAD, "m=staff_inbox"))
        self.assertEqual(
            tabs(reply),
            [
                [("รอรับงาน", "primary", "#D30005"), ("กำลังดำเนินการ", "secondary", None)],
                [("รอมอบหมาย", "secondary", None), ("ทั้งหมด", "secondary", None)],
            ],
        )
        self.assertEqual(status_of(reply, case.number), ("รอรับงาน", "#D30005"))
        reply = self.press(HEAD, "กำลังดำเนินการ", reply)
        self.assertEqual(tabs(reply)[0][0], ("รอรับงาน", "secondary", "#FADAD8"))
        self.assertEqual(tabs(reply)[0][1], ("กำลังดำเนินการ", "primary", "#16804A"))
        self.assertEqual(status_of(reply, case.number), ("รอรับงาน", "#D30005"))
        shown = texts(self.press(HEAD, case.number, reply))
        self.assertIn("สถานะ: รอรับงาน", shown)
        self.assertIn("· ยังไม่กดรับงาน", shown)

    def test_a_taken_case_shows_its_status_in_grey(self):
        case = self.act(
            self.kcw, self.assign(self.withdraw("4500070585"), self.kcw), "accept_assignment"
        )
        reply = self.press(HEAD, "กำลังดำเนินการ", self.say(self.postback(HEAD, "m=staff_inbox")))
        self.assertEqual(status_of(reply, case.number), ("กำลังดำเนินการ", "#888888"))


class WebTests(AcceptTestCase):
    def test_one_tag_on_the_list_and_the_case_page(self):
        case = self.assign(self.withdraw("4500070586"), self.kcw)
        client = Client()
        client.force_login(self.head)
        listed = reverse("division-detail", args=["กคว."]) + "?tab=host"
        tag = '<span class="tag tag--alert">รอรับงาน</span>'
        self.assertContains(client.get(listed), tag, count=1)
        self.assertContains(client.get(listed), "ส่งต่อมาถึงกอง")  # ADR 0053 Q3 still holds
        page = client.get(reverse("case-detail", args=[case.number]))
        self.assertContains(page, tag, count=1)
        self.assertContains(page, "ยังไม่กดรับงาน")
        case = self.assign(self.act(self.kcw, case, "accept_assignment"), self.other)
        self.assertNotContains(client.get(listed), "รอรับงาน")
        page = client.get(reverse("case-detail", args=[case.number]))
        self.assertNotContains(page, tag)
        self.assertContains(page, "ยังไม่กดรับงาน")  # the new person has not pressed it yet


class BackfillTests(AcceptTestCase):
    def test_old_cases_take_the_first_accept_from_their_history(self):
        reassigned = self.act(
            self.kcw, self.assign(self.withdraw("4500070587"), self.kcw), "accept_assignment"
        )
        reassigned = self.assign(reassigned, self.other)
        mine = self.assign(self.withdraw("4500070588"), self.head, by=self.head)
        waiting = self.assign(self.withdraw("4500070589"), self.kcw)
        Case.objects.update(first_accepted_at=None)
        fill(apps, None)
        accepted = Decision.objects.get(case=reassigned, action="accept_assignment").created_at
        firsts = dict(Case.objects.values_list("pk", "first_accepted_at"))
        self.assertEqual(firsts[reassigned.pk], accepted)
        self.assertIsNotNone(firsts[mine.pk])
        self.assertIsNone(firsts[waiting.pk])
