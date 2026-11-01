"""flow LINE ในหน้าแอดมิน (ADR 0023): drafts are checked graphs, only admins edit them and
publishing needs the flow runner. The requirement page is gone (grill กคอ. Q41)."""

import copy
import json
from io import StringIO
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import Client, TestCase

from operations import line_flows, line_menu_catalog
from operations.models import AuditEvent, LineFlow, LineFlowVersion, Role
from operations.tests.test_m02_cases import make_staff


def graph(*nodes, edges=()):
    return {"nodes": list(nodes), "edges": list(edges)}


def node(nid, kind, title="กล่อง", buttons=(), **config):
    return {
        "id": nid,
        "kind": kind,
        "title": title,
        "text": "",
        "buttons": [
            {"id": f"{nid}.{i}", "label": b, "full": "", "note": ""} for i, b in enumerate(buttons)
        ],
        "config": config,
        "x": 0,
        "y": 0,
        "w": 260,
    }


def edge(a, b, port=None):
    return {"id": f"{a}-{b}-{port}", "from": a, "port": port, "to": b, "label": ""}


class FlowTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("load_districts", stdout=StringIO())
        call_command("load_org", stdout=StringIO())
        line_menu_catalog.official_division_code()  # กองอุปกรณ์ฯ is กอจ. on production
        call_command("seed_line_flows", stdout=StringIO())
        cls.admin = make_staff("flowadmin", (Role.ADMIN, None))
        cls.staff = make_staff("flowstaff", (Role.STAFF, "กคว."))
        cls.flow = LineFlow.objects.get(key="meter-ct-vt")

    def draft(self):
        return line_flows.draft_of(self.flow)


class CheckTests(FlowTestCase):
    def messages(self, g, level="error"):
        return [
            i["message"] for i in line_flows.check(line_flows.clean_graph(g)) if i["level"] == level
        ]

    def test_the_kcw_conclusion_passes_the_checks(self):
        self.assertEqual(self.messages(self.draft().graph), [])
        self.assertEqual(self.messages(self.draft().graph, "warn"), [])
        kinds = {n["kind"] for n in self.draft().graph["nodes"]}
        self.assertTrue(
            {"entry", "message", "ask", "branch", "summary", "create_case", "book"} <= kinds
        )

    def test_dead_ends_line_limits_and_questions_without_rules_are_errors(self):
        long = "ขอเครื่องวัดฯ กลับคืน"  # 21 characters: LINE cuts it
        g = graph(
            node("s", "entry"),
            node("m", "message", buttons=[long, "ไปต่อ"]),
            node("a", "ask", title="ถาม"),
            node("x", "result"),
            edges=[edge("s", "m"), edge("m", "a", "m.1")],
        )
        errors = self.messages(g)
        self.assertTrue(any("ยาวเกิน 20" in m for m in errors))
        self.assertTrue(any(f"ปุ่ม '{long}' ยังไม่ได้บอกว่าพาไปไหน" in m for m in errors))
        self.assertTrue(any("ทางตัน" in m for m in errors))  # the question leads nowhere
        self.assertTrue(any("เก็บลงช่องไหน" in m for m in errors))
        self.assertTrue(any("กฎตรวจ" in m for m in errors))
        self.assertTrue(any("ไม่ถึงจากปุ่มเมนู" in m for m in self.messages(g, "warn")))

    def test_more_than_thirteen_buttons_and_two_entries_are_errors(self):
        g = graph(
            node("s", "entry"),
            node("t", "entry"),
            node("m", "message", buttons=[str(i) for i in range(14)]),
        )
        errors = self.messages(g)
        self.assertTrue(any("หนึ่งกล่องพอดี" in m for m in errors))
        self.assertTrue(any("เกิน 13 ปุ่ม" in m for m in errors))

    def test_a_bad_shape_is_refused(self):
        for bad in (
            {"nodes": "x"},
            {"nodes": [{"id": "a", "kind": "code"}]},
            {"nodes": [node("a", "entry"), node("a", "result")]},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                line_flows.clean_graph(bad)


class DraftTests(FlowTestCase):
    def test_only_admins_save_and_a_stale_revision_is_refused(self):
        draft = self.draft()
        g = copy.deepcopy(draft.graph)
        with self.assertRaises(PermissionDenied):
            line_flows.save_draft(self.staff, draft, g, draft.revision)
        saved = line_flows.save_draft(self.admin, draft, g, draft.revision)
        self.assertEqual(saved.revision, draft.revision + 1)
        with self.assertRaises(ValidationError):
            line_flows.save_draft(self.admin, draft, g, draft.revision)  # someone saved since

    def test_publishing_needs_the_runner_and_keeps_the_old_version(self):
        draft = self.draft()
        with patch.object(line_flows, "RUNNER_READY", False), self.assertRaises(ValidationError):
            line_flows.publish(self.admin, draft, "ลองเผยแพร่")
        with patch.object(line_flows, "RUNNER_READY", True):
            with self.assertRaises(ValidationError):
                line_flows.publish(self.admin, draft, "")  # a reason is required
            first = line_flows.publish(self.admin, draft, "ตามข้อสรุป กคว.")
            self.assertEqual(first.status, LineFlowVersion.Status.PUBLISHED)
            second = line_flows.start_draft(self.admin, self.flow)
            self.assertEqual((second.number, second.graph), (2, first.graph))
            line_flows.publish(self.admin, second, "รอบสอง")
        first.refresh_from_db()
        self.assertEqual(first.status, LineFlowVersion.Status.RETIRED)
        self.assertEqual(AuditEvent.objects.filter(action="line_flow.published").count(), 2)

    def test_a_draft_with_errors_cannot_be_published(self):
        draft = self.draft()
        g = copy.deepcopy(draft.graph)
        g["nodes"][1]["buttons"][0]["label"] = "ชื่อปุ่มยาวเกินกว่าที่ LINE รับได้แน่นอน"
        line_flows.save_draft(self.admin, draft, g, draft.revision)
        with patch.object(line_flows, "RUNNER_READY", True), self.assertRaises(ValidationError):
            line_flows.publish(self.admin, self.draft(), "ลอง")


class SeedTests(FlowTestCase):
    def test_seeding_again_changes_nothing(self):
        from operations.models import DivisionSchedule

        before = (LineFlowVersion.objects.count(), DivisionSchedule.objects.count())
        call_command("seed_line_flows", stdout=StringIO())
        self.assertEqual(
            (LineFlowVersion.objects.count(), DivisionSchedule.objects.count()), before
        )

    def test_replace_draft_opens_a_draft_over_a_published_flow(self):
        # 1 Nov 2569: production had the 28 ต.ค. กตฟ. flow published and no draft, so the
        # 30 ต.ค. fixture (ADR 0035) never reached it. It comes in as a draft to publish.
        from operations.management.commands.seed_line_flows import FIXTURES

        flow = LineFlow.objects.get(key="usage")
        old = line_flows.draft_of(flow)
        old.graph["nodes"][1]["title"] = "รุ่น 28 ต.ค."
        old.save(update_fields=["graph"])
        line_flows.publish(self.admin, old, "รุ่นที่ production มีอยู่")
        call_command("seed_line_flows", division="กตฟ.", replace_draft=True, stdout=StringIO())
        fixture = json.loads((FIXTURES / "line_flow_usage_v1.json").read_text(encoding="utf-8"))
        draft = line_flows.draft_of(flow)
        self.assertEqual(draft.number, 2)
        self.assertEqual(draft.graph, line_flows.clean_graph(fixture["graph"]))
        published = flow.versions.get(status=LineFlowVersion.Status.PUBLISHED)
        self.assertEqual(published.number, 1)
        self.assertEqual(published.graph["nodes"][1]["title"], "รุ่น 28 ต.ค.")


class WebTests(FlowTestCase):
    def setUp(self):
        self.client = Client()
        self.client.force_login(self.admin)

    def test_only_admins_open_the_pages(self):
        staff = Client()
        staff.force_login(self.staff)
        for url in (
            "/console/admin/flows/",
            "/console/admin/flows/meter-ct-vt/",
            "/console/admin/districts/",
        ):
            with self.subTest(url=url):
                self.assertEqual(staff.get(url).status_code, 403)
                self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get("/console/admin/requirements/").status_code, 404)

    def test_the_editor_saves_through_json_and_answers_with_the_checks(self):
        page = self.client.get("/console/admin/flows/meter-ct-vt/?node=mult").content.decode()
        self.assertIn('id="flow-config"', page)
        self.assertIn('"focus": "mult"', page)
        draft = self.draft()
        g = copy.deepcopy(draft.graph)
        g["nodes"] = [n for n in g["nodes"] if n["id"] != "donew"]  # leave a dead end
        r = self.client.post(
            "/console/admin/flows/meter-ct-vt/save/",
            json.dumps({"graph": g, "revision": draft.revision}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(any("ทางตัน" in c["message"] for c in r.json()["checks"]))
        stale = self.client.post(
            "/console/admin/flows/meter-ct-vt/save/",
            json.dumps({"graph": g, "revision": draft.revision}),
            content_type="application/json",
        )
        self.assertEqual(stale.status_code, 409)


class MenuTabTests(FlowTestCase):
    def test_the_flow_pages_have_one_tab_per_rich_menu_button(self):
        from operations import line_menu_catalog
        from operations.models import LineMenuItem

        line_menu_catalog.sync()
        client = Client()
        client.force_login(self.admin)
        tiles = list(
            LineMenuItem.objects.filter(tab="report", place="tile", is_active=True).order_by(
                "position"
            )
        )
        page = client.get("/console/admin/flows/").content.decode()
        for tile in tiles:
            self.assertIn(tile.label, page)
        self.assertIn('aria-current="page"', page)
        other = next(t for t in tiles if t.label == "ระบบ SMG, AMR")
        seeded = LineFlow.objects.get(tile=other.label)  # remove the seeded draft to start over
        seeded.versions.all().delete()
        seeded.delete()
        r = client.post("/console/admin/flows/", {"tile": other.pk})
        flow = LineFlow.objects.get(tile=other.label)
        self.assertRedirects(r, f"/console/admin/flows/{flow.key}/", fetch_redirect_response=False)
        draft = line_flows.draft_of(flow)
        self.assertEqual(
            [b["label"] for b in draft.graph["nodes"][1]["buttons"]],
            [c.label[:20] for c in other.choices.filter(is_active=True).order_by("position", "pk")],
        )
        self.assertEqual(flow.versions.filter(status="published").count(), 0)  # chat unchanged
        client.post("/console/admin/flows/", {"tile": other.pk})
        self.assertEqual(LineFlow.objects.filter(tile=other.label).count(), 1)
        editor = client.get(f"/console/admin/flows/{flow.key}/").content.decode()
        self.assertIn("flow-tabs", editor)


class EveryButtonTests(FlowTestCase):
    def test_every_requester_button_has_a_draft_that_passes_the_checks(self):
        from operations import line_menu_catalog

        line_menu_catalog.sync()
        for tab in line_flows.menu_tabs():
            if not tab["can_flow"]:
                continue
            with self.subTest(button=tab["item"].label):
                self.assertIsNotNone(tab["flow"])
                version = line_flows.draft_of(tab["flow"])
                errors = [
                    i["message"] for i in line_flows.check(version.graph) if i["level"] == "error"
                ]
                self.assertEqual(errors, [])
