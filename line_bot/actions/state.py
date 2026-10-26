# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว

# packages ที่ install เข้ามา

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from shared.cache.utils import delete, get_json, set_json

ACTION_TTL = 600 # วินาที (10 นาที) ทิ้งไว้เกินนี้ action หมดอายุเอง

# ปุ่ม quick_reply (ต้องตรงกับที่ user พิมพ์ทุกตัวอักษร)
ADD_ITEM = "เพิ่มรายการ"
CANCEL = "ยกเลิกทำรายการ"
CONFIRM = "ยืนยัน"
CONTINUE = "กลับไปทำรายการต่อ"
DELETE_ITEM = "ลบรายการ"
EDIT = "แก้ไข"
EDIT_ITEM = "แก้ไขรายการ"
NO = "ไม่"
SUMMARY = "สรุปรายการ"
YES = "ใช่"

# หน้าตา state:
# {"intent": "ขอแจ้งปัญหามิเตอร์", "step": "wait_meter_id", "draft": None, "pending_intent": None, "pending_cancel": False, "data": {}}
# - draft = คำตอบของ user (เพื่อรอการยืนยัน)
# - pending_intent = จะทิ้ง action เดิมไปทำ action ใหม่ไหม
# - pending_cancel = จะทิ้ง action นี้ไหม
# - data = คำตอบที่ยืนยันแล้ว
def new_state(intent: str, step: str) -> dict:
    return {"intent": intent, "step": step, "draft": None, "pending_intent": None, "pending_cancel": False, "data": {}}

def get_state_key(line_user_id: str) -> str:
    return f"action:{line_user_id}"   # "action:U1234..."

async def get_state(line_user_id: str) -> dict | None:
    return await get_json(get_state_key(line_user_id))

async def set_state(line_user_id: str, state: dict):
    await set_json(get_state_key(line_user_id), state, ACTION_TTL)

async def clear_state(line_user_id: str):
    await delete(get_state_key(line_user_id))
