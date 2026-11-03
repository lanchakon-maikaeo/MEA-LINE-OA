---
status: partly superseded by ADR 0043
---

# production ชั่วคราวบน E5.Flex ด้วยเครดิตทดลอง โดยมี Mac เป็นเครื่องสำรอง

> production ย้ายไป GCP แล้วตั้งแต่ 2 พ.ย. 2569 ตาม [ADR 0043](0043-production-on-gcp-free-trial.md) ส่วนเครื่อง prod และทางเข้าในเอกสารนี้จึงเลิกใช้ ส่วน Mac standby, การจัดการตอนระบบล่ม และการแยกรุ่น minor กับ major ยังใช้ต่อ

แก้ไขบางส่วนของ [ADR 0006](0006-oci-always-free-singapore-without-payg.md) ผู้ใช้ตกลงวันที่ 14 ตุลาคม 2569 (grill Q1–Q22) หลังสร้างเครื่อง A1 ไม่ได้มาสองวัน (K16) tenancy `MEAFlow` มีเครดิตทดลอง SGD 400 ที่หมดอายุ 10 พฤศจิกายน 2569 บัญชียังเป็น `FREE_TIER` และ**ห้ามอัปเกรด** ถ้าเครดิตหมดหรือถึงวันหมดอายุ Oracle จะหยุดเครื่องที่ไม่ฟรี ไม่ตัดบัตร

- **prod:** `faoj-prod-e5` เป็น VM.Standard.E5.Flex 2 OCPU / 16 GB, boot 50 + data 100 GB ราว US$70 จนถึงวันหมดอายุ ตรวจ capacity วันที่ 14 ต.ค. แล้ว E5 ว่าง ส่วน E4 และ A1 เต็ม Terraform รับ shape ต่อเครื่อง โดยค่า default ยังเป็น A1 quota ของ E4/E5 เปิดเท่าที่เครื่องขอและเฉพาะก่อน `credit_until` เลยวันนั้นแล้ว plan จะปฏิเสธ มี budget แจ้งเตือนเมื่อใช้เครดิตถึง 50% และ 80%
- **Mac:** เป็น staging และ warm standby (Postgres โหมด recovery ดึง WAL จาก Object Storage ข้อมูลตามหลังไม่เกิน 5 นาที ไฟล์แนบ sync ทุกชั่วโมง) แยก stack จาก staging
- **ทางเข้า:** webhook LINE ใช้ ngrok static domain ที่ย้ายตามเครื่อง prod URL จึงไม่เปลี่ยน แพ็กเกจฟรีจำกัด 1 GB และ 20,000 request ต่อเดือน เว็บเจ้าหน้าที่จึงใช้ `sslip.io` ของ IP เครื่อง แล้วค่อยย้ายไป subdomain ของ กฟน. ระบบแจ้งเตือนเมื่อใช้ ngrok เกิน 70%
- **ข้อมูล:** งานที่วางแผนไว้ต้องไม่เสียข้อมูล (RPO 0): เปิด maintenance → backup รอบสุดท้าย → restore → `faoj move-verify` (จำนวนแถว, เลขเรื่อง W/R/P, ลำดับ audit, checksum ไฟล์, migrate, smoke) → เปิด ถ้าไม่ผ่านกลับไปเครื่องเดิม เครื่องพังกะทันหัน RPO ≤ 5 นาที RTO ≤ 2 ชม. backup อยู่สามที่ คือ OCI Object Storage, Mac และสำเนาเข้ารหัสรายสัปดาห์ใน OneDrive ของ กฟน. เครื่องเก่าเก็บไว้แบบอ่านอย่างเดียว 7 วัน
- **CI/CD:** CI ตรวจ migration ถ้าไม่ปลอดภัยจะนับเป็น major minor ขึ้น staging บน Mac แล้วรอ 1 ชม. ก่อนขึ้น prod เองในช่วง 18:00–07:00 major เจ้าของบัญชีกดเอง image อยู่ใน OCIR เครื่องนอก OCI ใช้ token ที่ pull ได้อย่างเดียว
- **ผู้ใช้ตอนระบบล่ม:** ตอบ 503 ให้ LINE ส่งซ้ำ พอกลับมาระบบจะทำต่อเอง ถ้า reply token หมดอายุ จะ push เฉพาะคนที่รอคำตอบ คนละ 1 ข้อความ หยุดเมื่อโควตาเหลือ 20% เว็บเก็บร่างฟอร์มในเบราว์เซอร์ 24 ชม. และกันส่งซ้ำด้วย idempotency key ล่มแบบวางแผนไว้แจ้งผ่าน rich menu และแบนเนอร์ 24 ชม. ล่วงหน้า (ย้ายครั้งแรกวันที่ 14 ต.ค. ผู้ใช้สั่งให้ย้ายทันที) Mac กับ UptimeRobot ตรวจทุกนาที การสลับเครื่องให้คนกด
- **ใครรัน:** เจ้าของบัญชีกด Apply และสลับ ngrok หรือ webhook ขั้นตอนบนเครื่อง ผู้ช่วยพัฒนารันได้เมื่อได้รับอนุญาตเป็นครั้งๆ หรือเจ้าของจะรันเองทั้งหมดก็ได้

## Considered Options

- **อัปเกรด PAYG เพื่อเก็บ E5 ไว้:** ไม่เลือก ขัด ADR 0006 และคำสั่ง "ห้ามตัดบัตร"
- **E5 สองเครื่อง (prod และ staging):** ไม่เลือก webhook ชี้ได้ทีละที่ Mac ทำหน้าที่ staging และเครื่องสำรองได้พร้อมกัน
- **ngrok ทั้งหมดรวมเว็บ:** ไม่เลือก เพดาน 1 GB ต่อเดือนอาจหมดกลางเดือนแล้วทำให้บอทล่มด้วย
- **AWS เป็นที่ไปหลังเครดิตหมด:** ไม่เลือกเป็นอันดับแรก พอเครดิตหมด Free Plan จะปิดบัญชี GCP แค่หยุดเครื่อง

## Consequences

- ต้องย้ายออกจาก E5 ภายใน **7 พ.ย. 2569** ลำดับคือ A1 (ถ้าได้ capacity) → GCP ($300 / 90 วัน เริ่มเตรียม 1 พ.ย. ถ้ายังไม่ได้ A1) วิธี restore เหมือนกันทุกที่ เพราะ backup อยู่ใน OCI Object Storage ที่เป็น Always Free
- เครื่อง A1 ตั้งเป็น `instance: false` ไว้ (ไม่สร้าง VM แต่เก็บ volume `faoj-prod-data` ไว้) และหยุด A1 retry ไว้ จะเปิดอีกครั้งเมื่อผู้ใช้สั่งตอนวางแผนย้ายออกจาก E5 เท่านั้น ผู้ใช้กำหนดให้ apply แต่ละครั้งมีแค่สิ่งที่สั่ง
- ตอนลบ E5 ต้องแก้ `prevent_destroy` ใน commit ที่ตรวจแล้ว
- ข้อมูลจริงอยู่ที่ Singapore และผ่าน ngrok ยังต้องให้เจ้าของข้อมูลรับรองตาม G02

## สิ่งที่ทำแล้ว (14 ต.ค. 2569)

- **ย้ายแล้ว 21:54:** `deploy/e5-move.sh` (tunnel → configure → secrets → deploy → cutover) `move_manifest` ตรงกันทุกอย่าง และ full backup ครั้งแรกขึ้นแล้ว LINE → ngrok (container บน E5) → `caddy:8081` เฉพาะ `/webhooks/line` ส่วนเจ้าหน้าที่ใช้ `https://168-107-70-170.sslip.io/` (Reserved IP)
- **Q12:** `notifications.plan_catch_up` และตัวเลือก `catch_up_push` กับ `catch_up_apology`
- **Q21:** `FormOnceMiddleware`, `FormSubmission` (0033) และร่างฟอร์มใน `app.js`
- **Q13:** `deploy/healthcheck.sh` (`make health-schedule`)
- **Q9 และ Q10:** `deploy/standby.sh` (`make standby-schedule`) ใช้ key ของ backup-reader ซึ่งเขียน `~/.config/faoj/backup-pull.env` ให้ `make backup-pull-schedule` ด้วย standby ทำงานตั้งแต่ 22:47 ข้อมูลตามหลัง production **5–10 นาที** ไม่ใช่ 5 นาทีตาม Q4 เพราะถาม WAL ทุก 5 นาทีเพื่อให้อยู่ในโควตา Object Storage 50,000 request ต่อเดือนของ Always Free ที่ใช้ร่วมกับ backup ของ E5 และคัดลอกไฟล์วันละครั้ง งานเบื้องหลังรันจาก `~/.config/faoj/bin` เพราะ macOS ไม่ให้ launchd อ่านไฟล์ใน OneDrive
- **Q6 และ Q20:** `release.json` มี `unsafe_migrations`, `postgres_major` และ `n8n_major` ส่วน `faoj auto-promote` ปิดไว้ก่อน มีช่วงเวลา 18-07, soak 60 นาที และ staging gate `metric`
- **บั๊กของ `faoj` ที่พบบน Linux จริง:** bundle อ่านไม่ได้ (`umask 077`), deploy ครั้งแรกที่ล้มทำให้รอบต่อไปติด และ `bash -s` ถูก docker กิน stdin

## เจ้าของต้องทำเอง

- **user `faoj-backup-reader`:** เพิ่มเข้า group `faoj-backup-readers` และสร้าง Customer Secret Key แล้วรัน `deploy/standby.sh setup → build → start` และ `make standby-schedule backup-pull-schedule`
- **UptimeRobot (ฟรี):** สร้าง keyword monitor ที่ `https://168-107-70-170.sslip.io/health/ready/` หาคำว่า `ok` ทุก 5 นาที ส่งแจ้งเตือนทางอีเมล และตั้ง `TEAMS_WEBHOOK_URL` ใน `~/.config/faoj/health.env` ถ้าต้องการ
- **CI → OCIR:** ตั้ง variables `OCIR_REGISTRY`, `OCIR_NAMESPACE` และ secrets `OCIR_USERNAME`, `OCIR_TOKEN` ของ user ใน group `faoj-ci` แล้วค่อยเปิด `faoj auto-promote on "เหตุผล"` จนกว่าจะมี staging ที่ส่ง metric ให้ OCI ต้องตั้ง `AUTO_PROMOTE_STAGING_GATE=none` เอง
