# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
from datetime import timedelta, timezone

# packages ที่ install เข้ามา
from sqlalchemy import Row, select

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from line_bot.schemas import UserInfo
from shared.db.main.models import MeterReportOrder, MeterReportOrderStatus
from shared.db.main.session import get_session

# one-shot: ตอบจบในข้อความเดียว ไม่มี state (process.py เป็นคนส่งข้อความเอง)
REQUIRES_AUTH = True

BANGKOK_TZ = timezone(timedelta(hours=7)) # ไทยไม่มี daylight saving ใช้ offset ตายตัวได้ ไม่ต้องพึ่ง tzdata ใน container
TRACK_LIMIT = 5

def format_order(row: Row) -> str:
    created_date = row.created_at.astimezone(BANGKOK_TZ).strftime("%d/%m/%Y")
    meter = f"{row.meter_id} ({row.project_name})" if row.project_name else row.meter_id

    return f"เลขที่ {row.meter_report_order_index} · {created_date}\nมิเตอร์: {meter}\nสถานะ: {row.meter_report_order_status_name}"

async def answer(user: UserInfo) -> str:
    async with get_session() as session:
        rows = (await session.execute(
            select(
                MeterReportOrder.meter_report_order_index,
                MeterReportOrder.meter_id,
                MeterReportOrder.project_name,
                MeterReportOrder.created_at,
                MeterReportOrderStatus.meter_report_order_status_name
            )
            .join(MeterReportOrderStatus)
            .where(MeterReportOrder.employee_index == user.employee_index)
            .order_by(MeterReportOrder.meter_report_order_index.desc()) # ไม่ใช้ created_at เพราะแจ้งหลายรายการพร้อมกันจะได้เวลาเดียวกัน
            .limit(TRACK_LIMIT)
        )).all()

    if not rows:
        return "ยังไม่มีรายการแจ้งปัญหามิเตอร์ครับ"

    orders = "\n\n".join(format_order(row) for row in rows)
    return f"การแจ้งปัญหามิเตอร์ {len(rows)} รายการล่าสุด\n\n{orders}"
