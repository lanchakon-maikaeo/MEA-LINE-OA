# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
from functools import lru_cache

# packages ที่ install เข้ามา
import redis.asyncio as redis

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from shared.config import require_setting

@lru_cache(maxsize=1)
def get_redis() -> redis.Redis:
    redis_url = require_setting("REDIS_URL")
    return redis.Redis.from_url(redis_url, decode_responses=True)
