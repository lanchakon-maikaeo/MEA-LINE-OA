"""ตัวเลือกระบบ: choices the organisation changes without code (ADR 0009).

The user (10 Oct 2569): flows and rules change later, so give options instead of fixing one
way, and keep them convenient and safe. Each option lists its choices with the safe one as the
default; only an admin changes one, with a reason, and every change is in the audit trail. A
new option is one entry here plus the place that reads it.
"""

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction

from operations.models import SystemOption


@dataclass(frozen=True)
class Option:
    label: str
    choices: tuple  # ((value, label, help), ...); the first is the safe default
    help: str = ""

    @property
    def default(self):
        return self.choices[0][0]

    def label_of(self, value):
        return next((label for v, label, _ in self.choices if v == value), value)


DIVISION_WORK = "division_work"
SCREENING = "screening"
SEPARATION = "separation_of_duties"
LINE_AUDIENCE = "line_audience"
REGISTRATION = "registration_approver"
REGISTRATION_CHECK = "registration_check"
REGISTRATION_LOCATION = "registration_location"
REGISTRATION_NAME = "registration_name"
ANONYMOUS = "anonymous_reports"
ANONYMOUS_LIMIT = "anonymous_daily_limit"
STAFF_LINE_BUDGET = "staff_line_budget"
STAFF_LINE_ACTIONS = "staff_line_actions"
STAFF_LINK = "staff_line_link"
STAFF_NOTIFY = "staff_notify"
ORDER_REMIND = "order_remind_days"
CHAT_ANSWERS = "chat_answers"
FREE_CHAT_DAILY = "free_chat_daily"
FREE_CHAT_DOCS = "free_chat_docs"
FREE_CHAT_TUNING = "free_chat_tuning"
CHAT_FOLLOWUP = "chat_followup"
CHAT_TYPED = "chat_typed_steps"
CHAT_MODE = "chat_mode"
USAGE_AI = "usage_ai"
INFO_REPLY = "info_reply"
LEARNED_ROUTES = "learned_routes"
ROUTING_AI = "routing_ai"
ROUTING_AI_ACCURACY = "routing_ai_accuracy"
CATCH_UP = "catch_up_push"
CATCH_UP_APOLOGY = "catch_up_apology"
REGISTRY_ACCESS = "registry_access"
OLD_STOCK = "old_stock_work"
SAP_LINE_ENTRY = "sap_line_entry"
EMPLOYEE_OTP = "employee_otp"
EMAIL_METHODS = "employee_email_methods"
EMAIL_TYPED = "employee_email_typed"
CODE_MINUTES = "email_code_minutes"
CODE_TRIES = "email_code_tries"
CODE_PER_DAY = "email_code_per_day"
MONITOR_TIME = "monitoring_time"
MONITOR_KEEP = "monitoring_keep_days"
MONITOR_ERROR = "monitoring_error_rate"
MONITOR_P95 = "monitoring_p95_ms"
MONITOR_UNANSWERED = "monitoring_unanswered_times"
MONITOR_EMAIL = "monitoring_email"
MONITOR_JUDGE = "monitoring_judge"
# Monitoring thresholds follow the right to change AI settings (15 ต.ค. 2569 Q45).
AI_RIGHT = {MONITOR_ERROR, MONITOR_P95, MONITOR_UNANSWERED, MONITOR_EMAIL, MONITOR_JUDGE}

OPTIONS = {
    SCREENING: Option(
        "ขั้นคัดกรองเรื่องใหม่",
        (
            (
                "off",
                "ไม่มีขั้นคัดกรอง: เรื่องใหม่เข้ารอรับเรื่องของกองเจ้าภาพทันที",
                "กองตั้งระดับความเร่งด่วนตอนรับเรื่อง เรื่องฉุกเฉินเริ่มงานทันที "
                "เรื่องที่ระบบบอกกองไม่ได้ยังรอผู้ประสานก่อน (ADR 0025)",
            ),
            (
                "on",
                "มีขั้นคัดกรอง: เรื่องใหม่รอคัดกรองแล้วค่อยส่งเข้าขั้นดำเนินงาน",
                "แบบก่อน 27 ต.ค. 2569",
            ),
        ),
        "มีผลกับเรื่องใหม่ เรื่องที่อยู่ในขั้นอื่นแล้วไม่เปลี่ยน",
    ),
    DIVISION_WORK: Option(
        "หน้าที่ในกอง",
        (
            (
                "member",
                "สมาชิกกองทำได้ทุกขั้นตอน",
                "สมาชิกกองรับเรื่อง อนุมัติ จ่าย รับคืน ตรวจสภาพ รับรองสภาพ และแก้ปัญหาได้ทุกเรื่อง"
                "ของกอง แอดมินกองทำได้เท่าสมาชิก และสั่งงานหรือมอบงานได้เพิ่ม",
            ),
            (
                "roles",
                "แยกหน้าที่ย่อยในกอง",
                "แต่ละคนทำได้เฉพาะหน้าที่ที่แอดมินให้ (ผู้อนุมัติ ผู้จ่าย ผู้รับคืน ผู้ตรวจสภาพ ผู้แก้ปัญหา) "
                "และอำนาจอนุมัติต้องให้ทีละคนในหน้าอำนาจ",
            ),
        ),
        "เปลี่ยนแล้วมีผลทันทีกับทุกกอง ไม่ต้องให้บทบาทใหม่",
    ),
    SEPARATION: Option(
        "การแยกหน้าที่ในเรื่องเดียวกัน",
        (
            (
                "off",
                "ปิด: คนเดียวทำได้ทุกขั้นในกองของตน",
                "ทุกขั้นยังบันทึกชื่อผู้ทำในไทม์ไลน์และ audit",
            ),
            (
                "on",
                "เปิด: ขั้นที่ต่อกันต้องเป็นคนละคน",
                "เช่น ผู้อนุมัติไม่จ่ายเอง ผู้จ่ายไม่บันทึกการยืนยันรับแทนหน่วยงาน (เขต/ฝ่าย) "
                "ผู้แก้ปัญหาไม่บันทึกคำตอบแทนหน่วยงาน (เขต/ฝ่าย) ผู้รับของคืนไม่ตรวจสภาพเอง",
            ),
        ),
        "ผู้ช่วยเหลือระบบไม่ถูกจำกัดอยู่แล้วไม่ว่าเลือกแบบใด",
    ),
    LINE_AUDIENCE: Option(
        "ใครคุยกับบอทได้ เมื่อ LINE OA ชี้มาที่เครื่องที่ไม่ใช่ระบบจริง",
        (
            (
                "testers",
                "เฉพาะบัญชีทดสอบ",
                "บอทตอบและส่งข้อความเฉพาะ LINE ID ในรายชื่อทดสอบของเครื่อง "
                "(LINE_ALLOWED_RECIPIENTS) คนอื่นที่ทักมาจะไม่ได้คำตอบ",
            ),
            (
                "everyone",
                "ทุกคน: ทักมาลงทะเบียนได้ ได้รับการรับรองแล้วใช้ได้เลย",
                "บอทตอบทุกคนเหมือนระบบจริง และส่งข้อความแจ้งเตือนได้ภายในงบเดิม "
                "ผู้แจ้งยังต้องได้รับการรับรองก่อนยื่นเรื่องตามข้อการยืนยันตัวตน",
            ),
        ),
        "LINE OA เดียวใช้กับทุกเครื่อง บนระบบจริงบอทตอบทุกคนอยู่แล้ว ข้อนี้จึงมีผลกับ staging และเครื่องทดสอบเท่านั้น",
    ),
    REGISTRATION: Option(
        "ผู้อนุมัติการลงทะเบียนผู้แจ้งทาง LINE",
        (
            ("admin", "แอดมินระบบเท่านั้น", "ปลอดภัยที่สุด ทุกคำขอผ่านแอดมิน"),
            (
                "supervisor",
                "แอดมิน หรือ แอดมินกองใดก็ได้",
                "กระจายงานอนุมัติไปที่ แอดมินของแต่ละกอง",
            ),
            (
                "staff",
                "แอดมิน หรือสมาชิกกองใดก็ได้",
                "สะดวกที่สุด เหมาะเมื่อแต่ละกองมีผู้ใช้คนเดียว",
            ),
        ),
        "ใช้กับคำขอที่ยังต้องให้คนอนุมัติ ดูการยืนยันตัวตนด้วยรหัสพนักงานในข้อถัดไป",
    ),
    REGISTRATION_CHECK: Option(
        "การยืนยันตัวตนตอนลงทะเบียนทาง LINE",
        (
            ("approval", "ให้คนอนุมัติทุกคำขอ", "ไม่ถามรหัสพนักงาน"),
            (
                "employee_review",
                "ถามรหัสพนักงานและข้อมูลยืนยัน แล้วให้คนอนุมัติ",
                "ผู้อนุมัติเห็นว่าตรงทะเบียนพนักงานหรือไม่ ช่วยตัดสินเร็วขึ้น",
            ),
            (
                "employee_auto",
                "ถามรหัสพนักงานและข้อมูลยืนยัน ถ้าตรงทะเบียนให้ระบบอนุมัติเอง",
                "บันทึกใน audit ทุกครั้ง คนที่ไม่มีรหัสหรือไม่ตรงยังเลือกให้คนอนุมัติได้เสมอ",
            ),
        ),
        "ใช้ได้เมื่อแอดมินนำเข้าทะเบียนพนักงานแล้ว ผิดได้วันละ 3 ครั้งต่อบัญชี LINE",
    ),
    REGISTRATION_LOCATION: Option(
        "ตำแหน่งของผู้ลงทะเบียน",
        (
            ("off", "ไม่ถาม", ""),
            ("optional", "ถาม แต่ข้ามได้", "เก็บละติจูด/ลองจิจูดที่ผู้ลงทะเบียนส่งจาก LINE"),
            ("required", "ต้องส่งตำแหน่ง", "ลงทะเบียนไม่ได้ถ้าไม่ส่งตำแหน่ง"),
        ),
        "LINE ส่งตำแหน่งเมื่อผู้ใช้กดส่งเองเท่านั้น ระบบแจ้งเหตุผลก่อนขอ",
    ),
    REGISTRATION_NAME: Option(
        "ชื่อที่พิมพ์ตอนลงทะเบียนทาง LINE",
        (
            (
                "full",
                "ต้องมีทั้งชื่อและนามสกุล",
                "คำทักทาย คำถาม คำขอ ข้อความที่มีตัวเลข หรือคำเดียว ระบบไม่รับเป็นชื่อ "
                "แต่ถามใหม่พร้อมตัวอย่าง และมีปุ่ม 'แก้ชื่อ' ตอนเลือกหน่วยงาน (เขต/ฝ่าย)",
            ),
            ("any", "รับข้อความที่พิมพ์เป็นชื่อ (2-200 ตัวอักษร)", "ผู้อนุมัติต้องตรวจชื่อเอง"),
        ),
    ),
    ANONYMOUS: Option(
        "แจ้งปัญหา/ข้อเสนอแนะแบบไม่ระบุตัวตน",
        (
            (
                "on",
                "เปิด: คนที่ยังไม่ลงทะเบียนแจ้งปัญหา/ข้อเสนอแนะได้ ต้องส่งตำแหน่ง",
                "เบิกและคืนของยังต้องยืนยันตัวตน เจ้าหน้าที่ไม่เห็นว่าใครแจ้ง",
            ),
        ),
    ),
    ANONYMOUS_LIMIT: Option(
        "จำนวนเรื่องไม่ระบุตัวตนต่อบัญชี LINE ต่อวัน",
        (("3", "3 เรื่อง", ""), ("5", "5 เรื่อง", ""), ("10", "10 เรื่อง", "")),
        "กันการแจ้งซ้ำจำนวนมาก",
    ),
    STAFF_LINE_BUDGET: Option(
        "งบข้อความ LINE ที่เด้งถึงเจ้าหน้าที่ต่อเดือน",
        (
            ("220", "220 ข้อความ", "ค่าที่ตกลงไว้ 10 ต.ค. 2569"),
            ("100", "100 ข้อความ", ""),
            ("50", "50 ข้อความ", ""),
            ("0", "ไม่เด้งถึงเจ้าหน้าที่", "เจ้าหน้าที่ดูงานจากเว็บหรือเมนูใน LINE"),
        ),
        "นับรวมกับโควตาของ LINE OA (แพ็กเกจฟรี 300 ข้อความ/เดือน) เมื่อใกล้หมดส่งเฉพาะเรื่องด่วน",
    ),
    STAFF_LINK: Option(
        "วิธีเชื่อม LINE กับบัญชีเจ้าหน้าที่",
        (
            (
                "code_or_otp",
                "รหัสจากเว็บ หรือรหัสพนักงานกับรหัสทางอีเมล",
                "พิมพ์รหัสพนักงานใน LINE แล้วระบบส่งรหัส 6 หลักไปที่อีเมลในทะเบียนพนักงาน "
                "ใช้ได้เมื่อบัญชีเจ้าหน้าที่มีรหัสพนักงานและเครื่องส่งอีเมลได้ "
                "เมื่อปิดการส่งรหัส OTP รหัสพนักงานอย่างเดียวเชื่อมได้ทันที (ADR 0059)",
            ),
            ("code", "รหัส 6 หลักจากหน้าเว็บ บัญชีของฉัน", "พิสูจน์ทั้งสองฝั่ง ต้องเข้าเว็บได้ก่อน"),
            (
                "code_or_employee",
                "รหัสจากเว็บ หรือรหัสพนักงานกับข้อมูลยืนยัน (ระบบเชื่อมให้เอง)",
                "ใช้ทะเบียนพนักงานและรหัสพนักงานในบัญชีเจ้าหน้าที่ บันทึกใน audit ทุกครั้ง",
            ),
        ),
    ),
    STAFF_LINE_ACTIONS: Option(
        "สิ่งที่เจ้าหน้าที่ทำได้ใน LINE",
        (
            (
                "act",
                "ทำงานได้ใน LINE ทุกขั้นที่ไม่ต้องใช้ฟอร์มยาว",
                "ถามทีละข้อแล้วให้กดยืนยัน ขั้นที่มีรายการของ หมายเลขเครื่อง หรือจำนวน ทำในเว็บ "
                "ทุกการกระทำผ่านกติกาและสิทธิ์เดียวกับเว็บ และบันทึกใน audit",
            ),
            ("view", "ดูงานใน LINE แล้วเปิดเว็บเพื่อทำงาน", ""),
        ),
    ),
    STAFF_NOTIFY: Option(
        "ใครได้ข้อความแจ้งเตือนงานสั่ง",
        (
            (
                "acting",
                "เฉพาะคนที่ต้องทำ",
                "งานที่สั่งถึงคนไหนแจ้งคนนั้น งานที่สั่งถึงทั้งกองแจ้ง แอดมินกอง "
                "(ถ้ากองไม่มีแอดมินกอง แจ้งทุกคนในกอง) คนอื่นดูงานได้จากเมนูโดยไม่ใช้โควตาข้อความ",
            ),
            (
                "division",
                "ทุกคนในกอง",
                "ทุกคนรู้พร้อมกัน แต่ใช้โควตาข้อความ LINE มากขึ้นตามจำนวนคน",
            ),
        ),
        "ผู้สั่งได้ข้อความเมื่อมีคนกดดำเนินการ เสร็จสิ้น หรือส่งงานกลับเสมอ",
    ),
    ORDER_REMIND: Option(
        "เตือนงานสั่งก่อนถึงกำหนด",
        (
            ("1", "1 วันก่อนกำหนด", "เตือนครั้งเดียว และเตือนอีกครั้งเมื่อเกินกำหนด"),
            ("2", "2 วันก่อนกำหนด", ""),
            ("3", "3 วันก่อนกำหนด", ""),
            ("0", "ไม่เตือนก่อนกำหนด", "ยังเตือนเมื่อเกินกำหนด"),
        ),
    ),
    CHAT_ANSWERS: Option(
        "การตอบคำถามในแชต LINE",
        (
            ("off", "ปิด: ถามตามขั้นตอนอย่างเดียว", ""),
            (
                "faq",
                "ตอบจากคลังคำตอบด้วยคำสำคัญ ไม่ใช้ AI",
                "ใช้เฉพาะคำตอบที่รับรองแล้ว ข้อความที่ส่งเป็นข้อความที่รับรองเสมอ",
            ),
            (
                "choose",
                "คำสำคัญก่อน ไม่ตรงให้ AI เลือกคำตอบจากคลัง",
                "AI เลือกได้เฉพาะคำตอบที่รับรองแล้ว ข้อความที่ส่งยังเป็นข้อความที่รับรอง",
            ),
            (
                "write",
                "คำสำคัญก่อน ไม่ตรงให้ AI เขียนคำตอบจากคลังและเรื่องของผู้แจ้งเอง",
                "ระบบตรวจก่อนส่ง (ห้ามรับปาก ห้ามตัวเลขที่ไม่มีในข้อมูล ห้ามเรื่องของคนอื่น) ไม่ผ่านใช้ข้อความที่รับรองแทน",
            ),
            (
                "agent",
                "คำสำคัญก่อน ไม่ตรงให้ AI ตอบอิสระด้วยเครื่องมือและความรู้ทั่วไป (ADR 0037)",
                "AI ค้นเรื่องของผู้ถาม ผังหน่วยงาน เมนู และเอกสารความรู้ ไม่ให้ข้อมูลรายบุคคล "
                "ระบบตรวจข้อมูลส่วนบุคคลก่อนส่ง ไม่ผ่านใช้ข้อความที่รับรองแทน",
            ),
        ),
        "AI ใช้ได้เมื่อมีรุ่น AI งานตอบแชตที่ผ่านการประเมินและเปิดใช้แล้ว ระดับตอบอิสระต้องมีผลประเมินของงานนี้ด้วย "
        "ข้อความเรื่องการใช้ไฟฟ้าไม่ถึง AI",
    ),
    FREE_CHAT_DAILY: Option(
        "จำนวนคำถามที่ AI ตอบอิสระต่อบัญชี LINE ต่อวัน",
        (("30", "30 คำถาม", ""), ("10", "10 คำถาม", ""), ("50", "50 คำถาม", "")),
        "เกินแล้วตอบจากคลังคำตอบด้วยคำสำคัญอย่างเดียว กันโควตา AI หมดจนงานอ่านร่างใช้ไม่ได้",
    ),
    FREE_CHAT_DOCS: Option(
        "การค้นเอกสารความรู้บน Pinecone เมื่อ AI ตอบอิสระ",
        (
            ("off", "ปิด: ไม่ส่งอะไรไป Pinecone", ""),
            (
                "on",
                "เปิด: ส่งเอกสารที่รับรองแล้ว และคำถามที่ปิดข้อมูลส่วนบุคคลแล้ว",
                "ต้องตั้งคีย์และ host ของ index ไว้ในค่าระบบก่อน ใช้แผน Starter ที่ไม่ผูกบัตร",
            ),
        ),
        "ADR 0037 Q21: ปิดไว้จนกว่าจะมีเอกสารจริงและบัญชี Pinecone",
    ),
    FREE_CHAT_TUNING: Option(
        "ให้ AI ปรับคำแนะนำเฉพาะเรื่องเองทุกคืน",
        (
            (
                "on",
                "เปิด: AI ปรับจากคำถามที่ผู้ใช้ 3 คนขึ้นไปถามภายใน 7 วัน",
                "ทุกฉบับต้องผ่านกฎและชุดตรวจก่อนใช้ และย้อนกลับได้ในหน้าคำแนะนำ",
            ),
            ("off", "ปิด: แอดมินแก้เองเท่านั้น", ""),
        ),
        "ADR 0037 Q23: แกนของ prompt ไม่ถูกแตะ AI ปรับได้เฉพาะคำแนะนำเฉพาะเรื่อง",
    ),
    CHAT_FOLLOWUP: Option(
        "คำถามต่อหลัง AI เติมร่างจากข้อความ",
        (
            ("fixed", "ใช้คำถามมาตรฐานของแต่ละขั้น", ""),
            (
                "ai",
                "ให้ AI ถามต่อโดยอ้างถึงสิ่งที่ผู้แจ้งพิมพ์มา",
                "ขั้นต่อไปยังเป็นของระบบ AI แค่เรียบเรียงคำถาม ระบบตรวจก่อนส่ง ไม่ผ่านใช้คำถามมาตรฐาน",
            ),
        ),
        "ใช้ได้เมื่อมีรุ่น AI งานตอบแชตที่ผ่านการประเมินและเปิดใช้แล้ว",
    ),
    CHAT_TYPED: Option(
        "คำถามหรือคำขอใหม่ที่พิมพ์มาในขั้นที่ให้พิมพ์คำตอบ",
        (
            ("answer", "รับเป็นคำตอบของขั้นนั้นเสมอ", ""),
            (
                "check",
                "ตรวจก่อน: ข้อความที่เป็นคำถามหรือขึ้นต้นด้วยคำขอ ให้คลังคำตอบและ AI ดูก่อน",
                "เช่น พิมพ์ 'ขอเบิกมิเตอร์เพิ่ม' ตอนบอทถามวันที่ หรือ 'เปิดกี่โมง' ตอนบอทถามวัตถุประสงค์ "
                "ถ้าไม่ใช่คำถามในคลังและไม่ใช่คำขอ ระบบรับเป็นคำตอบของขั้นตามเดิม",
            ),
        ),
        "คำตอบจากคลังใช้ได้เมื่อเปิดการตอบคำถามในแชต ส่วน AI ใช้ได้เมื่อมีรุ่นที่เปิดใช้แล้ว",
    ),
    # ADR 0022 (owner, 25 Oct 2569 Q18): the owner chose the AI as the default; "guided" is the
    # step-by-step bot, kept so an admin can return to it at once without a release.
    CHAT_MODE: Option(
        "วิธีคุยในแชต",
        (
            (
                "llm",
                "AI คุยกับผู้ใช้ LINE และกรอกร่างจากข้อความเอง",
                "AI อ่านข้อความเทียบกับร่างและคำถามล่าสุด ตอบคำถาม และเรียบเรียงคำถามถัดไป "
                "ระบบยังตรวจทุกค่า สรุปจากร่าง และส่งเรื่องเมื่อผู้ใช้ยืนยันเท่านั้น",
            ),
            (
                "guided",
                "ถามทีละขั้นตามปุ่ม",
                "ข้อความที่พิมพ์ในขั้นที่ให้พิมพ์เป็นคำตอบของขั้นนั้น เหมือนก่อน 25 ต.ค. 2569",
            ),
        ),
        "AI คุยได้เมื่อรุ่น AI อ่านข้อความที่เปิดใช้ผ่านชุดทดสอบบทสนทนา (dialogue-v1) แล้ว "
        "ถ้ายังไม่ผ่าน หรือ AI ตอบไม่ทัน แชตถามทีละขั้นเอง",
    ),
    # ADR 0021 (owner, 25 Oct 2569): the owner's choice is the default; "never" is the Q14 rule
    # of 10 Oct 2569, kept so an admin can close the path at once without a release.
    USAGE_AI: Option(
        "ข้อความเรื่องการใช้ไฟฟ้ากับ AI",
        (
            (
                "masked",
                "ส่งให้ AI ได้หลังปิดข้อมูลส่วนบุคคล",
                "AI ทุกงานอ่านได้ ทั้งแชต การเติมร่าง การคัดกรอง การแนะนำกอง และคำที่ระบบจำ "
                "เบอร์โทร เลขยาว และชื่อที่ระบบรู้จักถูกปิดก่อนส่งทุกครั้ง",
            ),
            (
                "never",
                "ไม่ส่งให้ AI",
                "ข้อความและเรื่องการใช้ไฟฟ้าใช้ขั้นตอนตามปุ่มเท่านั้น เหมือนก่อน 25 ต.ค. 2569",
            ),
        ),
        "เปลี่ยนเป็นไม่ส่งได้ทันทีถ้าพบข้อมูลผู้ใช้ไฟหลุดไปผู้ให้บริการ",
    ),
    INFO_REPLY: Option(
        "ข้อความที่ผู้แจ้งพิมพ์ ขณะเจ้าหน้าที่รอข้อมูลเพิ่มจากเขา",
        (
            (
                "check",
                "ดูก่อน: ข้อมูลส่งถึงเจ้าหน้าที่ทันที คำถามหรือคำขอใหม่ให้ผู้แจ้งเลือกก่อน",
                "คำทักทายไม่ถูกส่ง บอทตอบแล้วบอกว่าเจ้าหน้าที่รอข้อมูลอะไร",
            ),
            (
                "confirm",
                "ให้ผู้แจ้งกดยืนยันก่อนส่งทุกข้อความ",
                "กดเพิ่มหนึ่งครั้ง แต่ไม่มีข้อความหลงไปถึงเจ้าหน้าที่",
            ),
        ),
        "ถ้าผู้แจ้งกด 'ไม่ใช่ข้อมูลเพิ่ม' ระบบทำกับข้อความนั้นเหมือนข้อความทั่วไป",
    ),
    LEARNED_ROUTES: Option(
        "คำที่ระบบจำ: เรื่องที่กติกาบอกกองไม่ได้ แต่มีคำที่เคยส่งไปกองหนึ่ง",
        (
            (
                "direct",
                "ส่งตรงไปกองที่คำนั้นเคยไป",
                "ไม่ผ่าน ฝอจ. ถ้ากองส่งต่อด้วยเหตุผล 'ส่งผิดกอง' และกองใหม่รับ "
                "คำจะย้ายตามไปเอง ค่าเริ่มต้นที่ตกลงไว้ (14 ต.ค. 2569)",
            ),
            (
                "propose",
                "ส่ง ฝอจ. ก่อน พร้อมเสนอกองที่คำนั้นเคยไป",
                "ฝอจ. กดส่งต่อตามข้อเสนอได้ในครั้งเดียว",
            ),
            (
                "off",
                "ปิดการจำ: ไม่ใช้และไม่จำคำใหม่",
                "เรื่องที่กติกาบอกกองไม่ได้ไป ฝอจ. ทุกเรื่อง คำที่จำไว้แล้วยังอยู่ เปิดกลับได้",
            ),
        ),
        "ระบบจำคำเมื่อ ฝอจ. ส่งต่อเรื่องที่กติกาบอกกองไม่ได้พร้อมใส่คำสำคัญ และกองปลายทางรับเรื่อง กติกาหลักมาก่อนเสมอ",
    ),
    ROUTING_AI: Option(
        "AI ส่งกองจากหน้าที่ของกอง: เมื่อกติกาและคำที่ระบบจำบอกกองไม่ได้",
        (
            (
                "propose",
                "เสนอ: เรื่องไป ฝอจ. พร้อมกองที่ AI เสนอและคำที่ AI ยกมา",
                "ฝอจ. กดส่งต่อตามข้อเสนอได้ในครั้งเดียว ช่องคำสำคัญมีคำของ AI เติมไว้ให้",
            ),
            (
                "direct",
                "ส่งตรงไปกองที่ AI เลือกจากรายการงานที่ดูแล",
                "ใช้เมื่อเห็นว่า AI แม่นพอ คำตอบที่ AI เลือกจากภาพรวมของกองยังเป็นการเสนอเสมอ "
                "ถ้ากองรับเรื่องโดยไม่โอนออก ระบบจำคำที่ AI ยกมา",
            ),
        ),
        "AI ทำงานเมื่อมีรุ่นที่เปิดใช้และผ่านการประเมินกับหน้าที่ของกองชุดปัจจุบัน "
        "แก้หน้าที่ของกองแล้วต้องประเมินใหม่ก่อน AI จะเสนออีก",
    ),
    ROUTING_AI_ACCURACY: Option(
        "เกณฑ์ผ่านการประเมิน AI ส่งกอง: ตอบกองถูกอย่างน้อย",
        (
            ("0.90", "90% ของตัวอย่าง", "ค่าที่ตกลงไว้ (14 ต.ค. 2569)"),
            ("0.95", "95% ของตัวอย่าง", "เข้มขึ้น"),
            ("0.85", "85% ของตัวอย่าง", "ผ่อนลง ใช้ช่วงที่ตัวอย่างยังน้อย"),
            ("0.80", "80% ของตัวอย่าง", "ผ่อนลงมาก"),
        ),
        "เกณฑ์อื่นไม่เปลี่ยน: ไม่ตอบนอกรายการเลย ข้อความหลอกเปลี่ยนผลไม่ได้เลย และต้องผ่าน 2 รอบติดกัน",
    ),
    CATCH_UP: Option(
        "คำตอบที่ค้างตอนระบบล่ม",
        (
            (
                "on",
                "ส่งคำตอบล่าสุดเป็นข้อความแจ้งเตือนให้คนที่รอบอทตอบ คนละ 1 ครั้ง",
                "ใช้เมื่อ LINE ส่งข้อความซ้ำมาหลังระบบกลับมาแล้ว reply ใช้ไม่ได้ "
                "หยุดส่งเมื่อโควตาข้อความแจ้งเตือนเหลือ 20% คนที่เหลือได้คำตอบตอนทักครั้งถัดไป",
            ),
            ("off", "ไม่ส่ง รอให้ผู้ใช้ทักมาใหม่", "ไม่ใช้โควตาข้อความแจ้งเตือนเลย"),
        ),
        "นับรวมกับโควตาข้อความแจ้งเตือนของ LINE OA ภายใน 30 นาทีส่งให้คนเดียวกันได้ครั้งเดียว",
    ),
    CATCH_UP_APOLOGY: Option(
        "ขอโทษที่ตอบช้าในคำตอบที่ค้าง เมื่อรอนานกว่า",
        (
            ("10", "10 นาที", "ค่าที่ตกลงไว้ (14 ต.ค. 2569)"),
            ("5", "5 นาที", ""),
            ("30", "30 นาที", ""),
            ("never", "ไม่ต้องขอโทษ", ""),
        ),
    ),
    SAP_LINE_ENTRY: Option(
        "เรื่องเบิก/คืนที่ไปกองที่ต้องมีเลข SAP แต่ไม่ได้เริ่มจากปุ่มของกอง",
        (
            (
                "typed",
                "พิมพ์ช่องเหมือนปุ่มของกอง",
                "ค่าที่ตกลงไว้ (23 ต.ค. 2569) ก่อนสรุป บอทถามเลข SAP และช่องที่ยังขาด เช่น "
                "หมายเลขเครื่องวัดและขนาด ข้อมูลของกองจึงเป็นแบบเดียวกัน",
            ),
            (
                "catalog",
                "เลือกรุ่นจากรายการ ถามแค่เลข SAP",
                "ทางที่เลือกรุ่นจากรายการทำแบบเดิม ก่อนสรุปบอทถามเฉพาะเลขใบเบิกหรือใบส่งคืน SAP",
            ),
        ),
    ),
    REGISTRY_ACCESS: Option(
        "เมนูทะเบียน (SAP) สำหรับเทียบกับไฟล์ที่ส่งออกจาก SAP",
        (
            (
                "off",
                "ไม่ใช้",
                "ค่าที่ตกลงไว้ (23 ต.ค. 2569) ระบบนี้เก็บเลขเอกสาร SAP ที่เจ้าหน้าที่พิมพ์ "
                "ไม่นำเข้าไฟล์ SAP มาเทียบ และไม่มีเมนูทะเบียน ข้อมูลที่เคยนำเข้ายังเก็บไว้",
            ),
            (
                "admin",
                "แอดมินเท่านั้น",
                "แอดมินนำเข้าไฟล์ SAP และตัดสินส่วนต่างเอง เจ้าหน้าที่กองไม่เห็นเมนูทะเบียน",
            ),
            (
                "staff",
                "แอดมินและเจ้าหน้าที่กอง",
                "แต่ละกองนำเข้ายอด SAP และตัดสินส่วนต่างของกองตัวเอง แอดมินดูได้อย่างเดียว",
            ),
        ),
    ),
    # ADR 0032: DED Center tracks slips; the stock work of M07, M08 and M11 stays in the code.
    OLD_STOCK: Option(
        "แสดงงานคลังเดิม (จำนวนอนุมัติ จัดสรร จ่าย รับของคืน ตรวจสภาพ)",
        (
            (
                "off",
                "ไม่แสดง",
                "ค่าที่ตกลงไว้ (ADR 0032, 29 ต.ค. 2569) ระบบติดตามใบเบิกและใบส่งคืนที่ออกใน SAP "
                "ไม่มีจำนวนของในคลัง กองรับเรื่องทั้งเรื่องด้วยปุ่มเดียว และไม่เห็นปุ่มจัดสรร "
                "บันทึกจ่าย รับของคืน ตรวจสภาพ และหน้าคลังของ ข้อมูลที่เคยบันทึกยังเก็บไว้",
            ),
            (
                "on",
                "แสดง",
                "กองอนุมัติจำนวนรายการ จัดสรร บันทึกจ่าย รับของคืน และตรวจสภาพในระบบได้ "
                "แอดมินเห็นหน้าคลังของ ใช้เมื่อมียอดจากไฟล์ CSV ของ SAP แล้วเท่านั้น",
            ),
        ),
    ),
    # ADR 0059: off by default, the owner's choice over the safer "on".
    EMPLOYEE_OTP: Option(
        "ส่งรหัส OTP ทางอีเมลเมื่อพนักงานยืนยันตัวตนใน LINE",
        (
            (
                "off",
                "ปิด: พิมพ์รหัสพนักงานที่ตรงทะเบียนพนักงานแล้วลงทะเบียนสำเร็จ",
                "ค่าที่เจ้าของระบบเลือก (ADR 0059) ไม่ส่งและไม่ถามอีเมล ระบบผูกบัญชีเจ้าหน้าที่ที่ถือรหัสเดียวกัน"
                "และให้บทบาทเหมือนยืนยันด้วย OTP สะดวกกว่าแต่ปลอดภัยน้อยกว่า: คนที่รู้รหัสพนักงานของคนอื่น"
                "ลงทะเบียนเป็นคนนั้นได้ รหัสที่ LINE อื่นใช้อยู่แล้วส่งให้แอดมินตรวจ",
            ),
            (
                "on",
                "เปิด: ยืนยันด้วยอีเมลในทะเบียนตามข้อถัดไป",
                "แบบก่อน 23 พ.ย. 2569 พิสูจน์ได้ว่าเป็นเจ้าของอีเมลจริง",
            ),
        ),
        "มีผลกับการลงทะเบียนและการพิมพ์ 'เชื่อมบัญชีเจ้าหน้าที่' ใน LINE ไม่มีผลกับการเข้าเว็บ "
        "พิมพ์รหัสผิดได้วันละ 3 ครั้งต่อบัญชี LINE เท่าเดิม",
    ),
    EMAIL_METHODS: Option(
        "พนักงานยืนยันตัวตนใน LINE ด้วยอีเมลในทะเบียนอย่างไร (หลังพิมพ์รหัสพนักงาน)",
        (
            (
                "both",
                "ให้ผู้ใช้เลือก: รับรหัส OTP ทางอีเมล หรือพิมพ์อีเมลเอง",
                "ค่าที่ตกลงไว้ (15 ต.ค. 2569) ถ้าเซิร์ฟเวอร์ยังส่งอีเมลไม่ได้ จะเหลือแบบพิมพ์อีเมล",
            ),
            ("otp", "รหัส OTP ทางอีเมลเท่านั้น", "พิสูจน์ได้ว่าเป็นเจ้าของอีเมลจริง"),
            ("typed", "พิมพ์อีเมลเท่านั้น", "ไม่ต้องส่งอีเมล แต่อีเมลองค์กรมักเดาได้จากชื่อ"),
        ),
        "ใช้เมื่อเปิดการส่งรหัส OTP ในข้อก่อนหน้า ใช้วิธีอื่นให้แอดมินอนุมัติได้เสมอ",
    ),
    EMAIL_TYPED: Option(
        "พิมพ์อีเมลตรงทะเบียนแล้ว",
        (
            (
                "auto",
                "ระบบอนุมัติให้เองเหมือนรหัส OTP และบันทึกวิธีที่ใช้",
                "ค่าที่ตกลงไว้ (15 ต.ค. 2569) ใช้เมื่อการยืนยันตัวตนให้ระบบอนุมัติเอง",
            ),
            (
                "review",
                "ส่งให้แอดมินอนุมัติ พร้อมป้ายว่าพิมพ์อีเมลตรงทะเบียน",
                "ปลอดภัยกว่า: คนที่รู้รหัสพนักงานกับชื่อของคนอื่นเดาอีเมลได้",
            ),
        ),
    ),
    CODE_MINUTES: Option(
        "รหัส OTP ทางอีเมลใช้ได้นาน",
        (
            ("10", "10 นาที", "ค่าที่ตกลงไว้ (15 ต.ค. 2569)"),
            ("5", "5 นาที", ""),
            ("15", "15 นาที", ""),
        ),
    ),
    CODE_TRIES: Option(
        "พิมพ์รหัส OTP ผิดได้ต่อรหัส",
        (("3", "3 ครั้ง", "ค่าที่ตกลงไว้ (15 ต.ค. 2569)"), ("5", "5 ครั้ง", "")),
    ),
    CODE_PER_DAY: Option(
        "ขอรหัส OTP ได้ต่อบัญชี LINE ต่อวัน",
        (
            ("3", "3 ครั้ง", "ค่าที่ตกลงไว้ (15 ต.ค. 2569)"),
            ("5", "5 ครั้ง", ""),
            ("1", "1 ครั้ง", ""),
        ),
        "นับรวมกับการยืนยันผิดวันละ 3 ครั้งต่อบัญชี LINE ที่มีอยู่แล้ว",
    ),
    MONITOR_TIME: Option(
        "Monitoring: เวลาที่คำนวณตัวเลขของเมื่อวาน",
        (
            ("02:00", "ตี 2", "ค่าที่ตกลงไว้ (15 ต.ค. 2569)"),
            ("00:30", "เที่ยงคืนครึ่ง", ""),
            ("05:00", "ตี 5", ""),
        ),
    ),
    MONITOR_KEEP: Option(
        "Monitoring: เก็บตัวเลขรายวันไว้นาน",
        (("365", "1 ปี", "ค่าที่ตกลงไว้ (15 ต.ค. 2569)"), ("180", "6 เดือน", ""), ("730", "2 ปี", "")),
        "เก็บเฉพาะตัวเลขรวม ไม่มีข้อความของใคร",
    ),
    MONITOR_ERROR: Option(
        "Monitoring: แจ้งเตือนเมื่อ AI ผิดพลาดเกิน",
        (
            ("0.10", "10% ของการเรียกในวันนั้น", "ค่าที่ตกลงไว้ (15 ต.ค. 2569)"),
            ("0.05", "5%", ""),
            ("0.20", "20%", ""),
        ),
        "นับเมื่อวันนั้นเรียก AI อย่างน้อย 5 ครั้ง",
    ),
    MONITOR_P95: Option(
        "Monitoring: แจ้งเตือนเมื่อ AI ตอบช้า (p95) เกิน",
        (
            ("12000", "12 วินาที", "เท่ากับเกณฑ์ประเมินตอบแชต"),
            ("8000", "8 วินาที", ""),
            ("20000", "20 วินาที", ""),
        ),
    ),
    MONITOR_UNANSWERED: Option(
        "Monitoring: แจ้งเตือนเมื่อคำถามที่บอทตอบไม่ได้สูงกว่าค่าเฉลี่ย 7 วัน",
        (("2", "2 เท่า", "ค่าที่ตกลงไว้ (15 ต.ค. 2569)"), ("1.5", "1.5 เท่า", ""), ("3", "3 เท่า", "")),
        "นับเมื่อวันนั้นมีอย่างน้อย 3 ครั้ง",
    ),
    MONITOR_EMAIL: Option(
        "Monitoring: ส่งอีเมลหาแอดมินเมื่อมีเรื่องที่ควรดู",
        (
            ("off", "ไม่ส่ง แสดงในหน้าแอดมินอย่างเดียว", "ค่าที่ตกลงไว้ (15 ต.ค. 2569)"),
            ("on", "ส่งอีเมลหาแอดมินที่มีอีเมล", "ส่งวันละครั้งหลังคำนวณเสร็จ"),
        ),
    ),
    MONITOR_JUDGE: Option(
        "Monitoring: ให้ AI ตรวจคำตอบแชต (Evidently LLM judge)",
        (
            (
                "off",
                "ปิด",
                "ค่าที่ตกลงไว้ (16 ต.ค. 2569) เปิดได้เมื่อตัวตัดสินผ่านชุดตัวอย่างอย่างน้อย 90%",
            ),
            (
                "on",
                "เปิด: ตรวจคำตอบแชตไม่เกิน 50 ข้อทุกคืน",
                "ตรวจว่าคำตอบตรงกับคลังคำตอบและตรงกับคำถาม ด้วยโมเดลหลักของรุ่นตอบแชต "
                "อ่านเฉพาะข้อความที่ปิดข้อมูลส่วนบุคคลแล้ว ไม่อ่านเรื่องการใช้ไฟฟ้า ต่ำกว่า 80% แจ้งเตือน",
            ),
        ),
    ),
}


# ADR 0028: the step a requester sees in LINE for each open status; closed, rejected and
# cancelled are fixed (เสร็จสิ้น, and no step bar for the other two).
STEP_PREFIX = "requester_step:"
STEP_CHOICES = {
    "received": ("รับเรื่องแล้ว", "แถบขั้น ●○○○"),
    "working": ("กำลังดำเนินการ", "แถบขั้น ●●○○"),
    "need_you": ("รอข้อมูลจากคุณ", "แถบขั้น ●●●○"),
    "done_confirm": ("เสร็จสิ้น รอคุณยืนยัน", "แถบขั้น ●●●● พร้อมปุ่มยืนยัน"),
}
STEP_DEFAULTS = (
    ("submitted", "รอคัดกรอง", "received", "ADR 0028"),
    ("triage", "กำลังคัดกรอง", "received", "ADR 0028"),
    ("awaiting_approval", "รอรับเรื่อง", "working", "30 ต.ค. 2569"),
    ("awaiting_execution", "รอดำเนินการ", "working", "30 ต.ค. 2569"),
    ("in_progress", "กำลังดำเนินการ", "working", "ADR 0028"),
    ("on_hold", "พักเรื่อง", "working", "ADR 0028"),
    ("awaiting_info", "รอข้อมูลเพิ่ม", "need_you", "ADR 0028"),
    ("awaiting_confirmation", "รอหน่วยงาน (เขต/ฝ่าย) ยืนยัน", "done_confirm", "ADR 0028"),
)
for _status, _label, _default, _agreed in STEP_DEFAULTS:
    OPTIONS[STEP_PREFIX + _status] = Option(
        f"ขั้นที่ผู้แจ้งเห็นใน LINE: สถานะ{_label}",
        (
            (_default, STEP_CHOICES[_default][0], f"ค่าที่ตกลงไว้ ({_agreed})"),
            *((v, title, bar) for v, (title, bar) in STEP_CHOICES.items() if v != _default),
        ),
        "ผู้แจ้งเห็นขั้นนี้ใน เรื่องของฉัน พร้อมสถานะจริงเป็นบรรทัดรอง",
    )

# 30 ต.ค. 2569: a case waiting for its division reads กำลังดำเนินการ to the LINE user; staff
# pages keep รอรับเรื่อง and its queue. รอดำเนินการ is then at the same step, so the bar
# never goes back once the division takes the case.
LINE_AWAITING = "line_awaiting_approval"
OPTIONS[LINE_AWAITING] = Option(
    "สถานะที่ผู้ใช้ LINE เห็นเมื่อเรื่องรอรับเรื่อง",
    (
        ("working", "กำลังดำเนินการ", "ค่าที่ตกลงไว้ (30 ต.ค. 2569)"),
        ("real", "รอรับเรื่อง", "คำเดียวกับหน้าเว็บเจ้าหน้าที่"),
    ),
    "ใช้ในข้อความหลังส่งเรื่อง ใน เรื่องของฉัน และในผลค้นเรื่อง หน้าเว็บเจ้าหน้าที่ยังเห็นรอรับเรื่อง",
)


# ADR 0048: Push Message, each kind on or off for the whole system (checked when it is sent).
PUSH_PREFIX = "push:"
PUSH_GROUPS = (
    ("requester", "ถึงผู้แจ้ง"),
    ("staff", "ถึงเจ้าหน้าที่"),
    ("order", "งานสั่ง"),
    ("koj", "งานติดตาม กอจ."),
)
# (kind, group, label, who gets it, a sample, on by default)
PUSH_KINDS = (
    (
        "request_info",
        "requester",
        "เจ้าหน้าที่ขอข้อมูลเพิ่ม",
        "ผู้แจ้ง",
        "เรื่อง กคว.69-000142: เจ้าหน้าที่ขอข้อมูลเพิ่ม",
        True,
    ),
    (
        "case_declined",
        "requester",
        "ไม่รับเรื่อง",
        "ผู้แจ้ง",
        "เรื่อง กคว.69-000142 ไม่ได้รับเรื่อง เหตุผล: …",
        True,
    ),
    (
        "case_cancelled",
        "requester",
        "เจ้าหน้าที่ยกเลิกเรื่อง",
        "ผู้แจ้ง",
        "เรื่อง กคว.69-000142 ถูกยกเลิกโดยเจ้าหน้าที่ เหตุผล: …",
        False,
    ),
    (
        "case_not_approved",
        "requester",
        "เรื่องไม่ได้รับอนุมัติ",
        "ผู้แจ้ง",
        "เรื่อง กคว.69-000142 ไม่ได้รับอนุมัติ",
        True,
    ),
    (
        "resolution_request",
        "requester",
        "ผลการแก้ปัญหา รอผู้แจ้งยืนยัน",
        "ผู้แจ้ง",
        "เรื่อง กคว.69-000142 แก้ไขแล้ว กดยืนยันหรือแจ้งว่ายังไม่หาย",
        True,
    ),
    (
        "receipt_request",
        "requester",
        "จ่ายของแล้ว รอยืนยันรับ",
        "ผู้แจ้ง",
        "เรื่อง กคว.69-000142 จ่ายของแล้ว กดยืนยันเมื่อได้รับ",
        True,
    ),
    (
        "return_instructions",
        "requester",
        "วิธีส่งคืนของ",
        "ผู้แจ้ง",
        "เรื่อง กคว.69-000142 ส่งคืนของได้ที่ …",
        True,
    ),
    ("case_closed", "requester", "ปิดเรื่อง", "ผู้แจ้ง", "เรื่อง กคว.69-000142 เสร็จสิ้นแล้ว", True),
    (
        "hazard_ack",
        "requester",
        "รับแจ้งเหตุอันตราย",
        "ผู้แจ้งที่เจ้าหน้าที่บันทึกเรื่องแทน",
        "รับแจ้งเหตุแล้ว ระหว่างรอให้ …",
        True,
    ),
    (
        "registration_result",
        "requester",
        "ผลการลงทะเบียน",
        "ผู้ขอลงทะเบียน",
        "ลงทะเบียนเรียบร้อยแล้ว กด 'ส่งเรื่องใหม่' เพื่อแจ้งเรื่องได้เลย",
        True,
    ),
    (
        "case_chat",
        "requester",
        "ข้อความจากเจ้าหน้าที่ในเรื่อง",
        "ผู้แจ้งที่ไม่ได้พิมพ์มาเกิน 50 วินาที",
        "ข้อความที่เจ้าหน้าที่พิมพ์จากหน้าต่างแชตบนเว็บ",
        True,
    ),
    (
        "staff_new_case",
        "staff",
        "เรื่องใหม่รอรับเรื่อง",
        "เลือกผู้รับด้านล่าง",
        "เรื่องใหม่รอรับเรื่อง\nเรื่อง กคว.69-000142 · กอง กคว.",
        True,
    ),
    (
        "staff_assigned",
        "staff",
        "มอบหมายผู้รับผิดชอบ",
        "เลือกผู้รับด้านล่าง",
        "มีเรื่องมอบหมายให้คุณ\nเรื่อง กคว.69-000142 · กอง กคว. [รับงาน] [ดูเรื่อง]",
        True,
    ),
    (
        "staff_assignment_returned",
        "staff",
        "ผู้รับผิดชอบขอคืนงาน",
        "แอดมินกองของกองเจ้าภาพ",
        "ผู้รับผิดชอบขอคืนงาน\nเรื่อง กคว.69-000142 · กอง กคว.",
        True,
    ),
    (
        "staff_hazard",
        "staff",
        "เรื่องใหม่แจ้งเหตุอันตราย (ฉุกเฉิน)",
        "ผู้รับผิดชอบ หรือผู้รับแจ้งเรื่องสำคัญของหน่วย",
        "เรื่องใหม่แจ้งเหตุอันตราย (ฉุกเฉิน)",
        True,
    ),
    ("staff_routing", "staff", "เรื่องรอผู้ประสานตัดสิน", "ผู้มีอำนาจตัดสินกอง", "มีเรื่องรอผู้ประสานตัดสิน", True),
    ("staff_escalated", "staff", "เรื่องถูกยกระดับ", "ผู้ที่เรื่องยกระดับมาถึง", "เรื่องถูกยกระดับมาถึงคุณ", True),
    ("staff_handoff", "staff", "เรื่องส่งต่อมาถึงกอง", "ผู้รับแจ้งของกองที่รับ", "มีเรื่องส่งต่อมาถึงกอง", True),
    ("staff_child_work", "staff", "งานย่อยใหม่ถึงกอง", "ผู้รับแจ้งของกองที่รับ", "มีงานย่อยใหม่ถึงกอง", True),
    ("staff_dispute", "staff", "ผู้แจ้งไม่ยอมรับผล", "ผู้รับผิดชอบเรื่อง", "ผู้แจ้งยังไม่ยอมรับผล", True),
    ("staff_reopen", "staff", "ผู้แจ้งเปิดเรื่องใหม่", "ผู้รับผิดชอบเรื่อง", "ผู้แจ้งเปิดเรื่องใหม่", True),
    (
        "staff_admin_waiting",
        "staff",
        "เรื่องรอแอดมิน",
        "แอดมินระบบที่เปิดรับแจ้งไว้",
        "มีเรื่องรอแอดมิน กคว.69-000142",
        True,
    ),
    (
        "staff_registration",
        "staff",
        "คำขอลงทะเบียนจาก LINE",
        "แอดมินระบบที่เปิดรับแจ้งไว้",
        "คำขอลงทะเบียน #12: รอรับรอง",
        True,
    ),
    ("staff_order_new", "order", "มีงานสั่งถึงคุณ", "ผู้รับงาน", "มีงานสั่งถึงคุณ", True),
    ("staff_order_ack", "order", "งานที่สั่งเริ่มดำเนินการ", "ผู้สั่ง", "งานที่คุณสั่งเริ่มดำเนินการแล้ว", True),
    ("staff_order_done", "order", "งานที่สั่งเสร็จสิ้น", "ผู้สั่ง", "งานที่คุณสั่งเสร็จสิ้นแล้ว", True),
    ("staff_order_back", "order", "งานที่สั่งถูกส่งกลับ", "ผู้สั่ง", "งานที่คุณสั่งถูกส่งกลับ", True),
    ("staff_order_soon", "order", "งานสั่งใกล้ถึงกำหนด", "ผู้รับงาน", "งานสั่งใกล้ถึงกำหนด", True),
    ("staff_order_late", "order", "งานสั่งเกินกำหนด", "ผู้รับงาน", "งานสั่งเกินกำหนดแล้ว", True),
    ("koj_started", "koj", "เริ่มติดตามงาน", "ผู้ติดตามและผู้รับแจ้งงานติดตาม", "กอจ. · เริ่มติดตามงาน", True),
    (
        "koj_changed",
        "koj",
        "สถานะงานเปลี่ยน",
        "ผู้ติดตามและผู้รับแจ้งงานติดตาม",
        "กอจ. · สถานะงานเปลี่ยน",
        True,
    ),
    ("koj_completed", "koj", "ติดตามจบแล้ว", "ผู้ติดตามและผู้รับแจ้งงานติดตาม", "กอจ. · ติดตามจบแล้ว", True),
)
# ADR 0050 Q6: every kind to staff and every order kind goes to the แอดมินฝ่าย on duty; the
# people each kind went to before are a switch of their own that starts off, except the assigned
# ผู้รับผิดชอบ (Q19). Registration requests stay with the people chosen to approve them.
ROUTED = frozenset(
    kind
    for kind, group, *_ in PUSH_KINDS
    if group in ("staff", "order") and kind != "staff_registration"
)
DUTY = "duty"
# The switch key of each kind's former recipients; kinds that had one keep its key and value.
ORIGINAL = {"staff_assigned": "assignee", "staff_new_case": "division_admin"}
ORIGINAL_LABEL = {
    "staff_new_case": "แอดมินกองของกองเจ้าภาพ",
    "staff_assigned": "ผู้รับผิดชอบที่ถูกมอบหมาย",
}
# ADR 0054 Q2: the owner กอง's แอดมินกอง hears of each new case, by LINE and by email.
ORIGINAL_ON = {"staff_assigned", "staff_new_case"}
# ADR 0054 Q10: the kinds about a case also reach the owner กอง's แอดมินกอง and the case's
# แอดมินแผนก/กลุ่มงาน, when switched on; not where the former recipients are that แอดมินกอง.
HOST, UNIT = "host_admin", "unit_admin"
UNIT_KINDS = frozenset(
    kind
    for kind, group, *_ in PUSH_KINDS
    if kind in ROUTED and group == "staff" and kind != "staff_admin_waiting"
)
HOSTED = UNIT_KINDS - {"staff_new_case", "staff_assignment_returned"}


def original(kind):
    return ORIGINAL.get(kind, "people")


# Kinds whose people are picked here as well: (group, label, on by default). Each group has a
# LINE switch and an email switch (ADR 0054 Q9), both starting the same.
PUSH_RECIPIENTS = {
    kind: (
        (DUTY, "แอดมินฝ่ายที่อยู่เวร", True),
        (original(kind), ORIGINAL_LABEL.get(kind, who), kind in ORIGINAL_ON),
        *(((HOST, "แอดมินกองเจ้าภาพ", False),) if kind in HOSTED else ()),
        *(((UNIT, "แอดมินแผนกหรือกลุ่มงานของเรื่อง", False),) if kind in UNIT_KINDS else ()),
    )
    for kind, _group, _label, who, _sample, _on in PUSH_KINDS
    if kind in ROUTED
}
# ADR 0050 Q13, Q15: someone who did the step hears nothing of it, unless switched on.
PUSH_SELF = PUSH_PREFIX + "self"
# The ตัวเลือกระบบ about pushes, shown on the Push Message page instead.
PUSH_MOVED = (STAFF_LINE_BUDGET, STAFF_NOTIFY, ORDER_REMIND, CATCH_UP, CATCH_UP_APOLOGY)


def _switch(label, on):
    choices = (("on", "ส่ง", ""), ("off", "ไม่ส่ง", ""))
    return Option(label, choices if on else choices[::-1])


# ADR 0049 Q6: staff and order kinds may go by email too (when the server sends staff email);
# the ones that went by LINE only before start off.
EMAIL_GROUPS = ("staff", "order")
EMAIL_OFF = {"staff_admin_waiting", "staff_registration"}  # ADR 0054 Q2: new cases mail too

OPTIONS[PUSH_SELF] = _switch("Push Message: แจ้งเรื่องที่ตัวเองทำ", False)
for _kind, _group, _label, _who, _sample, _on in PUSH_KINDS:
    OPTIONS[PUSH_PREFIX + _kind] = _switch(f"Push Message: {_label}", _on)
    if _group in EMAIL_GROUPS:
        OPTIONS[f"{PUSH_PREFIX}{_kind}:email"] = _switch(
            f"Push Message: {_label} → อีเมล", _kind not in EMAIL_OFF
        )
    for _to, _to_label, _to_on in PUSH_RECIPIENTS.get(_kind, ()):
        OPTIONS[f"{PUSH_PREFIX}{_kind}:{_to}"] = _switch(
            f"Push Message: {_label} → {_to_label}", _to_on
        )
        OPTIONS[f"{PUSH_PREFIX}{_kind}:{_to}:email"] = _switch(
            f"Push Message: {_label} → {_to_label} → อีเมล", _to_on
        )


def push_on(kind, to=""):
    """ADR 0048: is this kind (or its recipient group `to`) sent? Kinds not listed are sent."""
    key = PUSH_PREFIX + kind + (f":{to}" if to else "")
    return key not in OPTIONS or get(key) == "on"


def push_rows(sent):
    """The Push Message page (ADR 0048, 0050 Q10): [(group, label, [kind dict], counts)];
    `sent` counts this month's LINE pushes by kind. Each kind carries its LINE switch, its email
    switch (staff and order kinds) and the switches of its people."""
    groups = []
    for group, group_label in PUSH_GROUPS:
        kinds = []
        for kind, g, label, who, sample, _ in PUSH_KINDS:
            if g != group:
                continue
            # ADR 0054 Q9: each group's LINE and email switch, side by side.
            people = [
                {
                    "label": to_label,
                    "line": {"key": f"{PUSH_PREFIX}{kind}:{to}", "on": push_on(kind, to)},
                    "email": {
                        "key": f"{PUSH_PREFIX}{kind}:{to}:email",
                        "on": push_on(kind, f"{to}:email"),
                    },
                }
                for to, to_label, _ in PUSH_RECIPIENTS.get(kind, ())
            ]
            for r in people:
                r["on"] = r["line"]["on"] or r["email"]["on"]
            reached = [r["label"] for r in people if r["on"]]
            if not people:
                reach = who
            elif not reached:
                reach = "ยังไม่มีผู้รับ"
            else:
                reach = reached[0] + (f" +{len(reached) - 1}" if len(reached) > 1 else "")
            email = f"{PUSH_PREFIX}{kind}:email"
            kinds.append(
                {
                    "kind": kind,
                    "label": label,
                    "who": who,
                    "reach": reach,
                    "sample": sample,
                    "sent": sent.get(kind, 0),
                    "line": {"key": PUSH_PREFIX + kind, "on": push_on(kind)},
                    "email": {"key": email, "on": push_on(kind, "email")}
                    if group in EMAIL_GROUPS
                    else None,
                    "recipients": people,
                }
            )
        counts = {
            "line": sum(k["line"]["on"] for k in kinds),
            "email": sum(bool(k["email"] and k["email"]["on"]) for k in kinds),
            "has_email": group in EMAIL_GROUPS,
            "sent": sum(k["sent"] for k in kinds),
        }
        groups.append((group, group_label, kinds, counts))
    return groups


def get(key):
    """The current value, or the option's safe default."""
    option = OPTIONS[key]
    value = SystemOption.objects.filter(key=key).values_list("value", flat=True).first()
    return value if value in {v for v, _, _ in option.choices} else option.default


def old_stock():
    """ADR 0032: the stock work of M07, M08 and M11 shows only when an admin turned it on."""
    return get(OLD_STOCK) == "on"


def otp_on():
    """ADR 0059: an employee in LINE proves who they are with the roster e-mail only when on."""
    return get(EMPLOYEE_OTP) == "on"


@transaction.atomic
def change(actor, key, value, reason):
    """Admin only (actor None = manage.py on the host), with a reason; audited."""
    from operations import identity  # local: identity reads options (separation of duties)

    if actor is not None:
        identity.require_admin(actor)
        if key in AI_RIGHT:
            identity.require_ai_editor(actor)
    if key not in OPTIONS:
        raise ValidationError("ไม่รู้จักตัวเลือกนี้")
    option = OPTIONS[key]
    if value not in {v for v, _, _ in option.choices}:
        raise ValidationError("เลือกจากค่าที่มีให้เท่านั้น")
    if key == MONITOR_JUDGE and value == "on":
        from operations import monitor_judge  # local: the judge reads AI versions

        problem = monitor_judge.blocker()
        if problem:
            raise ValidationError(problem)
    reason = identity._require_reason(reason)
    row, _ = SystemOption.objects.select_for_update().get_or_create(
        key=key, defaults={"value": option.default}
    )
    previous = row.value
    if previous == value and row.updated_by_id is not None:
        raise ValidationError("ค่านี้ใช้อยู่แล้ว")
    row.value, row.reason, row.updated_by = value, reason, actor
    row.save(update_fields=["value", "reason", "updated_by", "updated_at"])
    identity.audit(
        actor,
        "option.changed",
        row,
        reason,
        actor_label="" if actor else "manage.py",
        key=key,
        previous=previous,
        value=value,
    )
    return row


def rows(push=False):
    """(key, option, current value, stored row or None) for the settings page; `push`: the
    Push Message page's own instead (ADR 0048)."""
    stored = {row.key: row for row in SystemOption.objects.select_related("updated_by")}
    # Historical settings remain readable for migrations, but cannot override ADR 0019; LINE
    # registration asks no location since 31 ต.ค. 2569, whatever the stored value.
    # ADR 0041: one role per person leaves no duties to split; only the SIM test world does.
    retired = {REGISTRATION_CHECK, EMAIL_TYPED, REGISTRATION_LOCATION, DIVISION_WORK}
    return [
        (key, option, get(key), stored.get(key))
        for key, option in OPTIONS.items()
        if key not in retired and (key.startswith(PUSH_PREFIX) or key in PUSH_MOVED) == push
    ]
