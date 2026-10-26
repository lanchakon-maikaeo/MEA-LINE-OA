# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว

# packages ที่ install เข้ามา

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from line_bot.actions import meter_report, register, track_meter_report
from line_bot.intents import Intent

# เก็บเป็น module: ใช้ .REQUIRES_AUTH / .start() / .handle() / .prompt()
# intent ที่เป็น action แต่ยังไม่อยู่ในนี้ = กำลังพัฒนา
ACTION_HANDLERS = {
    Intent.REGISTER: register,
    Intent.METER_REPORT: meter_report
}

# one-shot = ต้องยืนยันตัวตน แต่ตอบจบในข้อความเดียว ไม่มี state: ใช้ .REQUIRES_AUTH / .answer()
# กดระหว่างทำ action อื่นได้เหมือน FAQ (ตอบ + ถามคำถามของขั้นเดิมต่อ)
ONE_SHOT_HANDLERS = {
    Intent.TRACK_METER_REPORT: track_meter_report
}
