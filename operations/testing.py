from django.conf import settings
from django.core.management.base import CommandError
from django.test.runner import DiscoverRunner

from config.environment import require_demo_database


class NonEmptyTestRunner(DiscoverRunner):
    def run_tests(self, test_labels, **kwargs):
        require_demo_database(settings.APP_ENV, settings.DATABASES["default"])
        return super().run_tests(test_labels, **kwargs)

    def build_suite(self, test_labels=None, **kwargs):
        suite = super().build_suite(test_labels, **kwargs)
        if not suite.countTestCases():
            raise CommandError("No tests selected; refusing a false successful run.")
        return suite
