"""ADR 0060: no location anywhere, and a person's unit is the roster's unit.

Acceptance → tests:
- Q5, Q10: the unit choices (Broadcast picker, LINE list, admin form) are the roster's units by
  name; a full-name เขต without roster people is not one; before any roster, every unit is.
- Q8: a requester tied to the roster moves to its unit; one not tied stays.
- Q2, Q1, Q7, Q8, Q9: migration 0112 removes the location sentence, the retired option and the
  place of stored location messages, moves tied requesters and narrows groups to roster units.
- Q7: a location message is stored without its place."""

from importlib import import_module

from django.apps import apps
from django.test import TestCase
from django.utils import timezone

from operations import broadcasts, employees, forms, line
from operations.models import (
    AuditEvent,
    BroadcastSegment,
    ChannelIdentity,
    ChatAnswer,
    District,
    Employee,
    Requester,
    Role,
    SystemOption,
    WebhookInbox,
)
from operations.tests.test_m02_cases import make_staff

MIGRATION = import_module("operations.migrations.0112_location_out_and_units_by_roster")


class UnitsByRosterTests(TestCase):
    def setUp(self):
        self.head = make_staff("unit-head", (Role.DEPARTMENT_SUPERVISOR, None))
        self.khet = District.objects.create(code="DT1", name="การไฟฟ้านครหลวงเขตทดสอบ")
        self.fk = District.objects.create(code="UT1", name="ฟขท.")
        self.office = District.objects.create(code="UT2", name="ฝทด.")
        self.ids = iter(range(900001, 900100))

    def employee(self, district, active=True):
        return Employee.objects.create(
            employee_id=str(next(self.ids)), district=district, is_active=active
        )

    def requester(self, district, employee=None, link="", following=True):
        row = Requester.objects.create(
            display_name="ผู้แจ้ง",
            district=district,
            status="active",
            approved_by=self.head,
            employee=employee,
            employee_link=link,
        )
        ChannelIdentity.objects.create(
            channel="line",
            external_id=f"U{row.pk:032d}",
            is_following=following,
            status="active",
            requester=row,
        )
        return row

    def test_the_choices_are_the_rosters_units_by_name(self):
        self.assertIn(self.khet, employees.units())  # no roster yet: every unit
        self.employee(self.fk)
        self.employee(self.office)
        self.employee(self.khet, active=False)  # a closed row makes no unit
        self.requester(self.fk)
        self.requester(self.fk, following=False)
        self.assertEqual([d.name for d in employees.units()], ["ฝทด.", "ฟขท."])
        self.assertEqual(
            broadcasts.units(),
            [
                {"id": self.office.pk, "name": "ฝทด.", "members": 0},
                {"id": self.fk.pk, "name": "ฟขท.", "members": 1},
            ],
        )
        form = forms.RequesterCreateForm()
        self.assertEqual(list(form.fields["district"].queryset), [self.office, self.fk])

    def test_a_requester_tied_to_the_roster_follows_its_unit(self):
        tied = self.requester(self.khet, self.employee(self.fk), link="verified")
        loose = self.requester(self.khet, self.employee(self.office), link="unlinked")
        self.assertEqual(employees.follow_roster(), 1)
        tied.refresh_from_db()
        loose.refresh_from_db()
        self.assertEqual((tied.district, loose.district), (self.fk, self.khet))
        self.assertTrue(AuditEvent.objects.filter(action="requester.unit_followed_roster").exists())
        self.assertEqual(employees.follow_roster(), 0)

    def test_the_migration_takes_location_out_and_units_to_the_roster(self):
        answer = ChatAnswer.objects.create(
            key="anon-old",
            title="แจ้งโดยไม่บอกชื่อ",
            text="ใช้ได้กับปัญหาและข้อเสนอแนะ ต้องส่งตำแหน่งที่พบเรื่อง การติดตามต้องลงทะเบียนก่อน",
            status="approved",
        )
        SystemOption.objects.create(key="registration_location", value="required")
        place = {"latitude": 13.7, "longitude": 100.5, "address": "จุดทดสอบ", "title": "ร้าน"}
        stored = WebhookInbox.objects.create(
            webhook_event_id="evt-0060",
            event_type="message",
            source_type="user",
            source_user_id="U" + "e" * 32,
            payload={"type": "message", "message": {"id": "1", "type": "location", **place}},
            payload_hash="x" * 64,
            occurred_at=timezone.now(),
            received_at=timezone.now(),
        )
        tied = self.requester(self.khet, self.employee(self.fk), link="verified")
        segment = BroadcastSegment.objects.create(
            name="ฟข.", created_by=self.head, criteria={"districts": [self.khet.pk, self.fk.pk]}
        )
        MIGRATION.forward(apps, None)
        answer.refresh_from_db()
        self.assertEqual(
            (answer.text, answer.status),
            ("ใช้ได้กับปัญหาและข้อเสนอแนะ การติดตามต้องลงทะเบียนก่อน", "approved"),
        )
        self.assertFalse(SystemOption.objects.filter(key="registration_location").exists())
        stored.refresh_from_db()
        self.assertEqual(stored.payload["message"], {"id": "1", "type": "location"})
        tied.refresh_from_db()
        self.assertEqual(tied.district, self.fk)
        segment.refresh_from_db()
        self.assertEqual((segment.criteria, segment.revision), ({"districts": [self.fk.pk]}, 2))
        self.assertEqual(broadcasts.segment_members(segment).count(), 1)

    def test_a_location_message_is_stored_without_its_place(self):
        event = {
            "type": "message",
            "webhookEventId": "evt-0060-new",
            "timestamp": int(timezone.now().timestamp() * 1000),
            "source": {"type": "user", "userId": "U" + "d" * 32},
            "message": {"id": "2", "type": "location", "latitude": 1.0, "address": "บ้าน"},
        }
        line.store_events([event])
        stored = WebhookInbox.objects.get(webhook_event_id="evt-0060-new")
        self.assertEqual(stored.payload["message"], {"id": "2", "type": "location"})
