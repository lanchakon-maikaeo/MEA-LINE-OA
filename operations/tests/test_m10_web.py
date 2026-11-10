"""M10 staff web (PROJECT.md M10; round 1 Q1–Q16, round 2 Q1–Q11).

Acceptance → tests: queues and counts are limited to the account's divisions and admins can
switch to every division (ScopeTests); a no-role or revoked user is refused and settings pages
are admin-only (GuardTests); the AI/rule proposal is shown apart from people's decisions
(CasePageTests); pages carry a strict CSP and serve their own static files (FoundationTests).
"""

import ast
import itertools
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.utils import timezone

import operations
from operations import (
    attachments,
    casepage,
    charts,
    identity,
    notifications,
    orchestration,
    policy,
    staff_email,
    wording,
    work_orders,
)
from operations.models import (
    AckClock,
    Attachment,
    AuditEvent,
    AuthorityGrant,
    Case,
    ChannelIdentity,
    Handoff,
    NotificationDelivery,
    OutboxEvent,
    Role,
    RoleGrant,
)
from operations.templatetags import ui
from operations.tests.test_m02_cases import make_staff, withdrawal
from operations.tests.test_m07_fulfilment import fuse
from operations.tests.test_m09_problems import Problems, ProblemTestCase, faulty
from operations.tests.test_m13_work_orders import keep_original_recipients

S = Case.Status
A = AuthorityGrant.Activity


class WebTestCase(ProblemTestCase):
    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client

    def at_meter(self):
        """A withdrawal owned by SIM-MTR, waiting for approval."""
        return self.ready()

    def at_transformer(self):
        return Problems.triaged(self)

    def post(self, user, case, action, data=None, htmx=False, **extra):
        case = Case.objects.get(pk=case.pk)
        form = {"revision": case.revision, "idempotency_key": uuid.uuid4().hex, **(data or {})}
        headers = {"HTTP_HX_REQUEST": "true"} if htmx else {}
        return self.client_for(user).post(
            f"/cases/{case.number}/act/{action}/", form, **headers, **extra
        )

    def offers(self, user, case):
        page = self.client_for(user).get(f"/cases/{case.number}/")
        menu = page.context["menu"]  # ADR 0055: the list the LINE card shows too
        return [(i.name, i is menu.first) for i in menu.items if i.kind == "action"]


# --- Scope (round 2 Q1–Q4) -------------------------------------------------------------------


class ScopeTests(WebTestCase):
    def test_division_queue_lists_only_its_own_cases(self):
        mine, theirs = self.at_meter(), self.at_transformer()
        page = self.client_for(self.meter).get("/divisions/SIM-MTR/")
        self.assertContains(page, mine.number)
        self.assertNotContains(page, theirs.number)
        self.assertEqual(self.client_for(self.meter).get("/divisions/SIM-TRF/").status_code, 404)

    def test_case_page_search_and_revision_are_404_outside_scope(self):
        theirs = self.at_transformer()
        client = self.client_for(self.meter)
        self.assertEqual(client.get(f"/cases/{theirs.number}/").status_code, 404)
        self.assertEqual(client.get(f"/cases/{theirs.number}/revision/?r=1").status_code, 404)
        search = client.get("/cases/", {"q": theirs.number})
        self.assertEqual(search.status_code, 200)
        self.assertNotContains(search, f"/cases/{theirs.number}/")

    def test_search_jumps_to_an_exact_visible_number(self):
        mine = self.at_meter()
        response = self.client_for(self.meter).get("/cases/", {"q": mine.number.lower()})
        self.assertRedirects(response, f"/cases/{mine.number}/")

    def test_every_role_in_a_division_sees_the_same_cases(self):
        case = self.at_meter()
        # The issuer acts only after approval, but still sees the case (round 2 Q1).
        page = self.client_for(self.issuer).get(f"/cases/{case.number}/")
        self.assertEqual(page.status_code, 200)
        queue = self.client_for(self.issuer).get("/divisions/SIM-MTR/", {"tab": "host"})
        self.assertContains(queue, case.number)

    def test_lone_duty_role_lands_on_its_preset_filter(self):
        page = self.client_for(self.issuer).get("/divisions/SIM-MTR/")
        self.assertEqual(page.context["filters"].status, S.AWAITING_EXECUTION)
        self.assertTrue(page.context["filters"].preset)
        cleared = self.client_for(self.issuer).get("/divisions/SIM-MTR/", {"clear": "1"})
        self.assertFalse(cleared.context["filters"].active)

    def test_tab_counts_never_include_other_divisions(self):
        self.at_meter()
        self.at_transformer()
        page = self.client_for(self.meter).get("/divisions/SIM-MTR/")
        counts = {key: count for key, _, count in page.context["tabs"]}
        self.assertEqual(counts["host"], 1)
        trf = self.client_for(self.trf_staff).get("/divisions/SIM-TRF/")
        self.assertEqual({k: c for k, _, c in trf.context["tabs"]}["host"], 1)

    def test_handoff_waiting_for_a_division_shows_in_its_incoming_tab(self):
        case = self.submit()
        self.act(self.intake, case, "start_triage")
        self.act(self.intake, case, "request_handoff", {"to_division": "SIM-MTR"})
        page = self.client_for(self.meter).get("/divisions/SIM-MTR/", {"tab": "incoming"})
        self.assertContains(page, case.number)
        self.assertContains(page, "รอ SIM-MTR ตอบรับ")

    def test_handoff_against_the_proposal_shows_the_typed_reason(self):
        case = self.submit()  # a meter withdrawal: the rules propose SIM-MTR
        self.act(self.intake, case, "start_triage")
        self.act(
            self.intake,
            case,
            "request_handoff",
            {
                "to_division": "SIM-TRF",
                "reason_category": "new_evidence",
                "reason": "ช่างกองหม้อแปลงอยู่หน้างาน",
            },
        )
        client = self.client_for(self.trf_staff)
        queue = client.get("/divisions/SIM-TRF/", {"tab": "incoming"})
        self.assertContains(queue, "ส่งต่างจากที่ระบบเสนอ (SIM-MTR)")
        self.assertContains(queue, "เหตุผลที่ส่งต่อ (ข้อมูลหน้างานใหม่): ช่างกองหม้อแปลงอยู่หน้างาน")
        page = client.get(f"/cases/{case.number}/")
        self.assertContains(page, "ส่งต่างจากที่ระบบเสนอ (ระบบเสนอ SIM-MTR)")
        self.assertContains(page, "ช่างกองหม้อแปลงอยู่หน้างาน")

    def test_handoff_that_follows_the_proposal_is_not_flagged(self):
        case = self.submit()
        self.act(self.intake, case, "start_triage")
        self.act(self.intake, case, "request_handoff", {"to_division": "SIM-MTR"})
        queue = self.client_for(self.meter).get("/divisions/SIM-MTR/", {"tab": "incoming"})
        self.assertContains(queue, "รอ SIM-MTR ตอบรับ")
        self.assertNotContains(queue, "ส่งต่างจากที่ระบบเสนอ")

    def test_cancelled_case_leaves_the_incoming_tab(self):
        case = self.submit()
        self.act(self.intake, case, "start_triage")
        self.act(self.intake, case, "request_handoff", {"to_division": "SIM-MTR"})
        self.act(self.intake, case, "cancel", {"reason": "ผู้แจ้งแจ้งยกเลิกทางโทรศัพท์"})
        handoff = case.handoffs.get()
        self.assertEqual(handoff.status, Handoff.Status.ENDED)
        self.assertEqual(handoff.decision_reason, "เรื่องยกเลิกก่อนตอบรับ")
        queue = self.client_for(self.meter).get("/divisions/SIM-MTR/", {"tab": "incoming"})
        self.assertNotContains(queue, case.number)

    def test_child_division_reads_the_parent_case(self):
        case = self.submit(
            withdrawal(
                lines=[
                    {"item_code": "MTR_SYN_A", "quantity": "1"},
                    {"item_code": "TRF_SYN_A", "quantity": "1"},
                ]
            )
        )
        self.act(self.intake, case, "start_triage")
        self.act(
            self.intake,
            case,
            "request_child_work",
            {"division": "SIM-TRF", "task": "จ่ายหม้อแปลง", "rule": "R09", "lines": [2]},
        )
        client = self.client_for(self.trf_staff)
        self.assertContains(client.get("/divisions/SIM-TRF/", {"tab": "child"}), case.number)
        page = client.get(f"/cases/{case.number}/")
        self.assertContains(page, "งานย่อย 1 · SIM-TRF")

    def test_admin_switches_to_every_division_and_views_are_audited(self):
        theirs = self.at_transformer()
        client = self.client_for(self.admin)
        self.assertContains(client.get("/queue/"), theirs.number)
        self.assertEqual(client.get(f"/cases/{theirs.number}/").status_code, 200)
        viewed = AuditEvent.objects.filter(action="case.viewed", target_id=str(theirs.pk))
        self.assertEqual(viewed.count(), 1)
        # The revision poll is not a visit.
        client.get(f"/cases/{theirs.number}/revision/?r={theirs.revision}")
        self.assertEqual(viewed.count(), 1)
        # Members are not audited for their own division's cases.
        self.client_for(self.trf_staff).get(f"/cases/{theirs.number}/")
        self.assertEqual(viewed.count(), 1)

    def test_attachment_download_follows_the_case_scope(self):
        from operations.models import Attachment, ChannelIdentity, WebhookInbox

        theirs = self.at_transformer()
        identity_row = ChannelIdentity.objects.create(channel="line", external_id="U" + "a" * 32)
        inbox = WebhookInbox.objects.create(
            webhook_event_id="evt-m10",
            event_type="message",
            payload={},
            payload_hash="0" * 64,
            occurred_at=theirs.submitted_at,
            received_at=theirs.submitted_at,
        )
        attachment = Attachment.objects.create(
            identity=identity_row,
            inbox=inbox,
            line_message_id="m10",
            case=theirs,
            content_type="image/png",
            scan_status=Attachment.ScanStatus.CLEAN,
            storage_name="m10-test.png",
        )
        with tempfile.TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            (attachments.storage_dir() / "m10-test.png").write_bytes(b"\x89PNG test")
            url = f"/attachments/{attachment.pk}/"
            self.assertEqual(self.client_for(self.trf_staff).get(url).status_code, 200)
            self.assertEqual(self.client_for(self.admin).get(url).status_code, 200)
            self.assertEqual(self.client_for(self.meter).get(url).status_code, 404)


# --- Guards (acceptance; round 2 Q4) --------------------------------------------------------


class GuardTests(WebTestCase):
    def test_no_role_and_revoked_users_are_refused(self):
        norole = make_staff("norole")
        client = self.client_for(norole)
        self.assertContains(client.get("/console/"), "ยังไม่ได้รับกองหรือบทบาท")
        case = self.at_meter()
        self.assertEqual(client.get(f"/cases/{case.number}/").status_code, 404)
        self.assertEqual(client.get("/cases/", {"q": case.number}).status_code, 404)
        meter = self.client_for(self.meter)
        identity.revoke_role(self.admin, RoleGrant.objects.get(user=self.meter), "ย้ายงาน")
        self.assertEqual(meter.get(f"/cases/{case.number}/").status_code, 404)
        self.assertEqual(meter.get("/divisions/SIM-MTR/").status_code, 404)

    def test_settings_pages_are_admin_only_and_hidden_from_staff(self):
        client = self.client_for(self.meter)
        page = client.get("/divisions/SIM-MTR/")
        self.assertNotContains(page, "/console/admin/")
        for url in (
            "/console/admin/users/",
            "/console/admin/policy/",
            "/console/admin/inventory/",
            "/console/admin/ai/",
            "/queue/",
        ):
            with self.subTest(url=url):
                response = client.get(url)
                self.assertEqual(response.status_code, 403)
                self.assertContains(response, "ไม่มีสิทธิ์เข้าหน้านี้", status_code=403)

    def test_ai_settings_link_for_every_admin(self):
        # 15 ต.ค. 2569 Q7/Q23: every admin opens the AI page; changing it is a separate right.
        page = self.client_for(self.admin).get("/console/admin/users/")
        self.assertContains(page, "/console/admin/ai/")
        self.assertNotContains(self.client_for(self.meter).get("/console/"), "/console/admin/ai/")
        root = make_staff("root", (Role.ADMIN, None), (Role.DEVELOPER_ADMIN, None))
        self.assertContains(
            self.client_for(root).get("/console/admin/users/"), "/console/admin/ai/"
        )

    def test_one_division_lands_on_its_queue_and_several_get_cards(self):
        self.assertRedirects(self.client_for(self.meter).get("/start/"), "/divisions/SIM-MTR/")
        # ภาพรวม stays reachable from the menu with the one card.
        own = self.client_for(self.meter).get("/console/")
        self.assertEqual([c["division"].code for c in own.context["cards"]], ["SIM-MTR"])
        both = make_staff("both", (Role.STAFF, "SIM-MTR"), (Role.STAFF, "SIM-TRF"))
        self.assertRedirects(self.client_for(both).get("/start/"), "/console/")
        page = self.client_for(both).get("/console/")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(
            [c["division"].code for c in page.context["cards"]], ["SIM-MTR", "SIM-TRF"]
        )


# --- Case page (round 2 Q8) ---------------------------------------------------------------------


class CasePageTests(WebTestCase):
    def test_proposal_is_apart_from_the_peoples_decisions(self):
        case = self.at_meter()
        page = self.client_for(self.meter).get(f"/cases/{case.number}/")
        self.assertContains(page, "ข้อเสนอจากระบบ")
        self.assertContains(page, "การตัดสินของเจ้าหน้าที่")
        decisions = page.context["page"]["decisions"]
        self.assertEqual(decisions[0]["what"], "ระดับปกติ")
        self.assertIn("ยังไม่ใช่การตัดสิน", page.content.decode())

    def test_timeline_shows_who_did_what_and_the_policy_events(self):
        case = self.at_meter()
        page = self.client_for(self.meter).get(f"/cases/{case.number}/")
        whats = [entry["what"] for entry in page.context["page"]["timeline"]]
        self.assertEqual(whats[:3], ["ยื่นเรื่อง", "เริ่มคัดกรอง", "ส่งต่อเรื่อง"])

    def test_revision_poll_is_silent_until_the_case_changes(self):
        case = self.at_meter()
        client = self.client_for(self.meter)
        self.assertEqual(
            client.get(f"/cases/{case.number}/revision/?r={case.revision}").status_code, 204
        )
        self.act(self.meter, case, "request_info", {"reason": "ขอเลขงาน"})
        changed = client.get(f"/cases/{case.number}/revision/?r={case.revision}")
        self.assertContains(changed, "เรื่องนี้มีการเปลี่ยนแปลง")

    def test_resolution_shows_the_internal_note_to_staff_only_label(self):
        case = Problems.resolved(self)
        page = self.client_for(self.trf_staff).get(f"/cases/{case.number}/")
        self.assertContains(page, "บันทึกภายใน (ผู้แจ้งไม่เห็น)")


# --- Actions (round 1 Q9, round 2 Q6) -----------------------------------------------------------


class ActionTests(WebTestCase):
    def test_intake_starts_triage_from_the_page(self):
        case = self.submit()
        self.assertIn(("start_triage", True), self.offers(self.intake, case))
        response = self.post(self.intake, case, "start_triage")
        self.assertRedirects(response, f"/cases/{case.number}/")
        self.assertEqual(Case.objects.get(pk=case.pk).status, S.TRIAGE)

    def test_primary_button_follows_status_and_role(self):
        case = self.at_meter()
        self.assertEqual(self.offers(self.approver, case)[0], ("approve", True))
        self.assertNotIn("issue", dict(self.offers(self.issuer, case)))
        self.act(self.approver, case, "approve", {"lines": [{"line_no": 1, "quantity": "3"}]})
        self.assertEqual(self.offers(self.issuer, case)[0], ("issue", True))
        # ADR 0041: แอดมินระบบ does what a แอดมินฝ่าย does: assign and send on.
        # ADR 0050 Q20: and take it themselves.
        self.assertEqual(
            self.offers(self.admin, case),
            [("claim", False), ("assign", False), ("admin_send", False)],
        )

    def test_approve_and_issue_through_the_forms(self):
        case = self.at_meter()
        form = self.client_for(self.approver).get(f"/cases/{case.number}/act/approve/")
        self.assertContains(form, 'name="line-1-quantity"')
        self.post(
            self.approver, case, "approve", {"line-1-quantity": "2", "line-1-reason": "ของมีจำกัด"}
        )
        line = Case.objects.get(pk=case.pk).lines.get(line_no=1)
        self.assertEqual(line.approved_quantity, 2)
        response = self.post(
            self.issuer,
            case,
            "issue",
            {"recipient_name": "ผู้รับจำลอง", "line-1-quantity": "1", "line-1-serials": "MTR_SYN_001"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Case.objects.get(pk=case.pk).lines.get(line_no=1).issued_quantity, 1)

    def test_role_without_permission_cannot_use_the_action_url(self):
        case = self.at_meter()
        self.assertEqual(
            self.client_for(self.meter).get(f"/cases/{case.number}/act/approve/").status_code, 403
        )
        response = self.post(self.meter, case, "approve", {"line-1-quantity": "3"})
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "ไม่มีอำนาจ", status_code=403)
        self.assertIsNone(Case.objects.get(pk=case.pk).lines.get(line_no=1).approved_quantity)

    def test_stale_form_reports_the_change_and_keeps_the_text(self):
        case = self.at_meter()
        old = Case.objects.get(pk=case.pk).revision
        self.act(self.meter, case, "request_info", {"reason": "ขอเลขงาน"})
        response = self.client_for(self.meter).post(
            f"/cases/{case.number}/act/info_received/",
            {"revision": old, "idempotency_key": uuid.uuid4().hex, "reason": "เขตส่งเลขงานแล้ว"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "มีการเปลี่ยนแปลงระหว่างที่คุณกรอก", status_code=409)
        self.assertContains(response, "ขอข้อมูลเพิ่ม", status_code=409)
        self.assertContains(response, "เขตส่งเลขงานแล้ว", status_code=409)
        fresh = Case.objects.get(pk=case.pk)
        self.assertEqual(fresh.status, S.AWAITING_INFO)
        self.assertContains(response, f'name="revision" value="{fresh.revision}"', status_code=409)

    def test_irreversible_action_needs_the_tick(self):
        case = self.at_meter()
        response = self.post(self.approver, case, "deny_approval", {"reason": "ไม่มีงบ"})
        self.assertContains(response, "ติ๊กยืนยันก่อนบันทึก")
        self.assertEqual(Case.objects.get(pk=case.pk).status, S.AWAITING_APPROVAL)
        self.post(
            self.approver, case, "deny_approval", {"reason": "ไม่มีงบ", "confirm_irreversible": "1"}
        )
        self.assertEqual(Case.objects.get(pk=case.pk).status, S.REJECTED)

    def test_htmx_save_redirects_and_errors_stay_in_the_form(self):
        case = self.submit()
        ok = self.post(self.intake, case, "start_triage", htmx=True)
        self.assertEqual(ok["HX-Redirect"], f"/cases/{case.number}/")
        bad = self.post(self.intake, case, "request_info", htmx=True)
        self.assertEqual(bad.status_code, 200)
        self.assertContains(bad, "notice--error")

    def test_resolution_files_wait_for_the_scan(self):
        case = Problems.triaged(self)
        upload = SimpleUploadedFile("photo.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, "image/png")
        with tempfile.TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            response = self.post(
                self.resolver,
                case,
                "resolve",
                {"category": "fixed", "message": "เปลี่ยนฟิวส์แล้ว", "files": upload},
            )
        self.assertEqual(response.status_code, 302)
        attachment = Attachment.objects.get(case=case)
        self.assertEqual(attachment.uploaded_by, self.resolver)
        self.assertEqual(attachment.scan_status, Attachment.ScanStatus.PENDING_SCAN)
        self.assertTrue(AuditEvent.objects.filter(action="case.attachment_added").exists())

    def test_wrong_file_types_are_refused_before_the_action(self):
        case = Problems.triaged(self)
        upload = SimpleUploadedFile("run.exe", b"MZ" + b"0" * 64, "application/octet-stream")
        response = self.post(
            self.resolver, case, "resolve", {"category": "fixed", "message": "x", "files": upload}
        )
        self.assertContains(response, "รับเฉพาะ JPG, PNG หรือ PDF")
        self.assertEqual(Case.objects.get(pk=case.pk).status, S.IN_PROGRESS)

    def test_staff_upload_on_the_case_page(self):
        case = self.at_meter()
        upload = SimpleUploadedFile("doc.pdf", b"%PDF-1.4 test", "application/pdf")
        with tempfile.TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            response = self.client_for(self.meter).post(
                f"/cases/{case.number}/files/", {"files": upload, "note": "ใบเสนอราคา"}
            )
            self.assertEqual(response.status_code, 302)
            other = self.client_for(self.trf_staff).post(
                f"/cases/{case.number}/files/", {"files": upload}
            )
            self.assertEqual(other.status_code, 404)
            # Admins read every case but add nothing where they hold no role (round 2 Q3).
            admin = self.client_for(self.admin).post(
                f"/cases/{case.number}/files/", {"files": upload}
            )
            self.assertEqual(admin.status_code, 403)
        self.assertEqual(Attachment.objects.get(case=case).note, "ใบเสนอราคา")

    def test_demo_form_shows_any_step_of_the_kind_and_cannot_be_sent(self):
        case = self.at_meter()
        # The meter staff may not approve, yet the tour shows what approving looks like.
        page = self.client_for(self.meter).get(
            f"/cases/{case.number}/act/approve/?demo=1", HTTP_HX_REQUEST="true"
        )
        self.assertContains(page, "แบบฟอร์มตัวอย่างสำหรับการสาธิต")
        self.assertContains(page, 'name="line-1-quantity"')
        self.assertNotContains(page, 'name="revision"')
        self.assertNotContains(page, 'name="idempotency_key"')
        self.assertNotContains(page, "hx-post")
        self.assertContains(page, "disabled")
        # Even the issue form lists the lines before anything is approved.
        issue = self.client_for(self.meter).get(f"/cases/{case.number}/act/issue/?demo=1")
        self.assertContains(issue, 'name="line-1-quantity"')

    def test_demo_form_keeps_the_case_scope_and_the_kind(self):
        case = self.at_meter()
        self.assertEqual(
            self.client_for(self.trf_staff)
            .get(f"/cases/{case.number}/act/approve/?demo=1")
            .status_code,
            404,
        )
        # A withdrawal has no problem-only steps.
        self.assertEqual(
            self.client_for(self.meter)
            .get(f"/cases/{case.number}/act/resolve/?demo=1")
            .status_code,
            404,
        )

    def test_tour_finds_a_case_of_the_kind_beyond_the_queue_in_view(self):
        meter_case = self.at_meter()
        eqp_case = self.ready(fuse(2), division="SIM-EQP")
        admin = self.client_for(self.admin)
        admin.get("/divisions/SIM-TRF/")  # a division with no withdrawal
        self.assertEqual(
            admin.get("/tour/where/?kind=withdrawal").json(),
            {
                "queue": "/divisions/SIM-EQP/?tab=host&kind=withdrawal",
                "case": f"/cases/{eqp_case.number}/",
            },
        )
        # The division in view comes first when it has one.
        admin.get("/divisions/SIM-MTR/")
        self.assertEqual(
            admin.get("/tour/where/?kind=withdrawal").json()["case"],
            f"/cases/{meter_case.number}/",
        )

    def test_tour_keeps_the_case_scope(self):
        self.at_meter()
        self.assertEqual(
            self.client_for(self.trf_staff).get("/tour/where/?kind=withdrawal").json(),
            {"queue": None, "case": None},
        )
        self.assertEqual(Client().get("/tour/where/").status_code, 302)

    def test_child_division_answers_its_child_work(self):
        case = self.submit()
        self.act(self.intake, case, "start_triage")
        self.act(
            self.intake,
            case,
            "request_child_work",
            {"division": "SIM-TRF", "task": "ตรวจหม้อแปลงที่เกี่ยวข้อง"},
        )
        self.assertIn(("accept_child_work", True), self.offers(self.trf_staff, case))
        self.post(self.trf_staff, case, "accept_child_work", {"child": "1"})
        self.assertEqual(case.child_works.get(seq=1).status, "accepted")


# --- Staff email (round 1 Q10, round 2 Q5) -----------------------------------------------------


class FriendlyFlowTests(WebTestCase):
    """User feedback 9 Oct 2569: an English message inside the Thai, and steps that looked
    possible but were refused. Now a button shows only when its step can succeed, the page says
    why and when instead, triage ends in one submit with the level, and errors read in Thai."""

    def at_owner(self):
        """A problem case in triage at SIM-TRF, its level not yet confirmed."""
        case = self.submit(faulty(impact="ร้านค้า 3 รายไม่มีไฟ"))
        self.act(self.intake, case, "start_triage")
        self.act(self.intake, case, "request_handoff", {"to_division": "SIM-TRF"})
        self.act(self.trf_staff, case, "accept_handoff")
        return Case.objects.get(pk=case.pk)

    def test_triage_ends_in_one_submit_with_the_level(self):
        case = self.at_owner()
        self.assertIn("complete_triage", dict(self.offers(self.trf_staff, case)))
        form = self.client_for(self.trf_staff).get(f"/cases/{case.number}/act/complete_triage/")
        self.assertContains(form, 'name="priority"')
        self.assertContains(form, "ยืนยันระดับความเร่งด่วนไปพร้อมกัน")
        response = self.post(
            self.trf_staff, case, "complete_triage", {"priority": "P2", "reason": "ไฟดับบางส่วน"}
        )
        self.assertRedirects(response, f"/cases/{case.number}/")
        case.refresh_from_db()
        self.assertEqual(case.status, S.IN_PROGRESS)
        self.assertEqual(
            list(case.decisions.order_by("id").values_list("action", flat=True))[-2:],
            ["set_priority", "complete_triage"],
        )
        self.assertEqual(policy.state_for(case).confirmed_priority, "P2")

    def test_triage_accepts_a_level_change_without_an_edit_reason(self):
        case = self.at_owner()
        response = self.post(self.trf_staff, case, "complete_triage", {"priority": "P0"})
        self.assertRedirects(response, f"/cases/{case.number}/")
        case.refresh_from_db()
        self.assertEqual(case.status, S.IN_PROGRESS)
        self.assertTrue(case.decisions.filter(action="set_priority", reason="").exists())

    def test_once_the_level_is_confirmed_the_form_does_not_ask_again(self):
        case = self.at_owner()
        self.act(self.trf_staff, case, "set_priority", {"priority": "P2", "reason": "ทดสอบ"})
        form = self.client_for(self.trf_staff).get(f"/cases/{case.number}/act/complete_triage/")
        self.assertNotContains(form, 'name="priority"')
        self.post(self.trf_staff, case, "complete_triage")
        self.assertEqual(Case.objects.get(pk=case.pk).status, S.IN_PROGRESS)

    def test_the_intake_desk_is_told_to_hand_the_case_on(self):
        case = self.submit(faulty(impact="ร้านค้า 3 รายไม่มีไฟ"))
        self.act(self.intake, case, "start_triage")
        offers = dict(self.offers(self.intake, case))
        self.assertNotIn("complete_triage", offers)
        self.assertIn("request_handoff", offers)
        page = self.client_for(self.intake).get(f"/cases/{case.number}/")
        self.assertContains(page, "ส่งต่อให้กองที่รับผิดชอบก่อน")

    def test_close_waits_seven_days_and_the_page_says_from_when(self):
        case = Problems.resolved(self)
        offers = dict(self.offers(self.trf_staff, case))
        self.assertNotIn("close", offers)
        page = self.client_for(self.trf_staff).get(f"/cases/{case.number}/")
        self.assertContains(page, "ปิดเรื่องเองได้ตั้งแต่")
        self.assertContains(page, "ผู้แจ้งไม่ได้ผูก LINE ไว้")
        self.assertContains(page, "SIM-TRIAGE กด “บันทึกคำตอบแทนหน่วยงาน (เขต/ฝ่าย)”")
        self.assertIn("confirm", dict(self.offers(self.intake, case)))
        case.decisions.filter(action="resolve").update(
            created_at=timezone.now() - timedelta(days=8)
        )
        self.assertIn("close", dict(self.offers(self.trf_staff, case)))
        page = self.client_for(self.trf_staff).get(f"/cases/{case.number}/")
        self.assertContains(page, "ตัวเลือกที่ย้อนกลับไม่ได้ (ยกเลิกเรื่อง, ปิดเรื่อง)")
        self.post(
            self.trf_staff,
            case,
            "close",
            {"reason": "เขตไม่ตอบ", "notice": "ปิดเรื่องแล้ว", "confirm_irreversible": "1"},
        )
        self.assertEqual(Case.objects.get(pk=case.pk).status, S.CLOSED)

    def test_a_requester_with_line_answers_there(self):
        case = Problems.resolved(self)
        ChannelIdentity.objects.create(
            channel=ChannelIdentity.Channel.LINE,
            external_id="U" + "1" * 32,
            requester=case.requester,
            status="active",
        )
        for user in (self.trf_staff, self.intake):
            page = self.client_for(user).get(f"/cases/{case.number}/")
            self.assertContains(page, "ผู้แจ้งตอบผลใน LINE ได้เอง กด “ยืนยัน”")
            self.assertNotContains(page, "ไม่ได้ผูก LINE")

    def test_the_step_tracker_follows_the_case(self):
        def states(case):
            return [j["state"] for j in casepage.journey(Case.objects.get(pk=case.pk))]

        case = self.at_owner()
        self.assertEqual(states(case), ["current", "todo", "todo", "todo"])
        case = Problems.resolved(self)
        self.assertEqual(states(case), ["done", "done", "current", "todo"])
        self.act(self.trf_staff, case, "hold", {"reason": "รอช่าง"})
        steps = casepage.journey(Case.objects.get(pk=case.pk))
        self.assertEqual((steps[2]["state"], steps[2]["note"]), ("current", "พักเรื่องไว้"))
        page = self.client_for(self.trf_staff).get(f"/cases/{case.number}/")
        self.assertContains(page, 'aria-label="ขั้นของเรื่อง"')

    def test_wording_covers_the_services(self):
        """Every English message a service can refuse with has Thai for the screen."""
        missing = []
        for path in sorted(Path(operations.__file__).parent.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if not (
                    isinstance(node, ast.Call)
                    and getattr(node.func, "id", "") == "fail"
                    and len(node.args) >= 2
                ):
                    continue
                message = _sample(node.args[1])
                if message and message.isascii() and wording.thai(message) is None:
                    missing.append(f"{path.name}:{node.lineno} {message}")
        self.assertEqual(missing, [])


def _sample(node):
    """A message as a service would raise it: f-string holes filled with a plausible value."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if not isinstance(node, ast.JoinedStr):
        return None
    parts = []
    for part in node.values:
        if isinstance(part, ast.Constant):
            parts.append(part.value)
            continue
        hole = ast.unparse(part.value).lower()
        if any(word in hole for word in ("floor", "value", "priority")):
            parts.append("P2")
        elif any(word in hole for word in ("max", "limit")):
            parts.append("50")
        else:
            parts.append("x1")
    return "".join(parts)


def with_email(user, address):
    user.email = address
    user.save(update_fields=["email"])
    return user


@override_settings(
    STAFF_EMAIL_ENABLED=True,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    STAFF_WEB_BASE_URL="https://faoj.example.test",
)
class StaffEmailTests(WebTestCase):
    def setUp(self):
        super().setUp()
        keep_original_recipients()  # these follow each kind's own people (ADR 0050 Q6)

    def trf_staff_division(self):
        from operations.models import Division

        return Division.objects.get(code="SIM-TRF")

    def decision_event(self, case):
        return (
            OutboxEvent.objects.filter(case=case, event_type=orchestration.EVENT_DECISION)
            .order_by("-id")
            .first()
        )

    def test_handoff_mails_the_receiving_division_once_without_requester_details(self):
        with_email(self.meter, "meter@example.test")
        case = self.submit()
        self.act(self.intake, case, "start_triage")
        self.act(self.intake, case, "request_handoff", {"to_division": "SIM-MTR"})
        event = self.decision_event(case)
        planned = staff_email.plan_for_event(event)
        self.assertEqual([d.email for d in planned], ["meter@example.test"])
        self.assertEqual(staff_email.plan_for_event(event), [])  # a replay plans nothing
        delivery = planned[0]
        self.assertNotIn(case.requester.display_name, delivery.text)
        self.assertNotIn(case.purpose, delivery.text)
        self.assertEqual(notifications.send(delivery.pk).outcome, "sent")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(case.number, mail.outbox[0].subject)
        self.assertIn(f"https://faoj.example.test/cases/{case.number}/", mail.outbox[0].body)
        self.assertEqual(notifications.send(delivery.pk).outcome, "already")

    def test_recipient_who_lost_access_is_skipped_at_send_time(self):
        with_email(self.meter, "meter@example.test")
        case = self.submit()
        self.act(self.intake, case, "start_triage")
        self.act(self.intake, case, "request_handoff", {"to_division": "SIM-MTR"})
        delivery = staff_email.plan_for_event(self.decision_event(case))[0]
        # Still a working account, but moved to a division that does not see this case.
        identity.change_division(self.admin, self.meter, self.mtr, self.trf_staff_division(), "ย้าย")
        self.assertEqual(notifications.send(delivery.pk).outcome, "skipped")
        self.assertEqual(mail.outbox, [])
        self.assertEqual(
            NotificationDelivery.objects.get(pk=delivery.pk).detail,
            "recipient no longer sees the case",
        )

    def test_nothing_is_planned_while_staff_email_is_off(self):
        with_email(self.meter, "meter@example.test")
        case = self.submit()
        self.act(self.intake, case, "start_triage")
        self.act(self.intake, case, "request_handoff", {"to_division": "SIM-MTR"})
        with override_settings(STAFF_EMAIL_ENABLED=False):
            self.assertEqual(staff_email.plan_for_event(self.decision_event(case)), [])

    def test_overdue_goes_to_the_owner_and_escalation_to_its_supervisors(self):
        with_email(self.trf_staff, "trf@example.test")
        with_email(self.trf_lead, "lead@example.test")
        case = Problems.triaged(self)
        clock = Problems.clock(self, case, AckClock.Kind.RESOLVE)
        self.settle_outbox()
        policy.sweep_clocks(clock.due_at)
        overdue = NotificationDelivery.objects.filter(kind="staff_overdue")
        self.assertIn("trf@example.test", set(overdue.values_list("email", flat=True)))
        OutboxEvent.objects.update(status=OutboxEvent.Status.DONE)
        policy.sweep_clocks(clock.escalate_at)
        escalated = NotificationDelivery.objects.filter(kind="staff_escalated")
        self.assertEqual(list(escalated.values_list("email", flat=True)), ["lead@example.test"])
        policy.sweep_clocks(clock.escalate_at + timedelta(hours=1))
        self.assertEqual(escalated.count(), 1)

    def test_new_hazard_case_alerts_intake_staff(self):
        with_email(self.intake, "intake@example.test")
        case = self.submit(withdrawal(purpose="สายไฟไหม้ที่ตู้ ต้องการมิเตอร์ด่วน"))
        event = OutboxEvent.objects.get(case=case, event_type=orchestration.EVENT_CASE_SUBMITTED)
        planned = staff_email.plan_for_event(event)
        self.assertEqual([d.kind for d in planned], ["staff_hazard"])


# --- CSV export and reports (round 1 Q12, Q13) ------------------------------------------------


class ExportTests(WebTestCase):
    def test_csv_holds_only_the_division_and_is_spreadsheet_safe(self):
        mine, theirs = self.at_meter(), self.at_transformer()
        self.meter.staff_profile.display_name = "=HYPERLINK(1)"
        self.meter.staff_profile.save()
        self.act(self.meter, mine, "claim")
        response = self.client_for(self.meter).get("/divisions/SIM-MTR/export.csv")
        text = response.content.decode("utf-8")
        self.assertTrue(text.startswith("\ufeff"))
        self.assertIn(mine.number, text)
        self.assertNotIn(theirs.number, text)
        self.assertIn("'=HYPERLINK(1)", text)
        self.assertNotIn(mine.requester.display_name, text)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        audit = AuditEvent.objects.get(action="cases.exported")
        self.assertEqual(audit.data["rows"], 1)
        self.assertEqual(audit.data["division"], "SIM-MTR")

    def test_every_division_export_is_admin_only(self):
        self.assertEqual(self.client_for(self.meter).get("/queue/export.csv").status_code, 403)
        self.at_transformer()
        self.assertEqual(self.client_for(self.admin).get("/queue/export.csv").status_code, 200)
        self.assertEqual(
            self.client_for(self.meter).get("/divisions/SIM-TRF/export.csv").status_code, 404
        )

    def test_long_exports_stop_at_the_limit(self):
        self.at_meter()
        self.at_meter()
        with override_settings(EXPORT_MAX_ROWS=1):
            text = self.client_for(self.meter).get("/divisions/SIM-MTR/export.csv").content
        self.assertIn("ตัดที่ 1 แถว".encode(), text)

    def test_report_counts_only_what_the_account_sees(self):
        self.at_meter()
        self.at_transformer()
        page = self.client_for(self.meter).get("/reports/")
        self.assertEqual(page.context["chosen"].code, "SIM-MTR")
        self.assertEqual(page.context["report"]["open"], 1)
        self.assertEqual(page.context["table"], [])
        admin = self.client_for(self.admin).get("/reports/")
        self.assertGreaterEqual(len(admin.context["table"]), 2)
        self.assertGreaterEqual(page.context["report"]["ack_count"], 1)


# --- Report drill-down and dashboard (9 ตุลาคม 2569) ----------------------------------------------


class ReportDrillDownTests(WebTestCase):
    def figures(self, dash):
        links = [(value, href) for _, value, href, _ in dash["kpis"]]
        links += [(n, href) for _, n, _, href, _ in dash["levels"]]
        links += [(n, href) for _, n, _, href in dash["kinds"]]
        links += [(n, href) for _, n, _, href in dash["statuses"]]
        links += [(n, href) for _, n, _, href, _ in dash["ages"]]
        links += [(n, href) for _, _, n, _, href in dash["items"]]
        return links

    def test_every_figure_opens_the_cases_it_counts(self):
        mine, theirs = self.at_meter(), self.at_transformer()
        client = self.client_for(self.meter)
        page = client.get("/reports/")
        self.assertContains(page, 'class="chart__hit"')
        self.assertContains(page, "ดูเป็นตาราง")
        dash = page.context["dash"]
        self.assertGreaterEqual(sum(value for value, _ in self.figures(dash)), 3)
        for value, href in self.figures(dash):
            listed = client.get(href)
            self.assertEqual(listed.status_code, 200, href)
            self.assertEqual(listed.context["pager"]["total"], value, href)
        day = [c for c in dash["trend"]["columns"] if c["new"]][-1]
        listed = client.get(day["href"])
        self.assertContains(listed, mine.number)
        self.assertNotContains(listed, theirs.number)

    def test_drill_down_keeps_the_scope(self):
        self.at_meter(), self.at_transformer()
        client = self.client_for(self.meter)
        self.assertEqual(
            client.get("/reports/cases/?division=SIM-TRF&metric=open").status_code, 404
        )
        self.assertEqual(client.get("/reports/cases/?metric=nothing").status_code, 404)
        self.assertEqual(client.get("/reports/cases/?metric=level&value=P9").status_code, 404)
        every = self.client_for(self.admin).get("/reports/cases/?metric=kind&value=withdrawal")
        self.assertEqual(every.status_code, 200)
        self.assertEqual(every.context["chosen"], None)

    def test_period_choice(self):
        client = self.client_for(self.meter)
        self.assertEqual(client.get("/reports/?days=7").context["days"], 7)
        self.assertEqual(len(client.get("/reports/?days=7").context["dash"]["trend"]["columns"]), 7)
        self.assertEqual(client.get("/reports/?days=13").context["days"], 30)


# --- Filing for a requester (round 1 Q16) -------------------------------------------------------


class FileForRequesterTests(WebTestCase):
    def form(self, **extra):
        return {
            "idempotency_key": uuid.uuid4().hex,
            "requester_id": str(self.req1.pk),
            "kind": "withdrawal",
            "channel": "phone",
            "intake_note": "เขต D01 โทรแจ้ง 09:15",
            "purpose": "เปลี่ยนมิเตอร์ชำรุด",
            "row-0-item": "MTR_SYN_A",
            "row-0-quantity": "2",
            **extra,
        }

    def test_intake_files_a_case_that_enters_the_queue(self):
        response = self.client_for(self.intake).post("/cases/new/", self.form())
        case = Case.objects.get(channel="phone")
        self.assertRedirects(response, f"/cases/{case.number}/")
        self.assertEqual(case.submitted_by, self.intake)
        self.assertEqual(case.requester, self.req1)
        self.assertEqual(case.lines.get().requested_quantity, 2)

    def test_only_intake_staff_may_file(self):
        self.assertEqual(self.client_for(self.meter).get("/cases/new/").status_code, 403)
        self.assertEqual(self.client_for(self.admin).get("/cases/new/").status_code, 403)

    def test_errors_keep_what_was_typed(self):
        response = self.client_for(self.intake).post(
            "/cases/new/", self.form(**{"row-0-item": "", "row-0-quantity": ""})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "notice--error")
        self.assertContains(response, "เขต D01 โทรแจ้ง 09:15")
        self.assertFalse(Case.objects.filter(channel="phone").exists())


# --- Foundation (round 1 Q2, Q3) ------------------------------------------------------------


class NavigationTests(WebTestCase):
    def test_the_division_switch_shows_every_division_after_choosing_it(self):
        # 15 ต.ค. 2569: on ทุกกอง the switch kept showing the division opened before.
        client = self.client_for(self.admin)
        client.get("/divisions/SIM-MTR/")
        page = client.get("/queue/")
        self.assertTrue(page.context["nav"]["all_queue"])
        self.assertEqual(page.context["nav"]["current"], "")
        self.assertContains(page, '<span class="chip__label">ทุกกอง</span>')
        later = client.get("/console/")
        self.assertTrue(later.context["nav"]["all_queue"])
        self.assertContains(later, 'href="/queue/" data-tour-link="queue"')
        client.get("/divisions/SIM-MTR/")
        again = client.get("/console/")
        self.assertFalse(again.context["nav"]["all_queue"])
        self.assertEqual(again.context["nav"]["current"], "SIM-MTR")

    def test_reference_links_are_for_admins(self):
        # 15 ต.ค. 2569 Q5: ข้อมูลประกอบงาน is hidden from other staff; the pages keep their rights.
        links = ('href="/duties/"', 'href="/routing/words/"', 'href="/console/pii/"')
        staff = self.client_for(self.meter).get("/console/")
        admin = self.client_for(self.admin).get("/console/")
        for link in links:
            self.assertNotContains(staff, link)
            self.assertContains(admin, link)
        self.assertEqual(self.client_for(self.meter).get("/duties/").status_code, 200)

    def test_trend_lines_add_up_over_the_period(self):
        # 15 ต.ค. 2569 Q8: cumulative lines; each day keeps its own counts for the table.
        days = [timezone.localdate() - timedelta(days=n) for n in (2, 1, 0)]
        points = list(zip(days, (2, 0, 3), (0, 1, 2), strict=True))
        trend = charts.trend(points, lambda metric, day: f"/{metric}/{day}")
        self.assertEqual([c["new_total"] for c in trend["columns"]], [2, 2, 5])
        self.assertEqual([c["ended_total"] for c in trend["columns"]], [0, 1, 3])
        self.assertEqual([c["new"] for c in trend["columns"]], [2, 0, 3])
        self.assertEqual((trend["total_new"], trend["total_ended"]), (5, 3))
        self.assertEqual(trend["gap"], "+2")
        tick = {t["label"]: t["y"] for t in trend["ticks"]}
        self.assertEqual(list(tick), ["0", "1", "2", "3", "4", "5"])
        # The new line ends on the top tick, below the frame, and steps up at the third day's edge.
        self.assertEqual(trend["ends"][0]["y"], tick["5"])
        self.assertGreater(tick["5"], trend["top"])
        edge = trend["columns"][2]["x"]
        self.assertIn(f"L{edge} {tick['2']} L{edge} {tick['5']}", trend["new_path"])

    def test_pie_shares_the_open_work_and_folds_the_rest(self):
        # 3 Nov 2569: each division's share of the open work; past five the smallest are อื่นๆ.
        rows = [(f"D{n}", n, f"/d/{n}") for n in (0, 1, 2, 3, 4, 10, 20, 60)]
        pie = charts.pie(rows, other_href="/all/")
        labels = [(s["label"], s["n"], s["percent"], s["tone"]) for s in pie["slices"]]
        self.assertEqual(pie["total"], 100)
        self.assertEqual(
            labels,
            [
                ("D60", 60, "60%", "c1"),
                ("D20", 20, "20%", "c2"),
                ("D10", 10, "10%", "c3"),
                ("D4", 4, "4%", "c4"),
                ("D3", 3, "3%", "c5"),
                ("อื่นๆ", 3, "3%", "other"),
            ],
        )
        self.assertEqual(pie["slices"][-1]["href"], "/all/")
        self.assertIn(" 0 1 ", pie["slices"][0]["path"])  # 60% takes the long way round
        # Each % sits at the end of a leader line, on its slice's side; small slices never touch.
        self.assertEqual(pie["slices"][0]["anchor"], "start")  # 0-60% clockwise: mostly right
        self.assertEqual(pie["slices"][-1]["anchor"], "end")
        for anchor in ("start", "end"):
            ys = sorted(s["label_y"] for s in pie["slices"] if s["anchor"] == anchor)
            self.assertTrue(all(b - a >= charts.LABEL_GAP for a, b in itertools.pairwise(ys)))
        alone = charts.pie([("D1", 0, ""), ("D2", 5, "")])["slices"]
        self.assertEqual([(s["label"], s["percent"]) for s in alone], [("D2", "100%")])
        self.assertEqual(alone[0]["path"].count("A"), 2)  # a whole disc, drawn as two halves

    def test_overview_shows_the_share_instead_of_the_ack_average(self):
        self.at_meter(), self.at_transformer()
        page = self.client_for(self.admin).get("/console/")
        self.assertNotContains(page, "ตอบรับเฉลี่ย</span>")
        self.assertContains(page, "สัดส่วนงานแต่ละกอง")
        self.assertContains(page, 'href="/divisions/SIM-MTR/?tab=host" aria-label="SIM-MTR')

    def test_one_division_shows_its_share_by_kind(self):
        # 4 Nov 2569: one division's pie is by kind, งานสั่ง too, each kind in its own colour;
        # the kind bars go, and each slice opens the queue filtered to that kind.
        case = self.at_meter()
        order = work_orders.create(self.admin, "งานทดสอบ", case.owner_division)
        head = make_staff("mtr-head", (Role.SUPERVISOR, "SIM-MTR"))
        page = self.client_for(head).get("/console/")
        self.assertContains(page, "สัดส่วนงานตามชนิด")
        self.assertNotContains(page, 'id="r-kind"')
        tones = {s["label"]: s["tone"] for s in page.context["share"]["slices"]}
        self.assertEqual(tones, {"เบิก": "c1", "งานสั่ง": "c4"})
        self.assertContains(page, 'href="/divisions/SIM-MTR/?tab=host&amp;kind=order"')
        queue = self.client_for(head).get("/divisions/SIM-MTR/", {"tab": "host", "kind": "order"})
        self.assertContains(queue, order.number)
        self.assertNotContains(queue, case.number)
        queue = self.client_for(head).get("/divisions/SIM-MTR/", {"kind": "withdrawal"})
        self.assertContains(queue, case.number)
        self.assertNotContains(queue, order.number)
        ranked = charts.pie([("A", 1, "", "c1"), ("B", 3, "", "c2")])["slices"]
        self.assertEqual([(s["label"], s["tone"]) for s in ranked], [("B", "c2"), ("A", "c1")])

    def test_trend_legend_stays_off_the_lines(self):
        # Rising lines leave the upper left free; one line along the top and one along the bottom
        # leave only the middle height.
        days = [timezone.localdate() - timedelta(days=n) for n in range(29, -1, -1)]
        link = lambda metric, day: ""  # noqa: E731
        rising = charts.trend([(d, 1, 0) for d in days], link)["legend"]
        early = charts.trend([(d, 9 if i == 0 else 0, 0) for i, d in enumerate(days)], link)
        self.assertEqual((rising["x"], rising["y"]), (charts.LEFT + 10, charts.TOP + 10))
        self.assertGreater(early["legend"]["y"], charts.TOP + 10)
        self.assertLess(early["legend"]["y"] + early["legend"]["height"], early["bottom"] - 10)
        self.assertEqual([i["label"] for i in rising["items"]], ["เข้า 30", "จบ 0", "ส่วนต่าง +30"])


class FoundationTests(TestCase):
    def test_pages_carry_a_strict_content_security_policy(self):
        response = self.client.get("/accounts/login/")
        policy = response["Content-Security-Policy"]
        self.assertIn("script-src 'self'", policy)
        self.assertIn("style-src 'self'", policy)
        self.assertNotIn("unsafe-inline", policy)
        self.assertNotIn("<script>", response.content.decode())
        self.assertNotIn(" style=", response.content.decode())

    def test_locked_static_files_are_served_by_the_app(self):
        for path in (
            "/static/operations/app.css",
            "/static/operations/htmx.min.js",
            "/static/operations/fonts/ibm-plex-sans-thai-thai-400-normal.woff2",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                b"".join(response.streaming_content)

    def test_thai_dates_and_service_durations(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        moment = datetime(2026, 10, 7, 8, 5, tzinfo=ZoneInfo("UTC"))
        self.assertEqual(ui.thai_datetime(moment), "07/10/2569 15:05 น.")
        self.assertEqual(ui.thai_short(moment), "07/10 15:05")
        self.assertEqual(ui.duration(135), "2 ชม. 15 นาที")
        self.assertEqual(ui.duration(-60), "1 ชม.")
        self.assertEqual(ui.duration(45), "45 นาที")
        self.assertEqual(ui.initial("เมธี"), "ม")
        # The web names levels in words, never P-codes (user, 8 Oct 2569).
        self.assertIn(">ฉุกเฉิน<", str(ui.prio("P0")))
        self.assertIn(">ปกติ<", str(ui.prio("P3")))
        self.assertIn(">รอคัดกรอง<", str(ui.prio("")))
        self.assertNotIn(">P", str(ui.prio("P1")))
