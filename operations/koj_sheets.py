"""Read a private Google Sheet with a server-side, read-only service account."""

import json
import os
from urllib.parse import quote

from django.core.exceptions import ValidationError


def _credentials(service_account):
    """The read-only key the host mounts (deploy/e5-move.sh google-key); no file, a clear error."""
    path = os.environ.get("KOJ_GOOGLE_CREDENTIALS_FILE", "")
    if not path:
        raise ValidationError("ยังไม่ได้ตั้งค่า KOJ_GOOGLE_CREDENTIALS_FILE บนเซิร์ฟเวอร์")
    try:
        return service_account.Credentials.from_service_account_file(
            path, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
        )
    except FileNotFoundError as exc:
        raise ValidationError("ยังไม่มีไฟล์กุญแจ Google สำหรับอ่านชีทบนเซิร์ฟเวอร์") from exc


def fetch(source):
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account

    from operations.koj_tracking import check_source

    check_source(source)
    credentials = _credentials(service_account)
    # The endpoint is fixed; administrators select only a spreadsheet ID and tab, not a URL.
    cell_range = "'" + source.sheet_name.replace("'", "''") + "'!A:ZZ"
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{source.spreadsheet_id}/values/" + quote(
        cell_range, safe=""
    )
    with AuthorizedSession(credentials, refresh_timeout=20) as session:
        with session.get(
            url,
            params={"valueRenderOption": "FORMATTED_VALUE"},
            timeout=20,
            allow_redirects=False,
            stream=True,
        ) as response:
            if response.status_code != 200:
                raise ValidationError("อ่านชีทไม่สำเร็จ ตรวจสิทธิ์อ่านของ service account และชื่อแท็บ")
            body = bytearray()
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body) > 20_000_000:
                    raise ValidationError("ข้อมูลชีทเกิน 20 MB กรุณาแยกแหล่งข้อมูล")
            return json.loads(body).get("values", [])


def read_columns(spreadsheet_id, sheet_name, columns, first_row=1):
    """Only the named columns of one tab, from first_row down (ADR 0027: ค้นในชีท reads a number
    column and a status column, never the rest of the row), as one list per column."""
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account

    credentials = _credentials(service_account)
    tab = "'" + sheet_name.replace("'", "''") + "'"
    ranges = [f"{tab}!{c}{first_row}:{c}" for c in columns]
    url = (
        "https://sheets.googleapis.com/v4/spreadsheets/"
        + quote(spreadsheet_id, safe="")
        + "/values:batchGet"
    )
    with AuthorizedSession(credentials, refresh_timeout=20) as session:
        with session.get(
            url,
            params={
                "ranges": ranges,
                "majorDimension": "COLUMNS",
                "valueRenderOption": "FORMATTED_VALUE",
            },
            timeout=20,
            allow_redirects=False,
            stream=True,
        ) as response:
            if response.status_code != 200:
                raise ValidationError("อ่านชีทไม่สำเร็จ ตรวจรหัสชีท ชื่อแท็บ และสิทธิ์อ่านของ service account")
            body = bytearray()
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body) > 20_000_000:
                    raise ValidationError("ข้อมูลชีทเกิน 20 MB")
    found = json.loads(body).get("valueRanges", [])
    lists = [(r.get("values") or [[]])[0] for r in found]
    return lists + [[] for _ in range(len(columns) - len(lists))]
