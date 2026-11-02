# แผน 5: ย้ายทีเดียว — key + webhook + ข้อมูล (ระยะ 2)

**ใครทำ:** เจ้าของสั่ง ผู้ช่วยรัน `gcp-move.sh move` **เวลา:** 40–50 นาที (ใช้เวลาจริงตาม dry-run) ระบบหยุดทั้งช่วงนี้ **รอ:** G3

ทุกขั้นทำตาม [กติกาข้อมูลห้ามหาย](0-data-safety.md) สคริปต์จะหยุดทันทีที่มีการตรวจข้อใดไม่ผ่าน

## ลำดับใน `gcp-move.sh move`

### หยุดการเขียน (freeze)

1. เจ้าของส่งข้อความแจ้งผู้ใช้ แล้วให้บัญชีทดสอบส่งข้อความ LINE 1 ข้อความหลังขั้น 3
2. **GCP:** `faoj maintenance on` แล้วหยุด web, worker และ n8n
3. **E5:** `faoj maintenance on` แล้วหยุด web, worker (`stop -t 60` ให้งานที่ค้างจบก่อน), n8n และ **ngrok**
4. **E5:** ตั้ง `ALTER DATABASE faoj SET default_transaction_read_only = on` แล้วตัด session ที่ค้างอยู่ จากนั้นตรวจ `pg_stat_activity` ว่าไม่เหลือ client อื่น ถ้ายังมี ให้หยุด
5. **E5:** `faoj backup incr` และ `faoj backup files` เพื่อให้ได้สำเนา ณ จุด freeze ใน OCI

### ย้ายข้อมูล

6. **E5:** `move_manifest --deep` → `pg_dump -Fc faoj` → `move_manifest --deep` อีกรอบ สองรอบต้องตรงกัน dump ของ DB `n8n` ไว้เป็นหลักฐาน และทำรายการ sha256 ของทุกไฟล์ใน `/srv/faoj/data/media`
7. คัดลอก dump, manifest และ media ไป GCP ผ่าน pipe (`tar` เก็บ owner และ mode) แล้วเทียบ sha256 ของไฟล์ dump และรายการไฟล์ทั้งโฟลเดอร์ให้ตรงกับต้นทาง
8. **Key:** `gcp-move.sh keys` merge ตามตารางด้านล่าง
9. **GCP:** เช็ก `FAOJ_HOST=faoj-prod-gcp` ก่อน → `DROP SCHEMA public` → `pg_restore --role=faoj_owner --exit-on-error` → `migrate`
10. **GCP:** **`move_manifest --deep --compare`** (G4) แล้ว **`decrypt-check`** ทั้งสองต้องผ่าน

### เปิดระบบ

11. **ตั้งค่า backup ให้เป็น production:** `BACKUP_MODE=pgbackrest`, `PG_ARCHIVE_MODE=on`, `BACKUP_REPO_PATH=/pgbackrest/production-gcp` แล้วเปิด timer ของ backup
12. **Webhook:** `NGROK_DOMAIN=crabmeat-lake-lunar.ngrok-free.dev`
13. **GCP:** `faoj deploy <digest จากแผน 4>` ซึ่งจะ restart db เพื่อเปิด archive, รัน `stanza-create` ที่ path ใหม่, เปิด ngrok และ smoke
14. `faoj maintenance off` → `faoj smoke` → `faoj backup full` → `faoj backup files`
15. ตรวจว่าข้อความ LINE จากขั้น 1 เข้ามาแล้ว (redelivery) และไม่ซ้ำ
16. **E5:** ปล่อยไว้ใน maintenance แบบ read-only เก็บข้อมูลไว้ถึง 10 พ.ย. dump ทั้งสองฝั่งเก็บไว้ 30 วัน **สคริปต์จะไม่ลบ dump เอง**

## กติกาการ merge key (ข้อ 8)

| ไฟล์บน GCP | ใช้ค่าจาก E5 | ใช้ค่าของ GCP เอง |
|---|---|---|
| `app.secret.env` | **ทุก key** เช่น `DJANGO_SECRET_KEY`, `AI_SECRET_KEY` (ถอดรหัส API key ที่เก็บใน DB), `PII_SECRET_KEY` (ถอดรหัสอีเมลใน roster), LINE secret/token, เมล, Mini App IDs | `DATABASE_URL`, `ORCHESTRATOR_TOKEN` |
| `db.secret.env` | `PGBACKREST_REPO1_S3_KEY`, `PGBACKREST_REPO1_S3_KEY_SECRET` | รหัสผ่านของ Postgres ทุกตัว และ `PGBACKREST_REPO1_CIPHER_PASS` (stanza ใหม่) |
| `backup.secret.env` | ทุก key (restic repo เดียวกับ E5) | – |
| `ngrok.secret.env` | `NGROK_AUTHTOKEN` | – |
| `n8n.secret.env` | – | ทุก key (n8n บน GCP เป็นของใหม่) |
| `env/google/` | service-account key | – |
| `django.env` | flag ทั้งหมด เช่น `AI_ENABLED`, `OUTBOUND_ENABLED`, `LINE_PUSH_ENABLED`, `STAFF_EMAIL_ENABLED`, `AI_EVIDENCE_PUBLIC_KEYS`, `INVENTORY_MODE` | `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `STAFF_WEB_BASE_URL` |
| `compose.env` | `FALLBACK_*`, `BACKUP_S3_ENDPOINT`, `OCI_COMPARTMENT_OCID`, `AUTO_PROMOTE_*` | `FAOJ_ROOT`, `PUBLIC_HOST`, `METRICS_SINK=stdout` และค่าจากข้อ 11 และ 12 |
| `tls/db/` | – | ทั้งหมด |

- ☐ key ใดที่ E5 มีแต่ไม่อยู่ในตาราง สคริปต์จะคัดลอกให้ แล้วแสดงชื่อ key ให้ตรวจ (ไม่แสดงค่า)
- ☐ ค่า `PGBACKREST_REPO1_CIPHER_PASS` ของ GCP เจ้าของต้องบันทึกลงตัวจัดการรหัสผ่านก่อนขั้น 13 แล้วยืนยันด้วย `escrow-check`

## G4

`--deep --compare` ต้องตรงทุกค่า (เนื้อหาทุกตาราง, sequence, เลข W/R/P, audit, ไฟล์ทั้งโฟลเดอร์, migrate) และ `decrypt-check` ต้องผ่าน ถ้าไม่ผ่าน สคริปต์จะหยุดแล้วรัน `back-to-e5`: ยกเลิก read-only ของ E5, เปิด web/worker/n8n/ngrok และออกจาก maintenance ส่วน GCP อยู่ใน maintenance ต่อ เนื่องจาก GCP ยังไม่เคยรับงาน ข้อมูลทั้งหมดจึงยังอยู่บน E5 ครบ

## เสร็จเมื่อ

- ☐ smoke ผ่าน, full backup และ restic snapshot ขึ้น `production-gcp` แล้ว
- ☐ ข้อความทดสอบจากขั้น 1 เข้ามาครบ 1 ข้อความ
- ☐ จดเวลาเริ่มและจบ downtime ไว้ใน `faoj releases`

## ถ้าพังหลังเปิดแล้ว

ห้ามแค่สลับ ngrok กลับ E5 เพราะงานที่เขียนบน GCP จะหาย ให้แก้บน GCP ถ้าจำเป็นต้องกลับจริง ๆ ให้ใช้ `gcp-move.sh reverse-move` ซึ่งทำแผนนี้ทั้งหมดในทิศกลับ (GCP → E5) และตรวจด้วยชุดเดียวกัน ทำได้ก่อนวันที่ 10 พ.ย. เท่านั้น
