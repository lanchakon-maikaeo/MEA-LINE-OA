import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from detect_secrets import SecretsCollection
from detect_secrets.settings import default_settings

# --secrets-only skips pip-audit; --staged scans only the files staged for commit, which is what
# the .githooks/pre-commit hook runs so a flagged string stops the commit, not CI 30 minutes later.
parser = argparse.ArgumentParser()
parser.add_argument("--secrets-only", action="store_true")
parser.add_argument("--staged", action="store_true")
args = parser.parse_args()

Path("reports").mkdir(exist_ok=True)
listing = (
    ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"]
    if args.staged
    else ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
)
files = subprocess.check_output(listing).decode().split("\0")
with default_settings():
    collection = SecretsCollection()
    for filename in files:
        if filename and filename != "uv.lock" and Path(filename).is_file():
            collection.scan_file(filename)
    report = collection.json()
# A release record (M13) is made of digests: the commit, image and file sha256s are hex by
# design, not secrets, and deploy/release_candidate.py refuses to write any secret value or
# LINE ID into it. Only the hex detector is skipped there; every other detector still runs.
RECORD = re.compile(r"^Docs/release/[^/]+/release-manifest\.json$")
ASSET_DIGESTS = {"operations/line_assets/menu-manifest.json"}
report = {
    path: kept
    for path, found in report.items()
    if (
        kept := [
            f
            for f in found
            if not (
                (RECORD.match(path) or path in ASSET_DIGESTS)
                and f["type"] == "Hex High Entropy String"
            )
        ]
    )
}
if not args.staged:
    Path("reports/secrets.json").write_text(json.dumps(report, indent=2))
if report:
    print("Secret scanner found candidates:")
    for path, found in report.items():
        for f in found:
            print(f"  {path}:{f['line_number']}  {f['type']}")
    print(
        "A real secret goes in .env, never in the repo. A value that is not one (a Google Sheets"
        " ID, a public digest) gets `# pragma: allowlist secret` at the end of its line."
    )
    sys.exit(1)
if args.secrets_only:
    sys.exit(0)
result = subprocess.run(
    [
        sys.executable,
        "-m",
        "pip_audit",
        "--format=json",
        "--output=reports/dependencies.json",
    ],
    check=False,
)
sys.exit(result.returncode)
