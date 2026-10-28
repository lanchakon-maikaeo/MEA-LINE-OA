"""flow ผู้แจ้งที่แอดมินแก้ในเว็บ (ADR 0023, grill กคว. 27 ต.ค. 2569 Q40 ถึง Q51).

A flow is the conversation one rich menu button starts. Its graph is data: boxes (nodes) of the
kinds below and lines (edges) from a box, or from one of its buttons, to the next box. Admins
change it in a draft, the program checks it (no dead ends, LINE's limits, every question has a
field and a rule), and an admin publishes it with a reason. The chat then follows the published
version (flow_runner) when someone taps the tile the flow names.

A button may set values ("case_kind=withdrawal; group=meter"); a branch box follows its first
button whose condition holds ("set_kind=ct_vt|meter_ct_vt") and otherwise its plain line.
"""

import re

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from operations import identity
from operations.models import DivisionRequirement, LineFlow, LineFlowVersion

V = LineFlowVersion.Status
RUNNER_READY = True  # flow_runner reads published flows (stage 2 of ADR 0023)
MAX_NODES = 300
MAX_EDGES = 800
MAX_TEXT = 5000
MAX_BUTTONS = 13  # LINE quick replies
MAX_LABEL = 20  # LINE quick reply label

# Kinds of box: (name in Thai, the box's outgoing role). "end" boxes may have no next box.
KINDS = {
    "entry": ("ปุ่มเมนู", "next"),
    "message": ("ข้อความบอท", "buttons"),
    "ask": ("ถามข้อมูล", "next"),
    "branch": ("แยกทาง", "buttons"),
    "summary": ("สรุปเรื่อง", "buttons"),
    "create_case": ("สร้างเรื่อง", "next"),
    "find_case": ("ค้นเรื่อง", "end"),  # the chat shows เรื่องของฉัน and the flow ends
    "koj_tracking": ("ติดตามงานต้นทาง กอจ.", "end"),
    "book": ("นัดในปฏิทิน", "next"),
    "ai_read": ("ให้ AI อ่าน", "next"),
    "handoff": ("ส่งต่อเจ้าหน้าที่", "end"),
    "web": ("ไปทำต่อบนเว็บ", "end"),
    "limit": ("ข้อจำกัดสิทธิ์", "end"),
    "result": ("ผลลัพธ์", "end"),
    "note": ("โน้ต", "none"),
}
# What an "ask" box may check, all deterministic (Q33, Q47): the program normalises spaces,
# other commas, Thai digits, dashes, unit case and number prefixes, then says what it read.
RULES = {
    "text": "ข้อความ",
    "number": "ตัวเลข",
    "date": "วันที่ (ไม่ใช่วันที่ผ่านมาแล้ว)",
    "tracking": "เลขติดตามตามชนิดที่เลือก (ใบเบิก ใบส่งคืน ใบสั่งงาน หมายเลขเครื่องวัดฯ โครงการ)",
    "sap_document": "เลข SAP (ไม่เกิน 30 ตัว ใช้ / ได้)",
    "size": "ขนาดหนึ่งค่า เช่น 100/5A",
    "meter_row": "หมายเลขเครื่องวัดฯ, ขนาด (บรรทัดละเครื่อง)",
    "size_quantity": "ขนาด, จำนวน",
    "name_size_quantity": "ชื่ออุปกรณ์, ขนาด, จำนวน",
    "numbers": "หลายเลขคั่นด้วย ,",
    "mea_numbers": "MEA No. หลายเครื่องคั่นด้วย ,",
    "multiplier": "ตัวคูณ (เทียบกับ CT และ VT)",
    "district": "ชื่อเขต (18 เขต)",
}
ACTIONS = {"create_case", "find_case", "book", "ai_read", "handoff", "koj_tracking"}
SETTING = re.compile(r"[a-z_]{1,30}\s*=\s*[^=;|]{1,40}(?:\|[^=;|]{1,40})*")


def _text(value, limit=MAX_TEXT):
    value = value if isinstance(value, str) else ""
    if len(value) > limit:
        raise ValidationError(f"ข้อความยาวเกิน {limit} ตัวอักษร")
    return value


def clean_graph(graph):
    """The graph with only known keys and sane sizes; raises ValidationError on a bad shape."""
    if not isinstance(graph, dict):
        raise ValidationError("รูปแบบ flow ไม่ถูกต้อง")
    nodes, edges = graph.get("nodes") or [], graph.get("edges") or []
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValidationError("รูปแบบ flow ไม่ถูกต้อง")
    if len(nodes) > MAX_NODES or len(edges) > MAX_EDGES:
        raise ValidationError(f"กล่องไม่เกิน {MAX_NODES} และเส้นไม่เกิน {MAX_EDGES}")
    clean_nodes, ids = [], set()
    for n in nodes:
        if not isinstance(n, dict) or n.get("kind") not in KINDS:
            raise ValidationError("มีกล่องชนิดที่ระบบไม่รู้จัก")
        nid = _text(n.get("id"), 40)
        if not nid or nid in ids:
            raise ValidationError("รหัสกล่องว่างหรือซ้ำ")
        ids.add(nid)
        buttons = []
        for b in n.get("buttons") or []:
            if not isinstance(b, dict):
                raise ValidationError("ปุ่มไม่ถูกต้อง")
            buttons.append(
                {
                    "id": _text(b.get("id"), 40),
                    "label": _text(b.get("label"), 60),
                    "full": _text(b.get("full"), 200),
                    "note": _text(b.get("note"), 300),
                    "set": _text(b.get("set"), 200),
                }
            )
        config = n.get("config") if isinstance(n.get("config"), dict) else {}
        clean_nodes.append(
            {
                "id": nid,
                "kind": n["kind"],
                "title": _text(n.get("title"), 200),
                "text": _text(n.get("text")),
                "buttons": buttons,
                "config": {str(k)[:40]: _text(v, 500) for k, v in config.items()},
                "x": int(n.get("x") or 0),
                "y": int(n.get("y") or 0),
                "w": max(160, min(int(n.get("w") or 260), 600)),
            }
        )
    clean_edges = []
    for e in edges:
        if not isinstance(e, dict):
            raise ValidationError("เส้นไม่ถูกต้อง")
        clean_edges.append(
            {
                "id": _text(e.get("id"), 40),
                "from": _text(e.get("from"), 40),
                "port": _text(e.get("port"), 40) or None,
                "to": _text(e.get("to"), 40),
                "label": _text(e.get("label"), 120),
            }
        )
    return {"nodes": clean_nodes, "edges": clean_edges}


def check(graph):
    """What stops this graph from being published (level "error") or deserves a look ("warn"):
    [{"level", "node", "message"}]."""
    issues = []
    nodes = {n["id"]: n for n in graph["nodes"]}

    def add(level, node, message):
        issues.append({"level": level, "node": node, "message": message})

    entries = [n for n in graph["nodes"] if n["kind"] == "entry"]
    if len(entries) != 1:
        add("error", None, "ต้องมีปุ่มเมนูเริ่มต้นหนึ่งกล่องพอดี")
    out = {}
    for e in graph["edges"]:
        if e["from"] not in nodes or e["to"] not in nodes:
            add("error", e["from"] if e["from"] in nodes else None, "มีเส้นที่ไปกล่องที่ไม่มีอยู่")
            continue
        out.setdefault(e["from"], []).append(e)
    for n in graph["nodes"]:
        name, role = KINDS[n["kind"]]
        if n["kind"] == "koj_tracking":
            from operations import koj_tracking

            equipment, service = n["config"].get("equipment", ""), n["config"].get("service", "")
            if equipment and equipment not in koj_tracking.EQUIPMENT:
                add("error", n["id"], "ไม่พบอุปกรณ์ กอจ. ที่เลือก")
            if (
                service
                and service != "watches"
                and (service not in koj_tracking.SERVICES or not equipment)
            ):
                add("error", n["id"], "เลือกอุปกรณ์และบริการ กอจ. ให้ครบ")
            if service == "repair" and equipment not in {"lbs", "rmu"}:
                add("error", n["id"], "เฟสแรกติดตามซ่อมเฉพาะ LBS และ RMU")
        edges = out.get(n["id"], [])
        if n["kind"] != "note" and not n["title"].strip():
            add("warn", n["id"], f"{name} ยังไม่มีชื่อ")
        if len(n["buttons"]) > MAX_BUTTONS:
            add("error", n["id"], f"ปุ่มเกิน {MAX_BUTTONS} ปุ่ม (ข้อจำกัดของ LINE)")
        for b in n["buttons"]:
            if not b["label"].strip():
                add("error", n["id"], "มีปุ่มที่ยังไม่มีชื่อ")
            elif len(b["label"]) > MAX_LABEL:
                add(
                    "error",
                    n["id"],
                    f"ปุ่ม '{b['label']}' ยาวเกิน {MAX_LABEL} ตัวอักษร ย่อชื่อปุ่ม แล้วใส่ชื่อเต็มไว้ในช่องชื่อเต็ม",
                )
            if not any(e["port"] == b["id"] for e in edges) and not b["note"].strip():
                add("error", n["id"], f"ปุ่ม '{b['label']}' ยังไม่ได้บอกว่าพาไปไหน")
            if b["set"].strip() and not all(
                SETTING.fullmatch(p.strip()) for p in b["set"].split(";") if p.strip()
            ):
                add("error", n["id"], f"ปุ่ม '{b['label']}' ตั้งค่าไม่ถูกรูปแบบ ใช้ ชื่อ=ค่า; ชื่อ=ค่า")
            if n["kind"] == "branch" and not b["set"].strip():
                add("error", n["id"], f"ทาง '{b['label']}' ยังไม่มีเงื่อนไข เช่น case_kind=withdrawal")
        if role in ("next",) and not any(e["port"] is None for e in edges):
            if not (n["kind"] == "ask" and n["buttons"]):
                add("error", n["id"], f"{name} '{n['title']}' ยังไม่มีกล่องถัดไป (ทางตัน)")
        if role == "buttons" and not n["buttons"] and not edges:
            add("error", n["id"], f"{name} '{n['title']}' ไม่มีปุ่มและไม่มีกล่องถัดไป (ทางตัน)")
        if n["kind"] == "ask":
            if not n["config"].get("field"):
                add("error", n["id"], f"ถามข้อมูล '{n['title']}' ยังไม่ได้บอกว่าเก็บลงช่องไหน")
            if n["config"].get("rule") not in RULES:
                add("error", n["id"], f"ถามข้อมูล '{n['title']}' ยังไม่ได้เลือกกฎตรวจ")
        if n["kind"] == "create_case" and not n["config"].get("division"):
            add("warn", n["id"], f"สร้างเรื่อง '{n['title']}' ยังไม่ได้บอกกอง ระบบจะเลือกกองตามกฎ")
    if len(entries) == 1:
        seen, todo = set(), [entries[0]["id"]]
        while todo:
            nid = todo.pop()
            if nid in seen:
                continue
            seen.add(nid)
            todo += [e["to"] for e in out.get(nid, [])]
        for n in graph["nodes"]:
            if n["kind"] != "note" and n["id"] not in seen:
                add("warn", n["id"], f"กด '{n['title']}' ไม่ถึงจากปุ่มเมนู")
    return issues


def blocked(issues):
    return any(i["level"] == "error" for i in issues)


# --- Drafts, publishing ---------------------------------------------------------------------


def draft_of(flow):
    return flow.versions.filter(status=V.DRAFT).first()


@transaction.atomic
def start_draft(actor, flow):
    """The flow's draft, created from the published version (or empty) when there is none."""
    identity.require_admin(actor)
    LineFlow.objects.select_for_update().get(pk=flow.pk)
    draft = draft_of(flow)
    if draft is not None:
        return draft
    base = flow.versions.filter(status=V.PUBLISHED).first() or flow.versions.first()
    number = (flow.versions.order_by("-number").values_list("number", flat=True).first() or 0) + 1
    graph = base.graph if base else {"nodes": [], "edges": []}
    return LineFlowVersion.objects.create(
        flow=flow,
        number=number,
        graph=graph,
        checks=check(clean_graph(graph)),
        created_by=actor,
        updated_by=actor,
    )


@transaction.atomic
def save_draft(actor, version, graph, revision, note=None):
    """Save the editor's graph over the draft it was opened from; a newer save by someone else
    is refused instead of overwritten."""
    identity.require_admin(actor)
    version = LineFlowVersion.objects.select_for_update().get(pk=version.pk)
    if version.status != V.DRAFT:
        raise ValidationError("รุ่นนี้ไม่ใช่ร่างแล้ว เปิดร่างใหม่ก่อนแก้")
    if int(revision) != version.revision:
        raise ValidationError("มีคนบันทึกร่างนี้หลังจากคุณเปิด โหลดหน้าใหม่แล้วแก้ต่อ")
    graph = clean_graph(graph)
    version.graph, version.checks = graph, check(graph)
    version.revision += 1
    version.updated_by = actor
    if note is not None:
        version.note = _text(note, 2000)
    version.save(update_fields=["graph", "checks", "revision", "updated_by", "note", "updated_at"])
    return version


@transaction.atomic
def publish(actor, version, reason):
    """Make a checked draft the flow's published version; the previous one stays for rollback."""
    identity.require_admin(actor)
    reason = identity._require_reason(reason)
    if not reason:
        raise ValidationError("ใส่เหตุผลก่อนเผยแพร่")
    if not RUNNER_READY:
        raise ValidationError("ตัวรัน flow ยังไม่พร้อม เผยแพร่ได้เมื่อแชตอ่าน flow จากระบบได้แล้ว")
    version = LineFlowVersion.objects.select_for_update().get(pk=version.pk)
    if version.status != V.DRAFT:
        raise ValidationError("เผยแพร่ได้เฉพาะร่าง")
    is_koj = version.flow.tile == "หม้อแปลง LBS RMU" or (
        version.flow.division_id and version.flow.division.code in {"กอจ.", "กองอุปกรณ์ฯ"}
    )
    if is_koj and any(n["kind"] == "create_case" for n in version.graph["nodes"]):
        raise ValidationError("กอจ. ติดตามงานต้นทาง ใช้กล่องติดตามงาน กอจ. แทนการสร้างเรื่องใหม่")
    issues = check(clean_graph(version.graph))
    if blocked(issues):
        raise ValidationError("ยังมีจุดที่ต้องแก้ก่อนเผยแพร่")
    LineFlowVersion.objects.filter(flow=version.flow, status=V.PUBLISHED).update(status=V.RETIRED)
    version.status, version.checks = V.PUBLISHED, issues
    version.published_by, version.published_at = actor, timezone.now()
    version.publish_reason = reason
    version.save(
        update_fields=["status", "checks", "published_by", "published_at", "publish_reason"]
    )
    identity.audit(actor, "line_flow.published", version, reason, number=version.number)
    return version


# --- The real rich menu --------------------------------------------------------------------
# A requester's flow starts from a big button of the report menu (Q49: registration and staff
# work stay in code). Buttons that open เรื่องของฉัน or a case number search are features.
FLOW_ACTIONS = {"topic", "general"}


def menu_tabs():
    """[{item, flow, can_flow}] for the requester rich menu as people see it: its big buttons,
    then the bar below them."""
    from operations.models import LineMenuItem

    items = LineMenuItem.objects.filter(
        tab=LineMenuItem.Tab.REPORT, is_active=True, place__in=["tile", "bar"]
    ).select_related("division")
    flows = {f.tile: f for f in LineFlow.objects.exclude(tile="")}
    ordered = sorted(items, key=lambda i: (i.place != "tile", i.position, i.pk))
    return [
        {"item": i, "flow": flows.get(i.label), "can_flow": i.action in FLOW_ACTIONS}
        for i in ordered
    ]


def _starter(item):
    """A first draft for a button without a flow: its current choices, each still to be
    joined to the next box."""
    choices = list(item.choices.filter(is_active=True).order_by("position", "pk")[:MAX_BUTTONS])
    buttons = [
        {
            "id": f"c1.b{n}",
            "label": c.label[:MAX_LABEL],
            "full": c.label if len(c.label) > MAX_LABEL else "",
            "note": "ยังทำงานตามโปรแกรมเดิม ต่อเส้นไปกล่องถัดไปเมื่อออกแบบเสร็จ",
            "set": "",
        }
        for n, c in enumerate(choices)
    ]
    box = {"title": "", "text": "", "config": {}, "w": 260}
    nodes = [
        {
            **box,
            "id": "start",
            "kind": "entry",
            "title": item.label,
            "x": 40,
            "y": 40,
            "text": "ปุ่มบน rich menu",
        },
        {
            **box,
            "id": "c1",
            "kind": "message",
            "title": item.label,
            "x": 360,
            "y": 40,
            "text": item.note or "เลือกเรื่องที่ต้องการ",
            "buttons": buttons,
        },
    ]
    nodes[0]["buttons"] = []
    return {
        "nodes": nodes,
        "edges": [{"id": "e1", "from": "start", "port": None, "to": "c1", "label": ""}],
    }


@transaction.atomic
def flow_for_tile(actor, item):
    """The flow of a rich menu button, created with a starter draft when it has none."""
    identity.require_admin(actor)
    if item.action not in FLOW_ACTIONS:
        raise ValidationError("ปุ่มนี้เปิดเรื่องของฉันหรือค้นเลขเรื่อง ไม่ใช้ flow")
    flow = LineFlow.objects.filter(tile=item.label).first()
    if flow is not None:
        return flow
    flow = LineFlow.objects.create(
        key=f"tile-{item.pk}", name=item.label, tile=item.label, division=item.division
    )
    graph = clean_graph(_starter(item))
    LineFlowVersion.objects.create(
        flow=flow,
        number=1,
        graph=graph,
        checks=check(graph),
        note="ร่างแรกจากตัวเลือกที่ปุ่มนี้มีอยู่ตอนนี้",
        created_by=actor,
        updated_by=actor,
    )
    identity.audit(actor, "line_flow.created", flow, tile=item.label)
    return flow


# --- Requirements ---------------------------------------------------------------------------


def node_title(flow, node_id):
    if flow is None or not node_id:
        return ""
    version = draft_of(flow) or flow.versions.filter(status=V.PUBLISHED).first()
    for n in (version.graph if version else {}).get("nodes", []):
        if n["id"] == node_id:
            return n["title"]
    return ""


@transaction.atomic
def update_requirement(actor, requirement, *, status=None, remark=None, flow=None, node_id=None):
    identity.require_admin(actor)
    req = DivisionRequirement.objects.select_for_update().get(pk=requirement.pk)
    before = (req.status, req.remark, req.flow_id, req.node_id)
    if status is not None:
        if status not in DivisionRequirement.Status.values:
            raise ValidationError("สถานะไม่ถูกต้อง")
        req.status = status
    if remark is not None:
        req.remark = _text(remark, 2000)
    if flow is not None or node_id is not None:
        if flow is not None and node_id and not node_title(flow, node_id):
            raise ValidationError("ไม่พบกล่องนี้ใน flow")
        req.flow, req.node_id = flow, node_id or ""
    if (req.status, req.remark, req.flow_id, req.node_id) == before:
        return req
    req.save()
    identity.audit(
        actor,
        "division_requirement.updated",
        req,
        status=req.status,
        flow=req.flow.key if req.flow else "",
        node=req.node_id,
    )
    return req
