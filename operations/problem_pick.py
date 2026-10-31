"""ปุ่มปัญหา/ข้อเสนอแนะ: the requester picks the division and types (ADR 0030).

One bubble per division, with its full name only (Q17, Q23), in the order of the big rich menu
buttons, then ปัญหาการใช้งานหรืออื่นๆ for the admins. Tapping a bubble asks for the details in one
message (Q6). The published flow of the tile keeps its own list (Q8); until it is published the
chat uses this one (Q22).
"""

from operations.models import Division

ORDER = ("กคว.", "กอจ.", "กคอ.", "ศอค.", "กตฟ.", "บท.")
# Codes a division had before its official abbreviation (the menu seed keeps both).
EARLIER = {"กอจ.": ("กองอุปกรณ์ฯ",), "กคอ.": ("กองเครื่องมือฯ",)}
# The rich menu tile and its published flow; a case filed from it carries the tile's name as the
# start of its menu label (ADR 0039, 31 Oct 2569 Q12, Q13).
TILE = "ปัญหา/ข้อเสนอแนะ"
FLOW_KEY = "general"
ADMIN = "admin"
ADMIN_LABEL = "ปัญหาการใช้งานหรืออื่นๆ"
ADMIN_TARGET = "แอดมินระบบ"
PROMPT = "เลือกกองที่ต้องการแจ้ง"
DETAIL = "พิมพ์รายละเอียดที่ต้องการแจ้ง ส่งรูปประกอบได้"


def from_tile():
    """Cases filed from the ปัญหา/ข้อเสนอแนะ tile, under any name the tile has had."""
    from django.db.models import Q

    from operations.models import LineFlow, LineMenuItem

    labels = {
        TILE,
        *LineFlow.objects.filter(key=FLOW_KEY).values_list("tile", flat=True),
        *LineMenuItem.objects.filter(action=LineMenuItem.Action.GENERAL).values_list(
            "label", flat=True
        ),
    }
    condition = Q(pk__in=[])
    for label in filter(None, labels):
        condition |= Q(menu_label=label) | Q(menu_label__startswith=f"{label} · ")
    return condition


def choices():
    """[(code, label)]: the active real divisions in menu order, then the admins."""
    divisions = Division.objects.filter(is_active=True, kind="division")
    real = divisions.filter(is_synthetic=False)
    if not real.exists():  # a demo with only the synthetic divisions: those, by code
        return [*((d.code, d.name) for d in divisions.order_by("code")), (ADMIN, ADMIN_LABEL)]
    found = {d.code: d for d in real}
    picked = []
    for code in ORDER:
        division = next((found[c] for c in (code, *EARLIER.get(code, ())) if c in found), None)
        if division is not None:
            picked.append((division.code, division.name))
    return [*picked, (ADMIN, ADMIN_LABEL)]


def target(code):
    """Who the summary says it goes to: the division's full name, or the admins."""
    if code == ADMIN:
        return ADMIN_TARGET
    division = Division.objects.filter(code=code).first()
    return division.name if division else code


def carousel(buttons):
    """A Flex carousel with one small bubble per (title, postback data); the whole bubble is
    the button, and the title shows in the chat when tapped."""
    bubbles = [
        {
            "type": "bubble",
            "size": "micro",
            "body": {
                "type": "box",
                "layout": "vertical",
                "justifyContent": "center",
                "height": "96px",
                "action": {"type": "postback", "data": data, "displayText": title},
                "contents": [
                    {
                        "type": "text",
                        "text": title,
                        "wrap": True,
                        "weight": "bold",
                        "size": "sm",
                        "align": "center",
                    }
                ],
            },
        }
        for title, data in buttons
    ]
    return {
        "type": "flex",
        "altText": PROMPT,
        "contents": {"type": "carousel", "contents": bubbles},
    }


def done_text(case):
    """Q26: what the requester reads once the case is in."""
    if case.held_by_admin:
        return f"ส่งเรื่อง {case.number} แล้ว แอดมินจะตอบเองหรือส่งให้กองที่ดูแล ดูความคืบหน้าได้ที่ เรื่องของฉัน"
    return f"ส่งเรื่อง {case.number} ถึง{case.owner_division.name}แล้ว ดูความคืบหน้าได้ที่ เรื่องของฉัน"


# --- Publishing the flow on production (Q22, Q24) ------------------------------------------

FIXTURE = "line_flow_general_v2.json"
MARK = "ADR 0030 อัตโนมัติ"  # the version's note; once there, a later migrate leaves it alone
TEST_TEXT = "ทดสอบ"


def simulate(graph):
    """Walk the flow as a requester would, tapping every bubble of its first box to the summary,
    typing where it asks, then เปลี่ยนกอง back to the summary. Returns what went wrong."""
    from operations import flow_runner as fr  # local: flow_runner builds on conversation
    from operations import line_flows

    problems = [i["message"] for i in line_flows.check(graph) if i["level"] == "error"]
    entry = next((n for n in graph["nodes"] if n["kind"] == "entry"), None)
    first = fr.node(graph, fr.follow(graph, entry["id"])) if entry else None
    if first is None or first["config"].get("style") != "carousel" or not first["buttons"]:
        return [*problems, "กล่องแรกต้องเป็น carousel ที่มีปุ่มกอง"]
    real = {d.code for d in Division.objects.filter(is_active=True, is_synthetic=False)}

    def walk(nid, values):
        for _ in range(fr.MAX_HOPS):
            box = fr.node(graph, nid)
            if box is None:
                return None, "ทางตันก่อนถึงหน้าสรุป"
            if box["kind"] == "summary":
                return box, ""
            if box["kind"] == "branch":
                nid = fr.branch_target(graph, box, values)
            elif box["kind"] == "ask":
                values[box["config"].get("field", "")] = TEST_TEXT
                nid = fr.follow(graph, box["id"])
            elif box["kind"] in ("entry", "note", "message", "ai_read"):
                nid = fr.follow(graph, box["id"])
            else:
                return None, f"เจอกล่อง '{box['title']}' ก่อนถึงหน้าสรุป"
        return None, "flow วนไม่จบ"

    for button in first["buttons"]:
        name = button.get("full") or button["label"]
        values = fr.assign({}, button["set"])
        picked = values.get("division", "")
        if picked != ADMIN and picked not in real:
            problems.append(f"{name}: ไม่พบกอง '{picked}' ที่ใช้งานอยู่")
            continue
        summary, problem = walk(fr.follow(graph, first["id"], button["id"]), values)
        if summary is None:
            problems.append(f"{name}: {problem}")
            continue
        if not values.get("description"):
            problems.append(f"{name}: ไม่ได้ถามรายละเอียด")
        targets = {b["label"]: fr.follow(graph, summary["id"], b["id"]) for b in summary["buttons"]}
        made = fr.node(graph, targets.get("ยืนยันส่งเรื่อง"))
        if made is None or made["kind"] != "create_case":
            problems.append(f"{name}: ปุ่มยืนยันส่งเรื่องไม่ได้สร้างเรื่อง")
        if targets.get("เปลี่ยนกอง") != first["id"]:
            problems.append(f"{name}: ปุ่มเปลี่ยนกองไม่กลับไปเลือกกอง")
            continue
        again, problem = walk(fr.follow(graph, first["id"], first["buttons"][0]["id"]), values)
        if again is None or again["id"] != summary["id"]:
            problems.append(f"{name}: เปลี่ยนกองแล้วไม่กลับมาที่สรุป ({problem or 'ถามซ้ำ'})")
    return problems


def autopublish(out=None):
    """On production after migrate: put the flow into the tile's draft, walk it, and publish it
    if every bubble reaches the summary; otherwise keep the draft and say what failed."""
    import json
    from pathlib import Path

    from django.conf import settings
    from django.db import transaction

    from operations import identity, line_flows
    from operations.models import LineFlow, LineFlowVersion

    if settings.APP_ENV != "production":
        return None
    say = out or (lambda text: None)
    data = json.loads(
        (Path(settings.BASE_DIR) / "operations" / "fixtures" / FIXTURE).read_text(encoding="utf-8")
    )
    spec, graph = data["flow"], line_flows.clean_graph(data["graph"])
    actor = identity.system_user()
    with transaction.atomic():
        flow = (
            LineFlow.objects.select_for_update().filter(tile=spec["tile"]).first()
            or LineFlow.objects.select_for_update().filter(key=spec["key"]).first()
        )
        if flow is None:
            flow = LineFlow.objects.create(key=spec["key"], name=spec["name"], tile=spec["tile"])
        if flow.versions.filter(note__startswith=MARK).exists():
            return None
        version = line_flows.draft_of(flow)
        if version is None:
            last = flow.versions.order_by("-number").values_list("number", flat=True).first()
            version = LineFlowVersion(flow=flow, number=(last or 0) + 1, created_by=actor)
        version.graph, version.checks = graph, line_flows.check(graph)
        version.note, version.updated_by = f"{MARK}: {data['note']}", actor
        version.save()
        problems = simulate(graph)
        if problems:
            identity.audit(actor, "line_flow.autopublish_failed", version, problems=problems)
            say("flow ปัญหา/ข้อเสนอแนะ ไม่ผ่านการทดสอบ ยังเป็นร่าง:\n- " + "\n- ".join(problems))
            return False
        line_flows.publish_as(
            actor, version, "เผยแพร่อัตโนมัติหลังติดตั้ง ทดสอบกดทุกกองถึงหน้าสรุปผ่าน (ADR 0030)"
        )
    say(f"flow ปัญหา/ข้อเสนอแนะ ผ่านการทดสอบและเผยแพร่แล้ว (รุ่น {version.number})")
    return True
