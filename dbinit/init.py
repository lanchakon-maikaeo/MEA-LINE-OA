# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว

# packages ที่ install เข้ามา

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from shared.db.main import models # ห้ามลบ ใช้สำหรับ initialize
from shared.db.main.base import Base
from shared.db.main.session import get_engine


def main() -> None:
    engine = get_engine()
    Base.metadata.drop_all(engine)  # For Dev: ลบทุกตารางทิ้งก่อน
    Base.metadata.create_all(engine)
    engine.dispose()


if __name__ == "__main__":
    main()
