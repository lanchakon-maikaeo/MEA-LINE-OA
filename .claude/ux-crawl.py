# Crawl every page reachable from the console as one user and report broken ones (mea_ci only).
#   .claude/ux-preview.sh manage shell -c "exec(open('.claude/ux-crawl.py').read())"
import os
import re
from collections import deque
from urllib.parse import urljoin, urlsplit

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import setup_test_environment

assert connection.settings_dict["NAME"] == "mea_ci"
setup_test_environment()
username = os.environ.get("UX_USER", "ux-admin")
client = Client(HTTP_HOST="localhost")
client.force_login(get_user_model().objects.get(username=username))
SKIP = re.compile(
    r"^/(accounts/logout|static/|attachments/|health/|internal/|webhooks/)|\.csv$|/act/|/revision/|/files/"
)
seen, queue, problems = set(), deque(["/console/"]), []
pages = 0
while queue and pages < 400:
    path = queue.popleft()
    if path in seen:
        continue
    seen.add(path)
    response = client.get(path)
    pages += 1
    code = response.status_code
    if code in (301, 302):
        target = urlsplit(response["Location"]).path
        if target not in seen:
            queue.append(target)
        continue
    if code != 200:
        problems.append((code, path))
        continue
    if "text/html" not in response.get("Content-Type", ""):
        continue
    body = response.content.decode(errors="replace")
    for marker in (
        "Traceback",
        "TemplateSyntaxError",
        "VariableDoesNotExist",
        "None None",
        "{{",
        "{%",
    ):
        if marker in body:
            problems.append((f"text:{marker}", path))
    for href in re.findall(r'href="([^"#?]+)', body):
        href = urljoin(path, href)
        if href.startswith("/") and not SKIP.search(href) and href not in seen:
            queue.append(href)
print("pages", pages)
for p in problems:
    print("PROBLEM", *p)
