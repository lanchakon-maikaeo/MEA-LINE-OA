# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
import json

# packages ที่ install เข้ามา

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from shared.cache.client import get_redis

async def get_json(key: str):
    value = await get_redis().get(key)

    if value is None:
        return None

    return json.loads(value)

async def set_json(key: str, data: dict, ttl: int):
    await get_redis().set(key, json.dumps(data, ensure_ascii=False), ex=ttl)

async def delete(key: str):
    await get_redis().delete(key)
