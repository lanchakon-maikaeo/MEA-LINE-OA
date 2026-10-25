# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว

# packages ที่ install เข้ามา

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from shared.ai.base import AIProvider
from shared.ai.providers.gemini import Gemini
from shared.ai.providers.ollama import Ollama
from shared.ai.providers.openrouter import OpenRouter

PROVIDERS: dict[str, type[AIProvider]] = {provider.name: provider for provider in [OpenRouter, Gemini, Ollama]}

class AIFactory:
    @staticmethod # เรียกผ่านชื่อ class ได้เลย ไม่ต้องสร้าง object ก่อน
    def create(provider: str, model: str, think: bool | str | None) -> AIProvider:
        if provider not in PROVIDERS:
            raise ValueError(f"provider not found: {provider}")

        return PROVIDERS[provider](model, think)
