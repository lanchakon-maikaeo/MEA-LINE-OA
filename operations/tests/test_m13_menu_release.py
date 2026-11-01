"""A menu deployment resumes failed calls and never deletes menus before verifying links."""

from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings

from operations import line_api, line_menu, line_menu_catalog, line_menu_release
from operations.models import ChannelIdentity, LineMenuRelease


@override_settings(
    LINE_CHANNEL_ACCESS_TOKEN="test-token",
    LINE_CHANNEL_SECRET="test-secret",  # pragma: allowlist secret -- test-only fake credentials
)
class ReleaseTests(TestCase):
    def setUp(self):
        call_command("load_org", verbosity=0)
        line_menu_catalog.sync()
        self.images = line_menu_release.pictures()
        ChannelIdentity.objects.create(channel="line", external_id="test-viewer")
        self.calls, self.links, self.aliases = [], {}, {}
        self.created = 0
        self.fail_alias = False
        self.verify = True
        self.friend = True

    def api(self, path, data=None, **kwargs):
        method = kwargs.get("method", "POST" if data is not None else "GET")
        self.calls.append((path, method))
        if path == "/v2/bot/richmenu/alias/list":
            return {
                "aliases": [
                    {"richMenuAliasId": k, "richMenuId": v} for k, v in self.aliases.items()
                ]
            }
        if path == "/v2/bot/richmenu/list":
            return {"richmenus": [{"richMenuId": "old-menu", "name": "faoj-main-v1"}]}
        if path == "/v2/bot/user/all/richmenu":
            return {"richMenuId": "old-menu"}
        if path == "/v2/bot/richmenu":
            self.created += 1
            return {"richMenuId": f"new-menu-{self.created}"}
        if "/alias" in path and method == "POST":
            if self.fail_alias:
                self.fail_alias = False
                raise OSError("temporary network error")
            self.aliases[data.get("richMenuAliasId", path.rsplit("/", 1)[-1])] = data["richMenuId"]
        if path.startswith("/v2/bot/user/test-viewer/richmenu/"):
            self.links["test-viewer"] = path.rsplit("/", 1)[-1]
        if path == "/v2/bot/profile/test-viewer" and not self.friend:
            raise line_api.LineApiError(404, "Not found")
        if path == "/v2/bot/user/test-viewer/richmenu":
            return {"richMenuId": self.links.get("test-viewer") if self.verify else "old-menu"}
        return {}

    def test_failure_resumes_uploaded_menus_and_verifies_before_cleanup(self):
        self.fail_alias = True
        with patch.object(line_api, "call", side_effect=self.api):
            with self.assertRaises(ValidationError):
                line_menu.publish(None, self.images, "release test")
            release = LineMenuRelease.objects.get()
            self.assertEqual(release.status, "partial")
            self.assertEqual(self.created, len(line_menu.PAGES))
            self.assertNotIn(("/v2/bot/richmenu/old-menu", "DELETE"), self.calls)
            self.assertFalse(any(p.startswith("/v2/bot/user/all/richmenu/") for p, _ in self.calls))
            line_menu.publish(None, self.images, "retry")
            self.assertEqual(self.created, len(line_menu.PAGES))
            self.assertIn(("/v2/bot/richmenu/old-menu", "DELETE"), self.calls)
            release.refresh_from_db()
            self.assertEqual(release.status, "complete")
            before = len(self.calls)
            line_menu.publish(None, self.images, "already current")
            self.assertEqual(self.calls[before:], [("/v2/bot/user/test-viewer/richmenu", "GET")])

    def test_completed_release_repairs_a_line_user_menu_changed_outside_the_app(self):
        with patch.object(line_api, "call", side_effect=self.api):
            line_menu.publish(None, self.images, "first release")
            self.links["test-viewer"] = "old-menu"

            line_menu.publish(None, self.images, "repair LINE link")

        release = LineMenuRelease.objects.get()
        self.assertEqual(release.status, "complete")
        self.assertEqual(self.links["test-viewer"], release.menus["user"])
        self.assertIn(
            (f"/v2/bot/user/test-viewer/richmenu/{release.menus['user']}", "POST"), self.calls
        )

    def test_completed_release_preserves_the_tab_selected_within_the_users_role(self):
        with patch.object(line_api, "call", side_effect=self.api):
            for role, tabs in line_menu.ROLE_TABS.items():
                with patch.object(
                    line_menu_release, "audience", return_value={"test-viewer": f"{role}-work"}
                ):
                    line_menu.publish(None, self.images, "first role release")
                    release = LineMenuRelease.objects.get()
                    for tab in tabs:
                        with self.subTest(role=role, tab=tab):
                            chosen = release.menus[f"{role}-{tab}"]
                            self.links["test-viewer"] = chosen
                            before = len(self.calls)

                            line_menu.publish(None, self.images, "check selected tab")

                            self.assertEqual(self.links["test-viewer"], chosen)
                            self.assertEqual(
                                self.calls[before:],
                                [("/v2/bot/user/test-viewer/richmenu", "GET")],
                            )

    def test_revoking_supervisor_replaces_its_tabs_with_the_staff_menu(self):
        with patch.object(line_api, "call", side_effect=self.api):
            with patch.object(
                line_menu_release, "audience", return_value={"test-viewer": "sup-work"}
            ):
                line_menu.publish(None, self.images, "supervisor release")
            release = LineMenuRelease.objects.get()
            self.links["test-viewer"] = release.menus["sup-order"]
            with patch.object(
                line_menu_release, "audience", return_value={"test-viewer": "staff-work"}
            ):
                line_menu.publish(None, self.images, "supervisor revoked")

        self.assertEqual(self.links["test-viewer"], release.menus["staff-work"])

    def test_accepted_but_unverified_link_keeps_old_menu(self):
        self.verify = False
        with patch.object(line_api, "call", side_effect=self.api):
            with self.assertRaises(ValidationError):
                line_menu.publish(None, self.images, "verify")
        self.assertNotIn(("/v2/bot/richmenu/old-menu", "DELETE"), self.calls)
        self.assertEqual(LineMenuRelease.objects.get().status, "partial")

    def test_a_line_that_left_the_oa_no_longer_holds_the_release_back(self):
        # 1 Nov 2569: one blocked LINE kept releases 6-8 partial and the old menus alive.
        self.verify, self.friend = False, False
        with patch.object(line_api, "call", side_effect=self.api):
            line_menu.publish(None, self.images, "verify")
        self.assertEqual(LineMenuRelease.objects.get().status, "complete")
        self.assertIn(("/v2/bot/richmenu/old-menu", "DELETE"), self.calls)
        self.assertFalse(ChannelIdentity.objects.get(external_id="test-viewer").is_following)

    def test_everyones_menu_is_the_default_so_no_own_link_is_needed(self):
        # 1 Nov 2569: LINE kept no own link for one pending account (404 after every accepted
        # link), which held releases 6-8 partial though the default already showed its menu.
        with patch.object(line_api, "call", side_effect=self.api):
            line_menu.publish(None, self.images, "verify")
        release = LineMenuRelease.objects.get()
        self.assertEqual(release.status, "complete")
        self.assertNotIn("test-viewer", self.links)
        self.assertIn((f"/v2/bot/user/all/richmenu/{release.menus['user']}", "POST"), self.calls)

    def test_guest_image_matches_guest_actions_and_every_image_fits(self):
        labels = [a["action"]["displayText"] for a in line_menu.rich_menu("user")["areas"]]
        self.assertEqual(
            labels,
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
        self.assertTrue(all(len(image) <= line_menu.MAX_IMAGE for image in self.images.values()))
