import io
import struct
from datetime import timedelta
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from PIL import Image

from operations import attachments, broadcast_media, broadcasts
from operations.models import BroadcastMedia, Division, Role
from operations.tests.test_m02_cases import make_staff


@override_settings(BROADCAST_MEDIA_BASE_URL="https://media.example.test")
class BroadcastMediaTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        override = override_settings(MEDIA_ROOT=self.directory.name)
        override.enable()
        self.addCleanup(override.disable)
        self.division = Division.objects.create(code="BM1", name="กองทดสอบสื่อ")
        self.head = make_staff("media-head", (Role.SUPERVISOR, self.division.code))
        self.staff = make_staff("media-staff", (Role.STAFF, self.division.code))

    def image(self):
        data = io.BytesIO()
        Image.new("RGB", (300, 200), "green").save(data, "PNG")
        return SimpleUploadedFile("image.png", data.getvalue(), "application/octet-stream")

    def clean(self, row):
        with patch("operations.attachments.clamd_scan", return_value=(True, "")) as scanner:
            broadcast_media.scan(row.pk)
            self.assertTrue(hasattr(scanner.call_args.args[0], "read"))
        row.refresh_from_db()
        return row

    def test_pending_upload_is_private_then_clean_image_and_thumbnail_are_public(self):
        row = broadcast_media.store(self.head, self.image())
        self.assertEqual(row.status, "pending")
        self.assertEqual(broadcast_media.urls(row), {})
        path = f"/broadcasts/media/{row.pk}/original/"
        self.assertEqual(self.client.get(path).status_code, 404)
        self.clean(row)
        self.assertEqual(row.status, "clean")
        self.assertFalse((broadcast_media.root() / f"{row.pk}.upload").exists())
        for variant in ("original", "preview"):
            response = self.client.get(f"/broadcasts/media/{row.pk}/{variant}/")
            self.assertEqual(response.status_code, 200)
            data = b"".join(response.streaming_content)
            with Image.open(io.BytesIO(data)) as decoded:
                self.assertEqual(decoded.size, (300, 200))
                self.assertFalse(decoded.getexif())
            if variant == "preview":
                self.assertLess(len(data), broadcast_media.PREVIEW_LIMIT)
                self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertEqual(self.client.get(f"/broadcasts/media/{row.pk}/other/").status_code, 404)

    def test_upload_ids_cannot_send_before_clean_and_urls_cannot_override_them(self):
        row = broadcast_media.store(self.head, self.image())
        content = {"kind": "image", "media_id": str(row.pk), "url": "https://wrong.test/a"}
        with self.assertRaises(ValidationError):
            broadcasts.build_messages(content)
        self.clean(row)
        message = broadcasts.build_messages(content)[0]
        self.assertEqual(message["originalContentUrl"], broadcast_media.urls(row)["url"])
        self.assertEqual(message["previewImageUrl"], broadcast_media.urls(row)["preview"])
        with self.assertRaises(ValidationError):
            broadcasts.build_messages({**content, "kind": "video"})

    def test_card_uses_the_uploaded_pictures_own_ratio(self):
        row = self.clean(broadcast_media.store(self.head, self.image()))
        card = {
            "kind": "click_image",
            "title": "ภาพประกาศ",
            "media_id": str(row.pk),
            "link": "https://example.com/",
            "width": 20,
            "height": 13,
        }
        hero = broadcasts.build_messages(card)[0]["contents"]["hero"]
        self.assertEqual(hero["url"], broadcast_media.urls(row)["url"])
        self.assertEqual(hero["aspectRatio"], "300:200")

    def test_unavailable_scanner_retries_without_exposing_file(self):
        row = broadcast_media.store(self.head, self.image())
        with patch(
            "operations.attachments.clamd_scan", side_effect=attachments.ScannerUnavailable()
        ):
            broadcast_media.scan(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.status, "pending")
        self.assertGreater(row.next_scan_at, timezone.now())
        self.assertEqual(broadcast_media.urls(row), {})
        BroadcastMedia.objects.filter(pk=row.pk).update(
            next_scan_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(self.clean(row).status, "clean")

    def test_infected_changed_and_invalid_files_are_rejected(self):
        row = broadcast_media.store(self.head, self.image())
        with patch("operations.attachments.clamd_scan", return_value=(False, "test-virus")):
            broadcast_media.scan(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.status, "rejected")
        row = broadcast_media.store(self.head, self.image())
        (broadcast_media.root() / f"{row.pk}.upload").write_bytes(b"changed")
        with patch("operations.attachments.clamd_scan") as scanner:
            broadcast_media.scan(row.pk)
        scanner.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.status, "rejected")
        broken = SimpleUploadedFile("broken.png", b"\x89PNG\r\n\x1a\nnot an image")
        row = broadcast_media.store(self.head, broken)
        self.assertEqual(self.clean(row).status, "rejected")

    def test_permissions_type_limits_and_https_configuration(self):
        with self.assertRaises(PermissionDenied):
            broadcast_media.store(self.staff, self.image())
        with self.assertRaises(ValidationError):
            broadcast_media.store(self.head, SimpleUploadedFile("photo.png", b"<svg></svg>"))
        large = self.image()
        large.size = broadcast_media.IMAGE_LIMIT + 1
        with self.assertRaises(ValidationError):
            broadcast_media.store(self.head, large)
        with (
            override_settings(BROADCAST_MEDIA_BASE_URL="http://localhost:8000"),
            self.assertRaises(ValidationError),
        ):
            broadcast_media.store(self.head, self.image())

    def test_video_ranges_and_missing_preview(self):
        def box(kind, payload):
            return struct.pack(">I4s", len(payload) + 8, kind) + payload

        video = (
            box(b"ftyp", b"isom\0\0\0\0mp42")
            + box(b"moov", b"hdlr\0\0vide")
            + box(b"mdat", b"0123456789")
        )
        row = broadcast_media.store(self.head, SimpleUploadedFile("clip.mp4", video))
        self.assertEqual(self.clean(row).status, "clean")
        response = self.client.get(f"/broadcasts/media/{row.pk}/original/", HTTP_RANGE="bytes=8-15")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(b"".join(response.streaming_content), video[8:16])
        self.assertEqual(response["Content-Length"], "8")
        self.assertEqual(self.client.get(f"/broadcasts/media/{row.pk}/preview/").status_code, 404)
        self.assertEqual(
            self.client.get(
                f"/broadcasts/media/{row.pk}/original/", HTTP_RANGE="bytes=9999-"
            ).status_code,
            416,
        )
        with self.assertRaises(ValidationError):
            broadcasts.build_messages({"kind": "video", "media_id": str(row.pk)})
        cover = self.clean(broadcast_media.store(self.head, self.image()))
        message = broadcasts.build_messages(
            {"kind": "video", "media_id": str(row.pk), "preview_id": str(cover.pk)}
        )[0]
        self.assertEqual(message["previewImageUrl"], broadcast_media.urls(cover)["preview"])

    def test_live_scan_lease_is_not_taken_but_expired_lease_is_recovered(self):
        row = broadcast_media.store(self.head, self.image())
        BroadcastMedia.objects.filter(pk=row.pk).update(
            status="scanning", next_scan_at=timezone.now() + timedelta(minutes=1)
        )
        with patch("operations.attachments.clamd_scan") as scanner:
            broadcast_media.scan(row.pk)
        scanner.assert_not_called()
        BroadcastMedia.objects.filter(pk=row.pk).update(
            next_scan_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(self.clean(row).status, "clean")

    def test_upload_and_status_api_are_scoped(self):
        self.client.force_login(self.head)
        response = self.client.post("/broadcasts/api/media/", {"file": self.image()})
        self.assertEqual(response.status_code, 202, response.content)
        pk = response.json()["media"]["id"]
        self.assertNotIn("url", response.json()["media"])
        self.assertEqual(self.client.get(f"/broadcasts/api/media/{pk}/").status_code, 200)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(f"/broadcasts/api/media/{pk}/").status_code, 403)
        self.assertEqual(
            self.client.post("/broadcasts/api/media/", {"file": self.image()}).status_code, 403
        )


class StreamingScannerTests(SimpleTestCase):
    def test_bytes_and_stream_use_identical_clamd_frames(self):
        sent = []
        for source in (b"abcdefghij", io.BytesIO(b"abcdefghij")):
            connection = MagicMock()
            connection.recv.return_value = b"stream: OK\0"
            connection.__enter__.return_value = connection
            with patch("operations.attachments.socket.create_connection", return_value=connection):
                self.assertEqual(attachments.clamd_scan(source, chunk=3), (True, ""))
            sent.append([call.args[0] for call in connection.sendall.call_args_list])
        self.assertEqual(sent[0], sent[1])
