# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
import logging

# packages ที่ install เข้ามา

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from line_bot.ai_tasks.classify_intent import get_intents
from line_bot.schemas import UserMessage
from line_bot.user.lookup import get_user
from line_bot.user.register import register_user
from line_bot.line.reply import quick_reply, reply

async def process_messages(messages: list[UserMessage]) -> str:
    reply_messages = [] # เอาไว้รวบรวมข้อความตอบกลับ

    for message in messages:
        user = await get_user(message.user_id)

        if user is None:
            user = await register_user(message.user_id, message.user_message)

        intents = await get_intents(message.user_message)



# เริ่มจากโหลด forms เช่น ฟอร์มแจ้งปัญหามิเตอร์ ฟอร์มอื่นๆ
