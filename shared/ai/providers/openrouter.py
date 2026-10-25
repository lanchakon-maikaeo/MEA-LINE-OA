# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
import json

# packages ที่ install เข้ามา
from openai import AsyncOpenAI

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from shared.ai.base import AIProvider
from shared.config import require_setting

BASE_URL = "https://openrouter.ai/api/v1"

class OpenRouter(AIProvider):
    name = "openrouter"

    # think: ถ้าใช้ True มันจะเป็น default "medium" หรือเราจะตั้งเป็น "low" / "medium" / "high" เองก็ได้
    def __init__(self, model: str, think: bool | str | None):
        super().__init__(model, think)
        self.client = AsyncOpenAI(base_url=BASE_URL, api_key=require_setting("OPENROUTER_API_KEY"))

    async def _chat_request(self, system_prompt: str, user_message: str, **kwargs) -> str:
        if self.think is not None:
            reasoning = {"effort": self.think} if isinstance(self.think, str) else {"enabled": self.think} # ถ้าเป็น "low" ซึ่งเป็น str ให้ไปเก็บใน "effort" นอกนั้นให้ไปเก็บใน "enabled"
            kwargs["extra_body"] = {**kwargs.get("extra_body", {}), "reasoning": reasoning}

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_message}],
            **kwargs
        )

        return response.choices[0].message.content

    async def chat_text(self, system_prompt: str, user_message: str) -> str:
        return await self._chat_request(system_prompt, user_message)

    async def chat_json(self, system_prompt: str, user_message: str, schema: dict) -> dict:
        response = await self._chat_request(
            system_prompt,
            user_message,
            response_format={"type": "json_schema", "json_schema": {
                                                        "name": "response",
                                                        "strict": True,
                                                        "schema": schema
                                                    },
            },
            extra_body={"provider": {"require_parameters": True}}
        )

        return json.loads(response)

    async def close(self) -> None:
        await self.client.close()
