# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว

# packages ที่ install เข้ามา
from pydantic import BaseModel

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from shared.schemas.mixins import TimestampSchema

# table: asset_requisition_order_statuses

class AssetRequisitionOrderStatusBase(BaseModel):
    asset_requisition_order_status_name: str
    asset_requisition_order_status_is_default: bool = False
    asset_requisition_order_status_is_completed: bool = False

class AssetRequisitionOrderStatusCreate(AssetRequisitionOrderStatusBase):
    pass

class AssetRequisitionOrderStatusSchema(TimestampSchema, AssetRequisitionOrderStatusBase):
    asset_requisition_order_status_index: int
