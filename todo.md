# TODO — line_oa_meter

> พฤติกรรมที่ต้องได้และเหตุผล ดู `story.md` (ไฟล์นี้บอกแค่ "ทำอะไร ที่ไหน")
> ทำตามลำดับหัวข้อ · ติ๊ก `[x]` เมื่อเสร็จ · อัปเดต 2026-10-26

## กติกาโค้ดของโปรเจค (ทำตามทุกไฟล์)
- หัวไฟล์มีคอมเมนต์ 3 บรรทัดเรื่องลำดับ import + แบ่ง 3 หมวด (stdlib / ติดตั้งเพิ่ม / ของโปรเจค) เรียง A-Z, `import` ก่อน `from`, ชื่อเรียง `abc` → `Abc` → `ABC`
- คอมเมนต์ภาษาไทย สั้น อธิบาย "ทำไม"
- ข้อความที่บอทตอบเป็นภาษาไทย ลงท้าย "ครับ"
- action module ต้องมี `REQUIRES_AUTH`, `start(user, message)`, `handle(user, message, state) -> bool` (True = ตรงรูปแบบและตอบเองแล้ว, False = ส่งให้ AI), `prompt(state) -> tuple[str, list[str]]`
  one-shot (ติดตาม) มีแค่ `REQUIRES_AUTH` + `start` (ไม่ set state จึงไม่ถูกเรียก `handle`/`prompt`)
- ค่าคงที่ปุ่ม quick reply อยู่ใน `line_bot/actions/state.py`
- รันทุกอย่างผ่าน `docker compose` (Python 3.14)

## 0. ก่อนเริ่ม
- [ ] commit งานที่ค้าง (`line_bot/actions/`, `process.py`, `dbinit/init.py`, `user/*`, `docker-compose.yml`, `story.md`, `todo.md`)

## 1. งานเล็ก
- [ ] `process.py:process_messages` — ใน `except` ให้ลอง `reply(message.reply_token, "ระบบขัดข้อง กรุณาลองใหม่ภายหลังครับ")` ครอบด้วย try อีกชั้น (reply พังต้องไม่ทำให้ loop พัง)
- [ ] `actions/state.py` — `ACTION_TTL = 1800`
- [ ] แจ้ง action หมดอายุ:
  - `set_state` เขียน key ที่สอง `action_expired:{line_user_id}` (เช่น TTL 24 ชม.) คู่กับ state
  - `clear_state` ลบทั้งสอง key (ยกเลิก/สำเร็จ ไม่ต้องแจ้งหมดอายุ)
  - `process_message`: state เป็น None แต่ยังมี key หมดอายุ → ลบ key แล้ว
    - ข้อความตรงปุ่ม Intent เป๊ะ → ทำงานปกติ (user ตั้งใจเริ่มเรื่องใหม่)
    - ไม่ตรง → ตอบ "รายการเดิมหมดเวลาแล้ว กรุณาเริ่มใหม่ครับ" แล้วจบ
- [ ] `intents.py` — เอา `NONE_INTENT` ออกจาก `FAQ_ANSWERS` แล้วลด `is_faq` เหลือ `intent in FAQ_ANSWERS`
- [ ] ลบ `line_bot/forms.py` (ไม่มีใครใช้)
- [ ] `.gitignore` เพิ่ม `app.log` แล้ว `git rm --cached app.log`
- [ ] `shared/schemas/main/employee.py` — `user_password_hash: str = Field(exclude=True)`
- [ ] `docker-compose.yml` service `test` — ลบ `env_file: .env`

## 2. Schema: admins + comments
- [ ] `shared/db/main/models.py` `Admin`: ลบ `employee_index` + relationship `employee`; เพิ่ม `admin_username` (String, unique), `admin_password_hash` (String), `admin_name` (String)
- [ ] `Employee`: ลบ relationship `admin` (**เก็บ** `user_password_hash`)
- [ ] `MeterReportOrderComment`, `AssetRequisitionOrderComment`: `employee_index` → nullable, เพิ่ม `admin_index` (FK admins, nullable, index), `CheckConstraint("num_nonnulls(employee_index, admin_index) = 1", name="ck_<table>_one_author")`, relationship `admin` + back_populates ฝั่ง `Admin`
- [ ] อัปเดต pydantic ใน `shared/schemas/main/` ให้ตรง (`admin.py`, `*_comment.py`) — `admin_password_hash` ต้อง `Field(exclude=True)`
- [ ] `requirements.txt` ที่เกี่ยวข้อง เพิ่ม `argon2-cffi` (ยังไม่ต้องมีโค้ด login — แค่เตรียมไว้ให้ admin web)
- [ ] รัน `docker compose up dbinit` ให้ผ่าน

## 3. OTP ทางอีเมล (`story.md` ข้อ 5)
- [ ] `shared/config.py` + `.env.example` เพิ่ม `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` (Gmail: `smtp.gmail.com`, 587, STARTTLS, App Password)
- [ ] `shared/mail/` (หรือ `line_bot/mail.py`) — `async def send_email(to, subject, body) -> bool` ใช้ `aiosmtplib`, พัง = log + `False`
- [ ] `line_bot/user/otp.py` เขียนใหม่ (Redis):
  - key `otp:{line_user_id}` = `{"code_hash", "attempts", "sent_at"}` TTL 300 วินาที (เก็บ hash เช่น sha256 ไม่เก็บเลขตรง ๆ)
  - `async def send_otp(line_user_id, email) -> int | None` สุ่มด้วย `secrets` 6 หลัก, ส่งอีเมล, คืน None ถ้าส่งไม่ได้; ถ้ายังไม่ครบ 60 วินาทีจาก `sent_at` → คืนวินาทีที่ต้องรอ (ออกแบบ return ให้ชัด เช่น enum/dataclass)
  - `async def verify_otp(line_user_id, code) -> OtpResult` (`OK` / `WRONG` / `EXPIRED` / `TOO_MANY`) ผิดครบ 5 → ลบ key
  - ลบ `VERY_REAL_OTP`
- [ ] `line_bot/user/utils.py` — ฟังก์ชัน `mask_email("jedsada@mea.or.th") -> "jed***@mea.or.th"`
- [ ] rate limit ค้นรหัสพนักงาน: key `emp_lookup:{line_user_id}` `INCR` + `EXPIRE 3600` ตอนครั้งแรก เกิน 10 → ตอบ "ค้นหาบ่อยเกินไป กรุณาลองใหม่ภายหลังครับ" (ต้องเพิ่ม helper `incr` ใน `shared/cache/utils.py`)
- [ ] `actions/register.py`:
  - `RegisterData` เพิ่ม `employee_email`, `department_name` เก็บผลจาก `search_employee` ไว้ใน state
  - `WAIT_EMPLOYEE_ID`: rate limit → ค้น → ไม่มีอีเมล ตอบ "ไม่พบอีเมลของรหัสพนักงานนี้ กรุณาติดต่อผู้ดูแลระบบ" (ไม่เปลี่ยน step) → `send_otp` → step `WAIT_OTP`
  - `prompt` ของ `WAIT_OTP`: `ส่ง OTP ไปที่ {masked} แล้ว (หมดอายุใน 5 นาที) ขอ OTP ครับ` ปุ่ม [`ขอ OTP ใหม่`] [`ยกเลิกทำรายการ`]
  - `WAIT_OTP`: ข้อความ `ขอ OTP ใหม่` → ส่งใหม่/บอกให้รอ; ตัวเลข → `verify_otp` ตาม `OtpResult`
- [ ] `user/register.py:register_user` — รับข้อมูลพนักงานที่ค้นแล้วเป็น parameter แทนการเรียก `search_employee` ซ้ำ
- [ ] `actions/state.py` เพิ่ม `RESEND_OTP = "ขอ OTP ใหม่"`
- [ ] ทดสอบด้วย LINE จริง: ถูก, ผิด 5 ครั้ง, หมดอายุ, ขอใหม่ก่อน 60 วิ, รหัสไม่มีอีเมล, ค้นเกิน 10 ครั้ง

## 4. แจ้งปัญหามิเตอร์หลายรายการ (`story.md` ข้อ 6)
- [ ] `actions/state.py` เพิ่ม `ADD_ITEM = "เพิ่มรายการ"`, `SUMMARY = "สรุปรายการ"`, `EDIT_ITEM = "แก้ไขรายการ"`, `DELETE_ITEM = "ลบรายการ"`
- [ ] `actions/meter_report.py` ปรับ state data เป็น (ข้อเสนอ ปรับได้):
  ```python
  class MeterReportItem(BaseModel):      # = MeterReportData เดิม
      meter_id: str | None = None
      project_name: MeterProject | None = None
      issue_detail: str | None = None

  class MeterReportData(BaseModel):
      items: list[MeterReportItem] = []  # ยืนยันครบแล้ว
      current: MeterReportItem | None    # กำลังกรอก/แก้
      edit_index: int | None = None      # None = รายการใหม่, int = กำลังแก้ข้อนั้น
  ```
- [ ] step ใหม่: `WAIT_ADD_MORE` ([เพิ่มรายการ] [สรุปรายการ]), `WAIT_SUBMIT` (หน้าสรุปหลายข้อ), `WAIT_EDIT_ITEM` (เลือกข้อ), `WAIT_EDIT_FIELD` (เดิม), `WAIT_DELETE_ITEM` (เลือกข้อ) — ปุ่มเลือกข้อใช้ "1", "2", …
- [ ] กรอกครบรายการใหม่ → `WAIT_ADD_MORE`; แก้ข้อเดิมเสร็จ → กลับ `WAIT_SUBMIT`
- [ ] ครบ 10 รายการ → ไม่แสดง/ไม่รับ "เพิ่มรายการ"; ลบจนหมด → กลับไปกรอกรายการแรก; มีข้อเดียวตอนกดแก้/ลบ → ข้ามขั้นเลือกข้อได้
- [ ] `submit` — insert ทุกรายการใน session/transaction เดียว คืน list ของ `meter_report_order_index` → "แจ้งปัญหามิเตอร์สำเร็จ N รายการ\nเลขที่: 12, 13"
- [ ] เช็คข้อความหน้าสรุปไม่เกินข้อจำกัด LINE (text ≤ 5000 ตัวอักษร) ถ้า issue ยาวมากให้ตัดในหน้าสรุป

## 5. ติดตามการแจ้งปัญหามิเตอร์ (`story.md` ข้อ 7)
- [ ] `actions/track_meter_report.py` one-shot: `REQUIRES_AUTH = True`, `start` query 5 แถวล่าสุดของ `user.employee_index` (join status, เรียง `created_at desc`) แล้ว `reply`
- [ ] ลงทะเบียนใน `actions/__init__.py:ACTION_HANDLERS`
- [ ] ⚠️ ถามเจ้าของก่อน: กดติดตามระหว่างทำ action อื่น ควรตอบแบบ FAQ ไหม (`story.md` ข้อ 11) ถ้าใช่ ต้องแยก one-shot ออกจาก action ใน `process.py:handle_intent_during_action`

## 6. ทดสอบ
- [ ] `tests/line_bot.py` (ตอนนี้ว่าง) — test `process.py` โดย mock `reply`/`quick_reply`, Redis (เช่น `fakeredis`), `get_intent`, `get_user`, `search_employee`, `send_email`
- [ ] ไล่ทุก flow ใน `story.md` ข้อ 4–7 รวมถึงยกเลิก, เปลี่ยน action กลางทาง, FAQ กลางทาง, หมดอายุ

## 7. ข้อมูลจริงที่ต้องใส่
- [ ] ข้อความ FAQ จริงใน `intents.py`
- [ ] เช็คข้อความปุ่ม Rich Menu ตรงกับ `Intent` ทุกตัวอักษร

---

## 💤 ยังไม่ทำ (รอเจ้าของสั่ง — spec อยู่ใน `story.md` ข้อ 8)
- [ ] เบิกของ: ตาราง `divisions`, `assets.division_index`, `asset_requisition_orders.division_index` + `requisition_reason`, CSV seed ใน `dbinit/data/`, `actions/asset_requisition.py`
- [ ] ติดตามการเบิกของ: `actions/track_asset_requisition.py`
- [ ] เลิก `drop_all` / ใช้ Alembic (เจ้าของจะทำเอง)
