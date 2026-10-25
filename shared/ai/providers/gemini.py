# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
import json

# packages ที่ install เข้ามา
from google import genai
from google.genai import types

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from shared.ai.base import AIProvider
from shared.config import require_setting

class Gemini(AIProvider):
    name = "gemini"

    # think: ถ้าใช้ True มันจะเป็น default "ให้มันเลือกเองว่าจะคิดแค่ไหน" หรือเราจะตั้งเป็น "low" / "medium" / "high" หรือ False เองก็ได้
    def __init__(self, model: str, think: bool | str | None):
        super().__init__(model, think)
        self.client = genai.Client(api_key=require_setting("GEMINI_API_KEY"))

    async def _chat_request(self, system_prompt: str, user_message: str, **kwargs) -> str:
        if isinstance(self.think, str):
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=self.think.upper())

        elif self.think is not None:
            # thinking_budget: 0 = ปิด, -1 = ให้ model เลือกเองว่าจะคิดแค่ไหน
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=-1 if self.think else 0)

        response = await self.client.aio.models.generate_content(
            model=self.model,
            contents=user_message,
            config=types.GenerateContentConfig(system_instruction=system_prompt, **kwargs)
        )

        return response.text

    async def chat_text(self, system_prompt: str, user_message: str) -> str:
        return await self._chat_request(system_prompt, user_message)

    async def chat_json(self, system_prompt: str, user_message: str, schema: dict) -> dict:
        response = await self._chat_request(
            system_prompt,
            user_message,
            response_mime_type="application/json",
            response_json_schema=schema
        )

        return json.loads(response)

    async def close(self) -> None:
        await self.client.aio.aclose()
