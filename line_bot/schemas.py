# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว

# packages ที่ install เข้ามา
from pydantic import BaseModel, ConfigDict

# packages ที่เขียนเอง และใช้ร่วมกันใน Project

class UserInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    line_user_id: str
    employee_index: int
    employee_id: str
    department_index: int | None

class UserMessage(BaseModel):
    user_id: str
    reply_token: str
    user_message: str
