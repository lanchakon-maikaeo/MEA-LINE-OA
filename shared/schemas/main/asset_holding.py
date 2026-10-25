# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว

# packages ที่ install เข้ามา
from pydantic import model_validator, BaseModel

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from shared.schemas.mixins import TimestampSchema

# table: asset_holdings

class AssetHoldingBase(BaseModel):
    asset_index: int
    employee_index: int | None = None
    department_index: int | None = None
    asset_requisition_order_detail_index: int | None = None

    @model_validator(mode="after")
    def check_one_owner(self):
        if (self.employee_index is None) == (self.department_index is None):
            raise ValueError("exactly one of employee_index or department_index must be provided")

        return self

class AssetHoldingCreate(AssetHoldingBase):
    pass

class AssetHoldingSchema(TimestampSchema, AssetHoldingBase):
    asset_holding_index: int
