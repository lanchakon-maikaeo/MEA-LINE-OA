# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
from datetime import datetime

# packages ที่ install เข้ามา
from pydantic import BaseModel, ConfigDict

# packages ที่เขียนเอง และใช้ร่วมกันใน Project

# คู่กับ TimestampMixin ใน shared/db/mixins.py
# from_attributes=True = สร้างจาก ORM object ได้ เช่น EmployeeSchema.model_validate(db_employee)
class TimestampSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    created_at: datetime
    updated_at: datetime
