import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from operations import ai, ai_eval
from operations.models import AIConfigVersion


class Command(BaseCommand):
    help = "Evaluate an AI config version on a held-out set (calls the real API)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--config-version", type=int, help="Version (default: newest candidate)"
        )
        parser.add_argument(
            "--limit", type=int, help="Smoke run on the first N cases (never passes)"
        )
        parser.add_argument(
            "--dataset",
            default="gate",
            help="gate (security + application heldout-v4/redteam-v1/v2), "
            "v1-v4, redteam, redteam2 or path",
        )
        parser.add_argument("--primary-only", action="store_true", help="Comparison runs")
        parser.add_argument(
            "--ablate",
            action="append",
            choices=ai_eval.ABLATIONS,
            default=[],
            help="Harness comparison runs: switch one step off (never recorded)",
        )
        parser.add_argument(
            "--repeats", type=int, default=3, help="security3: 3 to 5 full application runs"
        )
        parser.add_argument("--report", help="Report path (default reports/ai-eval-v<N>[...].json)")

    def handle(
        self,
        *args,
        config_version=None,
        limit=None,
        dataset,
        primary_only,
        ablate,
        report=None,
        repeats=3,
        **o,
    ):
        versions = AIConfigVersion.objects.all()
        if config_version is not None:
            target = versions.filter(number=config_version).first()
        else:
            target = versions.filter(status=AIConfigVersion.Status.CANDIDATE).first()
            target = target or ai.active_version()
        if target is None:
            raise CommandError("No AI config version to evaluate (run import_ai_config first).")
        # Partial/model-only/ablation reports never decide activation.
        gate = dataset == "gate" and not limit and not primary_only and not ablate
        if dataset == "security3":
            if limit or primary_only or ablate:
                raise CommandError("security3 readiness requires full application runs.")
            result = ai_eval.run_readiness(target, repeats)
            out = Path(report or f"reports/ai-readiness-v{target.number}.json")
            out.parent.mkdir(exist_ok=True)
            out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
            for row in result["rounds"]:
                self.stdout.write(
                    json.dumps(
                        {"run": row["run"], "metrics": row["metrics"], "benign": row["benign"]}
                    )
                )
            self.stdout.write(
                f"Readiness: {result['readiness_passed']}; report: {out}; "
                "not recorded for activation"
            )
            return
        if gate:
            result = ai_eval.run_gate(target)
            ai.record_eval(target, result)
        elif dataset in ai_eval.DIALOGUE_LOCKS:
            # Diagnostic: the dialogues alone; only the whole gate is recorded for activation.
            row = ai_eval.run_dialogue(target, dataset)
            result = {"dataset": row["dataset"], "profiles": [row]}
        else:
            result = ai_eval.run(
                target,
                limit=limit,
                path=ai_eval.DATASETS.get(
                    dataset, ai_eval.DATASET if dataset == "gate" else dataset
                ),
                primary_only=primary_only,
                ablate=tuple(ablate),
            )
        name = Path(dataset).stem if dataset not in ai_eval.DATASETS else dataset
        suffix = "" if gate else f"-{name}{'-primary' if primary_only else ''}"
        suffix += "".join(f"-no-{a}" for a in ablate)
        out = Path(report or f"reports/ai-eval-v{target.number}{suffix}.json")
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        for p in result["profiles"]:
            self.stdout.write(summary_line(p, result["dataset"]))
        self.stdout.write(f"Report: {out} (recorded for activation: {'yes' if gate else 'no'})")


def summary_line(row, dataset):
    """One line per measured set. The typed sets (typed-v1, typed-v2) score SAP numbers and rows
    instead of catalog items, so their line names those (it crashed on them before 24 Oct 2569);
    menu-v1 scores where each draft goes (25 Oct 2569); dialogue-v1 scores whole dialogues."""
    m = row["metrics"]
    if "right_case_rate" in m:
        return (
            f"{row.get('dataset', dataset)[:10]:10} {row['role']:8} {row['model']:42} "
            f"dialogues {m['cases']} ai turns {m['ai_turns']} availability {m['availability']} "
            f"right {m['right_case_rate']} unconfirmed {m['unconfirmed_filings']} "
            f"off-menu {m['off_menu_replies']} repeated {m['repeated_replies']} "
            f"turns/guided {m['turns_vs_guided']} on point {m['replies_on_point']} "
            f"attacks obeyed {m['attacks_obeyed']}/{m['attack_cases']} "
            f"p95 {m['latency_p95_ms']} ms -> {'PASS' if row['passed'] else 'FAIL'}"
        )
    head = (
        f"{row.get('dataset', dataset)[:10]:10} {row['role']:8} {row['model']:42} "
        f"answered {m['answered']}/{m['cases']} json {m['json_valid']:.3f} "
    )
    if "division_accuracy" in m:
        body = (
            f"division {m['division_accuracy']} topic {m['topic_accuracy']} "
            f"numbers {m['numbers_exact']} split {m['split_accuracy']} ask {m['ask_accuracy']} "
            f"invented {m['invented_values']} "
            f"attacks obeyed {m['contaminated_responses']}/{m['attack_cases']} "
            f"p95 {m['latency_p95_ms']} ms"
        )
        return f"{head}{body} -> {'PASS' if row['passed'] else 'FAIL'}"
    head += f"intent {m['intent_accuracy']:.3f} "
    if "sap_exact" in m:
        body = (
            f"sap {m['sap_exact']} rows {m['rows_exact']} invented {m['invented_values']} "
            f"attacks obeyed {m['contaminated_responses']}/{m['attack_cases']} "
            f"p95 {m['latency_p95_ms']} ms"
        )
    else:
        body = (
            f"items {m['items_exact']} invented {m['invented_items']} "
            f"injection {m['injection_resisted']} p95 {m['latency_p95_ms']} ms "
            f"repairs {m['repairs_accepted']}/{m['repairs']} "
            f"format {m['format_repairs_accepted']}/{m['format_repairs']} "
            f"cut {m['inputs_cut']} hedges {m['hedges']}"
        )
    return f"{head}{body} -> {'PASS' if row['passed'] else 'FAIL'}"
