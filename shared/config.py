# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
from functools import lru_cache

# packages ที่ install เข้ามา
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# packages ที่เขียนเอง และใช้ร่วมกันใน Project

class Settings(BaseSettings):
    model_config = SettingsConfigDict(frozen=True) # frozen = แก้ค่าหลังสร้างไม่ได้

    # ทุก Service ใช้: เป็น None ไม่ได้
    MAIN_DATABASE_URL: str
    AI_DATABASE_URL: str

    # บาง Service ไม่ใช้: เป็น None ได้
    REDIS_URL: str | None = None
    AI_PROVIDER: str | None = None
    AI_MODEL: str | None = None
    AI_THINK: bool | str | None = None
    GEMINI_API_KEY: str | None = None
    OPENROUTER_API_KEY: str | None = None
    OLLAMA_BASE_URL: str | None = None
    LINE_CHANNEL_ACCESS_TOKEN: str | None = None
    LINE_CHANNEL_SECRET: str | None = None
    LINE_REPLY_URL: str | None = None
    MEA_SMARTOFFICE_XAPI_KEY: str | None = None

    # mode="before" ทำให้มันรัน parse_think() ก่อนเอาค่าเก็บใน pydantic (__init__() ที่ถูกสืบทอดมาจาก BaseSettings เป็นตัวกำหนด)
    @field_validator("AI_THINK", mode="before")
    @classmethod
    def parse_value(cls, value: object) -> object: # Parse ค่าของ AI_THINK
        # ถ้า AI_THINK=False จะ return False กลับทันที เพราะมันเป็น bool ซึ่งไม่ต้อง parse
        if not isinstance(value, str):
            return value

        # parse strings
        if not value:
            return None

        if value.lower() == "true":
            return True

        if value.lower() == "false":
            return False

        return value

# เริ่มต้นตรงนี้
@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

# ดึงค่าจาก settings ถ้าไม่มีค่าให้ error ทันที
def require_setting(name: str) -> str:
    value = getattr(get_settings(), name)

    if not value:
        raise ValueError(f"{name} is not set")

    return value
