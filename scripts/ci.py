import json
import subprocess
import sys
from pathlib import Path

Path("reports").mkdir(exist_ok=True)
# security runs before the long test stages so a secret-scan hit shows within a minute.
stages = ["migrate", "check", "security", "test", "test-e2e"]
results = {}
for stage in stages:
    with Path(f"reports/{stage}.log").open("w") as log:
        result = subprocess.run(["make", stage], stdout=log, stderr=subprocess.STDOUT, check=False)
    results[stage] = result.returncode
    print(f"{stage}: {'PASS' if result.returncode == 0 else 'FAIL'}", flush=True)
# The tested tree lets the push to main reuse this run when the merge left the code unchanged.
tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], capture_output=True, text=True).stdout
Path("reports/ci.json").write_text(json.dumps({**results, "tree": tree.strip()}, indent=2))
sys.exit(int(any(results.values())))
