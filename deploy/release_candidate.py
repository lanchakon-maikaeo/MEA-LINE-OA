"""Release candidate record (M13, E.8 step 6, E.10): one commit, one image, one set of AI versions
and policy, and the evidence each acceptance row needs (deploy/acceptance.json). Writes
release-manifest.json (no secrets) and acceptance-matrix.md next to the evidence.

    python3 -m uv run --locked --env-file .env python deploy/release_candidate.py \
        --name m13-rc1 --app-image <ref@sha256:…>

The evidence lives in reports/release/<name>/evidence (see PROJECT.md M13 for the commands).
A row with an external need never passes from local evidence, and a gate passes only when every
row passes and its signer has signed (signoff.json in the evidence folder). The verdict is
"ready" only then; otherwise the matrix says what is missing and who owns it.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MATRIX = ROOT / "deploy/acceptance.json"
THAI = {
    "pass": "ผ่าน",
    "fail": "ไม่ผ่าน",
    "missing": "ยังไม่มีหลักฐาน",
    "external": "รอหลักฐานภายนอก",
    "signoff": "รอผู้รับรองลงนาม",
}
LINE_ID = re.compile(r"\bU[0-9a-f]{32}\b")
# What the image is built from (Dockerfile and its COPY sources): a change here is a new
# release. Documents, the matrix and test tooling may change after the candidate commit.
RELEASED = re.compile(
    r"^(Dockerfile|pyproject\.toml|uv\.lock|manage\.py|synthetic-data\.json|"
    r"priority-engine-demo\.json|config/|operations/|workflows/n8n/|deploy/server/|"
    r"deploy/n8n/import\.sh|deploy/release_manifest\.py)"
)
SECRET_ENV = re.compile(r"SECRET|TOKEN|PASSWORD|_KEY$|DATABASE_URL")

# --- Rules (pure; tested in operations/tests/test_m13_release.py) -----------------------------

TEST_ID = re.compile(r"\(operations\.tests\.([\w.]+)\)")
OUTCOME = re.compile(r" \.\.\. (ok|FAIL|ERROR|skipped|expected failure|unexpected success)\b")


def test_results(log):
    """Outcome per test from `manage.py test -v 2` (a docstring line may come in between).
    Skipped tests are not passes (F.3)."""
    results, current = {}, None
    for line in log.splitlines():
        found = TEST_ID.search(line)
        if found:
            current = found.group(1)
        outcome = OUTCOME.search(line)
        if outcome and current:
            word = outcome.group(1)
            results[current] = {"ok": "pass", "skipped": "missing"}.get(word, "fail")
            current = None
    return results


def named_tests(matrix):
    rows = [*matrix["m13"], *(row for gate in matrix["gates"] for row in gate["rows"])]
    return sorted({test for row in rows for test in row.get("tests", [])})


def row_status(row, tests, evidence):
    local = [tests.get(test, "missing") for test in row.get("tests", [])]
    local += [evidence.get(key, {}).get("status", "missing") for key in row.get("evidence", [])]
    if "fail" in local:
        return "fail"
    if "missing" in local:
        return "missing"
    return "external" if row.get("external") else "pass"


def evaluate(matrix, tests, evidence, signoff):
    def rows(items):
        return [{**row, "status": row_status(row, tests, evidence)} for row in items]

    gates = []
    for gate in matrix["gates"]:
        gate_rows = rows(gate["rows"])
        statuses = {row["status"] for row in gate_rows}
        status = next((s for s in ("fail", "missing", "external") if s in statuses), None)
        if status is None:
            status = "pass" if signoff.get(gate["id"]) else "signoff"
        gates.append({**gate, "rows": gate_rows, "status": status})
    m13 = rows(matrix["m13"])
    ready = all(g["status"] == "pass" for g in gates) and all(r["status"] == "pass" for r in m13)
    return {"m13": m13, "gates": gates, "verdict": "ready" if ready else "not-ready"}


def leaks(text, secrets):
    """Names of secret values (and LINE user IDs) that appear in the text."""
    found = ["LINE user ID"] if LINE_ID.search(text) else []
    return found + [name for name, value in secrets.items() if len(value) >= 8 and value in text]


# --- Evidence -----------------------------------------------------------------------------------


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def collect_evidence(folder, tests, ai, image):
    evidence = {}

    def put(key, status, detail, *paths):
        files = [
            {"path": str(p.relative_to(folder)), "sha256": _sha(p)} for p in paths if p.exists()
        ]
        evidence[key] = {"status": status, "detail": detail, "files": files}

    ci = folder / "ci.json"
    if ci.exists():
        stages = _read(ci)
        logs = [folder / f"{stage}.log" for stage in stages]
        put("ci", "pass" if stages and not any(stages.values()) else "fail", stages, ci, *logs)
    log = folder / "test.log"
    if log.exists():
        text = log.read_text(encoding="utf-8", errors="replace")
        ran = re.search(r"Ran (\d+) tests?", text)
        ok = ran and re.search(r"^OK\b", text, re.M) and "FAILED" not in text
        put("tests", "pass" if ok else "fail", {"tests": int(ran.group(1)) if ran else 0}, log)
    log = folder / "acceptance-tests.log"
    if log.exists():
        counts = {s: list(tests.values()).count(s) for s in ("pass", "fail", "missing")}
        put("acceptance-tests", "fail" if counts["fail"] else "pass", counts, log)
    sims = sorted(folder.glob("sim-*.json"))
    if sims:
        runs = [_read(p) for p in sims]
        good = all(r["dialogues"] and not r["failed"] for r in runs)
        detail = [
            {
                "seed": r["seed"],
                "dialogues": r["dialogues"],
                "failed": r["failed"],
                "coverage": r["coverage"]["edges"],
            }
            for r in runs
        ]
        put("simulate-line", "pass" if good else "fail", detail, *sims)
    for key, task in (("ai-extract", "extract"), ("ai-answer", "answer")):
        path = folder / f"{key}.json"
        info = ai.get(task) or {}
        if path.exists() and info.get("active"):
            put(key, "pass" if info.get("evidence_current") else "fail", info, path)
    scan = folder / "image-scan.json"
    if scan.exists():
        codes = _read(scan)
        logs = sorted(folder.glob("trivy-*.log"))
        put(
            "image-scan",
            "pass" if codes and not any(codes.values()) else "fail",
            codes,
            scan,
            *logs,
        )
    history = folder / "history.jsonl"
    if history.exists():
        rows = [json.loads(line) for line in history.read_text().splitlines() if line.strip()]
        deployed = [r for r in rows if r["app_image"] == image and r["approval"] != "rollback"]
        ok = [r for r in deployed if r["result"] == "ok"]
        put("rehearsal-deploy", "pass" if ok else "fail", {"deploys": len(deployed)}, history)
        back = [r for r in rows if r["approval"] == "rollback"]
        forward = rows and rows[-1]["result"] == "ok" and rows[-1]["app_image"] == image
        good = any(r["result"] == "ok" for r in back) and forward
        put("rollback-rehearsal", "pass" if good else "fail", {"rollbacks": len(back)}, history)
    drill = folder / "restore-drill.json"
    if drill.exists():
        report = _read(drill)
        keys = ("rpo_seconds", "rto_seconds", "attachments_missing", "attachments_mismatched")
        put(
            "restore-drill",
            "pass" if report.get("ok") else "fail",
            {k: report.get(k) for k in keys},
            drill,
        )
    k6 = folder / "load" / "k6-summary.json"
    if k6.exists():
        summary, inside = _read(k6), folder / "load" / "inside.json"
        detail = {"profile": summary.get("profile"), "passed": summary.get("passed")}
        if inside.exists():
            detail["inside"] = {k: _read(inside).get(k) for k in ("stored", "turn_seconds")}
        good = summary.get("passed") and summary.get("profile") == "design"
        put("load-rehearsal", "pass" if good else "fail", detail, k6, inside)
    usage = folder / "line-usage.json"
    if usage.exists():
        put("line-usage", "pass", _read(usage)["recommendation"], usage)
    live = folder / "live-line.json"
    if live.exists():
        trace = _read(live)
        good = trace.get("journeys") and all(j.get("complete") for j in trace["journeys"])
        put("live-line", "pass" if good else "fail", trace.get("summary", {}), live)
    return evidence


# --- The candidate itself -------------------------------------------------------------------------


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


def release_changes(commit):
    """Files the image is built from that differ from the commit, committed or not."""
    names = git("diff", "--name-only", commit).splitlines()
    return sorted(name for name in names if RELEASED.search(name))


def image_release(image):
    out = subprocess.check_output(
        ["docker", "run", "--rm", "--entrypoint", "cat", image, "/app/release.json"], text=True
    )
    return json.loads(out)


def runtime():
    """AI versions, policy, options, answer bank and units from the database (Django)."""
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()
    from operations import ai, ai_answer, ai_eval, ai_triage, answers, options, policy
    from operations.models import AIConfigVersion, AuditEvent, ChatAnswer, Division

    source = ai_eval.source_digest()
    gates = {
        "extract": (ai_eval.gate_passed, ai.PROMPT_VERSION, ai.EVAL_GATE),
        "answer": (ai_answer.gate_passed, ai_answer.PROMPT_VERSION, ai_answer.EVAL_GATE),
        "triage": (ai_triage.gate_passed, ai_triage.PROMPT_VERSION, ai_triage.EVAL_GATE),
    }
    tasks = {}
    for task, _label in AIConfigVersion.Task.choices:
        version = ai.active_version(task)
        if version is None:
            stop = (
                AuditEvent.objects.filter(action="ai.task_stopped", data__task=task)
                .order_by("-created_at")
                .first()
            )
            tasks[task] = {
                "active": None,
                "stopped": stop
                and {
                    "version": stop.data.get("number"),
                    "at": stop.created_at.isoformat(timespec="seconds"),
                    "reason": stop.reason,
                },
            }
            continue
        check, prompt, gate = gates[task]
        report = version.eval_result or {}
        current = bool(version.eval_passed and check(report, version))
        if task == "extract":
            current = current and report.get("gate") == ai.EVAL_GATE
        tasks[task] = {
            "active": version.number,
            "pipeline": version.pipeline,
            "models": [e.profile.model_id for e in version.entries.order_by("position")],
            "prompt": prompt,
            "gate": gate,
            "config_digest": ai_eval.config_digest(version),
            "evaluated_source": (report.get("security") or {}).get("source_digest", ""),
            "evidence_current": current,
        }
    active = policy.active_policy()
    return {
        "source_digest": source,
        "ai": tasks,
        "policy": active
        and {
            "code": active.code,
            "digest": active.digest,
            "synthetic": active.is_synthetic,
            "approval_reference": active.approval_reference,
        },
        "options": {key: options.get(key) for key in options.OPTIONS},
        "answer_bank": {
            "digest": answers.bank_digest(),
            "approved": ChatAnswer.objects.filter(status=ChatAnswer.Status.APPROVED).count(),
        },
        "divisions": sorted(Division.objects.filter(is_active=True).values_list("code", flat=True)),
    }


def manifest(name, image, release, info, evidence, result, commit, dirty):
    return {
        "schema": 1,
        "name": name,
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "commit": commit,
        "dirty": dirty,
        "app_image": image,
        "image_release": release,
        "source_digest": info["source_digest"],
        "ai": info["ai"],
        "policy": info["policy"],
        "options": info["options"],
        "answer_bank": info["answer_bank"],
        "divisions": info["divisions"],
        "evidence": evidence,
        "gates": {g["id"]: g["status"] for g in result["gates"]},
        "m13": {r["id"]: r["status"] for r in result["m13"]},
        "verdict": result["verdict"],
    }


def markdown(data, result):
    lines = [
        f"# Release candidate {data['name']}",
        "",
        f"- commit `{data['commit']}`{' (มีไฟล์ที่ยังไม่ commit)' if data['dirty'] else ''}",
        f"- image `{data['app_image']}`",
        f"- source digest `{data['source_digest'][:12]}`",
        f"- ผล: **{'พร้อม' if result['verdict'] == 'ready' else 'ยังไม่พร้อม production'}**",
        "",
        "## AI",
        "",
        "| งาน | รุ่น | โมเดล | ผลประเมินผูกกับโค้ดนี้ |",
        "| --- | --- | --- | --- |",
    ]
    for task, ai in data["ai"].items():
        if ai["active"]:
            bound = "ใช่" if ai["evidence_current"] else "ไม่"
            lines.append(f"| {task} | v{ai['active']} | {' → '.join(ai['models'])} | {bound} |")
        else:
            stopped = ai.get("stopped") or {}
            note = f"หยุดใช้ v{stopped['version']}: {stopped['reason']}" if stopped else "ไม่ใช้"
            lines.append(f"| {task} | - | {note} | - |")
    lines += [
        "",
        "## Gates G01–G10",
        "",
        "| Gate | สถานะ | ผู้รับรอง | ยังขาด |",
        "| --- | --- | --- | --- |",
    ]
    for gate in result["gates"]:
        missing = "; ".join(
            row.get("external") or row["need"] for row in gate["rows"] if row["status"] != "pass"
        )
        lines.append(
            f"| {gate['id']} {gate['title']} | {THAI[gate['status']]} | {gate['signer']} | "
            f"{missing or '-'} |"
        )
    lines += [
        "",
        "## Acceptance ของ M13",
        "",
        "| ข้อ | สิ่งที่ต้องมี | สถานะ | หลักฐาน |",
        "| --- | --- | --- | --- |",
    ]
    for row in result["m13"]:
        count = len(row.get("tests", []))
        proof = [*row.get("evidence", []), *([f"เทสต์ {count} รายการ"] if count else [])]
        note = f": {row['external']}" if row["status"] == "external" else ""
        lines.append(
            f"| {row['id']} | {row['need']} | {THAI[row['status']]}{note} | {', '.join(proof)} |"
        )
    lines += ["", "## หลักฐาน", "", "| หลักฐาน | ผล | ไฟล์ (sha256) |", "| --- | --- | --- |"]
    for key, item in data["evidence"].items():
        files = "<br>".join(f"{f['path']} `{f['sha256'][:12]}`" for f in item["files"])
        lines.append(f"| {key} | {THAI[item['status']]} | {files} |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--name")
    parser.add_argument("--app-image")
    parser.add_argument("--evidence", help="default reports/release/<name>/evidence")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--list-tests", action="store_true", help="test labels the matrix names")
    args = parser.parse_args(argv)
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    if args.list_tests:
        print(" ".join(f"operations.tests.{test}" for test in named_tests(matrix)))
        return
    if not args.name or not args.app_image:
        parser.error("--name and --app-image are required")
    if not re.search(r"@sha256:[0-9a-f]{64}$", args.app_image):
        sys.exit("--app-image must be pinned by digest")
    folder = Path(args.evidence or ROOT / "reports/release" / args.name / "evidence")
    release = image_release(args.app_image)
    built = release.get("commit", "")
    if not built or built.endswith("-dirty"):
        sys.exit(f"the image was built from uncommitted files ({built or 'no commit'})")
    commit = git("rev-parse", f"{built}^{{commit}}")
    changed = release_changes(commit)
    if changed and not args.allow_dirty:
        sys.exit("files in the image differ from its commit: " + ", ".join(changed[:10]))
    dirty = bool(changed)
    log = folder / "acceptance-tests.log"
    tests = test_results(log.read_text(encoding="utf-8", errors="replace")) if log.exists() else {}
    info = runtime()
    evidence = collect_evidence(folder, tests, info["ai"], args.app_image)
    signoff_path = folder / "signoff.json"
    signoff = _read(signoff_path) if signoff_path.exists() else {}
    result = evaluate(matrix, tests, evidence, signoff)
    data = manifest(args.name, args.app_image, release, info, evidence, result, commit, dirty)
    data["documents_at"] = git("rev-parse", "HEAD")
    data["matrix_sha256"] = _sha(MATRIX)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    page = markdown(data, result)
    secrets = {k: v for k, v in os.environ.items() if SECRET_ENV.search(k)}
    found = leaks(text + page, secrets)
    if found:
        sys.exit(f"refusing to write: the record would contain {', '.join(sorted(set(found)))}")
    out = folder.parent
    (out / "release-manifest.json").write_text(text, encoding="utf-8")
    (out / "acceptance-matrix.md").write_text(page, encoding="utf-8")
    gates = " ".join(f"{g}:{s}" for g, s in data["gates"].items())
    print(f"{args.name}: {data['verdict']} ({gates}) → {out}")


if __name__ == "__main__":
    main()
