# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
import re
from enum import StrEnum

# packages ที่ install เข้ามา
from pydantic import BaseModel

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from line_bot.actions.state import clear_state, new_state, set_state, CANCEL
from line_bot.intents import Intent
from line_bot.line.reply import quick_reply, reply
from line_bot.schemas import UserInfo, UserMessage
from line_bot.user.lookup import search_employee
from line_bot.user.otp import verify_otp
from line_bot.user.register import register_user

REQUIRES_AUTH = False

DIGITS_PATTERN = re.compile(r"[0-9]+") # ตัวเลขล้วน (ไม่ใช้ isdigit() เพราะจะรับเลขไทย ๑๒๓ ด้วย)

# ระบบตรวจเองได้ ไม่ต้อง recheck จึงไม่ใช้ draft
class RegisterStep(StrEnum):
    WAIT_EMPLOYEE_ID = "wait_employee_id"
    WAIT_OTP = "wait_otp"

# state["data"] ของ action นี้
class RegisterData(BaseModel):
    employee_id: str | None = None

def prompt(state: dict) -> tuple[str, list[str]]:
    if state["step"] == RegisterStep.WAIT_EMPLOYEE_ID:
        return "ขอรหัสพนักงานหน่อยครับ", [CANCEL]

    return "ขอ OTP ครับ", [CANCEL]

async def save_and_ask(message: UserMessage, state: dict):
    await set_state(message.user_id, state)
    text, options = prompt(state)
    await quick_reply(message.reply_token, text, options)

# ตรงรูปแบบแต่ใช้ไม่ได้ (ไม่พบ / ผิด) → บอก user แล้วรอคำตอบขั้นเดิม
async def ask_retry(message: UserMessage, text: str):
    await quick_reply(message.reply_token, text, [CANCEL])

async def handle_employee_id(message: UserMessage, state: dict, data: RegisterData, text: str) -> bool:
    if not DIGITS_PATTERN.fullmatch(text):
        return False

    # TODO: search_employee ถูกเรียกอีกรอบใน register_user → refactor
    # None = ไม่พบ หรือ API มีปัญหา (แยกกันไม่ได้)
    if await search_employee(text) is None:
        await ask_retry(message, "ไม่พบรหัสพนักงาน โปรดกรอกอีกครั้ง")
        return True

    data.employee_id = text
    state["step"] = RegisterStep.WAIT_OTP
    state["data"] = data.model_dump()
    await save_and_ask(message, state)
    return True

async def handle_otp(message: UserMessage, state: dict, data: RegisterData, text: str) -> bool:
    if not DIGITS_PATTERN.fullmatch(text):
        return False

    if not verify_otp(text):
        await ask_retry(message, "OTP ไม่ถูกต้อง โปรดกรอกอีกครั้ง")
        return True

    # None = API หา employee ไม่เจอ / มีปัญหา ระหว่างขั้น OTP → ไม่ลบ state ให้กรอก OTP ใหม่ได้
    if await register_user(data.employee_id, message.user_id) is None:
        await ask_retry(message, "ระบบขัดข้อง ไม่สามารถยืนยันตัวตนได้ กรุณากรอก OTP อีกครั้งภายหลังครับ")
        return True

    await clear_state(message.user_id)
    await reply(message.reply_token, "ยืนยันตัวตนสำเร็จ")
    return True

STEP_HANDLERS = {
    RegisterStep.WAIT_EMPLOYEE_ID: handle_employee_id,
    RegisterStep.WAIT_OTP: handle_otp
}

# ยืนยันตัวตนแล้วจะไม่ถึงตรงนี้ (process.py ดักไว้)
async def start(user: UserInfo | None, message: UserMessage) -> None:
    state = new_state(Intent.REGISTER, RegisterStep.WAIT_EMPLOYEE_ID)
    await save_and_ask(message, state)

async def handle(user: UserInfo | None, message: UserMessage, state: dict) -> bool:
    text = message.user_message.strip()
    data = RegisterData.model_validate(state["data"])
    return await STEP_HANDLERS[state["step"]](message, state, data, text)
