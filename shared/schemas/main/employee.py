# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว

# packages ที่ install เข้ามา
from pydantic import BaseModel, Field

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from shared.schemas.mixins import TimestampSchema

# table: employees

class EmployeeBase(BaseModel):
    employee_id: str
    department_index: int | None = None

class EmployeeCreate(EmployeeBase):
    pass

class EmployeeSchema(TimestampSchema, EmployeeBase):
    employee_index: int
    user_password_hash: str
    # user_password_hash: str = Field(exclude=True) # ไม่ออกไปกับ model_dump() / JSON
