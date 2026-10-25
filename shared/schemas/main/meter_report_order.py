# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว

# packages ที่ install เข้ามา
from pydantic import BaseModel

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from shared.schemas.mixins import TimestampSchema

# table: meter_report_orders

class MeterReportOrderBase(BaseModel):
    employee_index: int
    meter_id: str
    project_name: str | None = None
    issue_detail: str | None = None
    meter_report_order_status_index: int

class MeterReportOrderCreate(MeterReportOrderBase):
    pass

class MeterReportOrderSchema(TimestampSchema, MeterReportOrderBase):
    meter_report_order_index: int
    admin_index: int | None
