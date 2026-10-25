# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว

# packages ที่ install เข้ามา
from pydantic import BaseModel

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from shared.schemas.mixins import TimestampSchema

# table: asset_requisition_orders

class AssetRequisitionOrderBase(BaseModel):
    employee_index: int
    department_index: int | None = None
    asset_requisition_order_status_index: int

class AssetRequisitionOrderCreate(AssetRequisitionOrderBase):
    pass

class AssetRequisitionOrderSchema(TimestampSchema, AssetRequisitionOrderBase):
    asset_requisition_order_index: int
    admin_index: int | None
