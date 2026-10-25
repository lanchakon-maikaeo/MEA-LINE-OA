# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
from enum import StrEnum

# packages ที่ install เข้ามา

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from line_bot.ai_tasks.ai import AI
from line_bot.intents import Intent

class ResponseField(StrEnum):
    INTENT = "intent"

SYSTEM_PROMPT=f"""
คุณคือระบบจำแนกเจตนา (intent) ของข้อความจากผู้ใช้
ให้เลือก intent ที่ตรงกับข้อความมากที่สุดเพียง 1 อย่าง จากรายการนี้:
{"\n".join(f"- {intent.value}" for intent in Intent)}
"""

SCHEMA={
    "type": "object",
    "properties": {
        ResponseField.INTENT: {
            "type": "string",
            "description": "intent ของข้อความ",
            "enum": [intent.value for intent in Intent]
        }
    },
    "required": [ResponseField.INTENT],
    "additionalProperties": False
}

async def get_intent(message: str) -> Intent:
    response = await AI.chat_json(SYSTEM_PROMPT, message, SCHEMA)
    value = response.get(ResponseField.INTENT)
    return Intent(value) if value in Intent else Intent.NONE_INTENT
