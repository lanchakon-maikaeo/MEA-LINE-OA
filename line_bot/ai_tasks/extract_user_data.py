# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
from enum import StrEnum

# packages ที่ install เข้ามา

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from line_bot.ai_tasks.ai import AI

EMPLOYEE_ID_SYSTEM_PROMPT="""
"""

OTP_SYSTEM_PROMPT="""
"""

async def get_employee_id(message: str) -> str:
    id = await AI.chat_text(EMPLOYEE_ID_SYSTEM_PROMPT, message)
    return id

async def get_otp(message: str) -> str:
    otp = await AI.chat_text(OTP_SYSTEM_PROMPT, message)
    return otp
