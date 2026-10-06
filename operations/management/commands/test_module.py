from django.core.management.base import CommandError
from django.core.management.commands.test import Command as DjangoTest

MODULE_TESTS = {"M00": "operations.tests.test_m00_foundation"}


class Command(DjangoTest):
    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument("--module", required=True)

    def handle(self, *test_labels, **options):
        module = options.pop("module")
        if module not in MODULE_TESTS or test_labels:
            raise CommandError(
                "Select an implemented module ID; positional test labels are invalid."
            )
        return super().handle(MODULE_TESTS[module], **options)
