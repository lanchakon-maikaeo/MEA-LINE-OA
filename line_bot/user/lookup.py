# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
import logging

# packages ที่ install เข้ามา
import httpx
from sqlalchemy import select

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from line_bot.schemas import UserInfo
from line_bot.user.utils import get_cache_key, EmployeePayload, USER_CACHE_TTL
from shared.cache.utils import get_json, set_json
from shared.config import require_setting
from shared.db.main.models import Employee, LineUser
from shared.db.main.session import get_session

logger = logging.getLogger(__name__)

# เจอ = dict, ไม่เจอหรือ API มีปัญหา (404, 500, timeout, JSON พัง) = None
async def search_employee(employee_id: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                "https://api.mea.or.th/employee3/emp/search",
                params={"empId": employee_id, "returnContractEmployee": "true"},
                headers={
                    "accept": "application/json, text/plain, */*",
                    "cache-Control": "no-cache, no-store",
                    "user-agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:154.0) Gecko/20100101 Firefox/154.0",
                    "x-api-key": require_setting("MEA_SMARTOFFICE_XAPI_KEY"),
                    "origin": "https://smartoffice.mea.or.th",
                    "referer": "https://smartoffice.mea.or.th/",
                },
            )

        response.raise_for_status()

        items = response.json().get("data") or []

        if not items:
            return None

        return EmployeePayload.model_validate(items[0]).model_dump()

    except (httpx.HTTPError, ValueError):
        logger.exception("search employee failed: %s", employee_id)
        return None

async def cache_search(line_user_id: str) -> UserInfo | None:
    cache_key = get_cache_key(line_user_id)
    data = await get_json(cache_key)

    if not data:
        return None

    return UserInfo.model_validate(data)

async def db_search(line_user_id: str) -> UserInfo | None:
    row = None

    async with get_session() as session:
        result = await session.execute(
            select(
                LineUser.line_user_id,
                Employee.employee_index,
                Employee.employee_id,
                Employee.department_index
            )
            .join(Employee)
            .where(LineUser.line_user_id == line_user_id)
        )

        row = result.mappings().first()

    if row is None:
        return None

    user_info = UserInfo.model_validate(dict(row))

    await set_json(
        get_cache_key(line_user_id),
        user_info.model_dump(),
        USER_CACHE_TTL
    )

    return user_info

async def get_user(line_user_id: str) -> UserInfo | None:
    return await cache_search(line_user_id) or await db_search(line_user_id)
