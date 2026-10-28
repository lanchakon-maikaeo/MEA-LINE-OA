"""ขั้นที่ผู้แจ้งเห็น and ข้อความแจ้งสถานะ (ADR 0028, grill สถานะแบบ ServiceNow 28 ต.ค. 2569).

The requester reads a case in four steps with its real status as a second line; which step an
open status shows is an admin option (options.STEP_PREFIX). A status notice is the text staff
write to the requester when a case changes status. It rides on the Decision (data["notice"]),
or is the reason or message an action already shows the requester, so there is one history.
Nothing is pushed: notices are read in เรื่องของฉัน or at the requester's next message.
"""

from operations import options
from operations.models import Case

S = Case.Status

STEPS = ("รับเรื่องแล้ว", "กำลังดำเนินการ", "รอข้อมูลจากคุณ", "เสร็จแล้ว")
INDEX = {"received": 0, "working": 1, "need_you": 2, "done_confirm": 3, "done": 3}
LABEL = {**{key: STEPS[i] for key, i in INDEX.items()}, "done_confirm": "เสร็จแล้ว รอคุณยืนยัน"}
NEEDS_NOTICE = {"need_you", "done_confirm", "done"}
# Q14: actions whose own text already reaches the requester; it counts as their notice.
REASON_IS_NOTICE = {"request_info", "decline", "deny_approval", "cancel", "reopen"}
MESSAGE_IS_NOTICE = {"resolve"}
# No notice is required where the requester already knows the outcome: goods flows tell them
# themselves (the issue day, the receipt to confirm, what came back), an answer recorded for the
# district is their own word, and child work ends inside the case.
OWN_OUTCOME = {
    "allocate",
    "issue",
    "confirm",
    "dispute_resolution",
    "resolve_discrepancy",
    "terminate_outstanding",
    "receive",
    "inspect",
    "decide_inspection",
    "resolve_return_discrepancy",
    "terminate_return",
    "accept_child_work",
    "finish_child_work",
}


def key(status):
    """received, working, need_you, done_confirm, done, or None for rejected and cancelled."""
    if status == S.CLOSED:
        return "done"
    option = options.STEP_PREFIX + str(status)
    if status in (S.REJECTED, S.CANCELLED) or option not in options.OPTIONS:
        return None
    return options.get(option)


def label(case):
    """The step for the list in เรื่องของฉัน; a rejected or cancelled case shows its status."""
    step = key(case.status)
    return LABEL[step] if step else case.get_status_display()


def bar(status):
    step = key(status)
    if step is None:
        return ""
    return "".join("●" if i <= INDEX[step] else "○" for i in range(len(STEPS)))


def notice_of(action, payload, reason):
    """The text the requester reads for this decision ('' when there is none)."""
    text = str(payload.get("notice") or "").strip()
    if text:
        return text
    if action in REASON_IS_NOTICE:
        return reason
    if action in MESSAGE_IS_NOTICE:
        return str(payload.get("message") or "").strip()
    return ""


def notice_required(action, from_status, to_status):
    """Q3: staff moving a case into รอข้อมูลจากคุณ or เสร็จแล้ว write to the requester (the
    requester's own steps never need one)."""
    if to_status == from_status or to_status not in S.values or key(to_status) not in NEEDS_NOTICE:
        return False
    return action not in OWN_OUTCOME


def notices(case):
    """The case's status notices, newest first: [(decision, text)]."""
    rows = case.decisions.exclude(data__notice__isnull=True).exclude(data__notice="")
    return [(d, d.data["notice"]) for d in rows.select_related("actor").order_by("-id")]
