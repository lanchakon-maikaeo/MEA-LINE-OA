"""Minimal LINE Messaging API client (M03): reply and content download only.

Push, quota and delivery records belong to M04. Calls never run inside a database transaction.
"""

import json
import re
import urllib.error
import urllib.request

from django.conf import settings

MESSAGE_ID = re.compile(r"^\d{1,32}$")


class LineApiError(Exception):
    def __init__(self, status, detail=""):
        super().__init__(f"LINE API {status}: {detail}")
        self.status = status
        self.detail = detail


class ContentTooLarge(Exception):
    pass


def _request(url, data=None, timeout=10, content_type=None, method=None):
    headers = {"Authorization": f"Bearer {settings.LINE_CHANNEL_ACCESS_TOKEN}"}
    if data is not None and content_type is None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(data, ensure_ascii=False).encode()
    elif content_type is not None:
        headers["Content-Type"] = content_type
    return urllib.request.urlopen(  # noqa: S310 - fixed https LINE endpoints only
        urllib.request.Request(url, data=data, headers=headers, method=method), timeout=timeout
    )


def call(path, data=None, *, base=None, content_type=None, method=None):
    """Admin-time API call (rich menu setup); returns parsed JSON or {}."""
    try:
        with _request(
            f"{base or settings.LINE_API_BASE}{path}",
            data,
            content_type=content_type,
            method=method,
            timeout=30,
        ) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise LineApiError(exc.code, exc.read(300).decode(errors="replace")) from exc
    return json.loads(body) if body else {}


def reply(reply_token, messages):
    try:
        with _request(
            f"{settings.LINE_API_BASE}/v2/bot/message/reply",
            {"replyToken": reply_token, "messages": messages[:5]},
        ):
            return
    except urllib.error.HTTPError as exc:
        raise LineApiError(exc.code, exc.read(300).decode(errors="replace")) from exc


def fetch_content(message_id, max_bytes):
    """Download a user's file by message ID; never follows URLs the user sent."""
    if not MESSAGE_ID.match(str(message_id)):
        raise LineApiError(400, "Invalid message ID.")
    try:
        with _request(
            f"{settings.LINE_DATA_API_BASE}/v2/bot/message/{message_id}/content", timeout=30
        ) as response:
            data = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise LineApiError(exc.code, exc.reason) from exc
    if len(data) > max_bytes:
        raise ContentTooLarge
    return data
