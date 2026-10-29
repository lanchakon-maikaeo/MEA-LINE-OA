"""Building artwork must preserve the current menu, including its conversation choices."""

import io
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase, override_settings

from operations import line_menu
from operations.models import LineMenuItem


class ImageBuildTests(TestCase):
    def test_build_preserves_all_menu_records_and_never_calls_line(self):
        choice = LineMenuItem.objects.filter(place="choice").first()
        LineMenuItem.objects.filter(pk=choice.pk).update(note="คงขั้นตอนเดิม")
        before = list(LineMenuItem.objects.order_by("pk").values())
        specs = {page: line_menu.rich_menu(page) for page in line_menu.PAGES}
        source = Path(settings.BASE_DIR) / "operations"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "operations/line_assets"
            output.mkdir(parents=True)
            (root / "operations/static").symlink_to(source / "static", target_is_directory=True)
            copyfile(source / "line_assets/space-template.png", output / "space-template.png")
            with (
                override_settings(BASE_DIR=root),
                patch("operations.line_api.call", side_effect=AssertionError("No LINE calls")),
            ):
                call_command("build_line_menu_images", stdout=io.StringIO())
            for page in line_menu.PAGES:
                image = (output / f"{page}.jpg").read_bytes()
                self.assertEqual(line_menu._picture(image), "image/jpeg")
                self.assertLessEqual(len(image), line_menu.MAX_IMAGE)
        self.assertEqual(list(LineMenuItem.objects.order_by("pk").values()), before)
        self.assertEqual({page: line_menu.rich_menu(page) for page in line_menu.PAGES}, specs)
