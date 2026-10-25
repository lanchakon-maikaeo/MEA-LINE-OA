# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
from enum import StrEnum

# packages ที่ install เข้ามา

# packages ที่เขียนเอง และใช้ร่วมกันใน Project

class Intent(StrEnum):
    # ทุกคนใช้ได้
    REGISTER = "ขอยืนยันตัวตน"
    CONTACT_NUMBER = "ขอเบอร์โทรติดต่อ"
    CONTACT_EMAIL = "ขออีเมลติดต่อ"

    # ต้องยืนยันตัวตนก่อน
    METER_REPORT = "ขอแจ้งปัญหามิเตอร์"
    ASSET_REQUISITION = "ขอเบิกของ"
    TRACK_METER_REPORT = "ขอติดตามการแจ้งปัญหามิเตอร์"
    TRACK_ASSET_REQUISITION = "ขอติดตามการเบิกของ"

    # ไม่พบกับข้อใดเลย หรือไม่แน่ใจ
    NONE_INTENT = "ไม่ตรงกับข้อใดเลย หรือไม่แน่ใจ"

FAQ_ANSWERS = {
    Intent.CONTACT_NUMBER: "เบอร์โทรติดต่อ: xxx",
    Intent.CONTACT_EMAIL: "อีเมลติดต่อ: xxx",
    Intent.NONE_INTENT: "ไม่เข้าใจคำถามครับ"
}
