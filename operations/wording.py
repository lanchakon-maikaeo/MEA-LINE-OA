"""Thai text for the services' English messages, as staff read them on the web (9 ตุลาคม 2569).

The services keep English messages for the API contract; a person never sees them. Each entry
says what went wrong and what to do next. `test_wording_covers_the_services` scans the code so a
new English message cannot reach the screen untranslated.
"""

import re

from operations.models import Case

FIELDS = {
    "reason": "เหตุผล",
    "purpose": "จุดประสงค์",
    "description": "รายละเอียด",
    "impact": "ผลกระทบ",
    "condition": "สภาพของ",
    "intake_note": "บันทึกการรับแจ้ง",
    "recipient_name": "ชื่อผู้รับของ",
    "document_number": "เลขเอกสาร",
    "carrier_name": "ชื่อผู้นำส่ง",
    "message": "ข้อความถึงผู้แจ้ง",
    "note": "หมายเหตุ",
    "summary": "สรุปผล",
    "task": "งานที่ขอ",
    "reference": "เลขเอกสารอ้างอิง",
    "location": "สถานที่",
    "source_reference": "เอกสารอ้างอิง",
    "related_case_number": "เรื่องเดิมที่อ้าง",
    "reason_category": "ประเภทเหตุผล",
}
FORM_EXPIRED = "ฟอร์มนี้เก่าแล้ว เปิดฟอร์มใหม่แล้วบันทึกอีกครั้ง"
FORM_BROKEN = "ข้อมูลในฟอร์มไม่ครบหรือไม่ถูกต้อง เปิดฟอร์มใหม่แล้วกรอกอีกครั้ง"
QUANTITY = "จำนวนต้องเป็นตัวเลขมากกว่า 0"
FACTS = "ข้อเท็จจริงไม่ถูกต้อง เลือกจากรายการในฟอร์ม"

EXACT = {
    # contracts
    "idempotency_key must be 8-128 safe characters.": FORM_EXPIRED,
    "expected_revision must be a positive integer.": FORM_EXPIRED,
    # real structure (ADR 0008): topics, filing without an intake desk, แผนก
    "This topic lists no items.": "หัวข้อนี้ไม่ต้องระบุรายการของ ลบรายการของออก",
    "Choose a result category that fits this case.": (
        "เลือกหมวดผลให้ตรงกับเรื่อง (ข้อเสนอแนะมีหมวดผลของตัวเอง)"
    ),
    "topic must be meter, transformer, other, usage or suggestion.": "เลือกหัวข้อจากรายการในฟอร์ม",
    "sap_kind must be slip, return_slip, work_order, meter, project or memo.": (
        "เลือกชนิดเลขติดตาม: ใบเบิก ใบส่งคืน ใบสั่งงานจาก SAP หมายเลขเครื่องวัดฯ โครงการ หรือ M/N"
    ),
    "intake_data must be a small object.": "ข้อมูลที่ flow เก็บเพิ่มยาวเกินไป",
    "Only division staff can file a case for a requester.": (
        "เจ้าหน้าที่ที่สังกัดกองเท่านั้นบันทึกเรื่องแทนผู้แจ้งได้"
    ),
    "No intake division or coordinator is configured.": (
        "ระบบยังไม่ได้ตั้งผู้ประสานระดับฝ่าย จึงรับเรื่องใหม่ไม่ได้ แจ้งแอดมิน"
    ),
    "Only staff of the owner division can set the section.": "เจ้าหน้าที่ของกองเจ้าภาพเท่านั้นระบุแผนกได้",
    "Choose a section of the owner division.": "เลือกแผนกของกองเจ้าภาพ",
    "The case is already with this section.": "เรื่องอยู่กับแผนกนี้แล้ว",
    # ADR 0010: location, anonymous and internal reporters
    "latitude and longitude must be numbers.": "ตำแหน่งไม่ถูกต้อง ส่งตำแหน่งใหม่อีกครั้ง",
    "Anonymous reports are turned off.": "ตอนนี้ต้องลงทะเบียนก่อนแจ้งเรื่อง",
    "Withdrawals and returns need a registered requester.": (
        "เบิกและคืนของต้องลงทะเบียนและยืนยันตัวตนก่อน"
    ),
    "An anonymous report needs a location.": "แจ้งแบบไม่ระบุตัวตนต้องส่งตำแหน่งที่พบเรื่อง",
    "Daily limit of anonymous reports reached.": "วันนี้แจ้งแบบไม่ระบุตัวตนครบจำนวนแล้ว ลองใหม่พรุ่งนี้",
    "Only a problem with no division waits for the admins.": (
        "เรื่องที่ส่งถึงแอดมินต้องเป็นเรื่องแจ้งปัญหาที่ไม่ได้เลือกกอง"
    ),
    # registry_ops (ระบบทะเบียน)
    "This key was used for another registry request.": (
        "คำขอนี้ส่งไประบบทะเบียนแล้วด้วยข้อมูลอื่น เปิดฟอร์มใหม่"
    ),
    "The registry has not answered; ask again.": (
        "ระบบทะเบียนยังไม่ตอบ รอสักครู่แล้วลองอีกครั้ง (ระบบจะไม่ส่งซ้ำ)"
    ),
    "quantity must be a string or integer, not a float.": QUANTITY,
    "quantity must be a positive number.": QUANTITY,
    "quantity must be greater than zero.": QUANTITY,
    "serials must be a list.": "รูปแบบเลขเครื่องไม่ถูกต้อง ใส่บรรทัดละหนึ่งเลข",
    "Each serial must be 1-64 characters.": "เลขเครื่องแต่ละเลขยาวได้ 1 ถึง 64 ตัวอักษร",
    "Serials must not repeat.": "มีเลขเครื่องซ้ำกัน ตรวจแล้วลบเลขที่ซ้ำ",
    "payload must be an object.": FORM_BROKEN,
    "kind must be withdrawal, return or problem.": "เลือกชนิดเรื่อง: เบิก ส่งคืน หรือแจ้งปัญหา",
    "needed_by must be YYYY-MM-DD.": "วันที่ต้องใช้ไม่ถูกต้อง เลือกจากปฏิทิน",
    "At least one item line is required.": "ใส่รายการของอย่างน้อย 1 รายการ",
    "item_code is required.": "เลือกรายการของ",
    "quantity is required.": "ใส่จำนวน",
    "Serials are recorded at issue, not on request.": (
        "ไม่ต้องใส่เลขเครื่องตอนขอเบิก เลขเครื่องบันทึกตอนจ่ายของ"
    ),
    "Combine quantities of the same item into one line.": "มีรายการของซ้ำ รวมจำนวนไว้ในบรรทัดเดียว",
    "requester_id is required.": "เลือกผู้แจ้ง",
    "channel must be phone, letter, counter or fallback_form.": "เลือกช่องทางที่หน่วยงาน (เขต/ฝ่าย)แจ้ง",
    "reported_at is required for the fallback form.": "ใส่เวลาแจ้งจริงเมื่อรับเรื่องจากแบบฟอร์มสำรอง",
    "reported_at must be an ISO date-time.": "เวลาแจ้งจริงไม่ถูกต้อง เลือกวันและเวลาอีกครั้ง",
    "reported_at must be within the past 30 days.": (
        "เวลาแจ้งจริงต้องอยู่ในช่วง 30 วันที่ผ่านมา และไม่ใช่เวลาในอนาคต"
    ),
    # coordination
    "child (its number in this case) is required.": "เลือกงานย่อย",
    "Unknown child work for this case.": "ไม่พบงานย่อยนี้ในเรื่อง โหลดหน้าเรื่องใหม่",
    "rule is R07, R08, R09 or empty.": "เลือกกติกาที่ใช้ให้ถูกต้อง",
    "lines lists line numbers.": "เลือกรายการของในเรื่องนี้",
    "Unknown line_no for this case.": "ไม่พบรายการของนี้ในเรื่อง โหลดหน้าเรื่องใหม่",
    "result is done or failed.": "เลือกผล: ทำเสร็จ หรือ ทำไม่ได้",
    # fulfilment
    "List each line once.": "มีรายการของซ้ำ เลือกแต่ละรายการครั้งเดียว",
    "Requesters do not decide withdrawals.": "ผู้แจ้งอนุมัติเรื่องเบิกไม่ได้",
    "delivery (its number in this case) is required.": "เลือกการส่งมอบ",
    "Unknown delivery for this case.": "ไม่พบการส่งมอบนี้ในเรื่อง โหลดหน้าเรื่องใหม่",
    "Each line has line_no, quantity and serials.": "ข้อมูลรายการที่จ่ายไม่ครบ ใส่จำนวนและเลขเครื่อง",
    "confirm for this kind arrives with M09.": "เรื่องชนิดนี้ยังบันทึกการยืนยันรับไม่ได้",
    "result is full or mismatch.": "เลือกผลการรับ: ได้ครบ หรือ ไม่ตรง",
    "channel must be phone, letter or counter.": "เลือกช่องทางที่หน่วยงาน (เขต/ฝ่าย)แจ้ง",
    "resolution is issue_error or lost.": "เลือกผลการตรวจส่วนต่าง",
    "lines lists the line numbers to stop.": "เลือกรายการที่จะยุติ",
    "close for this kind arrives with M08/M09.": "เรื่องชนิดนี้ปิดเองเมื่อรับคืนและตรวจสภาพครบ",
    # problems
    # returns
    "unit (its number in this case) is required.": "เลือกหน่วยของที่คืน",
    "Unknown unit for this case.": "ไม่พบหน่วยนี้ในเรื่อง โหลดหน้าเรื่องใหม่",
    "to_date is before from_date.": "วันสิ้นสุดต้องไม่ก่อนวันเริ่ม",
    "items lists what arrived: item_code, quantity, serials.": "ใส่ของที่มาถึงอย่างน้อย 1 รายการ",
    "Each item has item_code, quantity and serials.": (
        "ข้อมูลของที่มาถึงไม่ครบ ใส่รายการ จำนวน และเลขเครื่อง"
    ),
    "split maps outcomes (ready/repair/other) to quantities.": (
        "แบ่งจำนวนตามผลตรวจ: พร้อมใช้ ต้องซ่อม หรือดำเนินการอื่น"
    ),
    "resolution is accepted, sent_back or registry_adjusted.": "เลือกผลการตรวจส่วนต่างการรับคืน",
    "category is not_returned, lost or other.": "เลือกเหตุที่ยุติ: ไม่ส่งคืน สูญหาย หรืออื่น ๆ",
    # routing
    "Choose an active division.": "เลือกกองที่ยังเปิดใช้งาน",
    "The case already belongs to this division.": "เรื่องนี้อยู่ที่กองนี้อยู่แล้ว",
    "A handoff is already waiting for an answer.": "มีการส่งต่อที่รอกองปลายทางตอบอยู่แล้ว",
    "The route is waiting for a coordinator's decision.": "เรื่องนี้รอผู้ประสานตัดสินเส้นทางอยู่",
    "Differs from the proposal: choose a reason category and explain briefly.": (
        "กองที่เลือกต่างจากที่ระบบเสนอ เลือกประเภทเหตุผลและอธิบายสั้น ๆ"
    ),
    "No handoff is waiting for an answer.": "ไม่มีการส่งต่อที่รอตอบ โหลดหน้าเรื่องใหม่",
    "Only the owner or the receiving division can ask.": (
        "เฉพาะกองเจ้าภาพหรือกองปลายทางขอให้ผู้ประสานตัดสินได้"
    ),
    "A coordinator's decision is already requested.": "ขอให้ผู้ประสานตัดสินไว้แล้ว",
    "Routing decisions need an active routing authority.": "ต้องมีอำนาจตัดสินเส้นทางจึงตัดสินได้",
    "Nothing is waiting for a coordinator on this case.": "เรื่องนี้ไม่มีอะไรรอผู้ประสานตัดสิน",
    "Only staff of the owner division can set the priority.": (
        "เฉพาะเจ้าหน้าที่ของกองเจ้าภาพยืนยันระดับความเร่งด่วนได้"
    ),
    "Priority must be P0-P3.": "เลือกระดับความเร่งด่วน",
    "Give the new facts behind this level.": "ระบุข้อเท็จจริงใหม่ประกอบการเปลี่ยนระดับ",
    "equipment is a list of {category, item}.": FACTS,
    "equipment rows are {category, item}.": FACTS,
    "Unknown category.": "เลือกหมวดอุปกรณ์",
    "Give facts to confirm or names to clear.": "เลือกข้อเท็จจริงที่จะยืนยันหรือล้างอย่างน้อยหนึ่งข้อ",
    "severe_impact is true, false or null (not stated).": FACTS,
    "A handoff or routing decision is still open.": (
        "ยังมีการส่งต่อหรือการตัดสินเส้นทางค้างอยู่ จบคัดกรองได้เมื่อเรื่องนั้นจบแล้ว"
    ),
    "Hand the case to the responsible division first.": (
        "ส่งต่อเรื่องให้กองที่รับผิดชอบก่อน แล้วกองนั้นจึงกดคัดกรองเสร็จได้"
    ),
    "The owner division confirms the priority first.": ("ยืนยันระดับความเร่งด่วนก่อน แล้วจึงกดคัดกรองเสร็จ"),
    # services
    "Requester is not active.": "บัญชีผู้แจ้งนี้ยังไม่ได้รับรองหรือถูกปิดแล้ว",
    "No role.": "บัญชีนี้ยังไม่มีบทบาทในกองใด ติดต่อแอดมิน",
    "Key was already used with a different request.": FORM_EXPIRED,
    "Only intake staff can file a case for a requester.": (
        "เฉพาะเจ้าหน้าที่กองรับเรื่องกลางบันทึกเรื่องแทนผู้แจ้งได้"
    ),
    "Requester must be approved and active.": "ผู้แจ้งต้องได้รับการรับรองและยังใช้งานอยู่",
    "No active intake division is configured.": "ยังไม่ได้ตั้งกองรับเรื่องกลาง ติดต่อแอดมิน",
    "Requester district is not active.": "หน่วยงาน (เขต/ฝ่าย)ของผู้แจ้งถูกปิดใช้งาน ติดต่อแอดมิน",
    "Related case not found for this requester.": "ไม่พบเรื่องเดิมที่อ้างของผู้แจ้งคนนี้ ตรวจเลขเรื่อง",
    "Only staff of the owner division can do this.": "เฉพาะเจ้าหน้าที่ของกองเจ้าภาพทำขั้นนี้ได้",
    "Only staff of the owner division can take a case.": ("เฉพาะเจ้าหน้าที่ของกองเจ้าภาพรับผิดชอบเรื่องได้"),
    "You are already responsible for this case.": "คุณเป็นผู้รับผิดชอบเรื่องนี้อยู่แล้ว",
    "Only a supervisor of the owner division can assign.": (
        "เฉพาะ Supervisor ของกองเจ้าภาพมอบหมายผู้รับผิดชอบได้"
    ),
    "assignee_id is required.": "เลือกผู้รับผิดชอบ",
    "Assignee must be active staff of the owner division.": (
        "ผู้รับผิดชอบต้องเป็นเจ้าหน้าที่ของกองเจ้าภาพที่ยังใช้งานอยู่"
    ),
    "This person is already responsible.": "คนนี้รับผิดชอบเรื่องอยู่แล้ว",
    "The case can no longer be cancelled by the requester.": (
        "ผู้แจ้งยกเลิกเรื่องนี้ไม่ได้แล้ว เพราะเริ่มดำเนินการแล้ว"
    ),
    "A reason is required.": "ใส่เหตุผลก่อนบันทึก",
    "Nothing was approved; no resolution is needed.": "ยังไม่มีรายการที่อนุมัติ จึงไม่ต้องปิดส่วนต่าง",
    "Unknown action.": "ไม่พบขั้นนี้ โหลดหน้าเรื่องใหม่",
    "The case changed; reload and try again.": "เรื่องเพิ่งเปลี่ยน โหลดหน้าเรื่องใหม่แล้วลองอีกครั้ง",
}


def _field(name):
    return FIELDS.get(name, name)


def _level(value):
    from operations import queues

    return queues.level_name(value) if value else value


def _status(value):
    return Case.Status(value).label if value in Case.Status.values else value


PATTERNS = [
    (r"(?P<f>\w+) must be text\.", lambda m: f"ช่อง{_field(m['f'])}ต้องเป็นข้อความ"),
    (r"(?P<f>\w+) is required\.", lambda m: f"กรอกช่อง{_field(m['f'])}ก่อนบันทึก"),
    (r"(?P<f>\w+) is too long\.", lambda m: f"ช่อง{_field(m['f'])}ยาวเกินกำหนด ตัดให้สั้นลง"),
    (r"\w+ must be an object\.", lambda m: FORM_BROKEN),
    (r"Unknown \w+ fields: .*", lambda m: FORM_BROKEN),
    (
        r"(?P<code>\S+) is counted in whole (?P<unit>.+)\.",
        lambda m: f"{m['code']} นับเป็น{m['unit']} ใส่จำนวนเต็ม",
    ),
    (
        r"(?P<code>\S+) allows at most 2 decimal places\.",
        lambda m: f"{m['code']} ใส่ทศนิยมได้ไม่เกิน 2 ตำแหน่ง",
    ),
    (
        r"lines must be a list of at most (?P<n>\d+)\.",
        lambda m: f"รายการของมากเกินกำหนด ใส่ได้ไม่เกิน {m['n']} รายการ",
    ),
    (r"\w+ must list the lines \(line_no\) to act on\.", lambda m: "เลือกรายการของอย่างน้อย 1 รายการ"),
    (r"Each line has line_no and .*", lambda m: "ข้อมูลรายการของไม่ครบ กรอกให้ครบทุกช่อง"),
    (r"(?P<f>\w+) must be YYYY-MM-DD\.", lambda m: f"วันที่ในช่อง{_field(m['f'])}ไม่ถูกต้อง"),
    (r"Outcomes are .*", lambda m: "เลือกผลตรวจ: พร้อมใช้ ต้องซ่อม หรือดำเนินการอื่น"),
    (
        r"(?P<v>P\d) is below the rule floor (?P<f>P\d); an authority exception is required\.",
        lambda m: (
            f"ระดับ{_level(m['v'])}ต่ำกว่าระดับขั้นต่ำของกติกา ({_level(m['f'])}) ต้องมีอำนาจข้อยกเว้นระดับ"
        ),
    ),
    (r"Unknown item (?P<i>.+)\.", lambda m: f"ไม่พบรายการของ {m['i']}"),
    (r"(?P<i>\S+) is not in (?P<c>\w+)\.", lambda m: f"{m['i']} ไม่อยู่ในหมวดที่เลือก"),
    (r"Unknown facts: .*", lambda m: FACTS),
    (r"\w+ is true or false\.", lambda m: FACTS),
    (
        r"Confirm a level of at least (?P<f>P\d) first\.",
        lambda m: f"ยืนยันระดับไม่ต่ำกว่า{_level(m['f'])}ก่อน",
    ),
    (r"(?P<c>\S+) is not tracked by serial\.", lambda m: f"{m['c']} ไม่ใช้เลขเครื่อง ลบเลขเครื่องออก"),
    (
        r"Give one serial per returned (?P<c>\S+)\.",
        lambda m: f"ใส่เลขเครื่องให้ครบตามจำนวน {m['c']} ที่คืน",
    ),
    (r"\w+ arrives with .*", lambda m: "ขั้นนี้ยังใช้กับเรื่องชนิดนี้ไม่ได้"),
    (
        r"(?P<a>\w+) is not allowed while (?P<s>\w+)\.",
        lambda m: f"ทำขั้นนี้ไม่ได้ขณะเรื่องอยู่ในสถานะ{_status(m['s'])} โหลดหน้าเรื่องใหม่",
    ),
]
PATTERNS = [(re.compile(rf"^{p}$"), make) for p, make in PATTERNS]


def thai(message):
    """The Thai text for a service message, or None when there is none."""
    if not message:
        return None
    if message in EXACT:
        return EXACT[message]
    for pattern, make in PATTERNS:
        match = pattern.match(message)
        if match:
            return make(match)
    return None
