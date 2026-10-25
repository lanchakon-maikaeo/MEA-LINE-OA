# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
import base64
import hashlib
import hmac

# packages ที่ install เข้ามา
from fastapi import HTTPException, Request

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from shared.config import require_setting

LINE_CHANNEL_SECRET = require_setting("LINE_CHANNEL_SECRET")

# เช็คว่า request มาจาก Line จริง แล้วคืน body ดิบ (bytes) กลับไป
async def verify_signature(request: Request) -> bytes:
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode(),
        body,
        hashlib.sha256
    ).digest()

    expected = base64.b64encode(digest).decode()

    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=400, detail="invalid signature")

    return body
