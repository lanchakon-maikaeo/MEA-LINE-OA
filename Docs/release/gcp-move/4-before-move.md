# แผน 4: เตรียมก่อนย้าย (ระยะ 2)

**ใครทำ:** ทั้งคู่ **เวลา:** ~45 นาที ทำในวันที่จะย้าย **รอ:** G2 และแผน 2 ระยะ 2 merge แล้ว

ทุกข้อในหน้านี้ต้องผ่านตาม [กติกาข้อมูลห้ามหาย](0-data-safety.md) ถ้าข้อใดไม่ผ่าน ให้เลื่อนการย้าย E5 ยังให้บริการอยู่

## ขั้นตอน

1. ☐ **เจ้าของ:** เปิด OCI session ที่ใช้เปิด tunnel ไป E5

   ```bash
   oci session authenticate --region ap-singapore-1 --tenancy-name MEAFlow --profile-name faoj
   ```

2. ☐ **สำเนา key (escrow):** `gcp-move.sh escrow-check` แสดง hash 16 ตัวของ `AI_SECRET_KEY`, `PII_SECRET_KEY`, `DJANGO_SECRET_KEY`, `PGBACKREST_REPO1_CIPHER_PASS` (ของ E5) และ `RESTIC_PASSWORD` บน E5 เจ้าของคัดลอกค่าจากตัวจัดการรหัสผ่านลง clipboard แล้วรัน `pbpaste | shasum -a 256 | cut -c1-16` ทีละตัว ทุกตัวต้องตรง ตัวที่ยังไม่มีให้บันทึกลงตัวจัดการรหัสผ่านก่อน
3. ☐ **พื้นที่ Object Storage:** ตรวจว่ารวมแล้วยังไม่เกิน 12 GB ถ้าเกิน ให้ลด `PGBACKREST_REPO1_RETENTION_FULL` ของ GCP เหลือ 1 ชั่วคราว ส่วน stanza เดิมห้ามลด

   ```bash
   for b in faoj-backup faoj-files; do oci os bucket get --bucket-name $b --fields approximateSize \
     --query 'data."approximate-size"'; done
   ```

4. ☐ **backup ล่าสุดของ E5:** `faoj backup full` และ `faoj backup files` บน E5 จากนั้น `faoj backup status` ต้องแสดงอายุไม่เกิน 10 นาที
5. ☐ **image ล่าสุด:** `gcp-move.sh source` จาก `main` ล่าสุด (commit ที่ Release ผ่าน) → `build` → จด digest ไว้
6. ☐ **dry-run:** `gcp-move.sh dry-run` ใช้ `--deep` และเทียบ sha256 ของไฟล์ทั้งโฟลเดอร์ ทั้งหมดต้องตรง (G3) ฐานข้อมูลหลักของ GCP ไม่ถูกแตะ แล้วจดเวลาที่ใช้ไว้ เพื่อประมาณ downtime ของรอบจริง
7. ☐ **เจ้าของ:** LINE Developers → Messaging API → **Webhook redelivery ต้องเปิด** ถ้าไม่เปิดห้ามย้าย
8. ☐ **เจ้าของ:** เตรียมบัญชี LINE ทดสอบ 1 บัญชี ไว้ส่งข้อความระหว่างที่ระบบปิด
9. ☐ **เจ้าของ:** เตรียมข้อความแจ้งผู้ใช้และข้อความแจ้งเจ้าหน้าที่พร้อม URL ใหม่
10. ☐ เลือกเวลาหลัง 18:00 และ**ต้องก่อน 7 พ.ย.**

## G3

ข้อ 2, 4, 5, 6 และ 7 ผ่าน แล้ว**เจ้าของพิมพ์ในแชตว่า "เริ่มย้าย"**
