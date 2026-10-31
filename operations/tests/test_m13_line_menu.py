"""Legacy menu compatibility, requester ownership, and role changes (ADR 0019)."""

from unittest.mock import patch

from django.test import Client, override_settings

from operations import identity, line_menu, services, staff_line
from operations.contracts import Actor
from operations.models import (
    Case,
    ChannelIdentity,
    ConversationDraft,
    Division,
    LineMenuItem,
    Role,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import ALICE, LineTestCase, buttons, texts
from operations.tests.test_m13_org import real_world, withdrawal

LINKS = {
    "Meter, CT, VT": "กคว.",
    "หม้อแปลง, LBS, RMU": "กองอุปกรณ์ฯ",
    "เครื่องมือและอุปกรณ์": "กคอ.",
    "ระบบ SMG, AMR": "ศอค.",
    "ตรวจสอบการใช้ไฟฟ้า": "กตฟ.",
}


class MenuTestCase(LineTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        real_world(cls)
        # The migration seeds the menu before any division exists in a test database.
        for label, code in LINKS.items():
            division = Division.objects.get(code=code)
            tile = LineMenuItem.objects.get(place="tile", label=label)
            LineMenuItem.objects.filter(pk=tile.pk).update(division=division)
            tile.choices.update(division=division)

    def setUp(self):
        super().setUp()
        self.requester = self.approve(ALICE)

    def tile(self, label):
        return LineMenuItem.objects.get(place="tile", label=label)

    def open(self, label):
        return self.say(self.postback(ALICE, f"m=item&id={self.tile(label).pk}"))


class SeedTests(MenuTestCase):
    def test_the_owners_menu_is_seeded(self):
        tiles = list(LineMenuItem.objects.filter(place="tile", tab="report").order_by("position"))
        self.assertEqual(
            [t.label for t in tiles],
            [*LINKS, "แจ้งปัญหา/ข้อเสนอแนะ"],
        )
        self.assertEqual(
            list(self.tile("Meter, CT, VT").choices.values_list("label", flat=True)),
            ["ติดตามใบเบิก", "ส่งคืน", "สถานะการซ่อม"],
        )
        self.assertEqual(LineMenuItem.objects.filter(place="bar").count(), 2)
        # 16 ต.ค. 2569 Q15: the staff tabs come seeded too.
        self.assertEqual(LineMenuItem.objects.filter(tab="work").count(), 6)
        self.assertEqual(LineMenuItem.objects.filter(tab="order").count(), 6)
        self.assertTrue(all(len(i.label) <= 20 for i in LineMenuItem.objects.all()))


class ChatTests(MenuTestCase):
    def test_tracking_with_no_case_asks_the_division(self):
        reply = self.open("Meter, CT, VT")
        self.assertEqual(list(buttons(reply)), ["ติดตามใบเบิก", "ส่งคืน", "สถานะการซ่อม"])
        reply = self.press(ALICE, "ติดตามใบเบิก")
        self.assertIn("ยังไม่มีเรื่อง", texts(reply))
        reply = self.press(ALICE, "ถามเรื่องอื่น")
        self.assertIn("แจ้งรายละเอียดที่ต้องการสอบถาม", texts(reply))
        reply = self.type(ALICE, "ใบเบิกเลข 4900001234 ได้ของเมื่อไร (ทดสอบ)")
        self.assertIn("ส่งถึง: กคว.", texts(reply))
        self.assertIn("หัวข้อ: Meter, CT, VT · ติดตามใบเบิก", texts(reply))
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual(
            (case.kind, case.topic, case.owner_division.code, case.menu_label),
            ("problem", "inquiry", "กคว.", "Meter, CT, VT · ติดตามใบเบิก"),
        )

    def test_tracking_lists_own_cases_first(self):
        services.submit_case(
            Actor.for_requester(self.requester),
            idempotency_key="menu-track-w1",
            payload=withdrawal(("MTR_SYN_A", "1")),
        )
        mine = Case.objects.get()
        self.open("Meter, CT, VT")
        reply = self.press(ALICE, "ติดตามใบเบิก")
        self.assertIn(mine.number, buttons(reply))
        self.assertIn("ถามเรื่องอื่น", buttons(reply))

    def test_a_system_problem_goes_to_its_division_without_items(self):
        self.open("ระบบ SMG, AMR")
        reply = self.press(ALICE, "การจัดทำบิล")
        self.assertIn("แจ้งปัญหาที่พบ", texts(reply))
        self.type(ALICE, "บิลเดือนนี้ไม่ออก (ทดสอบ)")
        reply = self.press(ALICE, "ข้าม")
        self.assertIn("ส่งถึง: ศอค.", texts(reply))
        self.press(ALICE, "ยืนยันส่งเรื่อง")
        case = Case.objects.get()
        self.assertEqual((case.topic, case.owner_division.code), ("system", "ศอค."))
        self.assertFalse(case.lines.exists())

    def test_a_return_starts_with_the_division_set(self):
        self.open("หม้อแปลง, LBS, RMU")
        reply = self.press(ALICE, "ส่งคืน")
        self.assertIn("เลือกหมวดอุปกรณ์", texts(reply))
        self.assertEqual(list(buttons(reply))[:4], ["มิเตอร์", "CT", "VT", "อุปกรณ์อื่น"])
        draft = ConversationDraft.objects.get(status="open")
        self.assertEqual((draft.kind, draft.data["division"]), ("return", "กองอุปกรณ์ฯ"))

    def test_a_closed_button_and_the_old_menu_still_answer(self):
        tile = self.tile("Meter, CT, VT")
        LineMenuItem.objects.filter(pk=tile.pk).update(is_active=False)
        self.assertIn("เมนูนี้ปิดแล้ว", texts(self.open("Meter, CT, VT")))
        self.assertIn("เรื่องของฉัน", texts(self.say(self.postback(ALICE, "m=mine"))))


class ChosenDivisionTests(MenuTestCase):
    def test_the_menu_division_takes_the_whole_request(self):
        payload = {**withdrawal(("MTR_SYN_A", "1"), ("TRF_SYN_A", "1")), "division_code": "กคอ."}
        services.submit_case(
            Actor.for_requester(self.requester), idempotency_key="menu-split", payload=payload
        )
        self.assertEqual(
            list(Case.objects.values_list("owner_division__code", flat=True)), ["กคอ."]
        )


class AdminTests(MenuTestCase):
    def test_menu_editor_and_browser_publish_endpoint_are_removed(self):
        client = Client()
        client.force_login(self.admin)
        self.assertEqual(client.get("/console/admin/line-menu/").status_code, 404)
        self.assertEqual(
            client.post("/console/admin/line-menu/", {"action": "publish"}).status_code, 404
        )


class StaffMenuTests(MenuTestCase):
    """16 ต.ค. 2569 Q2, Q9, Q13: staff and Supervisors get menus with tabs; a confirmed save
    publishes every page; a staff LINE follows its role."""

    PICTURE = b"\xff\xd8\xff" + b"0" * 100

    def setUp(self):
        super().setUp()
        self.boss = make_staff("menuadmin", (Role.ADMIN, None))
        self.calls = []

    def fake(self, path, data=None, **kwargs):
        self.calls.append((path, kwargs.get("method"), data))
        if path == "/v2/bot/richmenu/list":
            return {"richmenus": [{"richMenuId": "old-1", "name": "faoj-menu-v2"}]}
        if path == "/v2/bot/richmenu":
            return {"richMenuId": f"new-{data['name']}"}
        if path == "/v2/bot/richmenu/alias/list":
            return {"aliases": [{"richMenuAliasId": "faoj-staff-work"}]}
        if path.startswith("/v2/bot/richmenu/alias/faoj-"):
            return {"richMenuId": "menu-of-" + path.rsplit("/", 1)[1]}
        return {}

    def test_staff_pages_switch_tabs_and_the_user_menu_has_none(self):
        from operations import line_menu_catalog

        line_menu_catalog.sync()
        menu = line_menu.rich_menu("sup-order")
        switches = [a["action"] for a in menu["areas"] if a["action"]["type"] == "richmenuswitch"]
        self.assertEqual(
            [s["richMenuAliasId"] for s in switches],
            ["faoj-sup-report", "faoj-sup-work"],
        )
        tiles = [a for a in menu["areas"] if a["action"]["type"] == "postback"]
        self.assertEqual(len(tiles), 4)
        self.assertTrue(all(a["bounds"]["y"] >= line_menu.TAB_H for a in tiles))
        user = line_menu.rich_menu("user")
        self.assertFalse([a for a in user["areas"] if a["action"]["type"] == "richmenuswitch"])
        self.assertEqual(
            [a["action"]["displayText"] for a in user["areas"]],
            [
                "Meter, CT, VT",
                "หม้อแปลง LBS RMU",
                "เครื่องมือและอุปกรณ์",
                "ระบบ SMG, AMR",
                "ตรวจสอบการใช้ไฟฟ้า",
                "ปัญหา/ข้อเสนอแนะ",
                "เรื่องของฉัน",
                "ติดตามเลขเรื่อง",
            ],
        )
        self.assertEqual(set(line_menu.drawing()["pages"]), set(line_menu.PAGES))

    def test_every_page_limits_buttons_and_service_pages_have_personal_tracking(self):
        from operations import line_menu_catalog

        line_menu_catalog.sync()
        drawing = line_menu.drawing()["pages"]
        for page in line_menu.PAGES:
            with self.subTest(page=page):
                nav = [
                    a["action"]
                    for a in line_menu.rich_menu(page)["areas"]
                    if a["action"]["type"] == "postback"
                ]
                if page == "user" or page.endswith("report"):
                    self.assertEqual(len(nav), 8)
                    self.assertEqual(
                        [a["displayText"] for a in nav],
                        [
                            "Meter, CT, VT",
                            "หม้อแปลง LBS RMU",
                            "เครื่องมือและอุปกรณ์",
                            "ระบบ SMG, AMR",
                            "ตรวจสอบการใช้ไฟฟ้า",
                            "ปัญหา/ข้อเสนอแนะ",
                            "เรื่องของฉัน",
                            "ติดตามเลขเรื่อง",
                        ],
                    )
                else:
                    self.assertEqual(len(nav), 4)
                    self.assertEqual(nav[-1]["data"], "m=roles")
                if page.startswith(("staff-", "sup-")):
                    self.assertEqual(drawing[page][0]["label"], "บริการ")

    @override_settings(LINE_CHANNEL_ACCESS_TOKEN="test-token")
    def test_a_staff_line_follows_its_role(self):
        member = make_staff("kaemember", (Role.STAFF, "กองอุปกรณ์ฯ"))
        line = ChannelIdentity.objects.create(
            channel="line", external_id="U" + "e" * 32, staff_user=member, is_following=True
        )
        with (
            patch("operations.line_menu.line_api.call", side_effect=self.fake),
            self.captureOnCommitCallbacks(execute=True),
        ):
            identity.grant_role(
                self.boss, member, Role.SUPERVISOR, Division.objects.get(code="กองอุปกรณ์ฯ"), "หัวหน้า"
            )
        self.assertIn(
            (f"/v2/bot/user/{line.external_id}/richmenu/menu-of-faoj-sup-report", "POST", None),
            self.calls,
        )
        self.calls.clear()
        with (
            patch("operations.line_menu.line_api.call", side_effect=self.fake),
            self.captureOnCommitCallbacks(execute=True),
        ):
            staff_line.unlink(line, self.boss, "เลิกใช้")
        self.assertEqual(
            self.calls, [(f"/v2/bot/user/{line.external_id}/richmenu", "DELETE", None)]
        )

    @override_settings(LINE_CHANNEL_ACCESS_TOKEN="test-token", OUTBOUND_ENABLED=True)
    def test_removing_last_membership_clears_the_staff_menu(self):
        from operations.models import RoleGrant

        member = make_staff("lastmember", (Role.STAFF, "กองอุปกรณ์ฯ"))
        line = ChannelIdentity.objects.create(
            channel="line", external_id="U" + "f" * 32, staff_user=member, is_following=True
        )
        grant = RoleGrant.objects.get(user=member, role=Role.STAFF, revoked_at=None)
        with (
            patch("operations.line_menu.line_api.call", side_effect=self.fake),
            self.captureOnCommitCallbacks(execute=True),
        ):
            identity.revoke_role(self.boss, grant, "สิ้นสุดหน้าที่")
        self.assertEqual(
            self.calls, [(f"/v2/bot/user/{line.external_id}/richmenu", "DELETE", None)]
        )
