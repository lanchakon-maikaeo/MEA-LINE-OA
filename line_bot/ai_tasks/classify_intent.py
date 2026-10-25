# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
from enum import StrEnum

# packages ที่ install เข้ามา

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from line_bot.ai_tasks.ai import AI

SYSTEM_PROMPT="""
"""

SCHEMA={}

class ResponseField(StrEnum):
    INTENTS = "intents"

async def get_intents(message: str) -> list[str]:
    response = await AI.chat_json(SYSTEM_PROMPT, message, SCHEMA)
    intents = response[ResponseField.INTENTS]
    return intents
