import argparse
import json
from urllib.request import urlopen

parser = argparse.ArgumentParser()
parser.add_argument("base_url", help="Explicit local/staging base URL; read-only health checks")
args = parser.parse_args()
for route in ("/health/live/", "/health/ready/"):
    with urlopen(args.base_url.rstrip("/") + route, timeout=5) as response:
        assert response.status == 200
        assert json.load(response) == {"status": "ok"}
    print(f"PASS {route}")
