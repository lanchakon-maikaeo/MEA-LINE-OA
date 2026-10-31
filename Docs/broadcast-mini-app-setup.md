# ตั้งค่า Broadcast / LINE Mini App

สถานะ: โค้ดหน้าเว็บ, ยืนยันตัวตน, ตัวส่ง, สื่อและ Rich Menu มีแล้ว ยังต้องตั้งบัญชีจริงและทดสอบ integration ก่อนเปิดใช้งานจริง ไม่ใช่คำสั่งให้ส่งประกาศหรือ publish โดยอัตโนมัติ

## ค่าที่ต้องตั้ง

- `LINE_MINI_CHANNEL_ID`: channel ID ของ Mini App ที่ออก ID token (ต้องเป็น provider เดียวกับ OA เพื่อให้ LINE user ID ตรงกับบัญชีที่เชื่อมไว้)
- `LINE_LIFF_ID`: LIFF ID ของ Mini App นั้น ไม่ใช่ channel ID; Rich Menu ใช้ `https://miniapp.line.me/{LIFF_ID}`
- Endpoint URL ใน LINE Developers Console: `https://<staff-web-host>/broadcasts/`; เปิด scope `openid` ตามข้อกำหนด LIFF เพื่อให้ได้ ID token
- `STAFF_WEB_BASE_URL`: HTTPS public base URL ของเว็บ เช่น `https://ded.example.org`
- `BROADCAST_MEDIA_BASE_URL`: ไม่บังคับ หากว่างใช้ STAFF_WEB_BASE_URL; ถ้าตั้งให้ชี้ HTTPS host ที่ route `/broadcasts/media/` มาถึง Django ตัวเดียวกัน ไม่มีหน้า login หรือ redirect ที่ LINE ต้องตอบโต้
- Token/secret ของ Messaging API ใช้ค่าของ OA เดิม อย่าสลับจาก URL บัญชีตัวอย่างโดยยังไม่ได้ยืนยันการผูกบัญชี
- เปิด push/outbound และ quota ตามระบบเดิมเฉพาะเมื่อพร้อมเปิดใช้งานจริง การทดสอบ local/staging ต้องใช้ allowlist ตามเดิม

### บัญชีจริง: Mini App "DED Center" (provider MEA เดียวกับ OA @705ttrga)

| รุ่น | `LINE_MINI_CHANNEL_ID` | `LINE_LIFF_ID` | ใช้ที่ |
|---|---|---|---|
| Developing | 2012304618 | 2012304618-NA4nTywG | เครื่อง Mac ผ่าน HTTPS tunnel |
| Published | 2012304620 | 2012304620-sg7m4ScY | production |

- scope เปิด `openid, profile` แล้ว
- production: หลัง deploy รุ่นที่มี `/broadcasts/` ให้รัน `deploy/e5-move.sh mini-app` เพื่อใส่ค่ารุ่น Published แล้วเปลี่ยน Endpoint URL ของรุ่น Published ใน LINE Developers เป็น `https://168-107-70-170.sslip.io/broadcasts/` ตอนนี้ยังเป็นหน้าตัวอย่างของ LINE

ตาม [LINE ID token verification](https://developers.line.biz/en/reference/line-login/#verify-id-token) server ตรวจ token กับ channel ID โดยไม่เชื่อ roles/user ID จาก browser เซสชัน Mini App จำกัดเฉพาะ Broadcast และไม่เปลี่ยนเป็นเซสชันเว็บเจ้าหน้าที่ทั่วไป

## ติดตั้งและอัปเกรด

1. ติดตั้ง dependencies จาก lockfile (`uv sync --locked`) และ migrate รวม 0087–0090 โดยตรวจเลข migration ล่าสุดจริงก่อน deploy
2. Collect static ตามกระบวนการ deploy เดิม; web และ worker ต้องใช้ code/lockfile/schema รุ่นเดียวกัน
3. Web/worker แชร์ `PRIVATE_MEDIA_ROOT` เดิม แต่ Broadcast ใช้โฟลเดอร์ `broadcast/` แยกจาก attachments ส่วนตัว **ห้าม mount ทั้ง media root เป็น static public directory**
4. เปิด ClamAV และ worker; main worker เริ่ม thread สแกน Broadcast แยกเพื่อไม่กีดขวางแชต `run_worker --once` ยังตรวจ pending media แบบ synchronous ได้
5. Compose ทั้ง local/server กำหนด ClamAV `StreamMaxLength=210M`, `MaxFileSize=210M`, `MaxScanSize=420M`, `AlertExceedsMax=yes`; หลังอัปเกรด recreate container เพื่อรับค่าใหม่ ตรวจ effective config ไม่ใช่เพียงไฟล์ Compose
6. หากมี reverse proxy/load balancer อื่นให้รับ multipart upload ได้อย่างน้อย 201 MB และตั้ง request timeout ตามการเชื่อมต่อที่ใช้งานจริง ไม่เปลี่ยนข้อจำกัด case attachments เดิม
   - Caddy ของเซิร์ฟเวอร์ไม่จำกัดขนาด request แต่ gunicorn ใน Dockerfile ใช้ sync worker 2 ตัวกับ timeout ค่าเริ่มต้น 30 วินาที การอัปโหลดที่นานกว่านั้นถูกตัดกลางทาง และระหว่างอัปโหลด worker หนึ่งตัวรับงานอื่นไม่ได้ ต้องตัดสินค่าก่อนเปิดใช้อัปโหลดวิดีโอจริง เช่น เพิ่ม timeout หรือจำกัดขนาดวิดีโอให้เล็กลง
7. สร้าง/ตรวจ Rich Menu runtime assets ผ่าน `build_line_menu_images` ก่อน release; การ publish OA จริงต้องทำตามขั้นตอนเปิดใช้งานที่ได้รับอนุญาต

## สื่อที่รองรับ

- JPEG/PNG ไม่เกิน 10 MB, ไม่เกิน 40 ล้านพิกเซล; server สแกนก่อน decode/re-encode, ปรับ orientation และตัด metadata, สร้าง preview JPEG ภายใน 1 MB
- MP4 ไม่เกิน 200 MB; ตรวจ signature/โครงสร้าง container และมี video track ก่อนเผยแพร่ ต้องเลือกภาพปก JPEG/PNG ด้วย ไม่ได้แปลง codec วิดีโออัตโนมัติ
- Public URLs เป็น UUID และมีเฉพาะไฟล์ clean; pending/rejected ตอบ 404 แม้รู้ UUID; รองรับ GET/HEAD/single byte range สำหรับวิดีโอ
- Scanner ไม่พร้อม: คง pending และ retry ทุกนาที ไม่มีการยอมผ่านโดยไม่สแกน; lease งานสแกนหมดอายุหลัง 10 นาทีให้ worker กู้ได้
- หน้าเว็บอัปโหลด -> รอสถานะ -> preview -> เก็บ media ID ในร่าง; server resolve URL ใหม่จากไฟล์ clean ก่อนส่ง ป้องกัน URL จาก client ทับไฟล์ที่เลือก
- ขนาดและ HTTPS ตาม [LINE message objects](https://developers.line.biz/en/reference/messaging-api/nojs/#image-message) รูป/วิดีโอจาก URL ภายนอกต้องตรงข้อกำหนดเช่นกัน

## ตรวจรับก่อนเปิดจริง

- เข้าด้วยเว็บและ Mini App เป็น supervisor ฝ่าย/กองที่เชื่อม LINE แล้ว ตรวจการอ่านร่างเดียวกันและการถอนสิทธิ์
- ทดสอบ upload รูป/MP4/ภาพปก, scanner outage/rejection, public URL ผ่าน HTTPS โดยไม่ต้อง login, video playback บน LINE เครื่องจริง
- ส่งทดสอบถึงตัวเองก่อน ตรวจว่าร่างจริงไม่ถูกส่ง และ test/history/quota สอดคล้องกัน
- เมื่อได้รับอนุญาตให้ส่งจริง: Segment ที่ทราบสมาชิก, ผู้สร้างนอก Segment ต้องได้รับหนึ่งครั้ง, schedule/edit pause, web/Mini concurrent confirmation, และ all-friends ตามขอบเขตบัญชี/โควตา
- ตัวส่งรายงาน LINE accepted ไม่ได้แปลว่าเปิดอ่านแล้ว ห้ามกดสร้างส่งใหม่เพื่อแก้ UNKNOWN โดยไม่ตรวจผลเก่า
- ห้ามรัน worker จริงเพียงเพื่อทดสอบ scan หากยังไม่ต้องการให้ Outbox หรือ rich menu publisher ทำงาน ใช้ service scan โดยตรงในสภาพแวดล้อมทดสอบ
