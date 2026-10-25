# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว

# packages ที่ install เข้ามา
from pydantic import BaseModel

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from shared.schemas.mixins import TimestampSchema

# table: meter_report_order_comments

class MeterReportOrderCommentBase(BaseModel):
    meter_report_order_index: int
    employee_index: int
    comment_detail: str

class MeterReportOrderCommentCreate(MeterReportOrderCommentBase):
    pass

class MeterReportOrderCommentSchema(TimestampSchema, MeterReportOrderCommentBase):
    meter_report_order_comment_index: int
