# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว

# packages ที่ install เข้ามา
from pydantic import BaseModel, Field

# packages ที่เขียนเอง และใช้ร่วมกันใน Project

USER_CACHE_TTL = 1800 # วินาที (30 นาที)
VERY_REAL_OTP = "1234" # TODO: otp ของปลอม

class EmployeePayload(BaseModel):
    employee_id: str = Field(alias="empId")
    department_name: str | None = Field(default=None, alias="depShortName")

def get_cache_key(line_user_id: str) -> str:
    return f"user:{line_user_id}"
