"""The pages after the LINE case card (ADR 0055): the rest of the web's case page, a page per
part, a part with nothing in it left out. They read what the case page reads (casepage.build
and the warehouse panel), so the two show the same.
"""

from django.utils import timezone

from operations import calendars, case_chat, casepage, units, warehouse
from operations.models import Appointment, CaseMilestone

GREEN = "#16804A"
ROWS = 12  # rows on a page
WIDTH = 220  # characters of a row


def _when(at):
    if at is None:
        return ""
    local = timezone.localtime(at)
    return f"{units.thai_date(local.date())} {local:%H:%M} น."


def _bubble(title, rows):
    rows = [r[:WIDTH] for r in rows if r][:ROWS]
    return {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "12px",
            "backgroundColor": "#E8F5E9",
            "contents": [
                {"type": "text", "text": title, "weight": "bold", "color": "#1B5E20", "wrap": True}
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [{"type": "text", "text": r, "size": "sm", "wrap": True} for r in rows],
        },
    }


def _details(case, page):
    rows = []
    request = page.get("line_request")
    if request:
        if request.get("tile"):
            rows.append(f"บริการ: {request['tile']}")
        rows += [f"· {p}" for p in request.get("picks", [])]
        if request.get("number"):
            rows.append(f"{request.get('number_label') or 'เลขที่'}: {request['number']}")
        if request.get("details"):
            rows.append(request["details"])
    rows += [f"{label}: {value}" for label, value in page.get("texts", [])]
    for line in page["lines"].get("lines", []):
        name = line.get("item_name") or line.get("name") or line.get("item") or ""
        rows.append(f"{line.get('line_no', '')}. {name}".strip())
        rows += [f"  {t}" for t in line.get("typed", [])]
    return rows


def _warehouse(case):
    rows = []
    for m in case.milestones.select_related("recorded_by__staff_profile"):
        row = f"{warehouse.stage_label(case, m.stage)} {units.thai_date(m.on)}"
        if m.stage == CaseMilestone.Stage.KEYED and m.number:
            row += f" · {m.number}"
        if m.planned_on:
            row += f" · วันจ่าย {units.thai_date(m.planned_on)}"
        if m.via == "line":
            row += " · ทาง LINE"
        rows.append(row)
    for a in case.appointments.select_related("slot", "calendar").exclude(
        status=Appointment.Status.CANCELLED
    ):
        when = calendars.label(a.slot) if a.slot else "ยังไม่มีช่วงเวลา"
        row = f"นัด{a.calendar.name}: {when} · {a.get_status_display()}"
        if a.method and a.takes_method:
            row += f" · {warehouse.method_label(case, a.method)}"
        rows.append(row)
    return rows


def _results(case, page):
    rows = []
    problem = page.get("problem") or {}
    resolution = problem.get("resolution")
    if resolution:
        rows.append(f"ผล: {resolution['category_label']}")
        rows.append(resolution["message"])
    rows += [f"โต้แย้ง: {d['reason']}" for d in problem.get("disputes", [])]
    for d in page.get("decisions", []):
        by = f" · {d['by']}" if d.get("by") else ""
        rows.append(f"{d['what']}{by}" + (f" · {_when(d['at'])}" if d.get("at") else ""))
    return rows


def _clocks(page):
    rows = []
    for c in page.get("clocks", []):
        state = "พักไว้" if c["paused"] else "เกินแล้ว" if c["overdue"] else "ครบ"
        rows.append(f"{c['label']} · กอง {c['division']} · {state} {_when(c['due_at'])}")
    return rows


def _children(page):
    return [
        f"งานย่อย {w.seq} · {w.division.code} · {w.get_status_display()}\n{w.task[:150]}"
        for w in page.get("children", [])
    ]


def _files(page):
    return [
        f"· {a.declared_name or a.content_type or 'ไฟล์'} · {_when(a.created_at)}"
        for a in page.get("attachments", [])
    ]


def _messages(case):
    said = [i for i in case_chat.thread(case) if i["who"] == "requester" and not i["faint"]]
    return [f"{_when(i['at'])}\n{i['text'][:200]}" for i in said[-5:]]


def _timeline(page):
    rows = []
    for e in page.get("timeline", [])[-8:][::-1]:
        who = f" · {e['who']}" if e.get("who") else ""
        via = f" · {e['via']}" if e.get("via") else ""
        rows.append(f"{_when(e['at'])}\n{e['what']}{who}{via}")
    return rows


def pages(case):
    """The bubbles after the card, at most eleven (a carousel holds twelve)."""
    page = casepage.build(case)
    parts = [
        ("รายละเอียดเรื่อง", _details(case, page)),
        ("งานคลังและนัด", _warehouse(case)),
        ("ผลและการตัดสิน", _results(case, page)),
        ("กรอบเวลา", _clocks(page)),
        ("งานย่อย", _children(page)),
        ("ไฟล์แนบ", _files(page)),
        ("ข้อความล่าสุดจากผู้แจ้ง", _messages(case)),
        ("ความเคลื่อนไหวล่าสุด", _timeline(page)),
    ]
    return [_bubble(f"{title} · {case.number}", rows) for title, rows in parts if rows][:11]
