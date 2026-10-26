# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว

# packages ที่ install เข้ามา
from sqlalchemy import Engine
from sqlalchemy.orm import Session

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from shared.db.main import models # ห้ามลบ ใช้สำหรับ initialize
from shared.db.main.base import Base
from shared.db.main.session import get_engine
from shared.schemas.main.asset_requisition_order_status import AssetRequisitionOrderStatusCreate
from shared.schemas.main.meter_report_order_status import MeterReportOrderStatusCreate

# is_default = status ของเรื่องใหม่ (ต้องมี 1 อันเสมอ ไม่งั้นบันทึกเรื่องไม่ได้)
METER_REPORT_ORDER_STATUSES = [
    MeterReportOrderStatusCreate(meter_report_order_status_name="รอดำเนินการ", meter_report_order_status_is_default=True),
    MeterReportOrderStatusCreate(meter_report_order_status_name="กำลังดำเนินการ"),
    MeterReportOrderStatusCreate(meter_report_order_status_name="เสร็จสิ้น"),
    MeterReportOrderStatusCreate(meter_report_order_status_name="ยกเลิก")
]

ASSET_REQUISITION_ORDER_STATUSES = [
    AssetRequisitionOrderStatusCreate(asset_requisition_order_status_name="รอดำเนินการ", asset_requisition_order_status_is_default=True),
    AssetRequisitionOrderStatusCreate(asset_requisition_order_status_name="กำลังดำเนินการ"),
    AssetRequisitionOrderStatusCreate(asset_requisition_order_status_name="เสร็จสิ้น", asset_requisition_order_status_is_completed=True),
    AssetRequisitionOrderStatusCreate(asset_requisition_order_status_name="ยกเลิก")
]

# ข้อมูลตั้งต้น: ใส่ใหม่ทุกครั้ง เพราะ drop_all ลบทิ้งทุกรอบ
def seed(engine: Engine) -> None:
    with Session(engine) as session:
        session.add_all(models.MeterReportOrderStatus(**status.model_dump()) for status in METER_REPORT_ORDER_STATUSES)
        session.add_all(models.AssetRequisitionOrderStatus(**status.model_dump()) for status in ASSET_REQUISITION_ORDER_STATUSES)
        session.commit()


def main() -> None:
    engine = get_engine()
    Base.metadata.drop_all(engine)  # For Dev: ลบทุกตารางทิ้งก่อน
    Base.metadata.create_all(engine)
    seed(engine)
    engine.dispose()


if __name__ == "__main__":
    main()
