# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว
from datetime import datetime

# packages ที่ install เข้ามา
from sqlalchemy import func, DateTime
from sqlalchemy.orm import mapped_column, Mapped

# packages ที่เขียนเอง และใช้ร่วมกันใน Project

# columns created_at และ updated_at ที่ทุก tables ใช้ร่วมกัน
class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    updated_at: Mapped[datetime] = mapped_column(
                                        DateTime(timezone=True),
                                        server_default=func.now(),
                                        onupdate=func.now()
                                    )
