# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว

# packages ที่ install เข้ามา
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from line_bot.schemas import UserInfo
from line_bot.user.lookup import search_employee
from line_bot.user.utils import get_cache_key, USER_CACHE_TTL
from shared.cache.utils import set_json
from shared.db.main.models import Department, Employee, LineUser
from shared.db.main.session import get_session
from shared.schemas.main.department import DepartmentCreate
from shared.schemas.main.employee import EmployeeCreate
from shared.schemas.main.line_user import LineUserCreate

# ใช้ INSERT ... ON CONFLICT ให้ DB เช็คซ้ำกับ insert ในคำสั่งเดียว
# กันกรณี 2 คนลงทะเบียนพร้อมกันแล้วชน unique constraint
async def register_user(employee_id: str, line_user_id: str) -> UserInfo | None:
    employee_data = await search_employee(employee_id)

    if not employee_data:
        return None

    async with get_session() as session:
        # Department: ถ้ามีชื่อนี้แล้วให้ข้าม แล้วค่อย select เอา department_index
        department_index = None
        department_name = employee_data["department_name"]

        if department_name:
            await session.execute(
                insert(Department)
                .values(**DepartmentCreate(department_name=department_name).model_dump())
                .on_conflict_do_nothing(index_elements=[Department.department_name])
            )

            department_index = await session.scalar(
                select(Department.department_index).where(Department.department_name == department_name)
            )

        # Employee: ถ้ามี employee_id นี้แล้วให้ update แทน
        # updated_at ต้องใส่เอง เพราะ onupdate ของ ORM ไม่ทำงานกับ ON CONFLICT
        # coalesce: ถ้า API ไม่ส่งแผนกมา (None) ให้ใช้ department_index เดิมใน DB ไม่เขียนทับเป็น None
        employee_create = EmployeeCreate(employee_id=employee_data["employee_id"], department_index=department_index)
        employee_insert = insert(Employee).values(**employee_create.model_dump())

        employee_row = (await session.execute(
            employee_insert
            .on_conflict_do_update(
                index_elements=[Employee.employee_id],
                set_={
                    "department_index": func.coalesce(employee_insert.excluded.department_index, Employee.department_index),
                    "updated_at": func.now()
                }
            )
            .returning(Employee.employee_index, Employee.department_index)
        )).one()

        employee_index = employee_row.employee_index
        department_index = employee_row.department_index # ค่าจริงใน DB หลัง coalesce

        # LineUser: ถ้ามี line_user_id นี้แล้วให้ผูกกับ employee ใหม่แทน
        line_user_create = LineUserCreate(line_user_id=line_user_id, employee_index=employee_index)

        await session.execute(
            insert(LineUser)
            .values(**line_user_create.model_dump())
            .on_conflict_do_update(
                index_elements=[LineUser.line_user_id],
                set_={"employee_index": line_user_create.employee_index, "updated_at": func.now()}
            )
        )

        await session.commit()

    # return data
    user_info = UserInfo(
        line_user_id=line_user_id,
        employee_index=employee_index,
        employee_id=employee_create.employee_id,
        department_index=department_index
    )

    await set_json(
        get_cache_key(line_user_id),
        user_info.model_dump(),
        USER_CACHE_TTL
    )

    return user_info
