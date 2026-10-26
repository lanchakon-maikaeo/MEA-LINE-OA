# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
import logging

# packages ที่ install เข้ามา

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from line_bot.actions import ACTION_HANDLERS, ONE_SHOT_HANDLERS
from line_bot.actions.state import clear_state, get_state, set_state, CANCEL, CONTINUE, NO, YES
from line_bot.ai_tasks.classify_intent import get_intent
from line_bot.intents import Intent, FAQ_ANSWERS
from line_bot.line.reply import quick_reply, reply
from line_bot.schemas import UserInfo, UserMessage
from line_bot.user.lookup import get_user

logger = logging.getLogger(__name__)

ALREADY_REGISTERED_MESSAGE = "คุณยืนยันตัวตนแล้ว"
AUTH_REQUIRED_MESSAGE = f'กรุณายืนยันตัวตนก่อน โดยกดปุ่ม "{Intent.REGISTER}" ที่เมนูด้านล่าง'
CANCEL_QUESTION = "ต้องการยกเลิกทำรายการหรือไม่?"
CANCELLED_MESSAGE = "ยกเลิกทำรายการแล้วครับ"
IN_DEVELOPMENT_MESSAGE = "ขออภัย เมนูนี้กำลังพัฒนาครับ"
NOT_UNDERSTAND_MESSAGE = "ไม่เข้าใจครับ"
UNKNOWN_MESSAGE = "ขออภัย ไม่เข้าใจคำถาม กรุณาเลือกจากเมนูด้านล่าง"

def is_faq(intent: Intent) -> bool:
    return intent in FAQ_ANSWERS and intent != Intent.NONE_INTENT

# ตรงกับปุ่มเป๊ะ = Intent, ไม่ตรง = None
def exact_intent(text: str) -> Intent | None:
    return Intent(text) if text in Intent else None

# ตรงเป๊ะไม่ต้องเรียก AI
async def find_intent(text: str) -> Intent:
    return exact_intent(text) or await get_intent(text)

# เริ่ม action นี้ไม่ได้ = ข้อความบอก user, เริ่มได้ = None
def check_can_start(user: UserInfo | None, intent: Intent) -> str | None:
    action = ACTION_HANDLERS.get(intent) or ONE_SHOT_HANDLERS.get(intent)

    if action is None:
        return IN_DEVELOPMENT_MESSAGE

    if action.REQUIRES_AUTH and user is None:
        return AUTH_REQUIRED_MESSAGE

    # เช็คตรงนี้ ไม่ใช่ใน register.start() เพราะถ้ากด "ขอยืนยันตัวตน" ระหว่างทำ action อื่น
    # จะได้ไม่ถามว่า "ต้องการยกเลิก X ไหม" แล้วทิ้ง action เดิมไปเปล่า ๆ
    if intent == Intent.REGISTER and user is not None:
        return ALREADY_REGISTERED_MESSAGE

    return None

def action_name(intent: str) -> str:
    return intent.removeprefix("ขอ") # "ขอแจ้งปัญหามิเตอร์" → "แจ้งปัญหามิเตอร์"

# ถามคำถามของขั้นปัจจุบันซ้ำ
# prefix = ข้อความที่ต้องตอบก่อน เช่น FAQ (LINE ตอบได้ครั้งเดียว จึงรวมเป็นข้อความเดียว)
async def ask_again(message: UserMessage, state: dict, prefix: str | None = None):
    text, options = ACTION_HANDLERS[state["intent"]].prompt(state)

    if prefix:
        text = f"{prefix}\n\n{text}"

    await quick_reply(message.reply_token, text, options)

async def ask_cancel(message: UserMessage):
    await quick_reply(message.reply_token, CANCEL_QUESTION, [CANCEL, CONTINUE])

async def ask_switch(message: UserMessage, state: dict):
    await quick_reply(
        message.reply_token,
        f'ต้องการยกเลิก "{action_name(state["intent"])}" เพื่อไป "{action_name(state["pending_intent"])}" ไหมครับ?',
        [YES, NO]
    )

async def process_without_state(user: UserInfo | None, message: UserMessage, text: str):
    intent = await find_intent(text)

    if intent == Intent.NONE_INTENT:
        await reply(message.reply_token, UNKNOWN_MESSAGE)
        return

    if is_faq(intent):
        await reply(message.reply_token, FAQ_ANSWERS[intent])
        return

    error = check_can_start(user, intent)

    if error:
        await reply(message.reply_token, error)
        return

    if intent in ONE_SHOT_HANDLERS:
        await reply(message.reply_token, await ONE_SHOT_HANDLERS[intent].answer(user))
        return

    await ACTION_HANDLERS[intent].start(user, message)

# intent ที่เจอระหว่างทำ action (ตรงเป๊ะ หรือ AI เดา)
async def handle_intent_during_action(user: UserInfo | None, message: UserMessage, state: dict, intent: Intent):
    if is_faq(intent):
        await ask_again(message, state, FAQ_ANSWERS[intent])
        return

    if intent == Intent.NONE_INTENT or intent == state["intent"]:
        await ask_again(message, state, NOT_UNDERSTAND_MESSAGE)
        return

    # action อื่น: เช็คก่อนว่าเริ่มได้ไหม จะได้ไม่ทิ้ง action เดิมไปเปล่า ๆ
    error = check_can_start(user, intent)

    if error:
        await ask_again(message, state, error)
        return

    # one-shot ไม่มี state จึงไม่ต้องทิ้ง action เดิม: ตอบแบบ FAQ
    if intent in ONE_SHOT_HANDLERS:
        await ask_again(message, state, await ONE_SHOT_HANDLERS[intent].answer(user))
        return

    state["pending_intent"] = intent
    await set_state(message.user_id, state)
    await ask_switch(message, state)

async def process_with_state(user: UserInfo | None, message: UserMessage, text: str, state: dict):
    # 1. รอตอบว่าจะยกเลิกจริงไหม
    if state["pending_cancel"]:
        if text == CANCEL:
            await clear_state(message.user_id)
            await reply(message.reply_token, CANCELLED_MESSAGE)
            return

        if text == CONTINUE:
            state["pending_cancel"] = False
            await set_state(message.user_id, state)
            await ask_again(message, state)
            return

        await ask_cancel(message)
        return

    # 2. รอตอบว่าจะทิ้ง action เดิมไปทำ action ใหม่ไหม
    if state["pending_intent"] is not None:
        if text == YES:
            await clear_state(message.user_id)
            await ACTION_HANDLERS[state["pending_intent"]].start(user, message)
            return

        if text == NO:
            state["pending_intent"] = None
            await set_state(message.user_id, state)
            await ask_again(message, state)
            return

        await ask_switch(message, state)
        return

    # 3. กดยกเลิก → ถามก่อน
    if text == CANCEL:
        state["pending_cancel"] = True
        await set_state(message.user_id, state)
        await ask_cancel(message)
        return

    # 4. ตรงกับปุ่มเป๊ะ (FAQ / action อื่น) → ไม่ส่งให้ action เพราะขั้นที่รับข้อความอิสระจะรับไปหมด
    intent = exact_intent(text)

    if intent is not None:
        await handle_intent_during_action(user, message, state, intent)
        return

    # 5. ตรงรูปแบบที่ขั้นนี้รอ → action จัดการและตอบเอง
    if await ACTION_HANDLERS[state["intent"]].handle(user, message, state):
        return

    # 6. ไม่ตรง → AI เดา
    intent = await get_intent(text)
    await handle_intent_during_action(user, message, state, intent)

async def process_message(message: UserMessage):
    user = await get_user(message.user_id)
    state = await get_state(message.user_id)
    text = message.user_message.strip()

    # action ที่ค้างอยู่ถูกเอาออกจาก ACTION_HANDLERS แล้ว
    if state is not None and state["intent"] not in ACTION_HANDLERS:
        await clear_state(message.user_id)
        state = None

    if state is None:
        await process_without_state(user, message, text)
    else:
        await process_with_state(user, message, text, state)

async def process_messages(messages: list[UserMessage]):
    for message in messages:
        # ข้อความหนึ่งพัง ไม่ให้ข้อความที่เหลือพังไปด้วย
        try:
            await process_message(message)
        except Exception:
            logger.exception("process message failed: %s", message.user_id)
