# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว

# packages ที่ install เข้ามา

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from line_bot.user.utils import VERY_REAL_OTP

# TODO: OTP ของปลอม → ของจริงให้สุ่ม + เก็บใน Redis + ส่ง SMS (แก้แค่ไฟล์นี้)
def verify_otp(user_otp: str) -> bool:
    return user_otp.strip() == VERY_REAL_OTP
