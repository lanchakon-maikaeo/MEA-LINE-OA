from django.core.management.base import CommandError
from django.core.management.commands.test import Command as DjangoTest

MODULE_TESTS = {
    "M00": "operations.tests.test_m00_foundation",
    "M01": "operations.tests.test_m01_identity",
    "M02": "operations.tests.test_m02_cases",
    "M03": "operations.tests.test_m03_line",
    "M04": "operations.tests.test_m04_orchestration",
    "M05": ["operations.tests.test_m05_ai", "operations.tests.test_m05_security"],
    "M06": "operations.tests.test_m06_policy",
}


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
        labels = MODULE_TESTS[module]
        return super().handle(*([labels] if isinstance(labels, str) else labels), **options)
