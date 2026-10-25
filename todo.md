# TODO — line_oa_meter

## ✅ ทำแล้ว (2026-10-24)
- `Admin` เปลี่ยนเป็น `admin_index` (PK) + `employee_index` (FK, unique), orders ใช้ `admin_index` แทน `admin_employee_id` (แก้ models.py, schemas, query.sql แล้ว)
- `register_user`: upsert ไม่เขียนทับ `department_index` เป็น None (coalesce)
- `LineUserEmployeeSchema` → `UserInfo`, `get_user` / `cache_search` / `db_search` / `register_user` return `UserInfo | None`
- สร้าง `line_bot/line/reply.py` (`reply`, `quick_reply`)
- จัดไฟล์: `events.py` `reply.py` `signature.py` → `line_bot/line/`, `helpers.py` → `forms.py` (`ITEM_WITHDRAW` → `ASSET_REQUISITION`)

---

## 🔴 ต้องทำก่อน (ไม่งั้นรันไม่ได้)
- [ ] สร้าง `dbinit/Dockerfile` (+ script `create_all`) — compose build ไม่ผ่าน และ `line_bot` รอ `dbinit` อยู่
- [ ] `api/`, `tests/` ยังว่าง → สร้าง Dockerfile หรือ comment service ออกจาก `docker-compose.yml` ไปก่อน
- [ ] ลบตาราง `admins`, `meter_report_orders`, `asset_requisition_orders` (หรือลบ `dbdata/postgres/main`) เพราะ `create_all` ไม่แก้ตารางเดิม
- [ ] `.env` ใส่ให้ครบ: `LINE_REPLY_URL=https://api.line.me/v2/bot/message/reply`, `LINE_CHANNEL_ACCESS_TOKEN`, `LINE_CHANNEL_SECRET`, `AI_PROVIDER`, `AI_MODEL`, `MEA_SMARTOFFICE_XAPI_KEY`

## 🟡 ลำดับงานพรุ่งนี้

### 1. Register (OTP ปลอม)
- [ ] `user/utils.py` เพิ่ม
  - `VERY_REAL_OTP = "1234"  # TODO: ของปลอม`
  - `REGISTER_TTL = 300`
  - `class RegisterStep(StrEnum)`: `WAIT_EMPLOYEE_ID`, `WAIT_OTP`
  - `get_register_key(line_user_id) -> f"register:{line_user_id}"`
- [ ] `user/otp.py`: `verify_otp(user_otp: str) -> bool` (เทียบ `.strip()` กับ `VERY_REAL_OTP`)

### 2. `line_bot/intents.py` (ห้าม import อะไรในโปรเจค กัน circular import)
- [ ] `class Intent(StrEnum)` — ค่า = ข้อความบนปุ่ม/คำที่ match ตรงเป๊ะ
  - FAQ: `CONTACT_NUMBER = "ขอเบอร์โทรติดต่อ"`
  - action: `ASSET_REQUISITION = "เบิกของ"`, `CHECK_STATUS = "เช็คสถานะ"`, `METER_REPORT = "แจ้งปัญหามิเตอร์"`
- [ ] `FAQ_ANSWERS = {Intent.CONTACT_NUMBER: "เบอร์โทรติดต่อ: xxx"}`

### 3. `ai_tasks/classify_intent.py`
- [ ] เปลี่ยน `get_intents` (list) → `get_intent(message) -> Intent | None`
- [ ] `SCHEMA` ใช้ enum จาก `Intent`: `{"type": "string", "enum": [i.value for i in Intent] + ["none"]}` (ห่อเป็น object ตามที่ provider ต้องการ)
- [ ] เขียน `SYSTEM_PROMPT`

### 4. `line_bot/actions.py`
- [ ] `handle_asset_requisition(user, message)`, `handle_check_status(...)`, `handle_meter_report(...)` (โครงเปล่า reply ข้อความไปก่อนได้)
- [ ] ท้ายไฟล์: `ACTION_HANDLERS = {Intent.X: handle_x, ...}`

### 5. `process.py` (เขียน flow ให้เห็นในไฟล์นี้ ไม่ซ่อนใน handle_register)
ลำดับ:
```
1. text ตรง FAQ เป๊ะ                → reply FAQ (ใครก็ได้)
2. user ยังไม่ register
   ├─ ไม่มี register state
   │    → AI จับ intent: FAQ → ตอบ / อื่น ๆ → set state WAIT_EMPLOYEE_ID + "โปรดกรอกรหัสพนักงาน"
   ├─ WAIT_EMPLOYEE_ID → search_employee(text)
   │    ไม่เจอ → "ไม่พบรหัสพนักงาน โปรดกรอกอีกครั้ง" (state เดิม)
   │    เจอ   → set state WAIT_OTP + employee_id + "โปรดกรอก OTP"
   └─ WAIT_OTP → verify_otp
        ผิด → "OTP ไม่ถูกต้อง" (วนถาม OTP)
        ถูก → register_user(employee_id, line_user_id) → เช็ค None → ลบ state → "ลงทะเบียนสำเร็จ"
3. มี form ค้าง (forms.get_forms)     → ส่งต่อให้ handler ของ form นั้น
4. text ตรง action เป๊ะ             → ACTION_HANDLERS[Intent(text)]
5. AI get_intent
   ├─ FAQ    → reply
   ├─ action → handler
   └─ None   → quick_reply เมนู list(Intent)
```
- [ ] ใช้ `continue` ไม่ใช่ `return` (ข้อความอื่นใน batch ยังต้องทำต่อ)
- [ ] `reply_token` ใช้ได้ครั้งเดียว → reply 1 ครั้งต่อ message
- [ ] ลบ `reply_messages` ที่ไม่ได้ใช้, เปลี่ยน return type เป็น `None`
- [ ] import path ใหม่: `from line_bot.line.reply import quick_reply, reply`, `from line_bot.forms import ...`

### 6. Forms (หลัง process.py รันได้)
- [ ] state ของ form ใน Redis ผ่าน `form_key` / `get_forms` ใน `forms.py`
- [ ] ทำ quick reply ในแต่ละขั้นของ form

---

## ⚪ เก็บทีหลัง
- [ ] `EmployeeSchema`: เปิด `user_password_hash: str = Field(exclude=True)` (ตอนนี้หลุดออกไปกับ model_dump) + ลบ import `Field` ถ้าไม่ใช้
- [ ] `search_employee` ถูกเรียก 2 รอบ (ตอนเช็ครหัส + ใน `register_user`) → refactor
- [ ] `extract_user_data.py`: prompt ยังว่าง (`get_employee_id`, `get_otp`) — ตอนนี้ใช้ `.strip()` แทนไปก่อน
- [ ] OTP จริง: สุ่ม + เก็บใน Redis + ส่ง SMS (แก้แค่ `user/otp.py`)
- [ ] `query.sql` ซ้ำกับ `models.py` → เลือก models.py เป็นต้นฉบับ
- [ ] `docker-compose.yml` service `test`: ลบ `env_file: .env`
- [ ] `shared/db/ai/` ยังว่าง (ai_db ยังไม่ได้ใช้)
- [ ] `git init` ก่อน refactor
- [ ] รวม `1_todo.txt` เข้ามาไฟล์นี้แล้วลบทิ้ง
