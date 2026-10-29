import json
import re
import subprocess
import sys
from pathlib import Path

from detect_secrets import SecretsCollection
from detect_secrets.settings import default_settings

Path("reports").mkdir(exist_ok=True)
files = (
    subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"])
    .decode()
    .split("\0")
)
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
Path("reports/secrets.json").write_text(json.dumps(report, indent=2))
if report:
    print("Secret scanner found candidates; inspect reports/secrets.json (values are hashed).")
    sys.exit(1)
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
