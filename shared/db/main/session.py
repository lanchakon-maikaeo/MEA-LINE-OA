# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
from functools import lru_cache

# packages ที่ install เข้ามา
from sqlalchemy import create_engine, Engine
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncEngine, AsyncSession

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from shared.config import get_settings

# sync: สำหรับ dbinit (create_all)
@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return create_engine(get_settings().MAIN_DATABASE_URL)

# async: สำหรับ server
@lru_cache(maxsize=1)
def get_async_engine() -> AsyncEngine:
    return create_async_engine(get_settings().MAIN_DATABASE_URL)

@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_async_engine(), expire_on_commit=False)

def get_session() -> AsyncSession:
    return get_sessionmaker()()
