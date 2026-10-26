# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
import logging
import re
from enum import StrEnum

# packages ที่ install เข้ามา
from pydantic import BaseModel, Field
from sqlalchemy import insert, select

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from line_bot.actions.state import clear_state, new_state, set_state, ADD_ITEM, CANCEL, CONFIRM, DELETE_ITEM, EDIT, EDIT_ITEM, SUMMARY
from line_bot.intents import Intent
from line_bot.line.reply import quick_reply, reply
from line_bot.schemas import UserInfo, UserMessage
from shared.db.main.models import MeterReportOrder, MeterReportOrderStatus
from shared.db.main.session import get_session
from shared.schemas.main.meter_report_order import MeterReportOrderCreate

logger = logging.getLogger(__name__)

REQUIRES_AUTH = True

DIGITS_PATTERN = re.compile(r"[0-9]+") # เลขข้อที่เลือก (ไม่ใช้ isdigit() เพราะจะรับเลขไทย ๑๒๓ ด้วย)
ISSUE_PREVIEW_MAX = 200 # หน้าสรุปตัดรายละเอียดปัญหาให้สั้น กันข้อความเกิน 5000 ตัวอักษรของ Line
MAX_ITEMS = 10 # ต่อการแจ้ง 1 ครั้ง (ปุ่มเลือกข้อ 10 ปุ่ม + สรุปรายการ + ยกเลิก ยังไม่เกิน 13 ปุ่มของ quick reply)
METER_ID_PATTERN = re.compile(r"[0-9]+") # เอาเลขชุดแรก: "เลขคือ 1234, 4567" → "1234"
NO_NOTICE = "" # ตรงรูปแบบ แต่ไม่มีอะไรต้องบอกก่อนคำถามถัดไป

class MeterReportStep(StrEnum):
    WAIT_METER_ID = "wait_meter_id"       # ใช้ draft
    WAIT_PROJECT = "wait_project"         # เลือกจากปุ่ม ไม่ต้องใช้ draft
    WAIT_ISSUE = "wait_issue"             # ใช้ draft
    WAIT_ADD_MORE = "wait_add_more"       # กรอกครบ 1 รายการแล้ว ถามว่าจะเพิ่มอีกไหม
    WAIT_SUBMIT = "wait_submit"           # สรุปทุกรายการ
    WAIT_EDIT_ITEM = "wait_edit_item"     # เลือกว่าจะแก้ข้อไหน
    WAIT_EDIT_FIELD = "wait_edit_field"   # เลือกว่าจะแก้ช่องไหน
    WAIT_DELETE_ITEM = "wait_delete_item" # เลือกว่าจะลบข้อไหน

class MeterProject(StrEnum):
    AMR = "AMR"
    SMG = "SMG"
    BLUETOOTH = "Bluetooth"

class EditField(StrEnum):
    METER_ID = "เลขมิเตอร์"
    PROJECT = "โครงการ"
    ISSUE = "ปัญหา"

EDIT_FIELD_STEPS = {
    EditField.METER_ID: MeterReportStep.WAIT_METER_ID,
    EditField.PROJECT: MeterReportStep.WAIT_PROJECT,
    EditField.ISSUE: MeterReportStep.WAIT_ISSUE
}

# 1 รายการ = 1 เรื่องใน DB (ชื่อ field ตรงกับ MeterReportOrderCreate)
class MeterReportItem(BaseModel):
    meter_id: str | None = None
    project_name: MeterProject | None = None
    issue_detail: str | None = None

# state["data"] ของ action นี้
class MeterReportData(BaseModel):
    items: list[MeterReportItem] = Field(default_factory=list) # ยืนยันครบทุกช่องแล้ว
    current: MeterReportItem | None = None                      # รายการที่กำลังกรอก / แก้
    edit_index: int | None = None                               # None = รายการใหม่, ตัวเลข = กำลังแก้ข้อนั้น (เริ่มที่ 0)

def can_add_more(data: MeterReportData) -> bool:
    return len(data.items) < MAX_ITEMS

def start_new_item(state: dict, data: MeterReportData):
    data.current = MeterReportItem()
    data.edit_index = None
    state["step"] = MeterReportStep.WAIT_METER_ID

def back_to_summary(state: dict, data: MeterReportData):
    data.current = None
    data.edit_index = None
    state["step"] = MeterReportStep.WAIT_SUBMIT

# ไปช่องแรกที่ยังว่าง ถ้าครบแล้ว: รายการใหม่ → ถามว่าจะเพิ่มอีกไหม (เต็มแล้วไปหน้าสรุปเลย) / แก้ข้อเดิม → กลับหน้าสรุป
# ตอนแก้ไข ช่องอื่นมีค่าอยู่แล้ว ยืนยันช่องที่แก้เสร็จจึงครบทันที
def go_next(state: dict, data: MeterReportData):
    current = data.current

    if current.meter_id is None:
        state["step"] = MeterReportStep.WAIT_METER_ID
        return

    if current.project_name is None:
        state["step"] = MeterReportStep.WAIT_PROJECT
        return

    if current.issue_detail is None:
        state["step"] = MeterReportStep.WAIT_ISSUE
        return

    if data.edit_index is not None:
        data.items[data.edit_index] = current
        back_to_summary(state, data)
        return

    data.items.append(current)
    data.current = None
    state["step"] = MeterReportStep.WAIT_ADD_MORE if can_add_more(data) else MeterReportStep.WAIT_SUBMIT

def preview(text: str) -> str:
    return text if len(text) <= ISSUE_PREVIEW_MAX else f"{text[:ISSUE_PREVIEW_MAX]}…"

def summary(items: list[MeterReportItem]) -> str:
    return "\n\n".join(
        f"{number}. เลขมิเตอร์: {item.meter_id}\nโครงการ: {item.project_name}\nปัญหา: {preview(item.issue_detail)}"
        for number, item in enumerate(items, start=1)
    )

def item_numbers(data: MeterReportData) -> list[str]:
    return [str(number) for number in range(1, len(data.items) + 1)] # ["1", "2", ...]

# คำถามของขั้นปัจจุบัน + ปุ่ม (ใช้ถามซ้ำได้ เช่น ตอบ FAQ แล้วถามต่อ)
def prompt(state: dict) -> tuple[str, list[str]]:
    step = state["step"]
    draft = state["draft"]
    data = MeterReportData.model_validate(state["data"])

    if step == MeterReportStep.WAIT_METER_ID:
        if draft is None:
            return 'ขอทราบ "เลขมิเตอร์ / MEA Number" ครับ', [CANCEL]

        return f'ต้องการยืนยัน "เลขมิเตอร์ / MEA Number":\n"{draft}" หรือไม่?', [CONFIRM, EDIT, CANCEL]

    if step == MeterReportStep.WAIT_PROJECT:
        return 'ขอทราบ "ชื่อโครงการของมิเตอร์" ครับ', [*MeterProject, CANCEL]

    if step == MeterReportStep.WAIT_ISSUE:
        if draft is None:
            return 'ขอทราบ "รายละเอียดของปัญหาที่พบ" ครับ', [CANCEL]

        return f'ต้องการยืนยัน "รายละเอียดของปัญหาที่พบ":\n"{draft}" หรือไม่?', [CONFIRM, EDIT, CANCEL]

    if step == MeterReportStep.WAIT_ADD_MORE:
        return f"เพิ่มรายการที่ {len(data.items)} แล้ว ต้องการทำรายการเพิ่มไหมครับ?", [ADD_ITEM, SUMMARY, CANCEL]

    if step == MeterReportStep.WAIT_SUBMIT:
        options = [CONFIRM, ADD_ITEM, EDIT_ITEM, DELETE_ITEM, CANCEL] if can_add_more(data) else [CONFIRM, EDIT_ITEM, DELETE_ITEM, CANCEL]
        return f"{summary(data.items)}\n\nต้องการยืนยันทำรายการหรือไม่?", options

    if step == MeterReportStep.WAIT_EDIT_ITEM:
        return "ต้องการแก้ไขรายการข้อไหนครับ?", [*item_numbers(data), SUMMARY, CANCEL]

    if step == MeterReportStep.WAIT_DELETE_ITEM:
        return "ต้องการลบรายการข้อไหนครับ?", [*item_numbers(data), SUMMARY, CANCEL]

    return f"ต้องการแก้ไขข้อมูลไหนของรายการที่ {data.edit_index + 1} ครับ?", [*EditField, SUMMARY, CANCEL]

# เลขข้อที่ user เลือก: ถูกต้อง = index (เริ่มที่ 0), ไม่ใช่เลขข้อที่มี = None
def parse_item_index(data: MeterReportData, text: str) -> int | None:
    if not DIGITS_PATTERN.fullmatch(text):
        return None

    index = int(text) - 1
    return index if 0 <= index < len(data.items) else None

# เลขมิเตอร์ซ้ำกับรายการอื่น (ไม่นับข้อที่กำลังแก้อยู่)
def is_duplicate_meter_id(data: MeterReportData, meter_id: str) -> bool:
    return any(item.meter_id == meter_id for index, item in enumerate(data.items) if index != data.edit_index)

# ปุ่มของ draft: True = เป็นปุ่ม ยืนยัน / แก้ไข, False = ไม่ใช่ (ให้ขั้นนั้นตรวจข้อความต่อ)
def handle_draft_buttons(state: dict, data: MeterReportData, field: str, text: str) -> bool:
    if state["draft"] is None:
        return False

    if text == CONFIRM:
        setattr(data.current, field, state["draft"])
        state["draft"] = None
        go_next(state, data)
        return True

    if text == EDIT:
        state["draft"] = None
        return True

    return False

# ทุก handle_*: ไม่ตรงรูปแบบ = None (ส่งให้ AI และห้ามแก้ state/data), ตรง = ข้อความที่ต้องบอกก่อนคำถามถัดไป
# ระหว่างรอยืนยัน ถ้าพิมพ์ค่าใหม่มา ให้ใช้ค่าใหม่แทน draft เดิม แล้วถามยืนยันอีกรอบ
def handle_meter_id(state: dict, data: MeterReportData, text: str) -> str | None:
    if handle_draft_buttons(state, data, "meter_id", text):
        return NO_NOTICE

    match = METER_ID_PATTERN.search(text)

    if match is None:
        return None

    meter_id = match.group()

    if is_duplicate_meter_id(data, meter_id):
        return f'มีเลขมิเตอร์ "{meter_id}" ในรายการแล้วครับ'

    state["draft"] = meter_id
    return NO_NOTICE

def handle_project(state: dict, data: MeterReportData, text: str) -> str | None:
    project = next((project for project in MeterProject if project.lower() == text.lower()), None) # "amr" ก็รับ

    if project is None:
        return None

    data.current.project_name = project
    go_next(state, data)
    return NO_NOTICE

# ข้อความอิสระ: รับทุกข้อความ (ปุ่ม FAQ / ยกเลิก ถูกดักใน process.py ก่อนถึงตรงนี้แล้ว)
def handle_issue(state: dict, data: MeterReportData, text: str) -> str | None:
    if handle_draft_buttons(state, data, "issue_detail", text):
        return NO_NOTICE

    state["draft"] = text
    return NO_NOTICE

def handle_add_more(state: dict, data: MeterReportData, text: str) -> str | None:
    if text == ADD_ITEM:
        start_new_item(state, data)
        return NO_NOTICE

    if text == SUMMARY:
        back_to_summary(state, data)
        return NO_NOTICE

    return None

# ยืนยัน ต้องบันทึก DB จึงแยกไปทำใน handle()
def handle_submit(state: dict, data: MeterReportData, text: str) -> str | None:
    if text == ADD_ITEM and can_add_more(data):
        start_new_item(state, data)
        return NO_NOTICE

    if text == EDIT_ITEM:
        state["step"] = MeterReportStep.WAIT_EDIT_ITEM
        return NO_NOTICE

    if text == DELETE_ITEM:
        state["step"] = MeterReportStep.WAIT_DELETE_ITEM
        return NO_NOTICE

    return None

# แก้บนสำเนา (current) ยืนยันครบแล้วจึงเอาไปแทนข้อเดิม
def handle_edit_item(state: dict, data: MeterReportData, text: str) -> str | None:
    if text == SUMMARY:
        back_to_summary(state, data)
        return NO_NOTICE

    index = parse_item_index(data, text)

    if index is None:
        return None

    data.edit_index = index
    data.current = data.items[index].model_copy()
    state["step"] = MeterReportStep.WAIT_EDIT_FIELD
    return NO_NOTICE

def handle_edit_field(state: dict, data: MeterReportData, text: str) -> str | None:
    if text == SUMMARY:
        back_to_summary(state, data)
        return NO_NOTICE

    if text not in EditField:
        return None

    state["step"] = EDIT_FIELD_STEPS[EditField(text)]
    return NO_NOTICE

# ลบจนไม่เหลือรายการ → กลับไปกรอกรายการแรกใหม่
def handle_delete_item(state: dict, data: MeterReportData, text: str) -> str | None:
    if text == SUMMARY:
        back_to_summary(state, data)
        return NO_NOTICE

    index = parse_item_index(data, text)

    if index is None:
        return None

    data.items.pop(index)

    if not data.items:
        start_new_item(state, data)
        return "ลบรายการแล้ว ไม่มีรายการเหลืออยู่ครับ"

    back_to_summary(state, data)
    return f"ลบรายการที่ {index + 1} แล้วครับ"

STEP_HANDLERS = {
    MeterReportStep.WAIT_METER_ID: handle_meter_id,
    MeterReportStep.WAIT_PROJECT: handle_project,
    MeterReportStep.WAIT_ISSUE: handle_issue,
    MeterReportStep.WAIT_ADD_MORE: handle_add_more,
    MeterReportStep.WAIT_SUBMIT: handle_submit,
    MeterReportStep.WAIT_EDIT_ITEM: handle_edit_item,
    MeterReportStep.WAIT_EDIT_FIELD: handle_edit_field,
    MeterReportStep.WAIT_DELETE_ITEM: handle_delete_item
}

# notice = ข้อความที่ต้องบอกก่อนคำถาม (Line ตอบได้ครั้งเดียว จึงรวมเป็นข้อความเดียว)
async def save_and_ask(message: UserMessage, state: dict, notice: str = NO_NOTICE):
    await set_state(message.user_id, state)
    text, options = prompt(state)

    if notice:
        text = f"{notice}\n\n{text}"

    await quick_reply(message.reply_token, text, options)

# บันทึกทุกรายการในคำสั่งเดียว (พังก็ไม่บันทึกเลยสักรายการ)
# สำเร็จ = meter_report_order_index ทุกรายการ, ไม่มี status ตั้งต้นใน DB = None
async def submit(user: UserInfo, items: list[MeterReportItem]) -> list[int] | None:
    async with get_session() as session:
        status_index = await session.scalar(
            select(MeterReportOrderStatus.meter_report_order_status_index)
            .where(MeterReportOrderStatus.meter_report_order_status_is_default)
            .limit(1)
        )

        if status_index is None:
            logger.error("no default meter_report_order_status")
            return None

        orders = [
            MeterReportOrderCreate(
                employee_index=user.employee_index,
                meter_report_order_status_index=status_index,
                **item.model_dump()
            ).model_dump()
            for item in items
        ]

        order_indexes = (await session.scalars(
            insert(MeterReportOrder)
            .values(orders)
            .returning(MeterReportOrder.meter_report_order_index)
        )).all()

        await session.commit()

    return sorted(order_indexes)

async def start(user: UserInfo, message: UserMessage) -> None:
    state = new_state(Intent.METER_REPORT, MeterReportStep.WAIT_METER_ID)
    state["data"] = MeterReportData(current=MeterReportItem()).model_dump()
    await save_and_ask(message, state)

async def handle(user: UserInfo, message: UserMessage, state: dict) -> bool:
    text = message.user_message.strip()
    data = MeterReportData.model_validate(state["data"])

    if state["step"] == MeterReportStep.WAIT_SUBMIT and text == CONFIRM:
        order_indexes = await submit(user, data.items)

        if order_indexes is None:
            await reply(message.reply_token, "ระบบขัดข้อง ไม่สามารถบันทึกรายการได้ กรุณาลองใหม่ภายหลังครับ")
            return True

        await clear_state(message.user_id)
        await reply(
            message.reply_token,
            f"แจ้งปัญหามิเตอร์สำเร็จ {len(order_indexes)} รายการ\nเลขที่: {', '.join(map(str, order_indexes))}"
        )
        return True

    notice = STEP_HANDLERS[state["step"]](state, data, text)

    if notice is None:
        return False

    state["data"] = data.model_dump()
    await save_and_ask(message, state, notice)
    return True
