"""ปัญหาและข้อเสนอแนะ: the admin's list of every problem and suggestion (14 Oct 2569).

The user asked for an admin page with every problem and suggestion, who reported it, the channel
and when (Q1-Q3), and a CSV without personal data (Q4, Q6, Q7). An anonymous reporter appears
only as a random code, on this page only (Q1, Q5).

Acceptance → tests: the list shows every problem and suggestion (no withdrawals) with reporter,
channel and time, and an anonymous reporter only as a stable random code that never reaches the
case page (ListTests); filters and pages narrow it (FilterTests); only admins open the list or
download it, and the CSV is audited (ExportTests); every anonymous reporter gets a unique random
code, old ones through the migration (AliasTests).

ADR 0039 (31 Oct 2569): the CSV now carries every matching row and what the page shows, with the
full text; an anonymous reporter is still only their code, and no LINE ID or location leaves.
"""

import csv
import io
import uuid
from datetime import timedelta
from importlib import import_module
from unittest.mock import patch

from django.apps import apps
from django.utils import timezone

from operations import identity, options, problems_web, services
from operations.contracts import Actor
from operations.models import AuditEvent, Case, ChannelIdentity, District, Requester
from operations.tests.test_m02_cases import problem, withdrawal
from operations.tests.test_m03_line import ALICE, BOB, CAROL, LineTestCase

LIST = "/console/admin/problems/"
CSV = "/console/admin/problems.csv"
TYPED = "หม้อแปลงมีเสียงดัง โทร 081-234-5678"


class ProblemsTestCase(LineTestCase):
    def setUp(self):
        super().setUp()
        options.change(None, options.ANONYMOUS, "on", "ทดสอบ")
        self.requester = self.approve(ALICE)
        self.named = self.submit(Actor.for_requester(self.requester), problem(description=TYPED))
        line = ChannelIdentity.objects.create(channel="line", external_id=BOB)
        self.anonymous = identity.anonymous_requester(line)
        located = {"topic": "suggestion", "latitude": "13.7", "longitude": "100.5"}
        self.ideas = [
            self.submit(
                Actor.for_requester(self.anonymous),
                problem(description=f"อยากให้มีที่จอดรถ {n}", **located),
            )
            for n in (1, 2)
        ]
        self.phoned = self.submit(
            Actor.staff(self.intake),
            {
                **problem(description="มิเตอร์จอดับ", topic="meter"),
                "requester_id": self.requester.pk,
                "channel": "phone",
                "intake_note": "เขตโทรมา",
                "reported_at": (timezone.now() - timedelta(hours=2)).isoformat(),
            },
        )
        self.withdrawal = self.submit(Actor.for_requester(self.requester), withdrawal())
        self.client.force_login(self.admin)

    def submit(self, actor, payload):
        result = services.submit_case(
            actor, idempotency_key=f"k-{uuid.uuid4().hex}", payload=payload
        )
        return Case.objects.get(pk=result.case_id)


class ListTests(ProblemsTestCase):
    def test_every_problem_with_reporter_channel_and_time(self):
        page = self.client.get(LIST)
        for case in (self.named, *self.ideas, self.phoned):
            self.assertContains(page, case.number)
        self.assertNotContains(page, self.withdrawal.number)
        self.assertContains(page, "4 เรื่อง")
        district = District.objects.get(code="D01").name
        self.assertContains(page, f"{self.requester.display_name} · {district}")
        self.assertContains(page, f"/console/admin/requesters/{self.requester.pk}/")
        self.assertContains(page, f"ไม่ระบุตัวตน #{self.anonymous.alias}", count=2)
        self.assertNotContains(page, BOB)
        self.assertContains(page, "บันทึกแทนโดย Intake")
        self.assertContains(page, "บันทึก ")  # filed two hours after the real report
        self.assertContains(page, "ไม่ระบุหัวข้อ")
        self.assertContains(page, "ข้อเสนอแนะ")

    def test_the_code_stays_on_this_page(self):
        case_page = self.client.get(f"/cases/{self.ideas[0].number}/")
        self.assertContains(case_page, "ไม่ระบุตัวตน")
        self.assertNotContains(case_page, self.anonymous.alias)


class FilterTests(ProblemsTestCase):
    def numbers(self, query=""):
        return {row["case"].number for row in self.client.get(LIST + query).context["rows"]}

    def test_filters_narrow_the_list(self):
        ideas = {case.number for case in self.ideas}
        self.assertEqual(self.numbers("?topic=suggestion"), ideas)
        self.assertEqual(self.numbers("?topic=none"), {self.named.number})
        self.assertEqual(self.numbers("?channel=phone"), {self.phoned.number})
        self.assertEqual(len(self.numbers("?status=submitted")), 4)
        division = self.named.owner_division.code
        self.assertIn(self.named.number, self.numbers(f"?division={division}"))
        today = timezone.localdate()
        self.assertEqual(self.numbers(f"?from={today + timedelta(days=1)}"), set())
        self.assertEqual(self.numbers(f"?to={today - timedelta(days=1)}"), set())
        self.assertEqual(len(self.numbers(f"?from={today}&to={today}")), 4)
        self.assertEqual(len(self.numbers("?topic=bogus&division=NOPE&from=x")), 4)

    def test_pages_keep_the_filters(self):
        # Three problems came through LINE: two a page makes two pages.
        with patch.object(problems_web, "PER_PAGE", 2):
            first = self.client.get(LIST + "?channel=line")
            second = self.client.get(LIST + "?channel=line&page=2")
        self.assertEqual(len(first.context["rows"]), 2)
        self.assertContains(first, "หน้า 1 / 2")
        self.assertContains(first, "?tab=all&amp;channel=line&amp;page=2")  # and the sub-tab
        self.assertEqual(len(second.context["rows"]), 1)


class ExportTests(ProblemsTestCase):
    def test_only_admins_open_the_list_or_download_it(self):
        for user in (self.intake, self.meter):
            self.client.force_login(user)
            self.assertEqual(self.client.get(LIST).status_code, 403)
            self.assertEqual(self.client.get(CSV).status_code, 403)
        self.assertFalse(AuditEvent.objects.filter(action="cases.problems_exported").exists())

    def test_the_csv_has_what_the_page_shows_and_is_audited(self):
        response = self.client.get(CSV)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        text = response.content.decode("utf-8")
        self.assertTrue(text.startswith("﻿"))
        rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
        self.assertEqual(tuple(rows[0]), problems_web.HEADER)
        self.assertEqual(len(rows) - 1, 4)
        kinds = sorted(row[4] for row in rows[1:])
        self.assertEqual(kinds, sorted(["ไม่ระบุตัวตน"] * 2 + ["ผู้แจ้งหน่วยงาน (เขต/ฝ่าย)"] * 2))
        phoned = next(row for row in rows[1:] if row[0] == self.phoned.number)
        self.assertEqual((phoned[6], phoned[7], phoned[8]), ("โทรศัพท์", "Intake", "SIM-TRIAGE"))
        named = next(row for row in rows[1:] if row[0] == self.named.number)
        self.assertEqual(named[2], TYPED)
        self.assertTrue(named[3].startswith(self.requester.display_name))
        idea = next(row for row in rows[1:] if row[0] == self.ideas[0].number)
        self.assertEqual(idea[3], f"ไม่ระบุตัวตน #{self.anonymous.alias}")
        for private in ("13.7", ALICE, BOB):
            self.assertNotIn(private, text)
        event = AuditEvent.objects.get(action="cases.problems_exported")
        self.assertEqual(
            (event.actor, event.data["rows"], event.data["filters"]), (self.admin, 4, {})
        )

        phone_only = self.client.get(CSV + "?channel=phone").content.decode("utf-8")
        self.assertEqual(len(list(csv.reader(io.StringIO(phone_only)))) - 1, 1)


class AliasTests(ProblemsTestCase):
    def test_every_anonymous_reporter_gets_a_unique_random_code(self):
        alias = self.anonymous.alias
        self.assertEqual(len(alias), 4)
        self.assertTrue(set(alias) <= set(identity.ALIAS_ALPHABET))
        other = identity.anonymous_requester(
            ChannelIdentity.objects.create(channel="line", external_id=CAROL)
        )
        self.assertNotEqual(other.alias, alias)
        self.assertEqual(self.requester.alias, "")

    def test_the_migration_gives_old_anonymous_reporters_a_code(self):
        Requester.objects.filter(is_anonymous=True).update(alias="")
        backfill = import_module("operations.migrations.0029_requester_alias").backfill
        backfill(apps, None)
        self.anonymous.refresh_from_db()
        self.assertEqual(len(self.anonymous.alias), 4)
        self.assertEqual(Requester.objects.filter(is_anonymous=False).exclude(alias="").count(), 0)
