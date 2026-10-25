# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
import logging

# packages ที่ install เข้ามา
import httpx

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from shared.config import require_setting

logger = logging.getLogger(__name__)

LINE_CHANNEL_ACCESS_TOKEN = require_setting("LINE_CHANNEL_ACCESS_TOKEN")
LINE_REPLY_URL = require_setting("LINE_REPLY_URL") # https://api.line.me/v2/bot/message/reply

# ข้อจำกัดของ Line
QUICK_REPLY_LABEL_MAX = 20 # label ยาวได้ไม่เกิน 20 ตัวอักษร
QUICK_REPLY_MAX_ITEMS = 13 # ปุ่มได้ไม่เกิน 13 ปุ่ม

# reply_token ใช้ได้ครั้งเดียว และหมดอายุเร็ว (ประมาณ 1 นาที)
# สำเร็จ = True แต่ถ้า Line ตอบ error หรือ timeout = False
async def _send(reply_token: str, messages: list[dict]) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                LINE_REPLY_URL,
                headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
                json={"replyToken": reply_token, "messages": messages}
            )

        response.raise_for_status()
        return True

    except httpx.HTTPError:
        logger.exception("Line reply failed")
        return False

# ตอบข้อความธรรมดา
async def reply(reply_token: str, text: str) -> bool:
    return await _send(reply_token, [{"type": "text", "text": text}])

# ตอบข้อความพร้อมปุ่ม: กด bubbles แล้ว Line จะส่งข้อความใน bubbles นั้น เหมือน user พิมพ์เอง
async def quick_reply(reply_token: str, text: str, options: list[str]) -> bool:
    items = [
        {
            "type": "action",
            "action": {
                "type": "message",
                "label": option[:QUICK_REPLY_LABEL_MAX],
                "text": option
            }
        }
        for option in options[:QUICK_REPLY_MAX_ITEMS]
    ]

    return await _send(reply_token, [{"type": "text", "text": text, "quickReply": {"items": items}}])
