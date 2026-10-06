from django.conf import settings
from django.core.management.commands.flush import Command as DjangoFlush

from config.environment import require_demo_database


class Command(DjangoFlush):
    def handle(self, **options):
        require_demo_database(settings.APP_ENV, settings.DATABASES["default"])
        return super().handle(**options)
