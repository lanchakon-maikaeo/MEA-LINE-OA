# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
from abc import abstractmethod, ABC

# packages ที่ install เข้ามา

# packages ที่เขียนเอง และใช้ร่วมกันใน Project

# base ของ AI providers
# class ไหนสืบทอดไป ต้องเขียนเมธอดที่มี @abstractmethod ให้ครบ
# ถ้าไม่ครบ จะสร้าง object ไม่ได้ (TypeError)
class AIProvider(ABC):
    def __init__(self, model: str, think: bool | str | None):
        self.model = model
        self.think = think

    @abstractmethod
    async def chat_text(self, system_prompt: str, user_message: str) -> str:
        pass

    @abstractmethod
    async def chat_json(self, system_prompt: str, user_message: str, schema: dict) -> dict:
        pass

    @abstractmethod
    async def close(self) -> None:
        pass

    # สอง methods ล่างนี้ทำให้ใช้ async with ได้
    # async with OpenRouter(...) as ai:
    #     ...
    # ออกจากบล็อกเมื่อไหร่ จะเรียก close() ให้เอง (ต่อให้เกิด error)
    async def __aenter__(self):
        # return self ให้ตัวแปรหลัง as
        return self

    # *args = ข้อมูล error 3 ชิ้นที่ Python ส่งมาให้ เราไม่ใช้ เลยรับไว้เฉย ๆ
    async def __aexit__(self, *args) -> None:
        # ทำตอนออกบล็อก: ปิด connection
        await self.close()
