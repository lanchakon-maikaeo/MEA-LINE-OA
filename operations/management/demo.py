from django.conf import settings
from django.core.management.base import BaseCommand

from config.environment import require_demo_database


class DemoCommand(BaseCommand):
    """Base for M01/M02/M06 fixture commands; guard before their first write."""

    def handle(self, *args, **options):
        require_demo_database(settings.APP_ENV, settings.DATABASES["default"])
        return self.handle_demo(*args, **options)

    def handle_demo(self, *args, **options):
        raise NotImplementedError("Fixture import belongs to the owning business module.")
