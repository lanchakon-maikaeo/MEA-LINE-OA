"""ADR 0040: department acceptance uses the same web and chat action services."""

from operations import approvals, options
from operations.models import Case, Role
from operations.tests.test_m02_cases import make_staff
from operations.tests.test_m10_web import WebTestCase


class DepartmentAcceptanceTests(WebTestCase):
    def test_department_accepts_whole_case_without_taking_assignment(self):
        case = self.at_meter()
        options.change(self.admin, options.OLD_STOCK, "off", "รับเรื่องทั้งเรื่อง")
        director = make_staff("department", (Role.DEPARTMENT_SUPERVISOR, None))
        original = case.assignee_id
        self.assertTrue(approvals.may_approve(director, case))
        names = dict(self.offers(director, case))
        self.assertIn("approve", names)
        self.assertIn("admin_send", names)
        self.assertNotIn("deny_approval", names)
        response = self.post(director, case, "approve", {"priority": "P1"})
        self.assertEqual(response.status_code, 302)
        case.refresh_from_db()
        self.assertEqual(case.status, Case.Status.AWAITING_EXECUTION)
        self.assertEqual(case.assignee_id, original)

    def test_department_does_not_gain_legacy_inventory_approval(self):
        case = self.at_meter()
        options.change(self.admin, options.OLD_STOCK, "on", "ทดสอบสิทธิ์คลัง")
        director = make_staff("department", (Role.DEPARTMENT_SUPERVISOR, None))
        self.assertFalse(approvals.may_approve(director, case))


class InboxTests(WebTestCase):
    def test_inbox_combines_both_sources_and_limits_division_scope(self):
        from operations import staff_inbox, work_orders

        case = self.at_meter()
        other = self.at_transformer()
        director = make_staff("department", (Role.DEPARTMENT_SUPERVISOR, None))
        order = work_orders.create(director, "ตรวจข้อมูล", self.mtr)
        page, rows = staff_inbox.page_for(self.supervisor, "all")
        self.assertEqual(
            {(source, row.pk) for source, row in rows}, {("case", case.pk), ("order", order.pk)}
        )
        self.assertEqual(page.paginator.count, 2)
        _, rows = staff_inbox.page_for(director, "all")
        self.assertIn(("case", other.pk), {(source, row.pk) for source, row in rows})
        _, rows = staff_inbox.page_for(self.supervisor, "all", other.owner_division_id)
        self.assertEqual(rows, [])

    def test_inbox_waiting_filter_and_pagination(self):
        from operations import staff_inbox, work_orders

        director = make_staff("department", (Role.DEPARTMENT_SUPERVISOR, None))
        for number in range(7):
            work_orders.create(director, f"งาน {number}", self.mtr)
        page, first = staff_inbox.page_for(director)
        _, second = staff_inbox.page_for(director, page=2)
        self.assertEqual(page.paginator.count, 7)
        self.assertEqual((len(first), len(second)), (5, 2))
        self.assertFalse({r.pk for _, r in first} & {r.pk for _, r in second})
        _, active = staff_inbox.page_for(director, "active")
        self.assertEqual(active, [])


class DepartmentTransferTests(WebTestCase):
    def test_transfer_preserves_acceptance_and_clears_old_assignment(self):
        import uuid

        from operations import services
        from operations.contracts import Actor, CommandFailed
        from operations.models import Decision, Division, Handoff

        case = self.at_meter()
        Case.objects.filter(pk=case.pk).update(
            status=Case.Status.AWAITING_EXECUTION, assignee=self.meter
        )
        case.refresh_from_db()
        revision = case.revision
        director = make_staff("department", (Role.DEPARTMENT_SUPERVISOR, None))
        destination = Division.objects.create(code="ปลายทาง", name="กองปลายทาง")
        key = uuid.uuid4().hex
        args = dict(
            expected_revision=revision,
            idempotency_key=key,
            payload={"division": destination.code, "reason": "ส่งให้หน่วยรับผิดชอบ"},
        )
        handoffs = Handoff.objects.filter(case=case).count()
        services.perform_action(Actor.staff(director), case.pk, "admin_send", **args)
        case.refresh_from_db()
        self.assertEqual(case.owner_division, destination)
        self.assertEqual(case.status, Case.Status.AWAITING_EXECUTION)
        self.assertIsNone(case.assignee)
        self.assertEqual(Handoff.objects.filter(case=case).count(), handoffs)
        count = Decision.objects.filter(case=case).count()
        services.perform_action(Actor.staff(director), case.pk, "admin_send", **args)
        self.assertEqual(Decision.objects.filter(case=case).count(), count)
        with self.assertRaises(CommandFailed):
            services.perform_action(
                Actor.staff(director),
                case.pk,
                "admin_send",
                **{**args, "idempotency_key": uuid.uuid4().hex},
            )

    def test_division_supervisor_cannot_use_department_transfer(self):
        import uuid

        from operations import services
        from operations.contracts import Actor, CommandFailed
        from operations.models import Division

        case = self.at_meter()
        destination = Division.objects.create(code="ปลายทาง", name="กองปลายทาง")
        with self.assertRaises(CommandFailed):
            services.perform_action(
                Actor.staff(self.supervisor),
                case.pk,
                "admin_send",
                expected_revision=case.revision,
                idempotency_key=uuid.uuid4().hex,
                payload={"division": destination.code, "reason": "ทดสอบ"},
            )
        case.refresh_from_db()
        self.assertEqual(case.owner_division, self.mtr)

    def test_supervisors_accept_orders_without_becoming_the_assignee(self):
        from operations import work_orders
        from operations.models import WorkOrder

        director = make_staff("department", (Role.DEPARTMENT_SUPERVISOR, None))
        for supervisor in (director, self.supervisor):
            order = work_orders.create(director, "ตรวจสอบข้อมูล", self.mtr, assignee=self.meter)
            order = work_orders.act(supervisor, order, "acknowledge")
            self.assertEqual(order.status, WorkOrder.Status.ACKNOWLEDGED)
            self.assertEqual(order.acknowledged_by, supervisor)
            self.assertEqual(order.assignee, self.meter)
