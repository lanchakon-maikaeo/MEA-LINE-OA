# packages เรียงชื่อจาก A-Z
# import ต้องขึ้นก่อน from หากอยู่ในหมวดเดียวกัน
# objects ที่ import มาให้เรียงชื่อจาก A-Z โดยมีลำดับ: abc (fucntions, variables) , Abc (classes) , ABC (constants)

# packages ที่มากับ Python อยู่แล้ว

# packages ที่ install เข้ามา

# packages ที่เขียนเอง และใช้ร่วมกันใน Project
from shared.ai.factory import AIFactory
from shared.config import get_settings, require_setting

AI = AIFactory.create(
    require_setting("AI_PROVIDER"),
    require_setting("AI_MODEL"),
    get_settings().AI_THINK # เป็น None ได้ เลยไม่ใช้ require_setting
)
