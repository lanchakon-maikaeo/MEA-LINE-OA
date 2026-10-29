"""กอจ.: external lookup, district isolation, explicit follows, grants and refresh.

All rows and identifiers below are synthetic; no private sheet or live LINE is contacted.
"""

import json
from copy import deepcopy
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import Client, override_settings
from django.utils import timezone

from operations import koj_tracking as tracking
from operations import line_flows, notifications, options, staff_line
from operations.models import (
    Case,
    ChannelIdentity,
    Conversation,
    ConversationDraft,
    District,
    KojHistory,
    KojRecipient,
    KojRecord,
    KojSource,
    KojWatch,
    LineFlow,
    LineMenuItem,
    NotificationDelivery,
    Role,
    SystemOption,
)
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m03_line import ALICE, BOB, CAROL, buttons, texts
from operations.tests.test_m14_kcw_menu import KcwTestCase

NUMBER = "120000000001"
DOTS = "I/69.01.01.001.69.01.001"
ALPHANUMERIC = "1AB690X0001"


class KojTestCase(KcwTestCase):
    def setUp(self):
        super().setUp()
        self.source = KojSource.objects.create(
            name="ซจ. ข้อมูลจำลอง",
            provider="csv",
            enabled=True,
            service="repair",
            default_equipment="rmu",
            completed_statuses=["ส่งมอบแล้ว"],
            contact="ติดต่อ ซจ. (ทดสอบ)",
            columns={
                "row_key": "ID",
                "reference": "WO/WBS/เลขรับ/ใบแจ้งซ่อม",
                "status": "สถานะงาน",
                "district": "เขต",
                "equipment": "อุปกรณ์",
                "description": "รายละเอียด",
            },
            equipment_map={"RMU": "rmu", "LBS": "lbs"},
        )
        self.matrix = [
            ["ID", "WO/WBS/เลขรับ/ใบแจ้งซ่อม", "สถานะงาน", "เขต", "อุปกรณ์", "รายละเอียด"],
            ["r1", NUMBER, "รอทดสอบ", "D01", "RMU", "รายการจำลองหนึ่ง"],
            ["r2", NUMBER, "กำลังซ่อม", "D01", "RMU", "รายการจำลองสอง"],
            ["r3", NUMBER, "ข้อมูลเขตอื่น", "D02", "RMU", "ห้ามเห็นข้ามเขต"],
            ["r4", DOTS, "รอทดสอบ", "D01", "RMU", "มีจุดและทับ"],
            ["r5", ALPHANUMERIC, "รอทดสอบ", "D01", "LBS", "มีตัวอักษร"],
        ]
        tracking.apply_snapshot(self.source.pk, self.matrix)
        self.line = ChannelIdentity.objects.get(external_id=ALICE)
        self.koj = tracking.division()
        self.staff = make_staff("koj-clerk", (Role.STAFF, self.koj.code))
        self.staff_line = ChannelIdentity.objects.create(
            channel="line", external_id="U-koj-staff", staff_user=self.staff
        )
        self.admin_koj = make_staff("koj-admin", (Role.ADMIN, None))

    def open_menu(self):
        """Owner, 28 Oct 2569 (Q1): the button opens the กอจ. menu once its flow is published."""
        flow = LineFlow.objects.filter(key="koj-tracking").first()
        if flow is not None and flow.versions.filter(status="published").exists():
            return
        call_command("seed_line_flows", division="กอจ.", replace_draft=True, stdout=StringIO())
        flow = LineFlow.objects.get(key="koj-tracking")
        draft = line_flows.draft_of(flow) or line_flows.start_draft(self.admin_koj, flow)
        line_flows.publish(self.admin_koj, draft, "เปิดเมนู กอจ. ในการทดสอบ")

    def track(self, number=NUMBER, equipment="RMU", user=ALICE):
        self.open_menu()
        tile = LineMenuItem.objects.get(
            place="tile", division=self.koj, tab="report", is_active=True
        )
        self.say(self.postback(user, f"m=item&id={tile.pk}"))
        self.press(user, equipment)
        self.press(user, "ติดตามสถานะการซ่อม")
        return self.type(user, number)

    def follow(self):
        self.track()
        self.press(ALICE, "ติดตามสถานะ")
        return KojWatch.objects.get(identity=self.line)


class CanvasFlowTests(KojTestCase):
    def publish(self, edit=None):
        call_command("seed_line_flows", division="กอจ.", replace_draft=True, stdout=StringIO())
        flow = LineFlow.objects.get(key="koj-tracking")
        if line_flows.draft_of(flow) is None:  # already published by open_menu()
            line_flows.start_draft(self.admin_koj, flow)
            call_command("seed_line_flows", division="กอจ.", replace_draft=True, stdout=StringIO())
        draft = line_flows.draft_of(flow)
        if edit:
            edit(draft.graph)
            draft.save(update_fields=["graph"])
        self.assertEqual(line_flows.check(draft.graph), [])
        return line_flows.publish(self.admin_koj, draft, "ทดสอบเมนูบนผืนผ้าใบ กอจ.")

    def open(self, user=ALICE):
        return self.type(user, "หม้อแปลง LBS RMU")

    def test_the_button_keeps_its_earlier_card_until_the_flow_is_published(self):
        # Owner, 28 Oct 2569 (Q1): as for every button, the published flow switches it over.
        reply = self.open()
        self.assertIn("ติดตามเรื่อง", buttons(reply))
        self.assertNotIn("ตู้ metering", buttons(reply))
        self.press(ALICE, "RMU")
        self.assertNotEqual(Conversation.objects.get(identity=self.line).prompt, "koj_reference")
        self.publish()
        self.assertIn("ตู้ metering", buttons(self.open()))

    def test_every_equipment_service_reaches_its_configured_lookup(self):
        self.publish()
        expected = [
            "หม้อแปลง",
            "LBS",
            "RMU",
            "ตู้ metering",
            "LBS FRTU",
            "อุปกรณ์อื่น",
            "รายการติดตาม กอจ.",
        ]
        self.assertEqual(list(buttons(self.open()))[:-1], expected)  # engine adds ยกเลิกร่าง
        self.press(ALICE, "ยกเลิกร่าง")
        for equipment, label in tracking.EQUIPMENT.items():
            actions = ["withdrawal", "return"] + (["repair"] if equipment in {"lbs", "rmu"} else [])
            for service in actions:
                with self.subTest(equipment=equipment, service=service):
                    self.open()
                    name = "อุปกรณ์จำลองพิมพ์เอง" if equipment == "other" else ""
                    if equipment in {"bushing", "vt_lbs", "lv_cap", "other"}:
                        self.press(ALICE, "อุปกรณ์อื่น")
                    if name:
                        self.press(ALICE, "พิมพ์ชื่ออุปกรณ์")
                        reply = self.type(ALICE, name)
                        draft = ConversationDraft.objects.get(status="open")
                        self.assertEqual(draft.data["flow"]["values"]["name"], name)
                    else:
                        reply = self.press(ALICE, label)
                    self.assertIn("เลือกบริการติดตาม", texts(reply))
                    self.assertEqual(
                        list(buttons(reply)), [*[tracking.SERVICES[s] for s in actions], "ยกเลิกร่าง"]
                    )
                    reply = self.press(ALICE, tracking.SERVICES[service])
                    chat = Conversation.objects.get(identity=self.line)
                    self.assertEqual(chat.prompt, "koj_reference")
                    self.assertEqual(
                        chat.prompt_data, {"equipment": equipment, "service": service, "name": name}
                    )
                    self.assertIn(name or label, texts(reply))
                    self.assertFalse(ConversationDraft.objects.filter(status="open").exists())
        self.assertFalse(Case.objects.exists())
        self.assertFalse(KojWatch.objects.exists())  # lookup alone never subscribes

    def test_admin_button_labels_are_used_by_the_published_chat(self):
        def rename(graph):
            menu = next(n for n in graph["nodes"] if n["id"] == "tracking")
            menu["buttons"][0]["label"] = "หม้อแปลงทดสอบ"
            service = next(n for n in graph["nodes"] if n["id"] == "service-transformer")
            service["buttons"][1].update(label="เช็กใบคืน", full="ติดตามใบส่งคืนหม้อแปลง")

        self.publish(rename)
        reply = self.open()
        self.assertIn("หม้อแปลงทดสอบ", buttons(reply))
        self.assertNotIn("หม้อแปลง", buttons(reply))
        reply = self.press(ALICE, "หม้อแปลงทดสอบ")
        self.assertIn("เช็กใบคืน", buttons(reply))
        self.press(ALICE, "เช็กใบคืน")
        chat = Conversation.objects.get(identity=self.line)
        self.assertEqual(chat.prompt_data["service"], "return")
        self.assertFalse(Case.objects.exists())

    def test_watch_list_action_opens_existing_subscriptions(self):
        self.follow()
        self.publish()
        self.open()
        reply = self.press(ALICE, "รายการติดตาม กอจ.")
        self.assertIn(NUMBER, texts(reply))
        self.assertIn("ดูรายการ 1", buttons(reply))
        self.assertFalse(ConversationDraft.objects.filter(status="open").exists())
        self.assertEqual(KojWatch.objects.count(), 1)

    def test_publishing_the_canvas_does_not_open_it_for_anonymous_users(self):
        self.publish()
        self.say(self.event(BOB, "follow"))
        self.type(BOB, "ไม่ระบุตัวตน")
        drafts_before = ConversationDraft.objects.count()
        reply = self.open(BOB)
        self.assertIn("ยืนยันตัวตนพนักงานก่อนติดตามงาน กอจ.", texts(reply))
        self.assertNotIn("หม้อแปลง", buttons(reply))
        self.assertEqual(ConversationDraft.objects.count(), drafts_before)
        # A typed or old postback cannot reach the lookup either.
        reply = self.say(self.postback(BOB, "m=koj&do=equipment&key=transformer"))
        self.assertIn("ยืนยันตัวตนพนักงานก่อนติดตามงาน กอจ.", texts(reply))

    def test_a_problem_only_account_cannot_read_koj_external_documents(self):
        self.publish()
        self.requester.is_external = True
        self.requester.save(update_fields=["is_external"])
        reply = self.open()
        self.assertIn("ยืนยันตัวตนพนักงาน", texts(reply))
        self.assertNotIn("หม้อแปลง", buttons(reply))
        self.assertFalse(Case.objects.exists())


class LookupTests(KojTestCase):
    def test_pending_replies_do_not_drop_any_rows_between_result_pages(self):
        from operations import koj_chat
        from operations.conversation import Turn, text_message

        changed = deepcopy(self.matrix)
        for i in range(6, 9):
            changed.append([f"r{i}", NUMBER, "รอทดสอบ", "D01", "RMU", f"แถวเพิ่ม {i}"])
        tracking.apply_snapshot(self.source.pk, changed)
        self.track()
        turn = Turn(
            SimpleNamespace(),
            {},
            self.line,
            Conversation.objects.get(identity=self.line),
            timezone.now(),
            requester=self.requester,
            replies=[text_message("ข้อความก่อนหน้า 1"), text_message("ข้อความก่อนหน้า 2")],
        )
        koj_chat.results(turn, self.source.pk, NUMBER, "rmu")
        self.assertEqual(len(turn.replies), 5)
        self.assertIn("รายการ 2/5", texts(turn.replies))
        reply = self.say(self.postback(ALICE, buttons(turn.replies)["หน้าถัดไป"]))
        self.assertIn("รายการ 3/5", texts(reply))
        self.assertIn("รายการ 5/5", texts(reply))

    def test_older_history_is_paginated_and_checks_the_current_district(self):
        row = KojRecord.objects.get(source=self.source, row_key="r1")
        for i in range(14):
            KojHistory.objects.create(
                record=row,
                status=f"ประวัติจำลอง {i}",
                observed_at=timezone.now() + timedelta(seconds=i),
            )
        self.track()
        self.press(ALICE, "ประวัติสถานะ")
        reply = self.press(ALICE, "ประวัติรายการ 1")
        self.assertIn("ประวัติจำลอง 13", texts(reply))
        reply = self.press(ALICE, "เก่ากว่านี้")
        self.assertIn("ประวัติจำลอง 0", texts(reply))
        self.requester.district = District.objects.get(code="D02")
        self.requester.save(update_fields=["district"])
        reply = self.press(ALICE, "ใหม่กว่านี้")
        self.assertIn("ไม่พบรายการที่คุณมีสิทธิ์", texts(reply))

    def test_real_chat_reads_all_matching_rows_without_creating_a_case_or_draft(self):
        self.open_menu()
        before = (Case.objects.count(), ConversationDraft.objects.filter(status="open").count())
        reply = self.track()
        self.assertIn("รายการจำลองหนึ่ง", texts(reply))
        self.assertIn("รายการจำลองสอง", texts(reply))
        self.assertNotIn("ห้ามเห็นข้ามเขต", texts(reply))
        self.assertIn("ติดตามสถานะ", buttons(reply))
        self.assertFalse(KojWatch.objects.exists())
        self.assertEqual(
            (Case.objects.count(), ConversationDraft.objects.filter(status="open").count()), before
        )

    def test_periods_letters_prefixes_and_same_district_are_supported(self):
        self.assertIn(DOTS, texts(self.track(DOTS)))
        self.assertIn(ALPHANUMERIC, texts(self.track(ALPHANUMERIC, "LBS")))
        self.approve(BOB)
        self.assertIn("รายการจำลองสอง", texts(self.track(NUMBER, user=BOB)))
        self.approve(CAROL, district="D02")
        reply = self.track(NUMBER, user=CAROL)
        self.assertIn("ข้อมูลเขตอื่น", texts(reply))
        self.assertNotIn("รายการจำลองหนึ่ง", texts(reply))
        self.assertIn(DOTS, texts(self.track("I/69")))
        changed = deepcopy(self.matrix)
        changed.append(["r6", NUMBER + "/10", "รอทดสอบ", "D01", "RMU", "รายการย่อย"])
        tracking.apply_snapshot(self.source.pk, changed)
        reply = self.track("1200000")
        self.assertIn("พบหลายเอกสาร", texts(reply))
        self.assertIn(NUMBER + "/10", texts(reply))
        # An exact number still returns only the exact document, not its /10 neighbour.
        self.assertNotIn("รายการย่อย", texts(self.track(NUMBER)))

    def test_every_agreed_equipment_has_the_right_services(self):
        self.open_menu()
        tile = LineMenuItem.objects.get(
            place="tile", division=self.koj, tab="report", is_active=True
        )
        reply = self.say(self.postback(ALICE, f"m=item&id={tile.pk}"))
        self.assertEqual(
            list(buttons(reply))[:6], ["หม้อแปลง", "LBS", "RMU", "ตู้ metering", "LBS FRTU", "อุปกรณ์อื่น"]
        )
        for code in tracking.EQUIPMENT:
            if code == "other":
                continue
            self.say(self.postback(ALICE, f"m=koj&do=equipment&key={code}"))
            labels = buttons(self.sent[-1])
            self.assertIn("ติดตามใบเบิก", labels)
            self.assertIn("ติดตามใบส่งคืน", labels)
            self.assertEqual("ติดตามสถานะการซ่อม" in labels, code in {"lbs", "rmu"})
        self.say(self.postback(ALICE, "m=koj&do=other"))
        self.assertEqual(
            list(buttons(self.sent[-1])),
            ["bushing box", "VT ประกอบ LBS", "LV Cap.", "พิมพ์ชื่ออุปกรณ์", "ย้อนกลับ"],
        )
        self.press(ALICE, "พิมพ์ชื่ออุปกรณ์")
        self.assertIn("อุปกรณ์จำลอง", texts(self.type(ALICE, "อุปกรณ์จำลอง")))
        self.assertEqual(tracking.group_for("transformer", "withdrawal"), "mk")
        self.assertEqual(tracking.group_for("lv_cap", "return"), "mk")
        self.assertEqual(tracking.group_for("rmu", "withdrawal"), "other")
        self.assertEqual(tracking.group_for("lbs", "repair"), "sj")

    def test_unmapped_rows_and_disabled_sources_are_hidden_and_missing_is_not_a_new_case(self):
        changed = deepcopy(self.matrix)
        changed.append(["unmapped", "99990001", "รอทดสอบ", "ยังไม่รู้เขต", "RMU", "ห้ามแสดง"])
        tracking.apply_snapshot(self.source.pk, changed)
        reply = self.track("99990001")
        self.assertIn("ไม่พบในข้อมูลล่าสุด", texts(reply))
        self.assertIn(self.source.contact, texts(reply))
        self.assertFalse(KojWatch.objects.exists())
        self.source.enabled = False
        self.source.save()
        self.assertFalse(tracking.find(self.line, "rmu", "repair", NUMBER).exists())


class FollowTests(KojTestCase):
    @override_settings(
        LINE_PUSH_ENABLED=True,
        OUTBOUND_ENABLED=True,
        LINE_CHANNEL_ACCESS_TOKEN="synthetic-token",
        LINE_ALLOWED_RECIPIENTS=[ALICE, "U-koj-staff"],
    )
    def test_staff_tracking_alerts_respect_the_existing_staff_budget(self):
        tracking.set_recipients(self.admin_koj, self.staff, ["sj"])
        self.follow()
        SystemOption.objects.update_or_create(
            key=options.STAFF_LINE_BUDGET, defaults={"value": "0"}
        )
        delivery = NotificationDelivery.objects.get(kind="koj_started", recipient_user=self.staff)
        self.assertTrue(staff_line.is_staff_delivery(delivery))
        with patch("operations.line_api.push") as push:
            self.assertEqual(notifications.send(delivery.pk).outcome, "deferred")
        push.assert_not_called()

    def test_followers_hear_of_changes_at_their_next_message_and_staff_by_push(self):
        # Owner, 28 Oct 2569 (Q2): no push to the follower; staff recipients still get one.
        tracking.set_recipients(self.admin_koj, self.staff, ["sj"])
        watch = self.follow()
        started = NotificationDelivery.objects.filter(kind="koj_started")
        self.assertEqual([d.recipient_user for d in started], [self.staff])
        row = KojRecord.objects.get(source=self.source, row_key="r1")
        tracking.subscribe(self.line, row)
        tracking.apply_snapshot(self.source.pk, self.matrix)
        self.assertEqual(NotificationDelivery.objects.filter(kind__startswith="koj_").count(), 1)
        self.assertNotIn("สถานะงานเปลี่ยน", texts(self.type(ALICE, "สวัสดี")))  # nothing new
        changed = deepcopy(self.matrix)
        changed[1][2] = "กำลังทดสอบ"
        tracking.apply_snapshot(self.source.pk, changed)
        tracking.apply_snapshot(self.source.pk, changed)
        self.assertEqual(NotificationDelivery.objects.filter(kind="koj_changed").count(), 1)
        reply = self.type(ALICE, "สวัสดี")
        self.assertIn("กอจ. · สถานะงานเปลี่ยน", texts(reply))
        self.assertIn("กำลังทดสอบ", texts(reply))
        self.assertNotIn("สถานะงานเปลี่ยน", texts(self.type(ALICE, "ขอบคุณ")))  # shown once
        changed[1][2] = changed[2][2] = "ส่งมอบแล้ว"
        tracking.apply_snapshot(self.source.pk, changed)
        watch.refresh_from_db()
        self.assertEqual(watch.status, "completed")
        self.assertEqual(NotificationDelivery.objects.filter(kind="koj_completed").count(), 1)
        self.assertIn("กอจ. · ติดตามจบแล้ว", texts(self.type(ALICE, "สวัสดี")))
        self.assertEqual(row.history.count(), 3)
        self.say(self.postback(ALICE, "m=koj&do=watches"))
        reply = self.press(ALICE, "รายการที่จบแล้ว")
        self.assertIn(NUMBER, texts(reply))
        reply = self.press(ALICE, "ดูรายการ 1")
        reply = self.press(ALICE, "ประวัติสถานะ", reply)
        self.assertIn("กำลังทดสอบ", texts(reply))

    def test_unsubscribe_and_scope_changes_are_checked_for_staff_and_followers(self):
        tracking.set_recipients(self.admin_koj, self.staff, ["sj"])
        watch = self.follow()
        staff = NotificationDelivery.objects.get(kind="koj_started", recipient_user=self.staff)
        self.assertEqual(tracking.recipient_problem(staff), "")
        tracking.set_recipients(self.admin_koj, self.staff, [])
        self.assertIn("no longer allowed", tracking.recipient_problem(staff))
        tracking.unsubscribe(self.line, watch.pk)
        changed = deepcopy(self.matrix)
        changed[1][2] = "กำลังทดสอบ"
        tracking.apply_snapshot(self.source.pk, changed)
        self.assertNotIn("สถานะงานเปลี่ยน", texts(self.type(ALICE, "สวัสดี")))  # stopped
        with self.assertRaises(PermissionDenied):
            tracking.unsubscribe(self.staff_line, watch.pk)
        self.approve(BOB)
        bob = ChannelIdentity.objects.get(external_id=BOB)
        with self.assertRaises(PermissionDenied):
            tracking.unsubscribe(bob, watch.pk)

    def test_a_change_cannot_leak_after_a_row_moves_to_another_district(self):
        self.follow()
        changed = deepcopy(self.matrix)
        changed[1][3] = "D02"
        changed[1][2] = "ข้อมูลเขตอื่นแล้ว"
        tracking.apply_snapshot(self.source.pk, changed)
        reply = texts(self.type(ALICE, "สวัสดี"))
        self.assertNotIn("ข้อมูลเขตอื่นแล้ว", reply)
        self.assertNotIn("รายการจำลองหนึ่ง", reply)

    def test_two_followers_do_not_duplicate_the_staff_change_alert(self):
        tracking.set_recipients(self.admin_koj, self.staff, ["sj"])
        watch = self.follow()
        self.approve(BOB)
        bob = ChannelIdentity.objects.get(external_id=BOB)
        tracking.subscribe(bob, KojRecord.objects.get(row_key="r1", source=self.source))
        self.assertEqual(NotificationDelivery.objects.filter(kind="koj_started").count(), 1)
        staff = NotificationDelivery.objects.get(kind="koj_started", recipient_user=self.staff)
        tracking.unsubscribe(self.line, watch.pk)
        self.assertEqual(tracking.recipient_problem(staff), "")
        changed = deepcopy(self.matrix)
        changed[1][2] = "กำลังทดสอบ"
        tracking.apply_snapshot(self.source.pk, changed)
        self.assertEqual(
            NotificationDelivery.objects.filter(
                kind="koj_changed", recipient_user=self.staff
            ).count(),
            1,
        )
        self.assertFalse(
            NotificationDelivery.objects.filter(
                kind__startswith="koj_", recipient_user=None
            ).exists()
        )

    @override_settings(LINE_ALLOWED_RECIPIENTS=[ALICE, "U-koj-staff"])
    def test_the_staff_alert_opens_the_matching_source_document(self):
        tracking.set_recipients(self.admin_koj, self.staff, ["sj"])
        self.follow()
        staff = NotificationDelivery.objects.get(kind="koj_started", recipient_user=self.staff)
        data = staff.message["quickReply"]["items"][0]["action"]["data"]
        reply = self.say(self.postback(self.staff_line.external_id, data))
        self.assertIn(NUMBER, texts(reply))
        self.assertIn("รายการจำลองหนึ่ง", texts(reply))

    @override_settings(
        LINE_PUSH_ENABLED=True,
        OUTBOUND_ENABLED=True,
        LINE_CHANNEL_ACCESS_TOKEN="synthetic-token",
        LINE_ALLOWED_RECIPIENTS=[ALICE, "U-koj-staff"],
    )
    def test_an_uncertain_push_is_not_retried_after_the_recipient_is_removed(self):
        tracking.set_recipients(self.admin_koj, self.staff, ["sj"])
        self.follow()
        staff = NotificationDelivery.objects.get(kind="koj_started", recipient_user=self.staff)
        with patch("operations.line_api.push", side_effect=OSError("timeout")) as push:
            self.assertFalse(notifications.send(staff.pk).final)
            tracking.set_recipients(self.admin_koj, self.staff, [])
            self.assertEqual(notifications.send(staff.pk).outcome, "skipped")
        self.assertEqual(push.call_count, 1)
        staff.refresh_from_db()
        self.assertEqual((staff.status, staff.counted), ("unknown", 1))

    def test_a_missing_row_does_not_silently_complete_the_document(self):
        watch = self.follow()
        changed = deepcopy(self.matrix)
        changed[1][2] = "ส่งมอบแล้ว"
        del changed[2]
        tracking.apply_snapshot(self.source.pk, changed)
        watch.refresh_from_db()
        self.assertEqual(watch.status, "active")

    @override_settings(
        LINE_PUSH_ENABLED=True,
        OUTBOUND_ENABLED=True,
        LINE_CHANNEL_ACCESS_TOKEN="synthetic-token",
        LINE_ALLOWED_RECIPIENTS=[ALICE, "U-koj-staff"],
    )
    def test_sender_revalidates_every_member_of_a_batch(self):
        tracking.set_recipients(self.admin_koj, self.staff, ["sj"])
        watch = self.follow()
        staff = NotificationDelivery.objects.get(kind="koj_started", recipient_user=self.staff)
        invalid = NotificationDelivery.objects.create(
            kind="koj_changed",
            level="normal",
            identity=self.staff_line,
            recipient_user=self.staff,
            text="ห้ามส่ง",
            send_after=timezone.now(),
        )
        with patch("operations.line_api.push") as push:
            outcome = notifications.send(staff.pk)
        self.assertEqual(outcome.outcome, "sent")
        self.assertNotIn("ห้ามส่ง", str(push.call_args))
        invalid.refresh_from_db()
        self.assertEqual(invalid.status, "skipped")
        self.assertEqual(watch.status, "active")


class SourceTests(KojTestCase):
    def test_a_unique_document_column_can_also_supply_row_identity_and_district_mapping(self):
        # A source with one row per document need not add columns to its private sheet.
        self.source.columns = {
            "row_key": "WO",
            "reference": "WO",
            "district": "WO",
            "status": "สถานะงาน",
        }
        self.source.district_map = {"WO00001": "D01"}
        rows = tracking.read_rows(self.source, [["WO", "สถานะงาน"], ["WO00001", "รอทดสอบ"]])
        self.assertEqual(rows[0]["row_key"], "WO00001")
        self.assertEqual(rows[0]["district_id"], District.objects.get(code="D01").pk)

    def test_google_reader_uses_read_only_credentials_and_never_truncates_rows_silently(self):
        from operations import koj_sheets

        self.source.provider = "google"
        self.source.spreadsheet_id = "synthetic-spreadsheet-id-0001"
        self.source.sheet_name = "ข้อมูลจำลอง"
        with patch.dict("os.environ", {"KOJ_GOOGLE_CREDENTIALS_FILE": "/synthetic/key.json"}):
            with patch(
                "google.oauth2.service_account.Credentials.from_service_account_file"
            ) as credentials:
                with patch("google.auth.transport.requests.AuthorizedSession") as session_class:
                    session = session_class.return_value.__enter__.return_value
                    response = session.get.return_value.__enter__.return_value
                    response.status_code = 200
                    response.iter_content.return_value = [
                        json.dumps({"values": self.matrix}).encode()
                    ]
                    self.assertEqual(koj_sheets.fetch(self.source), self.matrix)
                    self.assertEqual(
                        credentials.call_args.kwargs["scopes"],
                        ["https://www.googleapis.com/auth/spreadsheets.readonly"],
                    )
                    get = session_class.return_value.__enter__.return_value.get
                    self.assertTrue(get.call_args.kwargs["stream"])
                    self.assertFalse(get.call_args.kwargs["allow_redirects"])
                    self.assertNotIn("20002", get.call_args.args[0])
                    response.status_code = 403
                    with self.assertRaises(ValidationError):
                        koj_sheets.fetch(self.source)

    def test_google_reader_without_the_mounted_key_says_so(self):
        from operations import koj_sheets

        self.source.provider = "google"
        self.source.spreadsheet_id = "synthetic-spreadsheet-id-0001"
        self.source.sheet_name = "ข้อมูลจำลอง"
        missing = {"KOJ_GOOGLE_CREDENTIALS_FILE": "/synthetic/no-such-dir/sheets-reader.json"}
        with patch.dict("os.environ", missing):
            with self.assertRaisesMessage(ValidationError, "ยังไม่มีไฟล์กุญแจ Google"):
                koj_sheets.fetch(self.source)
            with self.assertRaisesMessage(ValidationError, "ยังไม่มีไฟล์กุญแจ Google"):
                koj_sheets.read_columns("synthetic-spreadsheet-id-0001", "ข้อมูลจำลอง", ["J"])

    def test_invalid_snapshot_is_atomic_and_keeps_previous_data(self):
        before = list(KojRecord.objects.values_list("row_key", "status"))
        changed = deepcopy(self.matrix)
        changed[1][2] = "ต้องไม่ถูกบันทึก"
        changed[2][0] = changed[1][0]
        with self.assertRaises(ValidationError):
            tracking.apply_snapshot(self.source.pk, changed)
        self.assertEqual(list(KojRecord.objects.values_list("row_key", "status")), before)
        self.assertEqual(KojHistory.objects.count(), len(before))

    def test_hourly_and_manual_refresh_preserve_last_good_snapshot_on_failure(self):
        self.source.provider = "google"
        self.source.spreadsheet_id = "synthetic-spreadsheet-id-0001"
        self.source.sheet_name = "ข้อมูลจำลอง"
        self.source.next_sync_at = timezone.now() - timedelta(seconds=1)
        self.source.save()
        with patch("operations.koj_sheets.fetch", return_value=self.matrix) as fetch:
            self.assertTrue(tracking.sync_due())
            self.assertFalse(tracking.sync_due())
            tracking.request_refresh(self.admin_koj, self.source)
            self.assertTrue(tracking.sync_due())
            self.assertEqual(fetch.call_count, 2)
        before = KojRecord.objects.count()
        with self.assertRaises(PermissionDenied):
            tracking.request_refresh(self.staff, self.source)
        tracking.request_refresh(self.admin_koj, self.source)
        with patch(
            "operations.koj_sheets.fetch",
            side_effect=RuntimeError("private content must not escape"),
        ):
            self.assertTrue(tracking.sync_due())
        self.source.refresh_from_db()
        self.assertTrue(self.source.last_error)
        self.assertNotIn("private content", self.source.last_error)
        self.assertEqual(KojRecord.objects.count(), before)

    def test_web_grants_sources_history_and_refresh_are_scoped(self):
        client = Client()
        client.force_login(self.admin_koj)
        self.assertContains(client.get("/koj/"), self.source.name)
        self.assertContains(client.get(f"/koj/sources/{self.source.pk}/"), NUMBER)
        # ADR 0031 Q12, Q25: the recipient sets sit on the กอจ. cards of ผังหน่วยงาน.
        page = client.get("/console/admin/org/").content.decode()
        self.assertIn("ผู้รับแจ้งงานติดตาม กอจ.", page)
        response = client.post(
            "/console/admin/org/",
            {"action": "koj", "group": "sj", "person": [self.staff.pk], "unit": "d1"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            set(KojRecipient.objects.filter(user=self.staff).values_list("group", flat=True)),
            {"sj"},
        )
        foreign = make_staff("foreign-clerk", (Role.STAFF, "กคว."))
        client.force_login(foreign)
        self.assertEqual(client.get("/koj/").status_code, 403)
        self.assertEqual(
            client.post("/console/admin/org/", {"action": "koj", "group": "sj"}).status_code, 403
        )
        # 28 ต.ค. 2569: กอจ. staff wait for the hourly round; the page is admin only.
        client.force_login(self.staff)
        self.assertEqual(client.get("/koj/").status_code, 403)
        self.assertEqual(client.get(f"/koj/sources/{self.source.pk}/").status_code, 403)
        self.assertEqual(client.post("/koj/", {"action": "save"}).status_code, 403)

    def test_admin_configures_columns_without_disclosing_source_data(self):
        client = Client()
        client.force_login(self.admin_koj)
        payload = {
            "action": "save",
            "name": "มค. จำลอง",
            "enabled": "on",
            "provider": "csv",
            "service": "withdrawal",
            "default_equipment": "transformer",
            "contact": "ติดต่อ มค. (ทดสอบ)",
            "column_row_key": "ID",
            "column_reference": "ใบเบิก",
            "column_status": "สถานะ",
            "column_district": "เขต",
            "completed": "จ่ายแล้ว\nส่งมอบแล้ว",
        }
        self.assertEqual(client.post("/koj/", payload).status_code, 302)
        source = KojSource.objects.get(name="มค. จำลอง")
        self.assertEqual(source.columns["reference"], "ใบเบิก")
        self.assertEqual(source.completed_statuses, ["จ่ายแล้ว", "ส่งมอบแล้ว"])

    def test_the_seed_and_published_flow_leave_kcw_unchanged_and_finish_the_menu_draft(self):
        call_command("seed_line_flows", stdout=StringIO())
        kcw = LineFlow.objects.get(key="meter-ct-vt")
        draft = line_flows.draft_of(kcw)
        graph, revision = draft.graph, draft.revision
        call_command("seed_line_flows", division="กอจ.", replace_draft=True, stdout=StringIO())
        draft.refresh_from_db()
        self.assertEqual((draft.graph, draft.revision), (graph, revision))
        flow = LineFlow.objects.get(key="koj-tracking")
        self.assertFalse(line_flows.blocked(line_flows.check(line_flows.draft_of(flow).graph)))
        koj_draft = line_flows.draft_of(flow)
        original = deepcopy(koj_draft.graph)
        invalid = deepcopy(original)
        end = next(n for n in invalid["nodes"] if n["id"] == "transformer-withdrawal")
        end["config"] = {"equipment": "transformer", "service": "repair"}
        self.assertTrue(line_flows.blocked(line_flows.check(invalid)))
        koj_draft.graph["nodes"][1]["kind"] = "create_case"
        koj_draft.save(update_fields=["graph"])
        with self.assertRaisesMessage(ValidationError, "กอจ. ติดตามงานต้นทาง"):
            line_flows.publish(self.admin_koj, koj_draft, "ห้ามสร้างเรื่องซ้ำ")
        koj_draft.graph = original
        koj_draft.save(update_fields=["graph"])
        line_flows.publish(self.admin_koj, line_flows.draft_of(flow), "ทดสอบ กอจ.")
        self.assertIn("รายการจำลองหนึ่ง", texts(self.track()))
        self.assertFalse(ConversationDraft.objects.filter(status="open").exists())
        self.assertFalse(Case.objects.exists())

    def test_headers_can_have_unselected_blank_columns_and_a_refresh_claim_cannot_overlap(self):
        matrix = deepcopy(self.matrix)
        matrix[0].extend(["", ""])
        tracking.apply_snapshot(self.source.pk, matrix)
        self.source.provider = "google"
        self.source.spreadsheet_id = "synthetic-spreadsheet-id-0001"
        self.source.sheet_name = "ข้อมูลจำลอง"
        self.source.next_sync_at = timezone.now()
        self.source.save()

        def fetch(source):
            self.assertFalse(tracking.sync_due())
            tracking.request_refresh(self.admin_koj, source)
            return self.matrix

        with patch("operations.koj_sheets.fetch", side_effect=fetch):
            self.assertTrue(tracking.sync_due())
        self.source.refresh_from_db()
        self.assertTrue(self.source.refresh_requested)
