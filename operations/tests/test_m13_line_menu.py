"""เมนู LINE (15 ต.ค. 2569 Q17–Q20, Q32, Q35): six topics besides typing, each sending to its
division; tracking shows the person's own cases or asks the division; admins edit the buttons
and publish the picture to LINE only after confirming.
"""

from unittest.mock import patch

from django.core.exceptions import ValidationError
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
        self.assertIn("ยังไม่พบเรื่องของคุณ", texts(reply))
        self.assertIn("พิมพ์สิ่งที่อยากถาม", texts(reply))
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
        self.assertIn("เล่าปัญหาที่พบ", texts(reply))
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
        self.assertIn("เลือกหมวดของ", texts(reply))
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
    def setUp(self):
        super().setUp()
        self.boss = make_staff("menuadmin", (Role.ADMIN, None))

    def test_the_picture_areas_follow_the_buttons(self):
        menu = line_menu.rich_menu()
        self.assertEqual(len(menu["areas"]), 8)  # six tiles and two bar buttons
        first = menu["areas"][0]
        self.assertEqual(first["bounds"], {"x": 0, "y": 0, "width": 833, "height": 700})
        self.assertTrue(first["action"]["data"].startswith("m=item&id="))
        bar = menu["areas"][-1]
        self.assertEqual((bar["bounds"]["y"], bar["bounds"]["height"]), (1400, 286))

    def test_admins_edit_buttons_with_a_reason(self):
        choice = self.tile("ระบบ SMG, AMR").choices.first()
        client = Client()
        client.force_login(self.boss)
        page = client.get("/console/admin/line-menu/")
        self.assertContains(page, "data-menu-canvas")
        self.assertContains(page, 'id="menu-drawing"')
        form = {
            "action": "save",
            "item": choice.pk,
            "label": "ปัญหาการใช้งานระบบที่ยาวเกินยี่สิบ",
            "item_action": "problem",
            "reason": "ทดสอบ",
        }
        page = client.post("/console/admin/line-menu/", form, follow=True)
        self.assertContains(page, "ไม่เกิน 20")
        line_menu.save(
            self.boss,
            choice,
            {
                "label": "ใช้งานระบบไม่ได้",
                "item_action": "problem",
                "division": "ศอค.",
                "topic": "system",
                "is_active": "1",
            },
            "ชื่อสั้นลง",
        )
        choice.refresh_from_db()
        self.assertEqual(choice.label, "ใช้งานระบบไม่ได้")

    @override_settings(LINE_CHANNEL_ACCESS_TOKEN="test-token")
    def test_publishing_needs_the_confirmation_and_replaces_the_old_menu(self):
        client = Client()
        client.force_login(self.boss)
        picture = b"\xff\xd8\xff" + b"0" * 100
        calls = []

        def fake(path, data=None, **kwargs):
            calls.append((path, kwargs.get("method")))
            if path == "/v2/bot/richmenu/list":
                return {"richmenus": [{"richMenuId": "old-1", "name": "faoj-main-v1"}]}
            if path == "/v2/bot/richmenu":
                return {"richMenuId": "new-1"}
            return {}

        with patch("operations.line_menu.line_api.call", side_effect=fake):
            page = client.post(
                "/console/admin/line-menu/",
                {"action": "publish", "reason": "เมนูใหม่", "drawn": ""},
                follow=True,
            )
            self.assertContains(page, "ติ๊กยืนยันก่อน")
            self.assertEqual(calls, [])
            line_menu.publish(self.boss, b"\xff\xd8\xff" + b"0" * 100, "เมนูใหม่")
        self.assertIn(("/v2/bot/richmenu/old-1", "DELETE"), calls)
        self.assertIn(("/v2/bot/user/all/richmenu/new-1", "POST"), calls)
        self.assertTrue(picture)


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
        menu = line_menu.rich_menu("sup-order")
        switches = [a["action"] for a in menu["areas"] if a["action"]["type"] == "richmenuswitch"]
        self.assertEqual(
            [s["richMenuAliasId"] for s in switches],
            ["faoj-sup-report", "faoj-sup-work", "faoj-sup-order"],
        )
        tiles = [a for a in menu["areas"] if a["action"]["type"] == "postback"]
        self.assertEqual(len(tiles), 6)
        self.assertTrue(all(a["bounds"]["y"] >= line_menu.TAB_H for a in tiles))
        user = line_menu.rich_menu("user")
        self.assertFalse([a for a in user["areas"] if a["action"]["type"] == "richmenuswitch"])
        self.assertEqual(set(line_menu.drawing()["pages"]), set(line_menu.PAGES))

    @override_settings(LINE_CHANNEL_ACCESS_TOKEN="test-token")
    def test_publishing_every_page_sets_aliases_and_links_staff(self):
        head = make_staff("kaehead", (Role.SUPERVISOR, "กองอุปกรณ์ฯ"))
        ChannelIdentity.objects.create(
            channel="line", external_id="U" + "d" * 32, staff_user=head, is_following=True
        )
        with patch("operations.line_menu.line_api.call", side_effect=self.fake):
            line_menu.publish(self.boss, dict.fromkeys(line_menu.PAGES, self.PICTURE), "ครบทุกหน้า")
        paths = [(path, method) for path, method, _ in self.calls]
        self.assertIn(("/v2/bot/user/all/richmenu/new-faoj-menu-v2", "POST"), paths)
        self.assertIn(("/v2/bot/richmenu/alias/faoj-staff-work", None), paths)  # updated
        created = [data for path, _, data in self.calls if path == "/v2/bot/richmenu/alias"]
        self.assertEqual(len(created), 4)  # the other staff pages are new aliases
        link = next(data for path, _, data in self.calls if path == "/v2/bot/richmenu/bulk/link")
        self.assertEqual(
            (link["richMenuId"], link["userIds"]), ("new-faoj-sup-work", ["U" + "d" * 32])
        )
        self.assertIn(("/v2/bot/richmenu/old-1", "DELETE"), paths)

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
            (f"/v2/bot/user/{line.external_id}/richmenu/menu-of-faoj-sup-work", "POST", None),
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

    @override_settings(LINE_CHANNEL_ACCESS_TOKEN="test-token")
    def test_a_confirmed_save_publishes_from_the_page(self):
        client = Client()
        client.force_login(self.boss)
        tile = LineMenuItem.objects.get(tab="work", action="work_new")
        form = {
            "action": "save",
            "item": tile.pk,
            "label": "งานใหม่",
            "item_action": "work_new",
            "is_active": "1",
            "reason": "ชื่อสั้นลง",
        }
        page = client.post("/console/admin/line-menu/", form, follow=True)
        self.assertNotContains(page, "data-menu-auto")  # no tick, no publish
        page = client.post("/console/admin/line-menu/", {**form, "publish": "1"}, follow=True)
        self.assertContains(page, "data-menu-auto")
        self.assertContains(page, 'name="drawn-sup-order"')
        with self.assertRaises(ValidationError):
            line_menu.save(self.boss, tile, {**form, "item_action": "track"}, "ผิดแท็บ")
