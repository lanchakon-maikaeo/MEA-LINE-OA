# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
import json
import logging
from contextlib import asynccontextmanager

# packages ที่ install เข้ามา
from fastapi import BackgroundTasks, Depends, FastAPI

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from line_bot.ai_tasks.ai import AI
from line_bot.line.events import parse_events
from line_bot.line.signature import verify_signature
from line_bot.process import process_messages
from shared.cache.client import get_redis
from shared.db.main.session import get_async_engine as get_main_async_engine
from shared.utils import log

log.setup()
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield # pause ตรงนี้ แล้วระะหว่างนี้ line bot ทำงานไปเรื่อย ๆ

    # ปิด connection: postgres redis
    await get_main_async_engine().dispose()
    await get_redis().aclose()
    await AI.close()

app = FastAPI(lifespan=lifespan)

# ทดสอบ connection
@app.api_route("/test_connection", methods=["GET", "POST"])
async def test_connection():
    logger.info("Hello World From Line bot!")
    return {"message": "Hello World From Line bot!"}

# Line จะ request ที่ endpoint นี้
@app.post("/line_webhook")
async def line_webhook(background: BackgroundTasks, body: bytes = Depends(verify_signature)):
    messages = parse_events(json.loads(body))
    background.add_task(process_messages, messages)
    return {"status": "ok"}
