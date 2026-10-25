# TODO — line_oa_meter

> flow ทั้งหมดดูใน `story.md`

## 1. ให้รันได้ก่อน
- [ ] `.env` ใส่ค่าที่ยังว่าง: `AI_PROVIDER`, `AI_MODEL`, API key ของ AI ที่ใช้, `LINE_CHANNEL_ACCESS_TOKEN`, `LINE_CHANNEL_SECRET`, `LINE_REPLY_URL=https://api.line.me/v2/bot/message/reply`, `MEA_SMARTOFFICE_XAPI_KEY`
- [ ] `docker-compose.yml`: comment service `api` ไว้ก่อน (โฟลเดอร์ `api/` ยังว่าง)
- [ ] `process.py`: แก้ `get_intents` → `get_intent` (ไม่งั้น import พัง)
- [ ] เช็คปุ่ม Rich Menu ส่งข้อความตรงกับ `intents.py` ทุกตัว

## 2. `intents.py`
- [ ] เพิ่ม `CANCEL = "ยกเลิก"`
- [ ] ใส่เบอร์โทร / อีเมลจริงใน `FAQ_ANSWERS`

## 3. สร้าง `line_bot/actions/`
### 3.1 `actions/state.py` — เก็บว่า user ทำ action อะไรอยู่ (Redis)
- [ ] `ACTION_TTL = 600` (วินาที)
- [ ] `get_state_key(line_user_id)` → `"action:{line_user_id}"`
- [ ] `get_state(line_user_id) -> dict | None`
- [ ] `set_state(line_user_id, state)`
- [ ] `clear_state(line_user_id)`
- [ ] ค่าคงที่ปุ่ม: `CONFIRM = "ถูกต้อง"`, `EDIT = "แก้ไข"`, `YES = "ใช่"`, `NO = "ไม่"`

หน้าตา state:
```python
{"intent": "ขอยืนยันตัวตน", "step": "wait_employee_id", "draft": None, "pending_intent": None, "data": {}}
```
- `draft` = คำตอบที่รอ user กดถูกต้อง/แก้ไข
- `pending_intent` = action ใหม่ที่รอ user ตอบว่าจะยกเลิกของเดิมไหม

### 3.2 ทุกไฟล์ action ต้องมี 4 อย่างนี้
```python
REQUIRES_AUTH: bool
async def start(user, message) -> None          # เริ่ม action + set state + ถามคำถามแรก
async def handle(user, message, state) -> bool  # True = ตรงรูปแบบ, False = ไม่ตรง (ส่งให้ AI)
def prompt(state) -> str                        # คำถามของขั้นปัจจุบัน (ใช้ถามซ้ำ)
```

### 3.3 `actions/register.py` (ทำอันนี้ก่อน)
- [ ] `REQUIRES_AUTH = False`
- [ ] ย้าย `VERY_REAL_OTP` จาก `user/utils.py` มาไว้ใน `user/otp.py`
- [ ] `class RegisterStep(StrEnum)`: `WAIT_EMPLOYEE_ID`, `WAIT_OTP`
- [ ] `start`: ถ้ายืนยันแล้ว → "คุณยืนยันตัวตนแล้ว" / ยังไม่ → set state + "ขอรหัสพนักงานหน่อยครับ"
- [ ] `handle` ขั้น `WAIT_EMPLOYEE_ID`
  - ไม่ใช่ตัวเลขล้วน → `return False`
  - `search_employee` ไม่เจอ → "ไม่พบรหัสพนักงาน โปรดกรอกอีกครั้ง" → `return True`
  - เจอ → เก็บ `employee_id` ใน `data` → step = `WAIT_OTP` → "ขอ OTP ครับ" → `return True`
- [ ] `handle` ขั้น `WAIT_OTP`
  - ไม่ใช่ตัวเลขล้วน → `return False`
  - `verify_otp` ผิด → "OTP ไม่ถูกต้อง โปรดกรอกอีกครั้ง" → `return True`
  - ถูก → `register_user(employee_id, line_user_id)` → เช็ค None → `clear_state` → "ยืนยันตัวตนสำเร็จ" → `return True`

### 3.4 action อื่น (โครงเปล่าไปก่อน)
- [ ] `meter_report.py`, `asset_requisition.py`, `track_meter_report.py`, `track_asset_requisition.py`
- [ ] `REQUIRES_AUTH = True`
- [ ] `start` แค่ reply "กำลังพัฒนา" ไม่ต้อง set state

### 3.5 `actions/__init__.py`
- [ ] `ACTION_HANDLERS = {Intent.REGISTER: register, Intent.METER_REPORT: meter_report, ...}` (เก็บเป็น module)

## 4. เขียน `process.py` ใหม่
- [ ] ลบ `reply_messages`, return type → `None`
- [ ] ใช้ `continue` ไม่ใช้ `return`
- [ ] reply ได้ 1 ครั้งต่อ message → ถ้าตอบ FAQ แล้วต้องถามต่อ ให้รวมเป็นข้อความเดียว
- [ ] เขียนฟังก์ชันช่วย `find_intent(text) -> Intent`: ตรงเป๊ะ → `Intent(text)` / ไม่ตรง → `get_intent(text)` (กัน `NONE_INTENT` ที่ user พิมพ์มาตรง ๆ)

ลำดับใน loop:
```
user = get_user()
state = get_state()

ถ้ามี state:
  1. "ยกเลิก"                 → clear_state → "ยกเลิกแล้ว"
  2. มี pending_intent         → "ใช่" → clear_state → start(action ใหม่)
                                 "ไม่" → ลบ pending_intent → prompt(state)
  3. มี draft                  → "ถูกต้อง" / "แก้ไข" / ข้อความใหม่ (ให้ action จัดการใน handle)
  4. ตรง FAQ เป๊ะ               → FAQ + prompt(state)
  5. handle() == True          → จบ
  6. handle() == False         → find_intent
       FAQ                     → FAQ + prompt(state)
       action เดิม / NONE       → "ไม่เข้าใจครับ" + prompt(state)
       action อื่น              → set pending_intent → quick_reply "ต้องการยกเลิก X ไหม" [ใช่] [ไม่]

ไม่มี state:
  intent = find_intent
  FAQ                          → ตอบ
  action                       → REQUIRES_AUTH แต่ user เป็น None → "กรุณายืนยันตัวตนก่อน โดยกดปุ่ม 'ขอยืนยันตัวตน' ที่เมนูด้านล่าง"
                                 ผ่าน → start()
  NONE                         → "ขออภัย ไม่เข้าใจคำถาม กรุณาเลือกจากเมนูด้านล่าง"
```

## 5. ทดสอบ register
- [ ] รัน compose → ส่งข้อความจาก LINE จริง
- [ ] ไล่ Story 1–8, 10 ใน `story.md`

## 6. หลังคุยกับเพื่อน (ดู `story.md` ข้อ 6)
- [ ] ออกแบบขั้นตอน `meter_report` → เขียน + ใส่ recheck (`draft`) ในขั้นที่รับข้อความอิสระ
- [ ] ออกแบบขั้นตอน `asset_requisition`
- [ ] ทำ `track_*` (ดึงเรื่องจาก DB มาแสดง)

---

## เก็บทีหลัง
- [ ] ลบ `forms.py` (ใช้ `actions/state.py` แทน)
- [ ] `EmployeeSchema`: ใช้ `user_password_hash: str = Field(exclude=True)` (ตอนนี้หลุดไปกับ `model_dump`)
- [ ] `search_employee` ถูกเรียก 2 รอบ (ใน action + ใน `register_user`) → refactor
- [ ] OTP จริง: สุ่ม + เก็บ Redis + ส่ง SMS (แก้แค่ `user/otp.py`)
- [ ] `extract_user_data.py` ยังว่าง → ลบทิ้ง หรือใช้ทีหลัง
- [ ] `query.sql` ซ้ำกับ `models.py` → ใช้ `models.py` เป็นหลัก
- [ ] `docker-compose.yml` service `test`: ลบ `env_file: .env`
- [ ] `dbinit` ใช้ `drop_all` ทุกครั้ง → ข้อมูลหายทุกรอบที่ `compose up` (เอาออกก่อนขึ้นจริง)
- [ ] `.gitignore` เพิ่ม `app.log`
- [ ] commit `dbinit/`, `tests/`, `story.md`, `todo.md`
