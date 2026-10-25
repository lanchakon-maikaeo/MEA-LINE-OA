# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
import json

# packages ที่ install เข้ามา
import httpx

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from shared.ai.base import AIProvider
from shared.config import require_setting

class Ollama(AIProvider):
    name = "ollama"

    def __init__(self, model: str, think: bool | str | None):
        super().__init__(model, think)

        self.client = httpx.AsyncClient(
            base_url=require_setting("OLLAMA_BASE_URL"),
            headers={"ngrok-skip-browser-warning": "true"},
            timeout=httpx.Timeout(180.0)
        )

    async def _chat_request(self, system_prompt: str, user_message: str, **kwargs) -> str:
        if self.think is not None:
            kwargs["think"] = self.think

        response = await self.client.post("/api/chat", json={
                                                            "model": self.model,
                                                            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_message}],
                                                            "stream": False, # ถ้าไม่ปิด Ollama จะส่งกลับมาเป็น stream ทีละบรรทัด
                                                            **kwargs
                                                        }
        )

        response.raise_for_status()
        return response.json()["message"]["content"]

    async def chat_text(self, system_prompt: str, user_message: str) -> str:
        return await self._chat_request(system_prompt, user_message)

    async def chat_json(self, system_prompt: str, user_message: str, schema: dict) -> dict:
        response = await self._chat_request(
            system_prompt,
            user_message,
            format=schema
        )

        return json.loads(response)

    async def close(self) -> None:
        await self.client.aclose()
