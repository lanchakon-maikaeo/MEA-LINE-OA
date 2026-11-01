import csv
import io
from datetime import timedelta

from django.db import connection
from django.urls import reverse
from django.utils import timezone

from operations import work_orders
from operations.models import CaseTriage, Division, Role, WorkOrder
from operations.templatetags.ui import thai_short
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m10_web import WebTestCase


class MixedQueueTests(WebTestCase):
    def setUp(self):
        super().setUp()
        self.director = make_staff("director", (Role.DEPARTMENT_SUPERVISOR, None))

    def order(self, **extra):
        return work_orders.create(self.director, "ตรวจมิเตอร์", self.mtr, **extra)

    def numbers(self, page):
        return [(row.order or row.case).number for row in page.context["rows"]]

    def test_previous_release_can_create_without_channel_and_unknown_is_not_labeled_line(self):
        now = timezone.now()
        with connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO operations_workorder
                (number, title, detail, division_id, issued_by_id, status, result, reason,
                 is_urgent, reminded_soon, reminded_late, created_at, updated_at)
                VALUES (%s, %s, '', %s, %s, 'assigned', '', '', false, false, false, %s, %s)
                RETURNING channel""",
                ["T69-999999", "งานจากรุ่นเดิม", self.mtr.pk, self.supervisor.pk, now, now],
            )
            self.assertEqual(cursor.fetchone()[0], "")
        page = self.client_for(self.admin).get(reverse("queue-all"))
        self.assertContains(page, "T69-999999")
        self.assertContains(page, "ไม่ระบุช่องทาง")
        self.assertNotContains(page, "ผ่าน LINE")

    def test_normal_queues_render_both_sources_with_issuer_and_original_arrival(self):
        case = self.at_meter()
        line = self.order(assignee=self.meter, channel=WorkOrder.Channel.LINE)
        arrived = timezone.now() - timedelta(days=2, hours=3)
        WorkOrder.objects.filter(pk=line.pk).update(created_at=arrived)
        line.refresh_from_db()
        web = self.order()
        other_division = Division.objects.get(code="SIM-TRF")
        other = work_orders.create(self.director, "ตรวจหม้อแปลง", other_division)
        work_orders.act(self.meter, line, "start")
        for user in (self.meter, self.supervisor, self.admin):
            with self.subTest(user=user.username):
                client = self.client_for(user)
                page = client.get(reverse("division-detail", args=[self.mtr.code]))
                self.assertEqual(set(self.numbers(page)), {case.number, line.number, web.number})
                self.assertEqual(page.context["pager"]["total"], 3)
                self.assertEqual(dict((key, n) for key, _, n in page.context["tabs"])["host"], 3)
                self.assertContains(page, "สั่งโดย Director · ผ่าน LINE")
                self.assertContains(page, "สั่งโดย Director · ผ่าน เว็บ")
                for arrived in (case.submitted_at, line.created_at, web.created_at):
                    self.assertContains(page, f"เข้า {thai_short(arrived)} น.")
                self.assertContains(page, reverse("order-detail", args=[line.number]))
                self.assertNotContains(page, other.number)
                self.assertNotContains(page, 'href="/orders/"')
        admin = self.client_for(self.admin)
        self.assertContains(admin.get(reverse("queue-all")), other.number)
        self.assertEqual(
            self.client_for(self.meter)
            .get(reverse("order-detail", args=[other.number]))
            .status_code,
            404,
        )

    def test_workflow_tabs_filters_and_duty_presets_keep_work_visible(self):
        order = self.order(assignee=self.meter)
        client = self.client_for(self.meter)
        url = reverse("division-detail", args=[self.mtr.code])
        self.assertContains(client.get(url, {"tab": "mine"}), order.number)
        self.assertContains(client.get(url, {"status": "assigned"}), order.number)
        self.assertNotContains(client.get(url, {"kind": "withdrawal"}), order.number)
        self.assertNotContains(client.get(url, {"priority": "P0"}), order.number)
        created = order.created_at
        order = work_orders.act(self.meter, order, "done", result="ตรวจแล้ว")
        supervisor = self.client_for(self.supervisor)
        self.assertContains(supervisor.get(url, {"tab": "decide"}), order.number)
        self.assertContains(client.get(url), order.number)
        work_orders.act(self.supervisor, order, "close")
        self.assertNotContains(client.get(url), order.number)
        self.assertContains(client.get(url, {"tab": "closed"}), order.number)
        order.refresh_from_db()
        self.assertEqual(order.created_at, created)
        returned = self.order(assignee=self.meter)
        work_orders.act(self.meter, returned, "send_back", reason="ส่งกลับ")
        self.assertContains(supervisor.get(url, {"tab": "decide"}), returned.number)
        self.assertContains(client.get(url), returned.number)
        issuer = make_staff("issueronly", (Role.ISSUER, self.mtr.code))
        assigned = self.order(assignee=issuer)
        issuer_client = self.client_for(issuer)
        preset = issuer_client.get(url)
        self.assertTrue(preset.context["filters"].preset)
        self.assertContains(preset, assigned.number)
        query = preset.context["base_query"]
        self.assertContains(
            issuer_client.get(f"{url}?{query}", HTTP_HX_REQUEST="true"), assigned.number
        )
        csv_url = reverse("division-export", args=[self.mtr.code])
        self.assertIn(assigned.number, issuer_client.get(f"{csv_url}?{query}").content.decode())

    def test_mixed_sort_happens_before_pagination_and_htmx_matches(self):
        case = self.at_meter()
        CaseTriage.objects.filter(case=case).update(effective_priority="P0")
        start = timezone.now() - timedelta(days=3)
        orders = WorkOrder.objects.bulk_create(
            [
                WorkOrder(
                    number=f"T69-{i:06d}",
                    title=f"งาน {i}",
                    issued_by=self.supervisor,
                    division=self.mtr,
                    channel=WorkOrder.Channel.LINE,
                    is_urgent=i == 52,
                    due_on=timezone.localdate() + timedelta(days=1) if i == 51 else None,
                )
                for i in range(1, 53)
            ]
        )
        for i, order in enumerate(orders):
            order.created_at = start + timedelta(minutes=i)
        WorkOrder.objects.bulk_update(orders, ["created_at"])
        client = self.client_for(self.admin)
        url = reverse("queue-all")
        # The B.4 order is one tap away (grill กคว. 27 ต.ค. 2569 Q12: newest first by default).
        first = client.get(url, {"sort": "urgent"})
        second = client.get(url, {"page": 2, "sort": "urgent"})
        expected = [case.number, orders[51].number, orders[50].number] + [
            order.number for order in orders[:50]
        ]
        self.assertEqual(self.numbers(first) + self.numbers(second), expected)
        self.assertEqual(
            first.context["pager"], {"number": 1, "pages": 2, "total": 53, "more": 3, "next": 100}
        )
        # 28 ต.ค. 2569 Q8: "แสดงเพิ่มเติม" grows the list instead of paging it.
        grown = client.get(url, {"sort": "urgent", "shown": 100})
        self.assertEqual(self.numbers(grown), expected)
        self.assertEqual(grown.context["pager"]["more"], 0)
        poll = client.get(url, {"sort": "urgent"}, HTTP_HX_REQUEST="true")
        self.assertEqual(self.numbers(poll), self.numbers(first))
        self.assertEqual(poll.context["pager"], first.context["pager"])
        exported = client.get(reverse("queue-all-export"), {"sort": "urgent"})
        rows = list(csv.reader(io.StringIO(exported.content.decode("utf-8-sig"))))[1:]
        self.assertEqual([row[0] for row in rows], expected)
        newest = self.numbers(client.get(url))
        self.assertEqual(newest[:2], [case.number, orders[51].number])  # emergency on top

    def test_overview_and_csv_include_orders_while_case_reports_keep_their_scope(self):
        case = self.at_meter()
        line = self.order(assignee=self.meter, channel=WorkOrder.Channel.LINE)
        late = self.order()
        WorkOrder.objects.filter(pk=late.pk).update(due_on=timezone.localdate() - timedelta(days=1))
        client = self.client_for(self.admin)
        overview = client.get(reverse("console"))
        card = next(c for c in overview.context["cards"] if c["division"] == self.mtr)
        self.assertEqual(card["open"], 3)
        self.assertEqual(card["overdue"], 1)
        self.assertEqual(overview.context["open_count"], 3)
        self.assertEqual(overview.context["report"]["open"], 1)
        self.assertEqual(
            set(self.numbers(client.get(overview.context["open_link"]))),
            {
                case.number,
                line.number,
                late.number,
            },
        )
        self.assertContains(client.get(reverse("queue-all"), {"overdue": 1}), late.number)
        self.director.staff_profile.display_name = "=HYPERLINK(1)"
        self.director.staff_profile.save()
        exported = client.get(reverse("queue-all-export")).content.decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(exported)))
        row = next(r for r in rows if r["เลขเรื่อง"] == line.number)
        self.assertEqual(row["ผู้สั่ง"], "'=HYPERLINK(1)")
        self.assertEqual(row["ช่องทาง"], "LINE")
        self.assertEqual(len(rows), 3)
