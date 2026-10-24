"""M11 registry (SAP) reconciliation (PROJECT.md M11; Q1–Q21, ADR 0005).

Acceptance → tests: snapshots keep as_of/stale (BalanceTests); an unknown write is never retried
blind, the same key returns the same outcome and a duplicate callback is recorded once
(ExternalOperationTests); a registry result that does not match opens a queue for people
(ReconcileTests, DecisionTests); manual mode needs a document number, the person who recorded it
and evidence, with no hidden API write (EvidenceTests, ExternalOperationTests).
"""

import csv
import io
import tempfile
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from config.environment import inventory_mode
from operations import contracts, inventory, options, registry, registry_ops, staff_email
from operations.contracts import Actor, CommandFailed, ErrorCode
from operations.models import (
    Attachment,
    AuditEvent,
    Case,
    EquipmentType,
    ExternalOperation,
    NotificationDelivery,
    RegistryDiscrepancy,
    RegistryImport,
    RegistryRecord,
    Role,
    StockSnapshot,
)
from operations.tests.test_m02_cases import key, make_staff, setup_world
from operations.tests.test_m07_fulfilment import Staffing, evidence_file, fuse

D = RegistryDiscrepancy
HEADER = [
    "Material Document",
    "Item",
    "Posting Date",
    "Movement Type",
    "Material",
    "Quantity",
    "Serial Number",
    "Reference",
    "Reversed Document",
]
CODES = {"MTR_SYN_A": "300001", "FUSE_SYN_A": "300002", "CB_SYN_A": "300003", "TRF_SYN_A": "300004"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


def sheet(rows, header=HEADER):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode()


def doc(number, movement, code, quantity, serial="", reference="", reversed_doc="", day=None):
    day = day or timezone.localdate()
    return [number, 1, f"{day:%d.%m.%Y}", movement, code, quantity, serial, reference, reversed_doc]


class RegistryTestCase(Staffing, TestCase):
    @classmethod
    def setUpTestData(cls):
        setup_world(cls)
        Staffing.staff_up(cls)
        cls.reg = make_staff("reg", (Role.STAFF, "SIM-REG"))
        # These tests are the registry unit at work: ตัวเลือกระบบ registry_access = staff.
        options.change(cls.admin, options.REGISTRY_ACCESS, "staff", "หน่วยทะเบียนตัดสินเอง")
        cls.reg.email = "reg@example.test"
        cls.reg.save(update_fields=["email"])
        for code, official in CODES.items():
            registry.set_official_code(cls.admin, EquipmentType.objects.get(code=code), official)
        registry.update_config(
            cls.admin,
            {"issue": "201 261", "return": "651", "status": "321 344", "reversal": "202"},
            {},
            "",
            "ตั้งค่าทดสอบ",
        )

    manual = False

    def setUp(self):
        if self.manual:
            mode = override_settings(INVENTORY_MODE="manual_reference")
            mode.enable()
            self.addCleanup(mode.disable)
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        settings_patch = override_settings(MEDIA_ROOT=media.name)
        settings_patch.enable()
        self.addCleanup(settings_patch.disable)
        self.counter = 0

    def assertFails(self, code, fn, *args, **kwargs):
        with self.assertRaises(CommandFailed) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        return caught.exception

    # --- helpers -------------------------------------------------------------------------------

    def issued(self, quantity, document, days_ago=5):
        """A fuse withdrawal issued in manual mode, recorded `days_ago` days back."""
        case = self.approved(quantity, fuse(quantity), "SIM-EQP")
        self.issue(self.eqp_issuer, case, quantity, document_number=document)
        record = RegistryRecord.objects.get(case=case)
        RegistryRecord.objects.filter(pk=record.pk).update(
            recorded_at=timezone.now() - timedelta(days=days_ago)
        )
        return Case.objects.get(pk=case.pk), RegistryRecord.objects.get(pk=record.pk)

    def documents(self, rows, days=10, exported=None, user=None, header=HEADER):
        self.counter += 1
        today = timezone.localdate()
        return registry.import_documents(
            user or self.reg,
            f"mb51-{self.counter}.csv",
            sheet(rows, header) + b"\n" * self.counter,  # blank lines: another file
            exported or timezone.now() - timedelta(minutes=60 - self.counter),
            today - timedelta(days=days),
            today,
        )

    def open_kinds(self):
        return sorted(D.objects.exclude(status=D.Status.RESOLVED).values_list("kind", flat=True))


# --- Reading SAP files (Q14) -------------------------------------------------------------------


class ReadingTests(RegistryTestCase):
    def test_sap_numbers_and_dates_in_their_usual_shapes(self):
        self.assertEqual(registry.parse_number("1,234.000"), Decimal("1234.000"))
        self.assertEqual(registry.parse_number("3-"), Decimal("-3"))
        self.assertEqual(registry.parse_number("1.234,5"), Decimal("1234.5"))
        self.assertEqual(registry.parse_number(2.0), Decimal("2.0"))
        self.assertIsNone(registry.parse_number(""))
        with self.assertRaises(ValueError):
            registry.parse_number("สาม")
        day = timezone.localdate().replace(day=8)
        for text in (f"{day:%d.%m.%Y}", f"{day:%d/%m}/{day.year + 543}", day.isoformat()):
            self.assertEqual(registry.parse_date(text), day, text)

    def test_excel_and_windows_874_csv_are_read(self):
        from openpyxl import Workbook

        book = Workbook()
        book.active.append(HEADER)
        book.active.append(doc("4900000001", "201", "300002", 8))
        data = io.BytesIO()
        book.save(data)
        headers, rows = registry.read_table(data.getvalue(), "export.xlsx")
        columns = registry.map_columns(headers, RegistryImport.Kind.DOCUMENTS)
        self.assertEqual(rows[0][columns["document_number"]], "4900000001")
        thai = "เลขเอกสาร,ประเภทการเคลื่อนไหว,รหัสพัสดุ,จำนวน\n49,201,300002,1\n".encode("cp874")
        headers, rows = registry.read_table(thai, "export.csv")
        self.assertEqual(
            set(registry.map_columns(headers, RegistryImport.Kind.DOCUMENTS)),
            {"document_number", "movement_type", "official_code", "quantity"},
        )

    def test_missing_columns_are_named_and_an_admin_alias_adds_one(self):
        with self.assertRaises(ValidationError) as caught:
            registry.map_columns(["Doc No", "MvT", "Material", "Quantity"], "documents")
        self.assertIn("เลขเอกสาร", str(caught.exception))
        registry.update_config(
            self.admin,
            {"issue": "201", "return": "651", "status": "321", "reversal": "202"},
            {"document_number": "Doc No"},
            "",
            "ชื่อหัวตารางของ SAP ที่ ฝอจ. ใช้",
        )
        self.assertIn(
            "document_number",
            registry.map_columns(["Doc No", "MvT", "Material", "Quantity"], "documents"),
        )

    def test_settings_are_admin_only_audited_and_one_code_per_movement(self):
        with self.assertRaises(PermissionDenied):
            registry.update_config(self.reg, {}, {}, "", "x")
        with self.assertRaises(ValidationError):
            registry.update_config(self.admin, {"issue": "201", "return": "201"}, {}, "", "ซ้ำ")
        self.assertTrue(AuditEvent.objects.filter(action="registry.config_updated").exists())


# --- Imports (Q4, Q11, Q21) ----------------------------------------------------------------------


class ImportTests(RegistryTestCase):
    def test_only_registry_staff_or_an_admin_import(self):
        data = sheet([doc("4900000001", "201", "300002", 8)])
        today = timezone.localdate()
        with self.assertRaises(PermissionDenied):
            registry.import_documents(self.meter, "x.csv", data, timezone.now(), today, today)
        registry.import_documents(self.admin, "x.csv", data, timezone.now(), today, today)

    def test_the_same_file_twice_is_refused_and_kept_once(self):
        data = sheet([doc("4900000001", "201", "300002", 8)])
        today = timezone.localdate()
        registry.import_documents(self.reg, "a.csv", data, timezone.now(), today, today)
        with self.assertRaises(ValidationError) as caught:
            registry.import_documents(self.reg, "b.csv", data, timezone.now(), today, today)
        self.assertIn("นำเข้าแล้ว", str(caught.exception))
        self.assertEqual(RegistryImport.objects.count(), 1)

    def test_one_bad_row_refuses_the_whole_file(self):
        rows = [doc("4900000001", "201", "300002", 8), doc("4900000002", "201", "300002", "x")]
        with self.assertRaises(ValidationError) as caught:
            self.documents(rows)
        self.assertIn("แถว 3", str(caught.exception))
        self.assertFalse(RegistryImport.objects.exists())

    def test_a_period_in_the_future_or_without_movement_types_is_refused(self):
        tomorrow = timezone.localdate() + timedelta(days=1)
        data = sheet([doc("4900000001", "201", "300002", 8)])
        with self.assertRaises(ValidationError):
            registry.import_documents(self.reg, "a.csv", data, timezone.now(), tomorrow, tomorrow)
        registry.config().__class__.objects.update(movement_types={})
        with self.assertRaises(ValidationError) as caught:
            self.documents([doc("4900000001", "201", "300002", 8)])
        self.assertIn("ประเภทการเคลื่อนไหว", str(caught.exception))


# --- Reconciliation (Q9–Q11) ---------------------------------------------------------------------


class ReconcileTests(RegistryTestCase):
    manual = True  # manual_reference for the tests, after the fake-store fixtures

    def test_a_matching_document_opens_nothing_and_the_case_page_says_so(self):
        case, record = self.issued(8, "4900000001")
        upload = self.documents([doc("4900000001", "261", "300002", "8-")])
        self.assertEqual(upload.summary["new"], {})
        self.assertEqual(self.open_kinds(), [])
        self.assertEqual(registry.record_state(record), "matched")

    def test_not_found_waits_for_an_export_that_covers_the_day(self):
        _, old = self.issued(8, "4900000001", days_ago=5)
        _, today = self.issued(2, "4900000002", days_ago=0)
        self.documents([doc("4900000099", "201", "300002", 1)])
        found = D.objects.get(kind=D.Kind.NOT_FOUND)
        self.assertEqual(found.record, old)  # today is not covered (last working day or later)
        self.assertEqual(registry.record_state(today), "waiting")
        self.assertFalse(D.objects.filter(record=today).exists())

    def test_quantity_mismatch_closes_itself_when_a_later_export_agrees(self):
        _, record = self.issued(8, "4900000001")
        self.documents([doc("4900000001", "201", "300002", 7)])
        mismatch = D.objects.get(record=record)
        self.assertEqual(
            (mismatch.kind, mismatch.expected, mismatch.found),
            (D.Kind.QUANTITY, {"300002": "8"}, {"300002": "7"}),
        )
        upload = self.documents([doc("4900000001", "201", "300002", 8)])
        mismatch.refresh_from_db()
        self.assertEqual(
            (mismatch.status, mismatch.resolution), (D.Status.RESOLVED, D.Resolution.MATCHED)
        )
        self.assertEqual(upload.summary["resolved"], 1)

    def test_the_latest_export_wins_whatever_the_upload_order(self):
        _, record = self.issued(8, "4900000001")
        now = timezone.now()
        self.documents([doc("4900000001", "201", "300002", 8)], exported=now - timedelta(minutes=5))
        self.documents([doc("4900000001", "201", "300002", 7)], exported=now - timedelta(days=1))
        self.assertEqual(self.open_kinds(), [])

    def test_one_document_for_two_cases_is_compared_by_its_total(self):
        self.issued(3, "4900000001")
        self.issued(5, "4900000001")
        self.documents([doc("4900000001", "201", "300002", 8)])
        self.assertEqual(self.open_kinds(), [])

    def test_serials_come_from_a_serials_file_when_documents_lack_them(self):
        case = self.approved(1)
        self.issue(self.issuer, case, 1, ["S-1"], document_number="4900000003")
        RegistryRecord.objects.update(recorded_at=timezone.now() - timedelta(days=3))
        self.documents([doc("4900000003", "201", "300001", 1)])
        self.assertEqual(self.open_kinds(), [])  # no serials in SAP: quantity only
        registry.import_serials(
            self.reg,
            "serials.csv",
            sheet(
                [["4900000003", "300001", "S-2"]],
                ["Material Document", "Material", "Serial Number"],
            ),
            timezone.now(),
        )
        mismatch = D.objects.get(kind=D.Kind.SERIALS)
        self.assertIn("S-1", mismatch.detail)
        self.assertIn("S-2", mismatch.detail)

    def test_wrong_type_and_reversed_documents(self):
        _, issue = self.issued(8, "4900000001")
        _, other = self.issued(2, "4900000002")
        self.documents(
            [
                doc("4900000001", "651", "300002", 8),
                doc("4900000002", "201", "300002", 2),
                doc("4900000009", "202", "300002", 2, reversed_doc="4900000002"),
            ]
        )
        self.assertEqual(D.objects.get(record=issue).kind, D.Kind.TYPE)
        self.assertIn("กลับรายการ", D.objects.get(record=other).detail)

    def test_an_item_without_an_official_code_cannot_be_compared(self):
        case = self.approved(
            5,
            {
                "kind": "withdrawal",
                "purpose": "ทดสอบ",
                "lines": [{"item_code": "CBL_SYN_A", "quantity": "5"}],
            },
            "SIM-EQP",
        )
        self.issue(self.eqp_issuer, case, 5, document_number="4900000005")
        self.documents([doc("4900000005", "201", "300002", 5)])
        self.assertIn(D.Kind.NO_CODE, self.open_kinds())

    def test_a_sap_document_citing_a_case_without_a_record_here(self):
        case, _ = self.issued(8, "4900000001")
        self.documents(
            [
                doc("4900000001", "201", "300002", 8, reference=f"เบิก {case.number}"),
                doc("4900000077", "201", "300002", 1, reference=f"ตามเรื่อง {case.number}"),
                doc("4900000078", "201", "300002", 1, reference="W69-999999"),
            ]
        )
        cited = D.objects.filter(kind=D.Kind.UNRECORDED).order_by("document_number")
        self.assertEqual(
            [(d.document_number, d.case_id) for d in cited],
            [("4900000077", case.pk), ("4900000078", None)],
        )

    def test_reconciling_twice_opens_nothing_new(self):
        self.issued(8, "4900000001")
        self.documents([doc("4900000001", "201", "300002", 7)])
        summary = registry.reconcile()
        self.assertEqual((summary["new"], summary["resolved"]), ({}, 0))
        self.assertEqual(D.objects.count(), 1)

    def test_facts_older_than_seven_days_without_any_export_are_listed(self):
        _, old = self.issued(8, "4900000001", days_ago=9)
        self.issued(2, "4900000002", days_ago=2)
        self.assertEqual(registry.unreconciled(), [old])
        self.assertEqual(registry.report()["unreconciled"], 1)


# --- Evidence and recorded facts (Q3, Q13) -------------------------------------------------------


class EvidenceTests(RegistryTestCase):
    manual = True  # manual_reference for the tests, after the fake-store fixtures

    def raw_issue(self, case, evidence):
        return self.act_plain(
            self.eqp_issuer,
            case,
            "issue",
            {
                "recipient_name": "ผู้รับ",
                "document_number": "4900000001",
                "lines": [{"line_no": 1, "quantity": "2"}],
                "evidence": evidence,
            },
        )

    def act_plain(self, who, case, action, payload):
        from operations import services

        return services.perform_action(
            Actor.staff(who),
            case.pk,
            action,
            expected_revision=Case.objects.get(pk=case.pk).revision,
            idempotency_key=key(),
            payload=payload,
        )

    def test_evidence_is_optional_but_must_be_this_persons_file_on_this_case(self):
        # 23 Oct 2569: the SAP document number is what staff must give; a file may go with it.
        case = self.approved(2, fuse(2), "SIM-EQP")
        other_case = self.approved(2, fuse(2), "SIM-EQP")
        without = self.approved(2, fuse(2), "SIM-EQP")
        self.raw_issue(without, [])
        self.assertFalse(RegistryRecord.objects.get(case=without).evidence.exists())
        someone_else = evidence_file(self.eqp, case)
        self.assertFails(ErrorCode.VALIDATION_ERROR, self.raw_issue, case, [str(someone_else.pk)])
        elsewhere = evidence_file(self.eqp_issuer, other_case)
        self.assertFails(ErrorCode.VALIDATION_ERROR, self.raw_issue, case, [str(elsewhere.pk)])
        infected = evidence_file(self.eqp_issuer, case, status="infected")
        self.assertFails(ErrorCode.VALIDATION_ERROR, self.raw_issue, case, [str(infected.pk)])
        good = evidence_file(self.eqp_issuer, case)
        self.raw_issue(case, [str(good.pk)])
        record = RegistryRecord.objects.get(case=case)
        self.assertEqual(
            (record.kind, record.document_number, record.recorded_by),
            (RegistryRecord.Kind.ISSUE, "4900000001", self.eqp_issuer),
        )
        self.assertEqual(
            record.lines,
            [{"item": "FUSE_SYN_A", "official_code": "300002", "quantity": "2", "serials": []}],
        )
        self.assertEqual(list(record.evidence.all()), [good])
        self.assertIn("evidence", contracts.contract_document()["actions"]["issue"]["payload"])

    def test_returns_and_inspections_cite_the_registry_too(self):
        from operations.tests.test_m08_returns import Returning, meters_back

        Returning.return_staff(self)
        case = Returning.returning(self, meters_back("M-501"))
        Returning.receive(
            self,
            case,
            [{"item_code": "MTR_SYN_A", "serials": ["M-501"]}],
            document_number="5000000001",
        )
        Returning.inspect(self, case, {"repair": "1"}, reference="5000000002")
        kinds = list(
            RegistryRecord.objects.filter(case=case).values_list("kind", "document_number")
        )
        self.assertEqual(kinds, [("return", "5000000001"), ("status", "5000000002")])

    @override_settings(INVENTORY_MODE="fake")
    def test_the_fake_inventory_records_no_registry_facts(self):
        case = self.approved(2, fuse(2), "SIM-EQP")
        self.issue(self.eqp_issuer, case, 2)
        self.assertFalse(RegistryRecord.objects.exists())

    def test_evidence_failing_its_scan_opens_a_discrepancy_and_a_clean_one_closes_it(self):
        _, record = self.issued(2, "4900000001")
        bad = record.evidence.get()
        Attachment.objects.filter(pk=bad.pk).update(scan_status="infected")
        bad.refresh_from_db()
        registry.after_scan(bad)
        opened = D.objects.get(kind=D.Kind.EVIDENCE)
        self.assertEqual(opened.record, record)
        with self.assertRaises(PermissionDenied):
            registry.add_evidence(self.meter, record.pk, [SimpleUploadedFile("a.png", PNG)])
        new = registry.add_evidence(self.eqp_issuer, record.pk, [SimpleUploadedFile("a.png", PNG)])
        Attachment.objects.filter(pk=new[0].pk).update(scan_status="clean")
        new[0].refresh_from_db()
        registry.after_scan(new[0])
        opened.refresh_from_db()
        self.assertEqual(opened.resolution, D.Resolution.EVIDENCE_REPLACED)

    def test_the_web_stores_evidence_with_the_fact_and_drops_it_when_refused(self):
        case = self.approved(2, fuse(2), "SIM-EQP")
        client = Client()
        client.force_login(self.eqp_issuer)
        url = f"/cases/{case.number}/act/issue/"
        form = client.get(url)
        self.assertContains(form, "ไฟล์เอกสาร SAP")
        post = {
            "revision": Case.objects.get(pk=case.pk).revision,
            "idempotency_key": key(),
            "recipient_name": "ผู้รับ",
            "line-1-quantity": "2",
        }
        refused = client.post(url, {**post, "evidence": SimpleUploadedFile("sap.png", PNG)})
        self.assertEqual(refused.status_code, 400)  # no document number
        self.assertFalse(Attachment.objects.filter(case=case).exists())
        done = client.post(
            url,
            {
                **post,
                "idempotency_key": key(),
                "document_number": "4900000001",
                "evidence": SimpleUploadedFile("sap.png", PNG),
            },
        )
        self.assertEqual(done.status_code, 302)
        record = RegistryRecord.objects.get(case=case)
        self.assertEqual(record.evidence.count(), 1)
        page = client.get(f"/cases/{case.number}/")
        self.assertContains(page, "เลขเอกสาร SAP")
        self.assertContains(page, "รอไฟล์ SAP")


# --- Deciding discrepancies (Q12, Q18) ---------------------------------------------------------


class DecisionTests(RegistryTestCase):
    manual = True  # manual_reference for the tests, after the fake-store fixtures

    def test_registry_staff_fix_a_mistyped_number_and_it_is_compared_again(self):
        case, record = self.issued(8, "490000001")
        self.documents([doc("4900000001", "201", "300002", 8)])
        found = D.objects.get(kind=D.Kind.NOT_FOUND)
        with self.assertRaises(PermissionDenied):
            registry.fix_number(self.admin, found.pk, "4900000001", "x")  # admins read only
        with self.assertRaises(PermissionDenied):
            registry.fix_number(self.eqp, found.pk, "4900000001", "x")
        registry.fix_number(self.reg, found.pk, "4900000001", "พิมพ์ตกหนึ่งหลัก")
        found.refresh_from_db()
        self.assertEqual(found.resolution, D.Resolution.NUMBER_FIXED)
        self.assertEqual(self.open_kinds(), [])
        record.refresh_from_db()
        self.assertEqual(record.document_number, "4900000001")
        self.assertTrue(AuditEvent.objects.filter(action="registry.number_fixed").exists())
        self.assertEqual(Case.objects.get(pk=case.pk).status, case.status)  # the case is untouched

    def test_the_recording_division_checks_and_answers(self):
        _, record = self.issued(8, "4900000001")
        self.documents([doc("4900000001", "201", "300002", 7)])
        d = D.objects.get()
        registry.send_to_division(self.reg, d.pk, "ตรวจจำนวนที่จ่ายจริง")
        d.refresh_from_db()
        self.assertEqual((d.status, d.division.code), (D.Status.WITH_DIVISION, "SIM-EQP"))
        self.assertEqual(list(registry.visible_discrepancies(self.eqp)), [d])
        self.assertEqual(list(registry.visible_discrepancies(self.meter)), [])
        with self.assertRaises(PermissionDenied):
            registry.answer(self.meter, d.pk, "registry_wrong", "x")
        registry.answer(self.eqp, d.pk, "registry_wrong", "จ่ายจริง 8 ชิ้น เขตยืนยันรับแล้ว")
        d.refresh_from_db()
        self.assertEqual((d.status, d.division_answer), (D.Status.OPEN, "registry_wrong"))

    def test_waiting_for_the_registry_closes_once_an_export_agrees(self):
        self.issued(8, "4900000001")
        self.documents([doc("4900000001", "201", "300002", 7)])
        d = D.objects.get()
        with self.assertRaises(ValidationError):
            registry.wait_for_registry(self.reg, d.pk, "", "x")
        registry.wait_for_registry(self.reg, d.pk, "4900000500", "SAP จะออกเอกสารแก้")
        self.documents([doc("4900000001", "201", "300002", 7)], days=11)  # still wrong
        d.refresh_from_db()
        self.assertEqual(d.status, D.Status.WAITING_REGISTRY)
        self.documents([doc("4900000001", "201", "300002", 8)], days=12)
        d.refresh_from_db()
        self.assertEqual(d.resolution, D.Resolution.MATCHED)

    def test_an_accepted_discrepancy_is_not_raised_again(self):
        self.issued(8, "4900000001")
        self.documents([doc("4900000001", "201", "300002", 7)])
        d = D.objects.get()
        registry.accept(self.reg, d.pk, "รายการก่อนเริ่มใช้ระบบ")
        self.documents([doc("4900000001", "201", "300002", 6)], days=11)
        self.assertEqual(D.objects.count(), 1)
        self.assertEqual(self.open_kinds(), [])


# --- Web, email, report (Q6, Q19, Q20) ---------------------------------------------------------


class WebTests(RegistryTestCase):
    manual = True  # manual_reference for the tests, after the fake-store fixtures

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client

    def test_pages_for_registry_staff_admins_and_asked_divisions_only(self):
        self.issued(8, "4900000001")
        self.documents([doc("4900000001", "201", "300002", 7)])
        d = D.objects.get()
        self.assertContains(self.client_for(self.reg).get("/registry/"), "จำนวนไม่ตรง")
        self.assertEqual(self.client_for(self.admin).get("/registry/").status_code, 200)
        self.assertEqual(self.client_for(self.eqp).get("/registry/").status_code, 404)
        self.assertEqual(self.client_for(self.eqp).get(f"/registry/{d.pk}/").status_code, 404)
        registry.send_to_division(self.reg, d.pk, "ตรวจ")
        page = self.client_for(self.eqp).get("/registry/")
        self.assertContains(page, "รอกองตรวจ")
        self.assertNotContains(page, "นำเข้าไฟล์จาก SAP")
        self.assertContains(self.client_for(self.eqp).get(f"/registry/{d.pk}/"), "ผลการตรวจของกอง")
        self.assertEqual(self.client_for(self.reg).get("/registry/settings/").status_code, 403)
        self.assertContains(self.client_for(self.admin).get("/registry/settings/"), "movement type")
        # Item codes and LINE groups live on รายการอุปกรณ์ (23 Oct 2569).
        self.assertContains(self.client_for(self.admin).get("/console/admin/items/"), "300002")

    def test_registry_staff_read_the_cases_their_discrepancies_name_and_no_others(self):
        case, _ = self.issued(8, "4900000001")
        unrelated = self.approved(1)
        reader = self.client_for(self.reg)
        page = reader.get(f"/cases/{case.number}/")
        self.assertContains(page, "เลขเอกสาร SAP")
        self.assertEqual(page.context["offers"], [])  # read only: no actions offered
        self.assertEqual(reader.get(f"/cases/{unrelated.number}/").status_code, 404)
        # Only the registry unit gets this reading right, not every division.
        self.assertEqual(self.client_for(self.meter).get(f"/cases/{case.number}/").status_code, 404)

    def test_with_the_menu_off_the_case_shows_typed_numbers_without_comparing(self):
        case, _ = self.issued(8, "4900000001")
        self.documents([doc("4900000001", "201", "300002", 7)])  # a difference, while it was on
        options.change(self.admin, options.REGISTRY_ACCESS, "off", "ใช้แค่เลข SAP ที่พิมพ์")
        page = self.client_for(self.eqp_issuer).get(f"/cases/{case.number}/")
        self.assertContains(page, "เลขเอกสาร SAP")
        self.assertContains(page, "4900000001")
        for state in ("ตรงกับ SAP", "รอไฟล์ SAP", "มีส่วนต่างทะเบียน", "/registry/"):
            self.assertNotContains(page, state)
        self.assertTrue(D.objects.exists())  # kept, only not shown
        self.assertEqual(self.client_for(self.reg).get("/registry/").status_code, 404)
        self.assertIsNone(
            self.client_for(self.reg).get("/divisions/SIM-REG/").context.get("registry_count")
        )

    def test_queue_chip_for_the_registry_unit_and_for_a_division_asked_to_check(self):
        self.issued(8, "4900000001")
        self.documents([doc("4900000001", "201", "300002", 7)])
        self.assertContains(self.client_for(self.reg).get("/divisions/SIM-REG/"), "ไม่ตรงทะเบียน")
        self.assertNotContains(self.client_for(self.eqp).get("/divisions/SIM-EQP/"), "ไม่ตรงทะเบียน")
        registry.send_to_division(self.reg, D.objects.get().pk, "ตรวจ")
        self.assertContains(self.client_for(self.eqp).get("/divisions/SIM-EQP/"), "ไม่ตรงทะเบียน")

    def test_upload_decide_and_export_through_the_web(self):
        self.issued(8, "4900000001")
        client = self.client_for(self.reg)
        today = timezone.localdate()
        response = client.post(
            "/registry/import/documents/",
            {
                "file": SimpleUploadedFile(
                    "mb51.csv", sheet([doc("4900000001", "201", "300002", 7)])
                ),
                "exported_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
                "period_from": (today - timedelta(days=10)).isoformat(),
                "period_to": today.isoformat(),
            },
            follow=True,
        )
        self.assertContains(response, "ส่วนต่างใหม่ 1")
        d = D.objects.get()
        client.post(f"/registry/{d.pk}/", {"action": "accept", "reason": "=HYPERLINK(1)"})
        d.refresh_from_db()
        self.assertEqual(d.resolution, D.Resolution.ACCEPTED)
        export = client.get("/registry/export.csv")
        text = export.content.decode()
        self.assertTrue(text.startswith("﻿"))
        self.assertIn("'=HYPERLINK(1)", text)
        self.assertTrue(AuditEvent.objects.filter(action="registry.exported").exists())
        self.assertEqual(self.client_for(self.eqp).get("/registry/export.csv").status_code, 403)

    @override_settings(STAFF_EMAIL_ENABLED=True)
    def test_one_summary_email_per_import_and_none_without_news(self):
        self.issued(8, "4900000001")
        first = self.documents([doc("4900000001", "201", "300002", 7)])
        mails = NotificationDelivery.objects.filter(kind="staff_registry")
        self.assertEqual(list(mails.values_list("recipient_user", flat=True)), [self.reg.pk])
        self.assertIn("จำนวนไม่ตรง: 1", mails.get().text)
        self.documents([doc("4900000001", "201", "300002", 7)], days=11)
        self.assertEqual(mails.count(), 1)
        self.assertTrue(first.summary["new"])
        self.reg.role_grants.update(revoked_at=timezone.now())
        self.assertEqual(staff_email._problem(mails.get()), "recipient is no longer registry staff")

    def test_report_counts_open_and_old_discrepancies(self):
        self.issued(8, "4900000001")
        self.documents([doc("4900000001", "201", "300002", 7)])
        D.objects.update(opened_at=timezone.now() - timedelta(days=8))
        data = registry.report()
        self.assertEqual((data["open"], data["older_7"], data["older_30"]), (1, 1, 0))
        self.assertIn(("จำนวนไม่ตรง", 1), data["by_kind"])


# --- Balances and official codes (Q5, Q16) -----------------------------------------------------


class BalanceTests(RegistryTestCase):
    BAL_HEADER = [
        "Material",
        "Plant",
        "Storage Location",
        "Unrestricted",
        "In Quality Insp.",
        "Blocked",
    ]

    def test_sap_stock_types_map_to_our_figures_for_our_storage_locations(self):
        registry.update_config(self.admin, {"issue": "201"}, {}, "1000 / 0001", "ที่เก็บของ ฝอจ.")
        rows = [
            ["300002", "1000", "0001", "10.000", "2", "1"],
            ["300002", "1000", "0002", "99", "0", "0"],
            ["999999", "1000", "0001", "5", "0", "0"],
        ]
        exported = timezone.now() - timedelta(hours=2)
        upload = registry.import_balances(
            self.reg, "mb52.csv", sheet(rows, self.BAL_HEADER), exported
        )
        snap = StockSnapshot.objects.get(item__code="FUSE_SYN_A")
        self.assertEqual(
            (snap.available, snap.awaiting_inspection, snap.awaiting_repair, snap.as_of),
            (Decimal("10"), Decimal("2"), Decimal("1"), exported),
        )
        self.assertEqual(upload.summary["unmapped"], ["999999"])
        self.assertEqual(upload.summary["skipped_places"], 1)
        with override_settings(INVENTORY_MODE="manual_reference", INVENTORY_STALE_HOURS=1):
            self.assertTrue(inventory.availability(snap.item).stale)
        with self.assertRaises(PermissionDenied):
            registry.import_balances(self.meter, "x.csv", sheet(rows, self.BAL_HEADER), exported)

    def test_official_codes_are_unique_admin_only_and_importable(self):
        meter = EquipmentType.objects.get(code="MTR_SYN_A")
        with self.assertRaises(ValidationError):
            registry.set_official_code(self.admin, meter, "300002")
        with self.assertRaises(PermissionDenied):
            registry.set_official_code(self.reg, meter, "1")
        data = "code,รหัสพัสดุ\nMTR_SYN_A,700001\nCBL_SYN_A,700005\n".encode()
        self.assertEqual(registry.import_official_codes(self.admin, "codes.csv", data), 2)
        self.assertEqual(EquipmentType.objects.get(code="CBL_SYN_A").official_code, "700005")


# --- Writes to the registry (api_confirmed contract) -----------------------------------------


class ExternalOperationTests(TestCase):
    REQUEST = {"document": "issue", "material": "300002", "quantity": "8"}

    def test_api_confirmed_is_refused_until_a_contract_exists(self):
        for env in ("local", "production"):
            with self.assertRaises(ImproperlyConfigured):
                inventory_mode(env, "api_confirmed")

    def test_the_same_key_returns_the_first_outcome_and_another_request_is_refused(self):
        sap = registry_ops.SimulatedRegistry()
        first = registry_ops.submit("issue", "k-1", self.REQUEST, sap)
        again = registry_ops.submit("issue", "k-1", self.REQUEST, sap)
        self.assertEqual(
            (first.status, again.pk, sap.posts, sap.writes), ("confirmed", first.pk, 1, 1)
        )
        with self.assertRaises(CommandFailed) as caught:
            registry_ops.submit("issue", "k-1", {**self.REQUEST, "quantity": "9"}, sap)
        self.assertEqual(caught.exception.code, ErrorCode.IDEMPOTENCY_CONFLICT)

    def test_a_timeout_after_the_registry_applied_it_is_settled_by_asking(self):
        sap = registry_ops.SimulatedRegistry(post=["timeout_after"])
        op = registry_ops.submit("issue", "k-2", self.REQUEST, sap)
        self.assertEqual(op.status, ExternalOperation.Status.UNKNOWN)
        op = registry_ops.submit("issue", "k-2", self.REQUEST, sap)  # asks, never resends
        self.assertEqual((op.status, op.external_reference), ("confirmed", "SIM-SAP-000001"))
        self.assertEqual((sap.posts, sap.writes, op.queries), (1, 1, 1))

    def test_proven_not_done_may_be_sent_once_more(self):
        sap = registry_ops.SimulatedRegistry(post=["timeout_before"])
        registry_ops.submit("issue", "k-3", self.REQUEST, sap)
        op = registry_ops.submit("issue", "k-3", self.REQUEST, sap)
        self.assertEqual((op.status, sap.posts, sap.writes), ("confirmed", 2, 1))

    def test_no_answer_to_the_question_keeps_it_unknown_and_refuses_to_send(self):
        sap = registry_ops.SimulatedRegistry(post=["timeout_after"], status=["timeout"])
        op = registry_ops.submit("issue", "k-4", self.REQUEST, sap)
        with self.assertRaises(CommandFailed) as caught:
            registry_ops.submit("issue", "k-4", self.REQUEST, sap)
        self.assertEqual(caught.exception.code, ErrorCode.EXTERNAL_OUTCOME_UNKNOWN)
        self.assertEqual(sap.posts, 1)
        op = registry_ops.reconcile_external(op.operation_id, sap)
        self.assertEqual(op.status, "confirmed")

    def test_refused_connection_may_be_retried_and_a_rejection_is_final(self):
        sap = registry_ops.SimulatedRegistry(post=["down", "ok"])
        self.assertEqual(registry_ops.submit("issue", "k-5", self.REQUEST, sap).status, "pending")
        self.assertEqual(registry_ops.submit("issue", "k-5", self.REQUEST, sap).status, "confirmed")
        sap = registry_ops.SimulatedRegistry(post=["reject"])
        op = registry_ops.submit("issue", "k-6", self.REQUEST, sap)
        again = registry_ops.submit("issue", "k-6", self.REQUEST, sap)
        self.assertEqual((op.status, again.status, sap.posts), ("failed", "failed", 1))

    def test_a_duplicate_callback_is_recorded_once_and_never_overturns(self):
        sap = registry_ops.SimulatedRegistry(post=["timeout_before"])
        op = registry_ops.submit("issue", "k-7", self.REQUEST, sap)
        op, applied = registry_ops.on_callback(op.operation_id, "cb-1", "confirmed", "SAP-9")
        again, applied_again = registry_ops.on_callback(
            op.operation_id, "cb-1", "confirmed", "SAP-9"
        )
        self.assertEqual((applied, applied_again, again.status), (True, False, "confirmed"))
        late, _ = registry_ops.on_callback(op.operation_id, "cb-2", "failed", error="late")
        self.assertEqual(late.status, "confirmed")
        self.assertIn("conflicting callback", [h["event"] for h in late.history])


# --- Demo (manual check) -----------------------------------------------------------------------


class DemoTests(TestCase):
    def test_demo_registry_shows_matches_and_each_kind_of_discrepancy(self):
        out = StringIO()
        call_command("demo_registry", stdout=out)
        text = out.getvalue()
        self.assertIn("ตรงกับ SAP: 1", text)
        for label in ("จำนวนไม่ตรง", "ไม่พบเอกสารในทะเบียน", "เอกสารอ้างเลขเรื่องแต่ไม่มีบันทึก"):
            self.assertIn(label, text)
