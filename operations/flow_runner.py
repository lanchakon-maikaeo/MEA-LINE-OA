"""ตัวรัน flow (ADR 0023): the LINE chat follows a flow an admin published in the admin web.

A tap on the rich menu tile a published flow names (LineFlow.tile) starts a draft in step
"flow". The draft keeps the version it started with (Q43), the box it waits at and the values
the requester gave. Boxes that talk (message, ask, summary, book) wait for a tap or typed text;
the others run at once: a branch follows its first button whose condition holds
("set_kind=ct_vt|meter_ct_vt"), create_case files the case from the values and result shows
the real case number. A button may set values ("case_kind=withdrawal; group=meter").
Questions use the deterministic rules of typed_input; nothing here reads text with AI, so a
flow stays guided until the AI is evaluated on its topics (Q44, Q57).

grill กคว. รอบใหม่ (27 ต.ค. 2569): a เลขติดตาม has one of four kinds chosen before typing;
withdrawals and returns that cite one list no items (they live in SAP), so the requester types
รายละเอียด instead of rows; the summary offers แก้ไข (retype one answer) and, once filed,
ส่งใบถัดไป starts the same path again at the number.

ADR 0027: a ค้นในชีท box waits for typed numbers, answers from a sheet's status column and
waits again, with ถามเลข UI อื่น and จบ, until the requester ends it or stays quiet 30 minutes.
ADR 0029: a button of the box (ส่งให้ คม. ตรวจ) is offered when a number was not found, and
taken at once while the box's sheet is not set up; what was typed goes on to the case.
"""

from datetime import timedelta

from django.utils import timezone

from operations import conversation, services, typed_input, typed_lines, workflow
from operations.contracts import CommandFailed
from operations.models import (
    Appointment,
    Attachment,
    Case,
    CaseLine,
    ConversationDraft,
    LineFlowVersion,
)

D = ConversationDraft.Status
STEP = "flow"
# A choice of the เลขติดตาม box: (sap_kind, the case kind it implies).
TRACKING_CHOICES = {
    "เลขที่ใบเบิก": ("slip", Case.Kind.WITHDRAWAL),
    "เลขที่ใบส่งคืน": ("return_slip", Case.Kind.RETURN),
    "ใบสั่งงานจาก SAP": ("work_order", ""),
    "ใบสั่งงาน SAP": ("work_order", ""),  # drafts of the first version
    "หมายเลขเครื่องวัดฯ": ("meter", ""),
    "โครงการ": ("project", ""),
    "เลขที่ M/N": ("memo", Case.Kind.WITHDRAWAL),  # grill กคอ. Q20
}
TRACKING_LABELS = {
    "slip": "เลขที่ใบเบิก",
    "return_slip": "เลขที่ใบส่งคืน",
    "work_order": "ใบสั่งงานจาก SAP",
    "meter": "หมายเลขเครื่องวัดฯ",
    "project": "โครงการ",
    "memo": "เลขที่ M/N",
}
GROUP_LABELS = {"meter": "มิเตอร์", "ct": "CT", "vt": "VT", "other": "อุปกรณ์อื่น"}
VALUE_LABELS = {
    "equipment_name": "อุปกรณ์",
    "meter_numbers": "หมายเลขเครื่องวัดฯ",
    "district_name": "เขต",
    "ct_size": "ขนาด CT",
    "vt_size": "ขนาด VT",
    "details": "รายละเอียด",
    "mea_numbers": "MEA No.",
    "description": "รายละเอียด",
    "impact": "ข้อมูลเพิ่มเติม",
    "purpose": "วัตถุประสงค์",
    "condition_reason": "สภาพและเหตุผลที่ส่งคืน",
    "tools": "เครื่องมือ",
}
# What แก้ไข calls each typed answer (a quick reply label fits 20 characters).
EDIT_LABELS = {
    "tracking": "เลขติดตาม",
    "sap_document": "เลข SAP งานซ่อม",
    "details": "รายละเอียด",
    "description": "รายละเอียด",
    "rows.meter": "มิเตอร์",
    "ct_size": "ขนาด CT",
    "vt_size": "ขนาด VT",
    "multiplier": "ตัวคูณ",
    "mea_numbers": "MEA No.",
    "equipment_name": "ชื่ออุปกรณ์",
    "meter_numbers": "หมายเลขเครื่องวัดฯ",
    "district": "เขต",
    "purpose": "วัตถุประสงค์",
    "condition_reason": "สภาพและเหตุผล",
    "impact": "ข้อมูลเพิ่มเติม",
}
SET_LABELS = {
    "meter_ct": "มิเตอร์ + CT",
    "meter_ct_vt": "มิเตอร์ + CT + VT",
    "ct": "CT อย่างเดียว",
    "ct_vt": "CT + VT",
}
WITH_VT = {"meter_ct_vt", "ct_vt"}
EXTRA = (
    "group",
    "set_kind",
    "ct_size",
    "vt_size",
    "multiplier_check",
    "mea_numbers",
    "warnings",
    "staff_notes",
    "appointment",
    "no_number",
)
HOLD = timedelta(minutes=30)  # Q8: a chosen slot is kept this long for the summary
MAX_HOPS = 40


# --- The graph ------------------------------------------------------------------------------


def published_for(label):
    """The published version of the flow a tile with this label starts, or None."""
    if not label:
        return None
    return (
        LineFlowVersion.objects.filter(status=LineFlowVersion.Status.PUBLISHED, flow__tile=label)
        .select_related("flow")
        .first()
    )


def is_flow(draft):
    return draft is not None and draft.step == STEP and bool(draft.data.get("flow"))


def _state(draft):
    return dict(draft.data.get("flow") or {})


def _version(draft):
    return LineFlowVersion.objects.select_related("flow").get(pk=_state(draft)["version"])


def node(graph, nid):
    return next((n for n in graph["nodes"] if n["id"] == nid), None)


def follow(graph, nid, port=None):
    """The box after box nid (or after one of its buttons)."""
    return next(
        (e["to"] for e in graph["edges"] if e["from"] == nid and (e["port"] or None) == port), None
    )


def assign(values, text):
    """values with "key=value; key=value" applied."""
    for part in str(text or "").split(";"):
        if "=" in part:
            key, value = (x.strip() for x in part.split("=", 1))
            if key:
                values[key] = value
    return values


def holds(values, text):
    """Whether every "key=a|b" of text holds for values ("" never holds)."""
    parts = [p for p in str(text or "").split(";") if "=" in p]
    for part in parts:
        key, allowed = (x.strip() for x in part.split("=", 1))
        if str(values.get(key, "")) not in {a.strip() for a in allowed.split("|")}:
            return False
    return bool(parts)


def branch_target(graph, box, values):
    port = next((b["id"] for b in box["buttons"] if holds(values, b.get("set"))), None)
    return follow(graph, box["id"], port) if port else follow(graph, box["id"])


def _does(button, what):
    """A summary button's own job: do=edit (แก้ไข) or do=restart (กรอกใหม่)."""
    found = assign({}, button.get("set")).get("do")
    if found:
        return found == what
    return {"edit": "แก้ไข", "restart": "กรอกใหม่"}[what] in button["label"]


# --- Starting, moving, showing ---------------------------------------------------------------


def start(turn, version, node_id=None, values=None):
    """Start this published version in a new, active draft (at node_id with values when the
    requester sends the next slip of the same path)."""
    entry = next(n for n in version.graph["nodes"] if n["kind"] == "entry")
    first = node(version.graph, node_id or follow(version.graph, entry["id"]))
    if first and first["kind"] == "koj_tracking":
        from operations import koj_chat

        return koj_chat.from_flow(turn, first.get("config", {}))
    draft, node_id = _open(turn, version, entry, node_id, values)
    return _go(turn, draft, node_id)


def _open(turn, version, entry, node_id=None, values=None):
    """A new active draft of this version, at node_id (else the box after the entry)."""
    draft = conversation._new_draft(turn)
    state = _fresh(version, entry)
    if node_id and node(version.graph, node_id):
        state["values"] = dict(values or {})
    else:
        node_id = follow(version.graph, entry["id"])
    state["node"] = node_id
    data = {
        "lines": [],
        "fields": {},
        **({"anonymous": True} if draft.data.get("anonymous") else {}),
        "flow": state,
    }
    conversation._touch(draft, turn.now, kind="", step=STEP, data=data)
    return draft, node_id


def again(turn, value):
    """ส่งใบถัดไป (Q7): the flow's published version, back at the เลขติดตาม box."""
    old = LineFlowVersion.objects.select_related("flow").filter(pk=value.get("version")).first()
    version = published_for(old.flow.tile) if old else None
    if version is None:
        return conversation._menu(turn, "เมนูนี้เปลี่ยนไปแล้ว เลือกจากเมนูด้านล่างได้")
    return start(turn, version, value.get("node"), value.get("values"))


def _fresh(version, entry):
    return {"version": version.pk, "node": entry["id"], "values": {}, "rows": [], "path": []}


def _save(turn, draft, state):
    conversation._touch(draft, turn.now, data=dict(draft.data, flow=state))


def _go(turn, draft, nid, said=""):
    """Move to box nid, running the boxes that wait for nothing, and show the one that waits."""
    graph = _version(draft).graph
    state = _state(draft)
    if said:
        turn.say(said)
    for _ in range(MAX_HOPS):
        box = node(graph, nid) if nid else None
        if box is None:
            return _finish(turn, draft, "flow นี้ยังไม่มีขั้นถัดไป กรุณาติดต่อ ฝอจ.")
        kind = box["kind"]
        if kind == "branch":
            nid = branch_target(graph, box, state["values"])
            continue
        if kind in ("ai_read", "entry", "note"):
            nid = follow(graph, box["id"])
            continue
        if kind == "sheet_lookup" and (
            conversation._problems_only(turn) or draft.data.get("anonymous")
        ):
            return _finish(turn, draft, "ต้องยืนยันตัวตนพนักงานก่อนตรวจสอบสถานะ")
        if kind == "sheet_lookup" and box["config"].get("by") == "employee":
            return _finish(turn, draft, _own_reports(turn, box))
        state["node"] = box["id"]
        if kind == "summary":
            state["summary"] = box["id"]
        if kind == "ask" and box["config"].get("choices") and "base" not in state:
            # Where ส่งใบถัดไป starts again, with what the taps before it set.
            state["base"] = {"node": box["id"], "values": dict(state["values"])}
        _save(turn, draft, state)
        if kind == "create_case":
            return _create(turn, draft, graph, box)
        if kind == "find_case":
            _finish(turn, draft)
            return conversation._my_cases(turn)
        if kind == "koj_tracking":
            from operations import koj_chat

            _finish(turn, draft)
            return koj_chat.from_flow(turn, {**state["values"], **box["config"]})
        if kind == "web" and box["config"].get("url"):
            # The box's name is for the editor; LINE gets the link and any line above it
            # (เจษ's SMG flow answers with the link alone, 28 Oct 2569).
            return _finish(
                turn, draft, "\n".join(x for x in (box["text"], box["config"]["url"]) if x)
            )
        if kind in ("result", "limit", "web", "handoff"):
            text = "\n".join(x for x in (box["title"], box["text"]) if x)
            return _finish(turn, draft, text)
        return conversation._ask(turn, draft)
    return _finish(turn, draft, "flow นี้วนไม่จบ กรุณาติดต่อ ฝอจ.")


def _choices(box):
    return [c.strip() for c in (box["config"].get("choices") or "").split("|") if c.strip()]


def render(turn, draft):
    """Show the box the draft waits at (conversation._ask adds ยกเลิกร่าง)."""
    graph, state = _version(draft).graph, _state(draft)
    box = node(graph, state.get("node"))
    if box is None:
        return turn.say("flow นี้เปลี่ยนไปแล้ว กรุณาเริ่มใหม่จากเมนู")
    b, kind, quick = turn.button, box["kind"], []
    ttl = conversation.CONFIRM_TTL if kind == "summary" else conversation.BUTTON_TTL
    if kind == "ask":
        picked = state.get("choice", {}).get(box["id"])
        if _choices(box) and not picked:
            quick = [
                (*b(c, "flow", {"node": box["id"], "choice": c}, draft), c) for c in _choices(box)
            ]
            # A button of the box (ไม่มีเลข, grill กคอ. Q17) is offered beside the choices.
            for button in box["buttons"]:
                value = {"node": box["id"], "button": button["id"]}
                quick.append((*b(button["label"], "flow", value, draft), button["label"]))
            return turn.say(f"{box['title']}\nเลือกก่อนว่าจะติดตามด้วยอะไร", quick)
        title = f"{box['title']} ({picked})" if picked else box["title"]
        text = "\n".join(x for x in (title, box["text"]) if x)
        if state.get("editing"):
            text = "แก้ " + text
    elif kind == "summary":
        text = summary(draft, graph, box)
    elif kind == "sheet_lookup":
        text = box["text"] or box["title"]  # the box's name is for the editor (เจษ's flow)
    elif kind == "book":
        from operations import calendars  # local: calendars builds on this module

        slots = calendars.free_slots(box["config"].get("calendar", ""))
        if not slots:
            text = f"{box['title']}\nตอนนี้ยังไม่มีช่วงเวลาว่าง เจ้าหน้าที่จะติดต่อกลับเพื่อนัด"
            quick = [(*b("ไปต่อ", "flow", {"node": box["id"], "slot": ""}, draft), "ไปต่อ")]
        else:
            text = "\n".join(x for x in (box["title"], box["text"], "เลือกช่วงที่ต้องการ") if x)
            quick = [
                (
                    *b(calendars.short(s), "flow", {"node": box["id"], "slot": s.pk}, draft),
                    calendars.label(s),
                )
                for s in slots[: conversation.MAX_QUICK - 1]
            ]
        return turn.say(text, quick)
    else:
        text = "\n".join(x for x in (box["title"], box["text"]) if x)
    for button in box["buttons"]:
        value = {"node": box["id"], "button": button["id"]}
        label, data = b(button["label"], "flow", value, draft, ttl)
        quick.append((label, data, button.get("full") or button["label"]))
    turn.say(text, quick or None)


def summary(draft, graph, box):
    state = _state(draft)
    values = state["values"]
    lines = [box["title"]]
    confirm = next((b for b in box["buttons"] if follow(graph, box["id"], b["id"])), None)
    target = node(graph, follow(graph, box["id"], confirm["id"])) if confirm else None
    if target and target["config"].get("division"):
        lines.append(f"ส่งถึง: {target['config']['division']}")
    if values.get("set_kind"):
        lines.append(f"ชุด: {SET_LABELS.get(values['set_kind'], values['set_kind'])}")
    if values.get("tracking"):
        label = TRACKING_LABELS.get(values.get("sap_kind"), "เลขติดตาม")
        lines.append(f"{label}: {values['tracking']}")
    elif values.get("no_number"):
        lines.append("เลขติดตาม: ไม่มีเลข เจ้าหน้าที่หาเอกสารจากรายละเอียด")
    if values.get("sap_document"):
        lines.append(f"เลข SAP งานซ่อม: {values['sap_document']}")
    for row in state.get("rows", []):
        lines.append(row_text(row))
    for key in ("ct_size", "vt_size"):
        if values.get(key):
            lines.append(f"{VALUE_LABELS[key]}: {values[key]}")
    check = values.get("multiplier_check")
    if check:
        lines.append(
            f"ตัวคูณ: {check['given']} (ตรงกับที่คำนวณ)"
            if check["ok"]
            else f"ตัวคูณ: {check['given']} (คำนวณได้ {check['computed'] or 'ไม่ได้'} ให้เจ้าหน้าที่ตรวจ)"
        )
    for key in ("equipment_name", "meter_numbers", "district_name"):
        if values.get(key):
            value = values[key]
            lines.append(
                f"{VALUE_LABELS[key]}: {', '.join(value) if isinstance(value, list) else value}"
            )
    for key in (
        "tools",
        "details",
        "description",
        "purpose",
        "condition_reason",
        "impact",
        "mea_numbers",
    ):
        value = values.get(key)
        if value:
            lines.append(
                f"{VALUE_LABELS[key]}: {', '.join(value) if isinstance(value, list) else value}"
            )
    if values.get("appointment"):
        lines.append(f"ขอนัด: {values['appointment']['label']} (รอเจ้าหน้าที่ยืนยัน)")
    for warning in values.get("warnings", []):
        lines.append(f"หมายเหตุ: {warning}")
    lines.append("ข้อมูลถูกต้องกดยืนยันภายใน 30 นาที ถ้าพิมพ์ผิดกดแก้ไข")
    return "\n".join(lines)


def row_text(row):
    item = typed_lines.shared_item(row["group"])
    head = GROUP_LABELS.get(row["group"], row["group"])
    return f"{head} · {typed_lines.describe(row, item.unit if item else '')}"


# --- Answers ---------------------------------------------------------------------------------


def on_button(turn, draft, value):
    graph, state = _version(draft).graph, _state(draft)
    box = node(graph, value.get("node"))
    if box is None or box["id"] != state.get("node"):
        turn.say("ปุ่มนี้เป็นของขั้นก่อนหน้า ใช้คำถามล่าสุดแทน")
        return conversation._ask(turn, draft)
    values = state["values"] = dict(state["values"])
    if "choice" in value:
        sap_kind, case_kind = TRACKING_CHOICES.get(value["choice"], ("", ""))
        state["choice"] = {**state.get("choice", {}), box["id"]: value["choice"]}
        if box["config"].get("rule") in ("tracking", "sap_document"):
            values["sap_kind"] = sap_kind
            if case_kind and not values.get("case_kind"):
                values["case_kind"] = case_kind
        _save(turn, draft, state)
        return conversation._ask(turn, draft)
    if "slot" in value:
        from operations import calendars  # local: calendars builds on this module

        if value["slot"]:
            slot, problem = calendars.hold(value["slot"], draft)
            if problem:
                turn.say(problem)
                return conversation._ask(turn, draft)
            values["appointment"] = {
                "slot": slot.pk,
                "label": calendars.label(slot),
                "node": box["id"],
            }
        _save(turn, draft, state)
        return _go(turn, draft, follow(graph, box["id"]))
    if "edit" in value or "back" in value:
        return _edit(turn, draft, graph, state, value)
    if "lookup" in value:  # ถามเครื่องอื่น / จบ after an answer (ADR 0027)
        if value["lookup"] == "end":
            return _finish(turn, draft, "จบการตรวจสอบแล้ว เลือกเมนูด้านล่างได้เลย")
        _save(turn, draft, state)
        return conversation._ask(turn, draft)
    button = next((b for b in box["buttons"] if b["id"] == value.get("button")), None)
    if button is None:
        return conversation._ask(turn, draft)
    target = follow(graph, box["id"], button["id"])
    if box["kind"] == "summary":
        if target is None and _does(button, "edit"):
            return _edit_menu(turn, draft, graph, state, box)
        if target is None and _does(button, "restart"):
            return restart(turn, draft)
    else:
        assign(values, button.get("set"))
        if values.get("case_kind") in (Case.Kind.WITHDRAWAL, Case.Kind.RETURN) and (
            conversation._problems_only(turn) or draft.data.get("anonymous")
        ):
            return _finish(turn, draft, "ต้องยืนยันตัวตนพนักงานก่อนเบิกหรือส่งคืนอุปกรณ์")
        if box["kind"] == "ask":  # ข้าม on a question that may be skipped
            values.pop(box["config"].get("field", ""), None)
            _answered(state, box)
            if state.get("editing"):
                return _back_to_summary(turn, draft, state)
        else:
            state["path"] = [*state.get("path", []), button["label"]][:4]
    if target is None:
        if button.get("note"):
            turn.say(button["note"])
        _save(turn, draft, state)
        return conversation._ask(turn, draft)
    _save(turn, draft, state)
    return _go(turn, draft, target)


def _answered(state, box):
    state["answered"] = [n for n in state.get("answered", []) if n != box["id"]] + [box["id"]]


def _edit_menu(turn, draft, graph, state, box):
    """แก้ไข (Q6): one button per typed answer; the chosen one is asked again."""
    quick = []
    for nid in state.get("answered", []):
        asked = node(graph, nid)
        if asked is None or asked["kind"] != "ask":
            continue
        field = asked["config"].get("field", "")
        label = EDIT_LABELS.get(field) or asked["title"][:20]
        if any(q[0] == label for q in quick):
            continue
        quick.append(
            (
                *turn.button(label, "flow", {"node": box["id"], "edit": nid}, draft),
                f"แก้{label}",
            )
        )
    quick.append(
        (*turn.button("กลับไปที่สรุป", "flow", {"node": box["id"], "back": 1}, draft), "กลับไปที่สรุป")
    )
    turn.say("ต้องการแก้ข้อไหน", quick[: conversation.MAX_QUICK])


def _edit(turn, draft, graph, state, value):
    asked = node(graph, value.get("edit")) if "edit" in value else None
    if asked is None or asked["kind"] != "ask":
        return conversation._ask(turn, draft)  # กลับไปที่สรุป
    state["editing"] = True
    state["node"] = asked["id"]
    if _choices(asked):
        state["choice"] = {k: v for k, v in state.get("choice", {}).items() if k != asked["id"]}
    _save(turn, draft, state)
    return conversation._ask(turn, draft)


def _back_to_summary(turn, draft, state, said=""):
    state["editing"] = False
    _save(turn, draft, state)
    return _go(turn, draft, state.get("summary"), said)


def restart(turn, draft):
    """กรอกใหม่: the flow from its first box (files already sent stay with the draft)."""
    version = _version(draft)
    entry = next(n for n in version.graph["nodes"] if n["kind"] == "entry")
    _save(turn, draft, _fresh(version, entry))
    return _go(turn, draft, follow(version.graph, entry["id"]), "เริ่มกรอกใหม่")


def on_text(turn, draft, text):
    graph, state = _version(draft).graph, _state(draft)
    box = node(graph, state.get("node"))
    if box is not None and box["kind"] == "sheet_lookup":
        return _lookup(turn, draft, state, box, text)
    if box is None or box["kind"] != "ask":
        turn.say("เลือกจากปุ่มด้านล่าง")
        return conversation._ask(turn, draft)
    config = box["config"]
    if _choices(box) and not state.get("choice", {}).get(box["id"]):
        turn.say("กดเลือกก่อนว่าจะติดตามด้วยอะไร")
        return conversation._ask(turn, draft)
    values = state["values"] = dict(state["values"])
    field, rule = config.get("field", ""), config.get("rule", "text")
    value, shown, problem = read(rule, field, text, values.get("sap_kind", ""))
    if not problem and rule == "sap_document" and not values.get("sap_kind"):
        problem = typed_lines.sap_in_use(value, turn.requester)
    if not problem and config.get("max_rows") and isinstance(value, list):
        if len(value) > int(config["max_rows"]):
            problem = f"ขั้นนี้พิมพ์ได้ไม่เกิน {config['max_rows']} บรรทัด"
    if problem:
        turn.say(problem)
        return conversation._ask(turn, draft)
    notes = [f"อ่านเป็น: {shown}"] if shown and shown != text.strip() else []
    if field.startswith("rows."):
        group = field.split(".", 1)[1]
        kept = state.get("rows", [])
        if state.get("editing"):
            kept = [r for r in kept if r["group"] != group]
        state["rows"] = [*kept, *value]
        warnings = meter_warnings(value)
        if warnings:
            values["warnings"] = [*values.get("warnings", []), *warnings]
            notes += warnings
    elif rule == "multiplier":
        values["multiplier"] = value
    else:
        values[field] = value
        if rule == "district":
            values[f"{field}_name"] = shown  # the code is kept, the name is shown
    if rule == "tracking":
        values.pop("no_number", None)  # a number typed after all
        notes += _tracking_notes(turn, values, value)
    if rule in ("multiplier", "size") and values.get("multiplier"):
        mismatch = _check_multiplier(values)
        if mismatch and (rule == "multiplier" or state.get("editing")):
            notes.append(mismatch)
    _answered(state, box)
    _save(turn, draft, state)
    if state.get("editing"):
        return _back_to_summary(turn, draft, state, "\n".join(notes))
    return _go(turn, draft, follow(graph, box["id"]), "\n".join(notes))


def _own_reports(turn, box):
    """ค้นในชีทด้วยรหัสพนักงาน (เจษ): the rows this verified employee reported, no typing."""
    from operations import employees, sheet_lookup

    source = sheet_lookup.source_for(box["config"].get("source", ""))
    if source is None:
        return "ตอนนี้ยังตรวจสอบสถานะจากชีทไม่ได้ กรุณาติดต่อ ศอค."
    requester = turn.requester
    if requester is None or employees.identity_state(requester) != "verified":
        return "ต้องยืนยันรหัสพนักงานก่อน จึงจะดูรายการที่แจ้งไว้ได้"
    employee_id = requester.employee.employee_id
    reports = sheet_lookup.own_reports(source, employee_id)
    return sheet_lookup.own_answer(reports, employee_id, sheet_lookup.stale_note(source, turn.now))


def _lookup(turn, draft, state, box, text):
    """ค้นในชีท (ADR 0027): numbers in, statuses out, then wait for the next numbers."""
    from operations import ai_sheet_status, sheet_lookup

    if turn.now - draft.updated_at > sheet_lookup.IDLE:
        # Quiet too long: this text is a new message, not a number for the old question.
        _finish(turn, draft)
        return conversation._free_text(turn, None, text)
    source = sheet_lookup.source_for(box["config"].get("source", ""))
    handoff = next(
        (b for b in box["buttons"] if follow(_version(draft).graph, box["id"], b["id"])), None
    )
    if source is None and handoff is not None:
        # ADR 0029: no sheet yet, so what was typed goes to the division as a case.
        if not sheet_lookup.codes_in(text):
            turn.say("ยังไม่เห็นหมายเลขอุปกรณ์ในข้อความ พิมพ์ ชื่อเครื่องมือ, หมายเลข เช่น SAFETY BELT, NC801")
            _save(turn, draft, state)
            return None
        state["values"] = {**state["values"], "tools": " ".join(text.split())[:1000]}
        _save(turn, draft, state)
        target = follow(_version(draft).graph, box["id"], handoff["id"])
        return _go(turn, draft, target, "ตอนนี้ยังค้นจากชีตไม่ได้ ส่งให้เจ้าหน้าที่ตรวจแทน")
    if source is None:
        return _finish(turn, draft, "ตอนนี้ยังตรวจสอบสถานะจากชีทไม่ได้ กรุณาติดต่อ ศอค.")
    code = source.match == source.Match.CODE
    numbers = sheet_lookup.keys_in(source, text)
    if not numbers:
        turn.say(
            "ยังไม่เห็นหมายเลขอุปกรณ์ในข้อความ พิมพ์ ชื่อเครื่องมือ, หมายเลข เช่น SAFETY BELT, NC801"
            if code
            else "ยังไม่เห็นเลข UI ในข้อความ พิมพ์เป็นตัวเลข เช่น 12345678"
        )
        _save(turn, draft, state)
        return None
    extra = len(numbers) > sheet_lookup.MAX_NUMBERS
    district = turn.requester.district if source.district_column and turn.requester else None
    names = {n: name for name, n in sheet_lookup.codes_in(text)} if code else {}
    items = sheet_lookup.lookup(source, numbers[: sheet_lookup.MAX_NUMBERS], district, names)
    note = sheet_lookup.stale_note(source, turn.now)
    state["values"] = {**state["values"], "tools": " ".join(text.split())[:1000]}
    _save(turn, draft, state)
    again = "ถามชิ้นอื่น" if code else "ถามเลข UI อื่น"
    quick = [
        (*turn.button(label, "flow", {"node": box["id"], "lookup": key}, draft), label)
        for key, label in (("again", again), ("end", "จบ"))
    ]
    hint = ""
    if handoff is not None and any(not i["found"] for i in items):
        value = {"node": box["id"], "button": handoff["id"]}
        quick.insert(0, (*turn.button(handoff["label"], "flow", value, draft), handoff["label"]))
        hint = f"ตรวจหมายเลขอีกครั้ง หรือกด {handoff['label']}"
    turn.say(sheet_lookup.answer(items, extra, note, hint=hint), quick)
    if source.ai_words:
        ai_sheet_status.queue(turn, items, extra, note, hint)
    return None


def sheet_hit(turn, text):
    """ADR 0029 (grill กคอ. Q30): typed text outside any step that names a piece the asker's
    ฟข. has in a repair sheet a published flow looks up is answered by that flow's lookup box.
    The program finds the piece in the sheet; nothing is guessed. True when it answered."""
    from operations import sheet_lookup

    requester = turn.requester
    if requester is None or conversation._problems_only(turn) or not text:
        return False
    sources = {
        s.key: s
        for s in sheet_lookup.SheetSource.objects.filter(
            enabled=True, match=sheet_lookup.SheetSource.Match.CODE
        )
    }
    if not sources or not sheet_lookup.codes_in(text):
        return False
    for version in LineFlowVersion.objects.filter(
        status=LineFlowVersion.Status.PUBLISHED
    ).select_related("flow"):
        for box in version.graph.get("nodes", []):
            source = sources.get(box.get("config", {}).get("source", ""))
            if box.get("kind") != "sheet_lookup" or source is None:
                continue
            keys = sheet_lookup.keys_in(source, text)[: sheet_lookup.MAX_NUMBERS]
            names = {n: name for name, n in sheet_lookup.codes_in(text)}
            items = sheet_lookup.lookup(source, keys, requester.district, names)
            if not any(i["found"] for i in items):
                continue
            entry = next(n for n in version.graph["nodes"] if n["kind"] == "entry")
            draft, _ = _open(turn, version, entry, box["id"])
            _lookup(turn, draft, _state(draft), box, text)
            return True
    return False


def _tracking_notes(turn, values, number):
    """Q17: a repeated เลขติดตาม is taken; the requester hears of their own open case with it,
    staff see every repeat."""
    values["warnings"] = [w for w in values.get("warnings", []) if not w.startswith("เลขนี้")]
    values.pop("staff_notes", None)
    found = list(
        Case.objects.filter(sap_number__iexact=number)
        .exclude(status__in=workflow.TERMINAL)
        .values_list("number", "requester_id")[:5]
    )
    if not found:
        return []
    values["staff_notes"] = [f"เลขซ้ำกับเรื่อง {n}" for n, _ in found]
    own = [n for n, requester in found if turn.requester and requester == turn.requester.pk]
    if not own:
        return []
    warning = f"เลขนี้อยู่ในเรื่อง {', '.join(own)} ของคุณแล้ว ส่งต่อได้ถ้าเป็นคนละรายการ"
    values["warnings"] = [*values.get("warnings", []), warning]
    return [warning]


def _check_multiplier(values):
    """Compare the slip's multiplier with CT × VT (Q12); returns what to tell when they differ."""
    vt = values.get("vt_size", "") if values.get("set_kind") in WITH_VT else ""
    computed = typed_input.multiplier_from(values.get("ct_size", ""), vt)
    given = values["multiplier"]
    ok = computed is not None and abs(float(given) - computed) < 0.001
    values["multiplier_check"] = {
        "given": given,
        "computed": None if computed is None else _number(computed),
        "ok": ok,
    }
    if ok:
        return ""
    found = (
        f"ตัวคูณที่พิมพ์ ({given}) ไม่ตรงกับที่คำนวณจาก CT และ VT ({_number(computed)})"
        if computed is not None
        else "คำนวณตัวคูณจากขนาด CT และ VT ไม่ได้"
    )
    return found + " ส่งต่อได้ ระบบจะบอกเจ้าหน้าที่ให้ตรวจ"


def _number(value):
    return f"{value:g}"


def read(rule, field, text, kind=""):
    """(value, what it read, problem) of typed text by the box's rule."""
    group = field.split(".", 1)[1] if field.startswith("rows.") else ""
    if rule == "tracking":
        return typed_input.tracking_rule(text, kind)
    if rule == "size":
        return typed_input.size_rule(text)
    if rule == "meter_row":
        return typed_input.meter_rows(text, group or "meter")
    if rule == "size_quantity":
        return typed_input.size_quantity_rows(text, group or "ct")
    if rule == "name_size_quantity":
        return typed_input.name_rows(text, group or "other")
    if rule == "sap_document":
        return typed_input.sap_rule(text)
    if rule in ("number", "multiplier"):
        return typed_input.number_rule(text)
    if rule == "numbers":
        return typed_input.numbers_rule(text, typed_lines.METER_NUMBER, "หมายเลขเครื่องวัดฯ ")
    if rule == "mea_numbers":
        return typed_input.numbers_rule(text, label="MEA No. ")
    if rule == "date":
        parsed = conversation._parse_date(typed_input.tidy(text))
        if parsed is None or parsed < timezone.localdate():
            return None, "", "พิมพ์วันที่เช่น 31/12/2569 (ไม่ใช่วันที่ผ่านมาแล้ว)"
        return parsed.isoformat(), f"{parsed:%d/%m}/{parsed.year + 543}", ""
    if rule == "district":
        from operations import usage_meters  # local: usage_meters reads the districts

        found = usage_meters.match_district(text)
        if found is None:
            return None, "", f"ไม่พบเขตนี้ พิมพ์ชื่อเขต เช่น {usage_meters.examples()}"
        return found.code, usage_meters.short(found.name), ""
    return typed_input.text_rule(text)


def meter_warnings(rows):
    """Q55: a meter number already in an open case is kept, with a note for staff."""
    found = []
    for row in rows:
        for number in row.get("numbers", []):
            line = (
                CaseLine.objects.filter(details__contains=[{"numbers": [number]}])
                .exclude(case__status__in=workflow.TERMINAL)
                .first()
            )
            if line is not None:
                found.append(f"หมายเลข {number} อยู่ในเรื่องอื่นที่ยังไม่จบ ระบบรับไว้และแจ้งเจ้าหน้าที่")
    return found


# --- Filing ----------------------------------------------------------------------------------


def payload(draft, config, flow):
    """The submission of the case the values describe."""
    state = _state(draft)
    values = state["values"]
    kind = values.get("case_kind") or config.get("case_kind") or Case.Kind.PROBLEM
    # ไม่มีเลข (grill กคอ. Q17): still a tracking case, its document found by staff.
    tracking = bool(values.get("tracking") or values.get("no_number")) and kind != Case.Kind.PROBLEM
    lines = []
    if not tracking:
        for row in state.get("rows", []):
            entry = {k: v for k, v in row.items() if k != "group"}
            if row["group"] == "other" and values.get("equipment_name"):
                entry.setdefault("name", values["equipment_name"])  # กคอ.: the name came first
            lines = typed_lines.add(lines, row["group"], entry, kind)
    if topic_of(values, config) == Case.Topic.REPAIR and values.get("tools"):
        # ADR 0029: each typed piece is a row with its หมายเลขอุปกรณ์ (25 Oct 2569 Q7).
        from operations import sheet_lookup  # local: sheet_lookup reads the database

        for name, number in sheet_lookup.codes_in(values["tools"]):
            entry = {"name": name or "เครื่องมือ", "equipment_number": number}
            lines = typed_lines.add(lines, "other", entry, kind)
    path = " · ".join([flow.tile or flow.name, *state.get("path", [])[:2]])
    data = {"kind": kind, "lines": lines, "menu_label": path[:120]}
    if config.get("division"):
        data["division_code"] = config["division"]
    # A button may name the topic ("topic=suggestion"); otherwise the box says it.
    topic = (values.get("topic") or config.get("topic", "")) if kind == Case.Kind.PROBLEM else ""
    if topic:
        data["topic"] = topic
    if tracking:
        data["sap_number"] = values.get("tracking", "")
        data["sap_kind"] = values.get("sap_kind") or (
            "return_slip" if kind == Case.Kind.RETURN else "slip"
        )
    elif values.get("sap_document"):
        data["sap_number"] = values["sap_document"]
    details = values.get("details", "")
    if kind == Case.Kind.WITHDRAWAL:
        data["purpose"] = details or values.get("purpose") or "-"
    elif kind == Case.Kind.RETURN:
        data["condition"] = data["reason"] = details or values.get("condition_reason") or "-"
    else:
        bits = [values.get("tools", ""), details or values.get("description", "")]
        if values.get("mea_numbers"):
            bits.append("MEA No.: " + ", ".join(values["mea_numbers"]))
        if values.get("appointment"):
            bits.append(f"ขอนัด: {values['appointment']['label']}")
        data["description"] = "\n".join(b for b in bits if b) or dict(Case.Topic.choices).get(
            topic, "แจ้งปัญหา"
        )
        if values.get("impact"):
            data["impact"] = values["impact"]
        if topic == Case.Topic.USAGE and values.get("meter_numbers"):
            # กตฟ. (25 Oct 2569 Q11): each meter with the district it is in.
            data["meters"] = [
                {"number": n, "district": values.get("district", "")}
                for n in values["meter_numbers"]
            ]
    extra = {k: values[k] for k in EXTRA if values.get(k)}
    if tracking and state.get("rows"):
        extra["meters"] = [{k: v for k, v in r.items()} for r in state["rows"]]
    if extra:
        data["intake_data"] = extra
    return data


def topic_of(values, config):
    return values.get("topic") or config.get("topic", "")


def _keep_slot(turn, draft, graph, state):
    """Q8: the slot is held 30 minutes; after that it is held again if still free."""
    from operations import calendars  # local: calendars builds on this module

    chosen = state["values"].get("appointment")
    if not chosen or not chosen.get("slot"):
        return True
    held = Appointment.objects.filter(
        draft=draft, slot_id=chosen["slot"], status=Appointment.Status.REQUESTED
    ).exists()
    if held:
        return True
    slot, problem = calendars.hold(chosen["slot"], draft)
    if slot is not None:
        return True
    state["values"].pop("appointment", None)
    state["node"] = chosen.get("node") or state["node"]
    _save(turn, draft, state)
    turn.say("ช่วงเวลาที่เลือกไว้มีคนขอแล้วระหว่างรอยืนยัน เลือกช่วงใหม่")
    conversation._ask(turn, draft)
    return False


def _create(turn, draft, graph, box):
    flow = _version(draft).flow
    state = _state(draft)
    if not _keep_slot(turn, draft, graph, state):
        return None
    try:
        result = services.submit_case(
            conversation._actor(turn),
            idempotency_key=f"draft-{draft.pk}",
            payload=payload(draft, box["config"], flow),
        )
    except CommandFailed as exc:
        turn.say(f"ส่งเรื่องไม่ได้: {exc.message or 'ข้อมูลยังไม่ครบหรือไม่ถูกต้อง'}")
        state = _state(draft)
        state["node"] = _summary_before(graph, box["id"]) or state["node"]
        _save(turn, draft, state)
        return conversation._ask(turn, draft)
    case = Case.objects.get(pk=result.case_id)
    from operations import calendars  # local: calendars builds on this module

    calendars.attach(draft, case)
    after = node(graph, follow(graph, box["id"]))
    state = _state(draft)
    state["case"] = case.number
    conversation._touch(
        draft, turn.now, status=D.SUBMITTED, case=case, data=dict(draft.data, flow=state)
    )
    Attachment.objects.filter(draft=draft).update(case=case)
    turn.conversation.active_draft = None
    turn.conversation.save(update_fields=["active_draft", "updated_at"])
    numbers = [case.number, *(s["case_number"] for s in result.split)]
    result_box = after if after and after["kind"] == "result" else None
    rest = [
        line
        for line in (result_box["text"] if result_box else "ติดตามได้ที่ เรื่องของฉัน").splitlines()
        if not line.startswith(("เลขเรื่อง", "ขั้น:"))
    ]
    title = result_box["title"] if result_box else "ยื่นเรื่องแล้ว"
    status = f"สถานะ: {case.get_status_display()}"
    conversation._menu(turn, "\n".join([title, f"เลขเรื่อง: {', '.join(numbers)}", status, *rest]))
    base = state.get("base")
    if base:
        value = {"version": _version(draft).pk, "node": base["node"], "values": base["values"]}
        items = turn.replies[-1].setdefault("quickReply", {}).setdefault("items", [])
        items.insert(0, conversation._postback(*turn.button("ส่งใบถัดไป", "flow_again", value)))
        del items[conversation.MAX_QUICK :]


def _summary_before(graph, nid):
    for e in graph["edges"]:
        if e["to"] == nid and (node(graph, e["from"]) or {}).get("kind") == "summary":
            return e["from"]
    return None


def _finish(turn, draft, text=""):
    conversation._touch(draft, turn.now, status=D.DISCARDED)
    turn.conversation.active_draft = None
    turn.conversation.save(update_fields=["active_draft", "updated_at"])
    if text:
        conversation._menu(turn, text)


def expects_text(draft):
    if not is_flow(draft):
        return False
    box = node(_version(draft).graph, _state(draft).get("node"))
    return bool(box and box["kind"] in ("ask", "sheet_lookup"))
