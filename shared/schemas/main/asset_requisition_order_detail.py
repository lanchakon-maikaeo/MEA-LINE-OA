# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
from decimal import Decimal

# packages ที่ install เข้ามา
from pydantic import BaseModel

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from shared.schemas.mixins import TimestampSchema

# table: asset_requisition_order_details

class AssetRequisitionOrderDetailBase(BaseModel):
    asset_requisition_order_index: int
    asset_index: int
    asset_quantity: int
    asset_price: Decimal

class AssetRequisitionOrderDetailCreate(AssetRequisitionOrderDetailBase):
    pass

class AssetRequisitionOrderDetailSchema(TimestampSchema, AssetRequisitionOrderDetailBase):
    asset_requisition_order_detail_index: int
