# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
import logging

# packages ที่ install เข้ามา
from pydantic import ValidationError

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from line_bot.schemas import UserMessage

logger = logging.getLogger(__name__)

def parse_events(payload: dict) -> list[UserMessage]:
    messages = []

    for event in payload.get("events", []):
        source = event.get("source", {})
        message = event.get("message", {})

        if source.get("type") != "user":  # ข้ามข้อความจากกลุ่ม/ห้อง
            continue

        if event.get("type") != "message":  # ข้าม Follows, Postbacks, etc.
            continue

        if message.get("type") != "text":  # ข้าม Stickers, รูป, etc.
            continue

        try:
            messages.append(UserMessage(
                                user_id=source["userId"],
                                reply_token=event["replyToken"],
                                user_message=message["text"]
                            )
            )
        except (KeyError, ValidationError):
            logger.exception("skipped invalid Line event: %s", event.get("webhookEventId"))

    return messages
