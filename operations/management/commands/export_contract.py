import json

from django.core.management.base import BaseCommand

from operations.contracts import contract_document


class Command(BaseCommand):
    help = "Print case command contract v1 (errors, actions, transition matrix) as JSON."

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(contract_document(), ensure_ascii=False, indent=2))
