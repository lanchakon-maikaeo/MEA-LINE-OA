"""ADR 0059 (grill 23 พ.ย. 2569): the OTP switch in ตัวเลือกระบบ, off by default, and a sent
broadcast that remembers who it has not reached.

Acceptance → tests: with OTP off an employee ID that matches the roster registers at once, even a
row without an e-mail, ties the staff account holding it and asks nothing about OTP or e-mail; an
ID another LINE holds goes to an admin; switching off midway asks nothing more (IdOnlyTests);
"เชื่อมบัญชีเจ้าหน้าที่" links by the ID alone, never takes an account from another LINE and keeps
the daily limit (StaffLinkTests); a sent campaign counts who registered or befriended later and
who sat in a failed batch, its editor sends to them once under the same quota rules, and the
page shows the count (MissedTests); the recipient picker shows each unit once, named as the roster
names it, with its registered friends (UnitTests).
"""

from unittest.mock import patch

from django.core import mail
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from operations import broadcast_delivery, broadcasts, identity, options
from operations.models import (
    AuditEvent,
    BroadcastBatch,
    BroadcastCampaign,
    ChannelIdentity,
    District,
    Division,
    Employee,
    RegistrationRequest,
    Requester,
    Role,
    StaffProfile,
)

# Modules, not their test classes: a class imported here would run its tests twice.
from operations.tests import test_broadcasts, test_m13_roster
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import ALICE, BOB, buttons, texts
from operations.tests.test_m13_roster import RosterTestCase
from operations.tests.test_m13_staff_chat import StaffChatTestCase


class IdOnlyTests(RosterTestCase):
    start = test_m13_roster.LineEmailTests.start

    def say(self, *args, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):  # a code mail leaves after the commit
            return super().say(*args, **kwargs)

    def setUp(self):
        super().setUp()
        self.load()  # 510001 one@example.test in เขตวัดเลียบ
        Employee.objects.filter(employee_id="510001").update(
            division=Division.objects.get(code="กคว.")
        )
        options.change(None, options.REGISTRATION_CHECK, "employee_auto", "ทดสอบ")

    def test_a_matching_id_registers_at_once_and_ties_the_staff_account(self):
        user = make_staff("one", (Role.SUPERVISOR, "กคว."))
        StaffProfile.objects.filter(user=user).update(employee_id="510001")
        reply = self.start()
        self.assertIn("ยืนยันตัวตนแล้ว", texts(reply))
        self.assertFalse({"ส่งรหัสทางอีเมล", "พิมพ์อีเมลเอง"} & set(buttons(reply)))
        self.assertEqual(mail.outbox, [])
        registration = RegistrationRequest.objects.get()
        self.assertEqual(
            (registration.status, registration.verified_by), ("approved", "employee_id")
        )
        line = ChannelIdentity.objects.get(external_id=ALICE)
        self.assertEqual((line.requester.employee_link, line.staff_user), ("verified", user))
        event = AuditEvent.objects.get(action="identity.employee_check_passed")
        self.assertEqual(event.data["method"], "employee_id")

    def test_a_roster_row_without_an_email_registers_only_while_otp_is_off(self):
        Employee.objects.create(
            employee_id="510009",
            name="ไม่มีอีเมล",
            district=District.objects.get(code="D01"),
            division=Division.objects.get(code="กคว."),
        )
        self.assertIn("ยืนยันตัวตนแล้ว", texts(self.start(employee_id="510009")))
        options.change(None, options.EMPLOYEE_OTP, "on", "ทดสอบ")
        self.assertNotIn("ยืนยันตัวตนแล้ว", texts(self.start(BOB, "510009")))  # nothing to prove

    def test_an_id_another_line_holds_goes_to_an_admin(self):
        self.start()
        reply = self.start(BOB)
        self.assertIn("ลงทะเบียนกับ LINE อื่นอยู่แล้ว", texts(reply))
        self.assertIn("ส่งคำขอให้แอดมินตรวจแล้ว", texts(reply))
        waiting = RegistrationRequest.objects.get(identity__external_id=BOB)
        self.assertEqual(
            (waiting.status, waiting.verified_by, waiting.employee.employee_id),
            ("pending", "", "510001"),
        )
        self.assertEqual(identity.approve_verified_registrations(), 0)  # a person decides

    def test_switching_otp_off_midway_asks_nothing_more(self):
        options.change(None, options.EMPLOYEE_OTP, "on", "ทดสอบ")
        self.assertIn("ส่งรหัสทางอีเมล", buttons(self.start()))
        options.change(None, options.EMPLOYEE_OTP, "off", "ทดสอบ")
        self.assertIn("ยืนยันตัวตนแล้ว", texts(self.press(ALICE, "ส่งรหัสทางอีเมล")))
        self.assertEqual(mail.outbox, [])
        self.assertEqual(RegistrationRequest.objects.get().verified_by, "employee_id")


class StaffLinkTests(StaffChatTestCase):
    def test_the_employee_id_alone_links_line(self):
        self.assertNotIn("อีเมล", texts(self.type(ALICE, "เชื่อมบัญชีเจ้าหน้าที่")))
        reply = self.type(ALICE, "510001")
        self.assertIn("เชื่อม LINE กับบัญชีเจ้าหน้าที่แล้ว", texts(reply))
        self.assertEqual(ChannelIdentity.objects.get(external_id=ALICE).staff_user, self.head)
        self.assertEqual(mail.outbox, [])

    def test_an_id_never_takes_an_account_from_another_line(self):
        self.link(self.head, ALICE)  # the web code
        self.type(BOB, "เชื่อมบัญชีเจ้าหน้าที่")
        self.assertIn("เชื่อมกับ LINE อื่นอยู่แล้ว", texts(self.type(BOB, "510001")))
        self.assertEqual(ChannelIdentity.objects.get(external_id=ALICE).staff_user, self.head)
        self.assertIsNone(ChannelIdentity.objects.get(external_id=BOB).staff_user)

    def test_ids_that_link_nothing_run_out_for_the_day(self):
        for _ in range(3):
            self.type(BOB, "เชื่อมบัญชีเจ้าหน้าที่")
            self.assertIn("ไม่พบบัญชีเจ้าหน้าที่", texts(self.type(BOB, "510002")))
        self.type(BOB, "เชื่อมบัญชีเจ้าหน้าที่")
        self.assertIn("ลองครบจำนวนครั้ง", texts(self.type(BOB, "510001")))
        self.assertIsNone(ChannelIdentity.objects.get(external_id=BOB).staff_user)


@override_settings(
    APP_ENV="production",
    OUTBOUND_ENABLED=True,
    LINE_PUSH_ENABLED=True,
    LINE_CHANNEL_ACCESS_TOKEN="test-only",
    LINE_PUSH_MONTHLY_BUDGET=2000,
)
class MissedTests(TestCase):
    setUp = test_broadcasts.BroadcastDraftTests.setUp  # head with LINE "U a…", friend "U b…"
    draft = test_broadcasts.BroadcastDraftTests.draft
    quota = test_broadcasts.BroadcastDeliveryTests.quota
    prepare = test_broadcasts.BroadcastDeliveryTests.prepare
    send_events = test_broadcasts.BroadcastDeliveryTests.send_events

    def person(self, district, letter):
        requester = Requester.objects.create(
            display_name="ผู้แจ้ง", district=district, approved_by=self.head
        )
        return ChannelIdentity.objects.create(
            channel="line",
            external_id="U" + letter * 32,
            is_following=True,
            requester=requester,
            status="active",
        )

    def sent(self, campaign=None):
        campaign, _ = self.prepare(campaign)
        with patch("operations.line_api.campaign_send", return_value="request-id"):
            for event in self.send_events():
                broadcast_delivery.send_batch(event)
        campaign.refresh_from_db()
        return campaign

    def by_district(self, code):
        district = District.objects.create(code=code, name=f"เขตทดสอบ {code}")
        segment = broadcasts.save_segment(self.head, f"กลุ่ม {code}", {"districts": [district.pk]})
        return district, self.draft(audience="segment", segment_id=segment.pk)

    def test_who_registered_after_the_send_is_missed_and_sent_once(self):
        district, draft = self.by_district("DM1")
        self.person(district, "c")
        campaign = self.sent(draft)
        self.assertEqual(
            (campaign.status, broadcasts.missed_counts(campaign)), ("accepted", (0, 0))
        )
        late = self.person(district, "d")
        self.assertEqual(broadcasts.missed_counts(campaign), (1, 0))
        broadcasts.send_missed(self.head, campaign.pk)
        batch = campaign.batches.get(sequence=1)
        self.assertEqual(batch.payload["to"], [late.external_id])
        self.assertEqual(broadcasts.missed_counts(campaign), (0, 0))  # waiting counts as sent
        with self.assertRaises(ValidationError):
            broadcasts.send_missed(self.head, campaign.pk)
        with patch("operations.line_api.campaign_send", return_value="late-id") as send:
            for event in self.send_events():
                broadcast_delivery.send_batch(event)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[1]["to"], [late.external_id])
        campaign.refresh_from_db()
        self.assertEqual((campaign.status, campaign.recipient_count), ("accepted", 3))
        event = AuditEvent.objects.get(action="broadcast.missed_sent")
        self.assertEqual(event.data["recipients"], 1)

    def test_a_failed_batch_is_missed_under_the_same_quota_and_no_second_notice(self):
        campaign = self.sent()  # to the head and the friend
        campaign.batches.update(status=BroadcastBatch.Status.FAILED)
        BroadcastCampaign.objects.filter(pk=campaign.pk).update(status="partial")
        campaign.refresh_from_db()
        self.assertEqual(broadcasts.missed_counts(campaign), (1, 1))  # the friend, not registered
        self.quota_limit = 0
        self.quota(timezone.now())
        with self.assertRaisesMessage(ValidationError, "โควตา"):
            broadcasts.send_missed(self.head, campaign.pk)
        self.assertEqual(campaign.batches.count(), 1)
        self.quota_limit = 2000
        self.quota(timezone.now())
        broadcasts.send_missed(self.head, campaign.pk)
        with (
            patch("operations.line_api.campaign_send", return_value="again-id"),
            patch("operations.broadcast_delivery._notice") as notice,
        ):
            for event in self.send_events():
                broadcast_delivery.send_batch(event)
        notice.assert_not_called()  # still ส่งไม่ครบ: told once already
        campaign.refresh_from_db()
        self.assertEqual((campaign.status, broadcasts.missed_counts(campaign)), ("partial", (0, 0)))

    def test_all_friends_missed_are_the_friends_first_seen_after_the_send(self):
        campaign, _ = self.prepare(self.draft())
        event = self.send_events()[0]
        with patch("operations.line_api.campaign_send", return_value="all-id"):
            with self.assertRaises(broadcast_delivery.RetryBroadcast):
                broadcast_delivery.send_batch(event)
            done = {"phase": "succeeded", "successCount": 2, "failureCount": 0, "targetCount": 2}
            with patch("operations.line_api.call", return_value=done):
                broadcast_delivery.send_batch(event)
        campaign.refresh_from_db()
        self.assertEqual(
            (campaign.status, broadcasts.missed_counts(campaign)), ("accepted", (0, 0))
        )
        registered = self.person(District.objects.create(code="DM2", name="เขตทดสอบ DM2"), "e")
        stranger = ChannelIdentity.objects.create(
            channel="line", external_id="U" + "f" * 32, is_following=True
        )
        self.assertEqual(broadcasts.missed_counts(campaign), (2, 1))
        broadcasts.send_missed(self.head, campaign.pk)
        batch = campaign.batches.get(sequence=1)
        self.assertEqual(
            (batch.endpoint, set(batch.payload["to"])),
            ("multicast", {registered.external_id, stranger.external_id}),
        )

    def test_the_page_shows_the_count_and_only_an_editor_sends(self):
        district, draft = self.by_district("DM3")
        campaign = self.sent(draft)
        self.person(district, "g")
        url = f"/broadcasts/api/campaigns/{campaign.pk}/"
        body = {"revision": campaign.revision}
        self.client.force_login(self.other)  # a supervisor of the กอง who did not write it
        self.assertEqual(
            self.client.get(url).json()["campaign"]["missed"],
            {"count": 1, "unregistered": 0, "may_send": False},
        )
        response = self.client.post(url + "missed/", body, content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.client.force_login(self.head)
        response = self.client.post(url + "missed/", body, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(url).json()["campaign"]["missed"]["count"], 0)
