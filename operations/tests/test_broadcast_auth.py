import json
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.test import Client, RequestFactory, TestCase, override_settings
from django.utils import timezone

from operations import broadcast_auth
from operations.models import ChannelIdentity, Division, Role, RoleGrant, StaffProfile
from operations.tests.test_m02_cases import make_staff


@override_settings(LINE_MINI_CHANNEL_ID="12345")
class BroadcastAuthTests(TestCase):
    def setUp(self):
        self.division = Division.objects.create(code="BA1", name="กองทดสอบ")
        self.user = make_staff("mini-head", (Role.SUPERVISOR, self.division.code))
        self.link = ChannelIdentity.objects.create(
            channel="line", external_id="U" + "c" * 32, staff_user=self.user
        )
        self.claims = {
            "iss": "https://access.line.me",
            "aud": "12345",
            "exp": int(timezone.now().timestamp()) + 7200,
            "sub": self.link.external_id,
        }

    def login(self):
        with patch("operations.broadcast_auth.verify_token", return_value=self.claims):
            return self.client.post(
                "/broadcasts/auth/line/", {"id_token": "valid"}, content_type="application/json"
            )

    def request(self):
        request = RequestFactory().get("/broadcasts/")
        request.session = self.client.session
        request.user = AnonymousUser()
        return request

    def test_verified_login_is_scoped_and_bounded(self):
        self.assertEqual(self.login().status_code, 200)
        session = self.client.session
        self.assertNotIn("_auth_user_id", session)
        saved = session[broadcast_auth.SESSION_KEY]
        self.assertLessEqual(saved["expires"] - saved["issued"], 3600)
        self.assertEqual(broadcast_auth.actor(self.request()), self.user)
        self.assertNotIn("valid", str(saved))

    def test_revoked_role_is_checked_on_each_request(self):
        self.login()
        RoleGrant.objects.filter(user=self.user).update(revoked_at=timezone.now())
        with self.assertRaises(PermissionDenied):
            broadcast_auth.actor(self.request())

    def test_relinked_identity_does_not_transfer_session(self):
        self.login()
        other = make_staff("mini-other", (Role.SUPERVISOR, self.division.code))
        self.link.staff_user = other
        self.link.save()
        with self.assertRaises(PermissionDenied):
            broadcast_auth.actor(self.request())

    def test_expiry_and_admin_revocation(self):
        self.login()
        StaffProfile.objects.update_or_create(
            user=self.user, defaults={"sessions_valid_after": timezone.now()}
        )
        with self.assertRaises(PermissionDenied):
            broadcast_auth.actor(self.request())
        request = self.request()
        request.session[broadcast_auth.SESSION_KEY]["expires"] = 0
        with self.assertRaises(PermissionDenied):
            broadcast_auth.actor(request)

    def test_csrf_required_and_client_roles_ignored(self):
        response = Client(enforce_csrf_checks=True).post(
            "/broadcasts/auth/line/", {"id_token": "valid"}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 403)
        RoleGrant.objects.filter(user=self.user).delete()
        self.assertEqual(self.login().status_code, 403)
        self.assertNotIn(broadcast_auth.SESSION_KEY, self.client.session)

    def test_verify_posts_token_to_fixed_endpoint_and_validates_claims(self):
        response = MagicMock()
        with patch("operations.broadcast_auth.urllib.request.urlopen") as network:
            network.return_value.__enter__.return_value = response
            response.read.return_value = json.dumps(self.claims).encode()
            self.assertEqual(broadcast_auth.verify_token("secret"), self.claims)
            request = network.call_args.args[0]
            self.assertEqual(request.full_url, "https://api.line.me/oauth2/v2.1/verify")
            self.assertIn(b"client_id=12345", request.data)
            self.assertNotIn("Authorization", request.headers)
            for invalid in ({"aud": "other"}, {"exp": 0}, {"iss": "other"}, {"sub": None}):
                response.read.return_value = json.dumps({**self.claims, **invalid}).encode()
                with self.assertRaises(PermissionDenied):
                    broadcast_auth.verify_token("secret")

    def test_logout_removes_only_mini_session(self):
        self.login()
        self.assertEqual(self.client.post("/broadcasts/auth/logout/").status_code, 200)
        self.assertNotIn(broadcast_auth.SESSION_KEY, self.client.session)

    def post_api(self, path, payload):
        return self.client.post("/broadcasts/api/" + path, payload, content_type="application/json")

    def test_shared_api_create_preview_confirm_and_pause(self):
        self.link.is_following = True
        self.link.save()
        self.login()
        self.assertEqual(self.client.get("/broadcasts/api/bootstrap/").status_code, 200)
        response = self.post_api(
            "campaigns/", {"title": "ประกาศ", "content": {"kind": "text", "text": "ข้อความทดสอบ"}}
        )
        self.assertEqual(response.status_code, 200, response.content)
        row = response.json()["campaign"]
        path = f"campaigns/{row['id']}/"
        preview = self.post_api(path + "preview/", {"revision": row["revision"]})
        self.assertEqual(preview.status_code, 200, preview.content)
        self.assertIsNone(preview.json()["recipient_count"])
        confirmation = self.post_api(path + "confirm/", {"revision": row["revision"]})
        self.assertEqual(confirmation.status_code, 200, confirmation.content)
        current = confirmation.json()["campaign"]
        self.assertEqual(current["status"], "queued")
        paused = self.post_api(path + "edit/", {"revision": current["revision"]})
        self.assertEqual(paused.json()["campaign"]["status"], "paused")
        self.assertEqual(self.client.get("/broadcasts/api/campaigns/").json()["total"], 1)
        self.assertEqual(self.client.get("/broadcasts/api/" + path).status_code, 200)

    def test_group_picker_offers_real_units_and_saving_reports_members(self):
        from operations.models import District

        District.objects.create(code="DA1", name="การไฟฟ้านครหลวงเขตทดสอบ")
        District.objects.create(code="D00", name="ไม่ระบุหน่วยงาน (เขต/ฝ่าย)", is_active=False)
        Division.objects.create(code="SIM-X", name="กองจำลอง", is_synthetic=True)
        self.login()
        data = self.client.get("/broadcasts/api/bootstrap/").json()
        self.assertEqual([d["name"] for d in data["districts"]], ["การไฟฟ้านครหลวงเขตทดสอบ"])
        self.assertEqual([d["id"] for d in data["divisions"]], [self.division.pk])
        self.assertNotIn("developer_admin", [r["id"] for r in data["roles"]])
        saved = self.post_api(
            "segments/", {"name": "กองทดสอบ", "criteria": {"divisions": [self.division.pk]}}
        )
        self.assertEqual(saved.status_code, 200, saved.content)
        self.assertEqual(saved.json()["members"], 1)

    def test_api_rejects_invalid_input_and_anonymous_access(self):
        self.assertEqual(self.client.get("/broadcasts/api/bootstrap/").status_code, 403)
        self.login()
        for payload in ([], {"id": "bad"}, {"revision": True}):
            self.assertEqual(self.post_api("campaigns/", payload).status_code, 400)
        self.assertEqual(self.client.get("/broadcasts/api/campaigns/999999/").status_code, 404)

    def test_another_supervisor_can_read_but_not_edit_draft(self):
        from operations import broadcasts

        other = make_staff("mini-owner", (Role.SUPERVISOR, self.division.code))
        row = broadcasts.save_draft(
            other, title="ประกาศ", content={"kind": "text", "text": "แจ้งข่าว"}
        )
        self.login()
        path = f"campaigns/{row.pk}/"
        self.assertFalse(self.client.get("/broadcasts/api/" + path).json()["campaign"]["may_edit"])
        self.assertEqual(self.post_api(path + "edit/", {"revision": row.revision}).status_code, 403)

    def test_page_sets_csrf_cookie_and_keeps_sdk_policy_local_to_broadcast(self):
        response = self.client.get("/broadcasts/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("csrftoken", response.cookies)
        self.assertContains(response, "สร้างประกาศ")
        self.assertContains(response, "broadcast-config")
        self.assertIn("https://static.line-scdn.net", response["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", response["Content-Security-Policy"])
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_api_test_and_copy_remain_separate_from_real_draft(self):
        import uuid

        from operations.models import BroadcastCampaign

        self.link.is_following = True
        self.link.save()
        self.login()
        saved = self.post_api(
            "campaigns/", {"title": "ประกาศ", "content": {"kind": "text", "text": "ข้อความทดสอบ"}}
        ).json()["campaign"]
        path = f"campaigns/{saved['id']}/"
        payload = {"revision": saved["revision"], "request_key": str(uuid.uuid4())}
        test = self.post_api(path + "test/", payload)
        self.assertEqual(test.status_code, 200, test.content)
        self.assertTrue(test.json()["campaign"]["is_test"])
        self.assertFalse(test.json()["campaign"]["may_edit"])
        again = self.post_api(path + "test/", payload)
        self.assertEqual(test.json()["campaign"]["id"], again.json()["campaign"]["id"])
        copied = self.post_api(path + "copy/", {})
        self.assertEqual(copied.status_code, 200, copied.content)
        self.assertEqual(copied.json()["campaign"]["status"], "draft")
        self.assertEqual(BroadcastCampaign.objects.get(pk=saved["id"]).status, "draft")
