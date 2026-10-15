"""Load the draft answer bank (คลังคำตอบ, ADR 0012); existing answers are never overwritten."""

import json
from pathlib import Path

from django.core.management.base import BaseCommand

from operations import answers


class Command(BaseCommand):
    help = "Load chat answers from a fixture as drafts for ฝอจ. to edit and approve on the web."

    def add_arguments(self, parser):
        parser.add_argument("--source", default=str(answers.DEFAULT))

    def handle(self, *args, source, **options):
        made = answers.load(json.loads(Path(source).read_text(encoding="utf-8")))
        self.stdout.write(f"Loaded {made} new answer(s) as drafts; existing ones were kept.")
