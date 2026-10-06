import json
import subprocess
import sys
from pathlib import Path

Path("reports").mkdir(exist_ok=True)
stages = ["migrate", "check", "test", "test-e2e", "security"]
results = {}
for stage in stages:
    with Path(f"reports/{stage}.log").open("w") as log:
        result = subprocess.run(["make", stage], stdout=log, stderr=subprocess.STDOUT, check=False)
    results[stage] = result.returncode
    print(f"{stage}: {'PASS' if result.returncode == 0 else 'FAIL'}", flush=True)
Path("reports/ci.json").write_text(json.dumps(results, indent=2))
sys.exit(int(any(results.values())))
