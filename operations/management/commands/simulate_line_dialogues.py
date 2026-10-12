"""Synthetic LINE dialogues against the real bot, rolled back afterwards (dialogue_sim)."""

import json
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from operations import dialogue_sim, dialogue_writer


class Command(BaseCommand):
    help = (
        "Simulate requesters and staff talking to the LINE bot from the dialogue graph, check "
        "every reply and outcome, and roll everything back (nothing is kept or sent)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=50)
        parser.add_argument("--seed", type=int, default=1)
        parser.add_argument(
            "--scenario", action="append", default=[], help="Scenario id (repeatable)"
        )
        parser.add_argument("--dialogue", type=int, help="Replay one dialogue number of this seed")
        parser.add_argument("--graph", help="Graph file (default fixtures/dialogue_graph_v1.json)")
        parser.add_argument("--out", help="Write every dialogue as JSON lines here")
        parser.add_argument("--report", help="Write the summary report (JSON) here")
        parser.add_argument(
            "--ai-eval-out", help="Write candidate triage evaluation rows (JSON) for staff review"
        )
        parser.add_argument(
            "--fail-on-findings", action="store_true", help="Exit 1 when a dialogue fails"
        )
        parser.add_argument(
            "--writer",
            choices=["templates", "model"],
            default="templates",
            help="Who writes the requester's texts: templates (default) or a model (ADR 0011)",
        )
        parser.add_argument("--profile", type=int, help="AIProfile id for --writer model")
        parser.add_argument("--writer-temperature", type=float, default=0.7)
        parser.add_argument(
            "--max-model-calls", type=int, default=300, help="Calls to the model in one run"
        )

    def handle(self, *args, **o):
        try:
            graph = dialogue_sim.load_graph(o["graph"])
            writer = dialogue_writer.writer_for(
                o["writer"], o["profile"], o["writer_temperature"], o["max_model_calls"]
            )
            records, result = dialogue_sim.simulate(
                count=o["count"],
                seed=o["seed"],
                scenarios=o["scenario"] or None,
                graph=graph,
                only=o["dialogue"],
                writer=writer,
            )
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc
        if o["out"]:
            path = Path(o["out"])
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        if o["report"]:
            path = Path(o["report"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if o["ai_eval_out"]:
            name = f"triage-sim-{graph['code'].lower()}-seed{o['seed']}"
            rows = dialogue_sim.triage_candidates(records, name)
            path = Path(o["ai_eval_out"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        for line in dialogue_sim.summary_lines(result):
            self.stdout.write(line)
        if o["fail_on_findings"] and result["failed"]:
            raise CommandError(f"{result['failed']} dialogue(s) failed")
