"""ตัวรัน flow (ADR 0023): the LINE chat follows a flow an admin published in the admin web.

A tap on the rich menu tile a published flow names (LineFlow.tile) starts a draft in step
"flow". The draft keeps the version it started with (Q43), the box it waits at and the values
the requester gave. Boxes that talk (message, ask, summary, book) wait for a tap or typed text;
the others run at once: a branch follows its first button whose condition holds
("set_kind=ct_vt|meter_ct_vt"), create_case files the case from the values and result shows
the real case number. A button may set values ("case_kind=withdrawal; group=meter").
Questions use the deterministic rules of typed_input; nothing here reads text with AI, so a
flow stays guided until the AI is evaluated on its topics (Q44, Q57).
"""

from django.utils import timezone

from operations import conversation, services, typed_input, typed_lines, workflow
from operations.contracts import CommandFailed
from operations.models import Attachment, Case, CaseLine, ConversationDraft, LineFlowVersion

D = ConversationDraft.Status
STEP = "flow"
# A choice of an ask box that names a document: (sap_kind, the case kind it implies).
SAP_CHOICES = {
    "เลขที่ใบเบิก": ("slip", Case.Kind.WITHDRAWAL),
    "เลขที่ใบส่งคืน": ("return_slip", Case.Kind.RETURN),
    "ใบสั่งงาน SAP": ("work_order", ""),
}
SAP_KIND_LABELS = {
    "slip": "เลขที่ใบเบิก",
    "return_slip": "เลขที่ใบส่งคืน",
    "work_order": "ใบสั่งงาน SAP",
}
GROUP_LABELS = {"meter": "มิเตอร์", "ct": "CT", "vt": "VT", "other": "อุปกรณ์อื่น"}
VALUE_LABELS = {
    "paired_meter": "ใช้คู่กับหมายเลขเครื่องวัดฯ",
    "mea_numbers": "MEA No.",
    "purpose": "วัตถุประสงค์",
    "needed_by": "ต้องการใช้ภายใน",
    "condition_reason": "สภาพและเหตุผลที่ส่งคืน",
    "description": "รายละเอียด",
}
SET_LABELS = {
    "meter_ct": "มิเตอร์ + CT",
    "meter_ct_vt": "มิเตอร์ + CT + VT",
    "ct": "CT อย่างเดียว",
    "ct_vt": "CT + VT",
}
EXTRA = ("multiplier_check", "paired_meter", "mea_numbers", "set_kind", "warnings", "appointment")
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


# --- Starting, moving, showing ---------------------------------------------------------------


def start(turn, version):
    """Start this published version for the requester in a new, active draft."""
    draft = conversation._new_draft(turn)
    entry = next(n for n in version.graph["nodes"] if n["kind"] == "entry")
    data = {
        "lines": [],
        "fields": {},
        **({"anonymous": True} if draft.data.get("anonymous") else {}),
        "flow": _fresh(version, entry),
    }
    conversation._touch(draft, turn.now, kind="", step=STEP, data=data)
    return _go(turn, draft, follow(version.graph, entry["id"]))


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
        state["node"] = box["id"]
        _save(turn, draft, state)
        if kind == "create_case":
            return _create(turn, draft, graph, box)
        if kind == "find_case":
            _finish(turn, draft)
            return conversation._my_cases(turn)
        if kind in ("result", "limit", "web", "handoff"):
            text = "\n".join(x for x in (box["title"], box["text"]) if x)
            if kind == "web" and box["config"].get("url"):
                text += f"\n{box['config']['url']}"
            return _finish(turn, draft, text)
        return conversation._ask(turn, draft)
    return _finish(turn, draft, "flow นี้วนไม่จบ กรุณาติดต่อ ฝอจ.")


def render(turn, draft):
    """Show the box the draft waits at (conversation._ask adds ยกเลิกร่าง)."""
    graph, state = _version(draft).graph, _state(draft)
    box = node(graph, state.get("node"))
    if box is None:
        return turn.say("flow นี้เปลี่ยนไปแล้ว กรุณาเริ่มใหม่จากเมนู")
    b, kind, quick = turn.button, box["kind"], []
    ttl = conversation.CONFIRM_TTL if kind == "summary" else conversation.BUTTON_TTL
    if kind == "ask":
        choices = [c.strip() for c in (box["config"].get("choices") or "").split("|") if c.strip()]
        picked = state.get("choice", {}).get(box["id"])
        if choices and not picked:
            quick = [(*b(c, "flow", {"node": box["id"], "choice": c}, draft), c) for c in choices]
            return turn.say(f"{box['title']}\nเลขที่จะพิมพ์เป็นแบบไหน กดเลือกก่อน", quick)
        title = f"{box['title']} ({picked})" if picked else box["title"]
        text = "\n".join(x for x in (title, box["text"]) if x)
    elif kind == "summary":
        text = summary(draft, graph, box)
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
    if values.get("sap_document"):
        label = SAP_KIND_LABELS.get(values.get("sap_kind"), "เลข SAP")
        lines.append(f"{label}: {values['sap_document']}")
    for number, row in enumerate(state.get("rows", []), 1):
        lines.append(f"{number}. {row_text(row)}")
    check = values.get("multiplier_check")
    if check:
        lines.append(
            f"ตัวคูณ: {check['given']} (ตรงกับที่คำนวณ)"
            if check["ok"]
            else f"ตัวคูณ: {check['given']} (คำนวณได้ {check['computed'] or 'ไม่ได้'} ให้เจ้าหน้าที่ตรวจ)"
        )
    for key, label in VALUE_LABELS.items():
        value = values.get(key)
        if value:
            lines.append(f"{label}: {', '.join(value) if isinstance(value, list) else value}")
    if values.get("appointment"):
        lines.append(f"ขอนัด: {values['appointment']['label']} (รอเจ้าหน้าที่ยืนยัน)")
    for warning in values.get("warnings", []):
        lines.append(f"หมายเหตุ: {warning}")
    lines.append("กดยืนยันภายใน 30 นาที")
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
        sap_kind, case_kind = SAP_CHOICES.get(value["choice"], ("", ""))
        state["choice"] = {**state.get("choice", {}), box["id"]: value["choice"]}
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
            values["appointment"] = {"slot": slot.pk, "label": calendars.label(slot)}
        _save(turn, draft, state)
        return _go(turn, draft, follow(graph, box["id"]))
    button = next((b for b in box["buttons"] if b["id"] == value.get("button")), None)
    if button is None:
        return conversation._ask(turn, draft)
    target = follow(graph, box["id"], button["id"])
    if box["kind"] != "summary":
        assign(values, button.get("set"))
        if values.get("case_kind") in (Case.Kind.WITHDRAWAL, Case.Kind.RETURN) and (
            conversation._problems_only(turn) or draft.data.get("anonymous")
        ):
            return _finish(turn, draft, "ต้องยืนยันตัวตนพนักงานก่อนเบิกหรือส่งคืนอุปกรณ์")
        state["path"] = [*state.get("path", []), button["label"]][:4]
    if target is None:
        if "กรอกใหม่" in button["label"]:
            return restart(turn, draft)
        if button.get("note"):
            turn.say(button["note"])
        _save(turn, draft, state)
        return conversation._ask(turn, draft)
    _save(turn, draft, state)
    return _go(turn, draft, target)


def restart(turn, draft):
    """กรอกใหม่: the flow from its first box (files already sent stay with the draft)."""
    version = _version(draft)
    entry = next(n for n in version.graph["nodes"] if n["kind"] == "entry")
    _save(turn, draft, _fresh(version, entry))
    return _go(turn, draft, follow(version.graph, entry["id"]), "เริ่มกรอกใหม่")


def on_text(turn, draft, text):
    graph, state = _version(draft).graph, _state(draft)
    box = node(graph, state.get("node"))
    if box is None or box["kind"] != "ask":
        turn.say("เลือกจากปุ่มด้านล่าง")
        return conversation._ask(turn, draft)
    config = box["config"]
    if config.get("choices") and not state.get("choice", {}).get(box["id"]):
        turn.say("กดเลือกก่อนว่าเลขที่จะพิมพ์เป็นแบบไหน")
        return conversation._ask(turn, draft)
    values = state["values"] = dict(state["values"])
    field, rule = config.get("field", ""), config.get("rule", "text")
    value, shown, problem = read(rule, field, text)
    if not problem and rule == "sap_document":
        problem = typed_lines.sap_in_use(value, turn.requester)
    if not problem and config.get("max_rows") and isinstance(value, list):
        if len(value) > int(config["max_rows"]):
            problem = f"ขั้นนี้พิมพ์ได้ไม่เกิน {config['max_rows']} บรรทัด"
    if problem:
        turn.say(problem)
        return conversation._ask(turn, draft)
    notes = [f"อ่านเป็น: {shown}"] if shown and shown != text.strip() else []
    if field.startswith("rows."):
        state["rows"] = [*state.get("rows", []), *value]
        warnings = meter_warnings(value)
        if warnings:
            values["warnings"] = [*values.get("warnings", []), *warnings]
            notes += warnings
    elif rule == "multiplier":
        computed = typed_input.multiplier_of(state.get("rows", []))
        ok = computed is not None and abs(float(value) - computed) < 0.001
        values["multiplier_check"] = {
            "given": value,
            "computed": None if computed is None else _number(computed),
            "ok": ok,
        }
        if not ok:
            notes.append(
                (
                    f"ตัวคูณที่พิมพ์ ({value}) ไม่ตรงกับที่คำนวณจาก CT และ VT ({_number(computed)})"
                    if computed is not None
                    else "คำนวณตัวคูณจากขนาด CT และ VT ไม่ได้"
                )
                + " ส่งต่อได้ ระบบจะบอกเจ้าหน้าที่ให้ตรวจ"
            )
    else:
        values[field] = value
    _save(turn, draft, state)
    return _go(turn, draft, follow(graph, box["id"]), "\n".join(notes))


def _number(value):
    return f"{value:g}"


def read(rule, field, text):
    """(value, what it read, problem) of typed text by the box's rule."""
    group = field.split(".", 1)[1] if field.startswith("rows.") else ""
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
    lines = []
    for row in state.get("rows", []):
        entry = {k: v for k, v in row.items() if k != "group"}
        lines = typed_lines.add(lines, row["group"], entry, kind)
    path = " · ".join([flow.tile or flow.name, *state.get("path", [])[:2]])
    data = {"kind": kind, "lines": lines, "menu_label": path[:120]}
    if config.get("division"):
        data["division_code"] = config["division"]
    topic = config.get("topic", "") if kind == Case.Kind.PROBLEM else ""
    if topic:
        data["topic"] = topic
    if values.get("sap_document"):
        data["sap_number"] = values["sap_document"]
        data["sap_kind"] = values.get("sap_kind") or (
            "return_slip" if kind == Case.Kind.RETURN else "slip"
        )
    if kind == Case.Kind.WITHDRAWAL:
        data["purpose"] = values.get("purpose") or "-"
        if values.get("needed_by"):
            data["needed_by"] = values["needed_by"]
    elif kind == Case.Kind.RETURN:
        data["condition"] = data["reason"] = values.get("condition_reason") or "-"
    else:
        bits = [values.get("description", "")]
        if values.get("mea_numbers"):
            bits.append("MEA No.: " + ", ".join(values["mea_numbers"]))
        if values.get("appointment"):
            bits.append(f"ขอนัด: {values['appointment']['label']}")
        data["description"] = "\n".join(b for b in bits if b) or dict(Case.Topic.choices).get(
            topic, "แจ้งปัญหา"
        )
    extra = {k: values[k] for k in EXTRA if values.get(k)}
    if extra:
        data["intake_data"] = extra
    return data


def _create(turn, draft, graph, box):
    flow = _version(draft).flow
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
    return bool(box and box["kind"] == "ask")
