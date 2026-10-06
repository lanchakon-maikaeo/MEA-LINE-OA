import json
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
Path("reports/secrets.json").write_text(json.dumps(report, indent=2))
if report:
    print("Secret scanner found candidates; inspect reports/secrets.json (values are hashed).")
    sys.exit(1)
result = subprocess.run(
    [".venv/bin/pip-audit", "--format=json", "--output=reports/dependencies.json"], check=False
)
sys.exit(result.returncode)
