# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
from enum import StrEnum

# packages ที่ install เข้ามา

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from shared.cache.utils import get_json

class FormName(StrEnum):
    METER_REPORT = "meter_report_form"
    ASSET_REQUISITION = "asset_requisition_form"

def form_key(form_name: FormName, user_id: str) -> str:
    return f"{form_name}:{user_id}"   # "meter_report_form:U1234..."

# โหลดทุก forms ของ user คนนี้
async def get_forms(user_id: str) -> dict[str, dict | None]:
    return {name: await get_json(form_key(name, user_id)) for name in FormName} # {"meter_report_form": {...}, "asset_requisition_form": {...}}
