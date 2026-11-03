"""The กอจ. menu: external-document lookup, explicit following, and history; no case draft."""

from urllib.parse import parse_qs

from django.core.exceptions import PermissionDenied, ValidationError

from operations import koj_tracking as tracking
from operations.models import KojRecord, KojSource, KojWatch
from operations.templatetags.ui import thai_datetime

PAGE = 3


def from_flow(turn, config):
    if not allowed(turn):
        return home(turn)
    equipment, service = config.get("equipment", ""), config.get("service", "")
    if service == "watches":
        return watches(turn)
    if equipment in tracking.EQUIPMENT:
        if service in tracking.SERVICES:
            return ask(turn, equipment, service, config.get("name", ""))
        return services(turn, equipment, config.get("name", ""))
    return home(turn)


def allowed(turn):
    identity = turn.identity
    if tracking.staff_can_read(identity.staff_user):
        return True
    requester = turn.requester
    return bool(
        identity.status == "active"
        and requester
        and requester.status == "active"
        and not any(
            getattr(requester, f, False) for f in ("is_internal", "is_anonymous", "is_external")
        )
    )


def deliver_updates(turn):
    """At the follower's next message: what changed in the กอจ. jobs they follow."""
    if turn.identity is None or not allowed(turn):
        return
    found = tracking.updates(turn.identity)
    if not found:
        return
    parts = []
    for watch, title, rows in found:
        service = tracking.SERVICES[watch.source.service]
        head = f"กอจ. · {title}\n{service} · {tracking.EQUIPMENT[watch.equipment]}"
        parts.append(head + "\n" + "\n\n".join(tracking.describe(r) for r in rows))
    turn.say("\n\n".join(parts)[:4500], [("รายการติดตาม กอจ.", "m=koj&do=watches")])


def is_item(item):
    return item.division_id and item.division.code in tracking.DIVISION_CODES


def live(item):
    """Whether this button answers with the กอจ. tracking of ADR 0026: an admin published its
    flow with a tracking box and no case box. Since ADR 0034 the button opens whatever flow is
    published, like every button; a flow that files cases leaves this off."""
    from operations import flow_runner

    tile = item.parent if item.parent_id else item  # a choice belongs to its tile
    published = flow_runner.published_for(tile.label)
    if published is None:
        return False
    kinds = {n["kind"] for n in published.graph["nodes"]}
    return "koj_tracking" in kinds and "create_case" not in kinds


def home(turn):
    if not allowed(turn):
        turn.set_prompt()
        from operations import conversation  # local: conversation builds on this module

        # 3 พ.ย. 2569: with the button back to the employee check for those who may take it.
        return conversation._menu(turn, "ติดตามงาน กอจ. ใช้ได้เฉพาะพนักงาน กฟน. ที่ยืนยันตัวตนแล้ว")
    turn.set_prompt()
    choices = [
        (tracking.EQUIPMENT[key], f"m=koj&do=equipment&key={key}")
        for key in ("transformer", "lbs", "rmu", "metering", "frtu")
    ]
    turn.say(
        "กอจ. · เลือกอุปกรณ์ที่ต้องการติดตาม",
        [*choices, ("อุปกรณ์อื่น", "m=koj&do=other"), ("รายการติดตาม กอจ.", "m=koj&do=watches")],
    )


def services(turn, equipment, name=""):
    if not allowed(turn):
        return home(turn)
    if equipment not in tracking.EQUIPMENT:
        return home(turn)
    if equipment == "other" and not name:
        return static(turn, "m=koj&do=name")
    turn.set_prompt()
    actions = ["withdrawal", "return"] + (["repair"] if equipment in {"lbs", "rmu"} else [])
    buttons = [
        turn.button(
            tracking.SERVICES[action],
            "koj_ask",
            {
                "equipment": equipment,
                "service": action,
                "name": name,
            },
        )
        for action in actions
    ]
    title = name or tracking.EQUIPMENT[equipment]
    turn.say(f"กอจ. · {title}\nเลือกบริการติดตาม", [*buttons, ("ย้อนกลับ", "m=koj&do=home")])


def static(turn, data):
    if not allowed(turn):
        return home(turn)
    query = parse_qs(data.split("&", 1)[1] if "&" in data else "")
    action = (query.get("do") or ["home"])[0]
    if action == "equipment":
        return services(turn, (query.get("key") or [""])[0])
    if action == "other":
        turn.set_prompt()
        return turn.say(
            "กอจ. · อุปกรณ์อื่น\nเลือกอุปกรณ์",
            [
                *[
                    (tracking.EQUIPMENT[key], f"m=koj&do=equipment&key={key}")
                    for key in ("bushing", "vt_lbs", "lv_cap")
                ],
                ("พิมพ์ชื่ออุปกรณ์", "m=koj&do=name"),
                ("ย้อนกลับ", "m=koj&do=home"),
            ],
        )
    if action == "name":
        turn.set_prompt("koj_name")
        return turn.say("พิมพ์ชื่ออุปกรณ์ที่ต้องการติดตาม", [("ย้อนกลับ", "m=koj&do=other")])
    if action == "watches":
        return watches(turn)
    if action == "view":
        record_id = (query.get("id") or [""])[0]
        record = (
            tracking.visible(turn.identity).filter(pk=int(record_id)).first()
            if record_id.isdigit()
            else None
        )
        if record is not None:
            return results(
                turn,
                record.source_id,
                record.reference_key,
                record.equipment,
                record.equipment_name if record.equipment == "other" else "",
            )
        return turn.say("ไม่พบรายการที่คุณมีสิทธิ์ดู", [("กลับเมนู กอจ.", "m=koj&do=home")])
    return home(turn)


def ask(turn, equipment, service, name=""):
    if not allowed(turn):
        return home(turn)
    tracking.group_for(equipment, service)
    turn.set_prompt("koj_reference", equipment=equipment, service=service, name=name)
    label = (
        "เลข WO, WBS, เลขรับ หรือใบแจ้งซ่อม"
        if service == "repair"
        else "เลขที่ใบเบิก หรือ ใบสั่งงานจาก SAP"
        if service == "withdrawal"
        else "เลขที่ใบส่งคืน หรือใบโอนขอคืน"
    )
    turn.say(
        f"{name or tracking.EQUIPMENT[equipment]} · {tracking.SERVICES[service]}\nพิมพ์{label}",
        [
            turn.button("ย้อนกลับ", "koj_services", {"equipment": equipment, "name": name}),
            ("ยกเลิก", "m=koj&do=home"),
        ],
    )


def on_text(turn, text):
    if not allowed(turn):
        return home(turn)
    if text.strip() == "ยกเลิก":
        return home(turn)
    if turn.conversation.prompt == "koj_name":
        name = " ".join(text.split())
        if not name or len(name) > 150:
            return turn.say("พิมพ์ชื่ออุปกรณ์ไม่เกิน 150 ตัวอักษร")
        return services(turn, "other", name)
    values = turn.conversation.prompt_data
    try:
        rows = tracking.find(
            turn.identity, values["equipment"], values["service"], text, values.get("name", "")
        )
    except (ValidationError, KeyError) as exc:
        return turn.say(
            "; ".join(exc.messages) if isinstance(exc, ValidationError) else "กรุณาเลือกบริการใหม่"
        )
    if not rows.exists():
        sources = [
            s
            for s in KojSource.objects.filter(enabled=True, service=values["service"])
            if s.default_equipment == values["equipment"]
            or values["equipment"] in s.equipment_map.values()
        ]
        stamp = "\n".join(f"{s.name}: ข้อมูล ณ {thai_datetime(s.last_synced_at)}" for s in sources)
        if not stamp:
            stamp = "ยังไม่มีแหล่งข้อมูลที่เปิดใช้งานในบริการนี้"
        if any(s.last_error for s in sources):
            stamp += "\nการดึงข้อมูลรอบล่าสุดไม่สำเร็จ กำลังใช้ข้อมูลรอบที่บันทึกไว้"
        contact = " / ".join(dict.fromkeys(s.contact for s in sources if s.contact))
        return turn.say(
            f"ไม่พบในข้อมูลล่าสุด\n{stamp}\nติดต่อ กอจ. {contact}".strip(),
            [("ค้นใหม่", "m=koj&do=home"), ("รายการติดตาม กอจ.", "m=koj&do=watches")],
        )
    # An exact document can have several rows. Prefixes offer distinct documents to choose.
    documents = list(
        rows.order_by("source_id", "reference_key", "equipment")
        .values_list("source_id", "reference_key", "equipment")
        .distinct()[:101]
    )
    if len(documents) > 100:
        return turn.say("พบเกิน 100 เอกสาร กรุณาพิมพ์เลขให้ครบขึ้นเพื่อค้นต่อ")
    if len(documents) > 1:
        return choices(turn, documents, values.get("name", ""))
    source_id, reference, equipment = documents[0]
    return results(turn, source_id, reference, equipment, values.get("name", ""))


def choices(turn, documents, name="", page=0):
    turn.set_prompt()
    buttons = [
        turn.button(
            f"{reference[:15]} · {i + 1}",
            "koj_result",
            {
                "source": source,
                "reference": reference,
                "equipment": equipment,
                "name": name,
            },
        )
        for i, (source, reference, equipment) in enumerate(
            documents[page * 8 : page * 8 + 8], page * 8
        )
    ]
    if len(documents) > (page + 1) * 8:
        buttons.append(
            turn.button(
                "หน้าถัดไป", "koj_choices", {"documents": documents, "name": name, "page": page + 1}
            )
        )
    turn.say(
        "พบหลายเอกสาร เลือกเลขที่ต้องการติดตาม\n"
        + "\n".join(
            f"{i + 1}. {reference} · {tracking.EQUIPMENT[equipment]} · แหล่ง {source}"
            for i, (source, reference, equipment) in enumerate(
                documents[page * 8 : page * 8 + 8], page * 8
            )
        ),
        [*buttons, ("ค้นใหม่", "m=koj&do=home")],
    )


def results(turn, source_id, reference, equipment, name="", page=0, history=False):
    rows = (
        tracking.visible(turn.identity)
        .filter(source_id=source_id, reference_key=reference, equipment=equipment)
        .select_related("source")
    )
    if name:
        rows = rows.filter(equipment_name__iexact=name)
    count = rows.count()
    if not count:
        return turn.say("ไม่พบรายการที่คุณมีสิทธิ์ดูในข้อมูลล่าสุด", [("กลับเมนู กอจ.", "m=koj&do=home")])
    turn.set_prompt()
    values = {"source": source_id, "reference": reference, "equipment": equipment, "name": name}
    if len(turn.replies) >= 4:
        return turn.say(
            "พบรายการ กอจ. กดดูผลค้นเพื่ออ่านต่อ",
            [turn.button("ดูผลค้น", "koj_result", {**values, "page": page, "history": history})],
        )
    limit = min(PAGE, 4 - len(turn.replies))
    shown = list(rows[page : page + limit])
    if not shown:
        return results(turn, source_id, reference, equipment, name)
    for i, row in enumerate(shown, page + 1):
        text = f"รายการ {i}/{count}\n{tracking.describe(row)}"
        if history:
            changes = row.history.order_by("-observed_at", "-pk")[:12]
            text += "\nประวัติสถานะล่าสุด\n" + "\n".join(
                f"{thai_datetime(h.observed_at)} · {h.status}" for h in changes
            )
        turn.say(text)
    watch = KojWatch.objects.filter(
        identity=turn.identity,
        source_id=source_id,
        reference_key=reference,
        equipment=equipment,
        equipment_name=name.casefold() if equipment == "other" else "",
    ).first()
    buttons = []
    if watch and watch.status == "active":
        buttons.append(turn.button("เลิกติดตาม", "koj_stop", {"watch": watch.pk}))
    else:
        follow = turn.button("ติดตามสถานะ", "koj_follow", {"record": shown[0].pk})
        buttons.append((*follow, "ติดตามการเปลี่ยนสถานะ"))
    buttons.append(
        turn.button("ประวัติสถานะ", "koj_result", {**values, "history": True, "page": page})
    )
    if history:
        for i, row in enumerate(shown, page + 1):
            if row.history.count() > 12:
                buttons.append(turn.button(f"ประวัติรายการ {i}", "koj_history", {"record": row.pk}))
    if page:
        buttons.append(
            turn.button(
                "หน้าก่อนหน้า",
                "koj_result",
                {**values, "page": max(0, page - PAGE), "history": history},
            )
        )
    if count > page + len(shown):
        buttons.append(
            turn.button(
                "หน้าถัดไป", "koj_result", {**values, "page": page + len(shown), "history": history}
            )
        )
    turn.say(
        "เลือกติดตามเพื่อรับอัปเดตทาง LINE",
        [*buttons, ("รายการติดตาม กอจ.", "m=koj&do=watches"), ("ค้นใหม่", "m=koj&do=home")],
    )


def watches(turn, mode="active", page=0):
    turn.set_prompt()
    mode = "completed" if mode == "completed" else "active"
    rows = (
        KojWatch.objects.filter(identity=turn.identity, status=mode)
        .select_related("source")
        .order_by("-updated_at", "-pk")
    )
    shown = list(rows[page * 6 : page * 6 + 6])
    buttons, lines = [], []
    for i, watch in enumerate(shown, page * 6 + 1):
        if not tracking.watch_rows(watch).exists():
            lines.append(f"{i}. {watch.reference_key} · ไม่พบรายการในข้อมูลล่าสุดหรือสิทธิ์เปลี่ยน")
            buttons.append(turn.button(f"เลิกติดตาม {i}", "koj_stop", {"watch": watch.pk}))
            continue
        lines.append(f"{i}. {watch.reference_key} · {tracking.EQUIPMENT[watch.equipment]}")
        buttons.append(
            turn.button(
                f"ดูรายการ {i}",
                "koj_result",
                {
                    "source": watch.source_id,
                    "reference": watch.reference_key,
                    "equipment": watch.equipment,
                    "name": watch.equipment_name,
                },
            )
        )
    if rows.count() > (page + 1) * 6:
        buttons.append(turn.button("หน้าถัดไป", "koj_watches", {"mode": mode, "page": page + 1}))
    if page:
        buttons.append(turn.button("หน้าก่อนหน้า", "koj_watches", {"mode": mode, "page": page - 1}))
    other = "active" if mode == "completed" else "completed"
    buttons.append(
        turn.button(
            "กำลังติดตาม" if other == "active" else "รายการที่จบแล้ว", "koj_watches", {"mode": other}
        )
    )
    title = "รายการติดตามที่จบแล้ว" if mode == "completed" else "รายการติดตาม กอจ."
    turn.say(
        title + "\n" + ("\n".join(lines) or "ยังไม่มีรายการ"),
        [*buttons, ("กลับเมนู กอจ.", "m=koj&do=home")],
    )


def on_token(turn, token):
    if not allowed(turn):
        return home(turn)
    values = token.value
    try:
        if token.action == "koj_history":
            record = (
                tracking.visible(turn.identity)
                .filter(pk=values.get("record"))
                .select_related("source")
                .first()
            )
            if record is None:
                raise PermissionDenied
            offset = max(0, int(values.get("offset", 0)))
            events = record.history.order_by("-observed_at", "-pk")
            shown = list(events[offset : offset + 12])
            text = f"ประวัติ {record.reference} · {record.equipment_name}\n" + "\n".join(
                f"{thai_datetime(h.observed_at)} · {h.status}" for h in shown
            )
            quick = []
            if events.count() > offset + 12:
                quick.append(
                    turn.button(
                        "เก่ากว่านี้", "koj_history", {"record": record.pk, "offset": offset + 12}
                    )
                )
            if offset:
                quick.append(
                    turn.button(
                        "ใหม่กว่านี้",
                        "koj_history",
                        {"record": record.pk, "offset": max(0, offset - 12)},
                    )
                )
            quick.append(("กลับเมนู กอจ.", "m=koj&do=home"))
            return turn.say(text, quick)
        if token.action == "koj_services":
            return services(turn, values["equipment"], values.get("name", ""))
        if token.action == "koj_ask":
            return ask(turn, values["equipment"], values["service"], values.get("name", ""))
        if token.action == "koj_follow":
            record = KojRecord.objects.filter(pk=values.get("record")).first()
            if record is None:
                raise PermissionDenied
            watch = tracking.subscribe(turn.identity, record)
            turn.say(
                "เริ่มติดตามการเปลี่ยนสถานะแล้ว"
                if watch.status == "active"
                else "งานนี้จบแล้ว เก็บในรายการติดตามที่จบแล้ว"
            )
            return results(
                turn, watch.source_id, watch.reference_key, watch.equipment, watch.equipment_name
            )
        if token.action == "koj_stop":
            tracking.unsubscribe(turn.identity, values.get("watch"))
            turn.say("เลิกติดตามเลขงานนี้แล้ว")
            return watches(turn)
        if token.action == "koj_result":
            return results(
                turn,
                values["source"],
                values["reference"],
                values["equipment"],
                values.get("name", ""),
                max(0, int(values.get("page", 0))),
                values.get("history", False),
            )
        if token.action == "koj_choices":
            return choices(
                turn,
                values["documents"],
                values.get("name", ""),
                max(0, int(values.get("page", 0))),
            )
        if token.action == "koj_watches":
            return watches(turn, values.get("mode", "active"), max(0, int(values.get("page", 0))))
    except (PermissionDenied, ValidationError, KeyError, ValueError):
        return turn.say("ไม่พบรายการที่คุณมีสิทธิ์ดำเนินการ กรุณาค้นใหม่", [("กลับเมนู กอจ.", "m=koj&do=home")])
    return home(turn)
