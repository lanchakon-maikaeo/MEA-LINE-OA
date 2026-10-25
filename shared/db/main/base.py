# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว

# packages ที่ install เข้ามา
from sqlalchemy.orm import DeclarativeBase

# packages ที่เขียนเอง และใช้ร่วมกันใน Project

# เสมือนสร้างสมุดทะเบียนที่ชื่อ Base
class Base(DeclarativeBase):
    pass
