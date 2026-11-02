# แผน 6: หลังย้าย (ระยะ 2)

**ใครทำ:** เจ้าของ (console ของ LINE และ UptimeRobot) และผู้ช่วย (Mac และเครื่อง) **เวลา:** ~60 นาที วันเดียวกับที่ย้าย **รอ:** แผน 5 เสร็จ

> **2 พ.ย. 2569 (คืนที่ย้าย):**
> - **ข้อ 1:** LINE บน GCP รับและตอบได้ ข้อความจริง 8 รายการหลังย้ายตอบ `sent` ทั้งหมด outbox ไม่ค้าง ข้อความ 21:50:38 ที่ได้ 400 เข้ามาทาง redelivery ตอน 21:52:39 ส่วนการ login ของเจ้าหน้าที่ (OTP mail) เจ้าของยังต้องลอง
> - **ข้อ 2:** `faoj restore-drill` ของ `production-gcp` ผ่าน (RPO 533 s, RTO 79 s, ไฟล์แนบไม่หายและไม่เปลี่ยน) ก่อนหน้านั้น drill ล้มเพราะ `umask 077` ทำให้ postgres กับแอปเข้าโฟลเดอร์ drill ไม่ได้ แก้ใน `93c5d5e` (รอ release ถัดไป) `drill.secret.env` บน GCP สร้างจาก key ของเครื่องเอง
> - **ข้อ 3:** `pgbackrest check` ผ่าน, backup ล่าสุดไม่เกิน 2 นาที, restic ของ GCP ใช้ host `production-gcp` (Release `79d3c22`)
> - **ข้อ 4:** `standby.sh setup` ดึง env จาก GCP และเก็บ reader key เดิม standby เดิม (ตาม E5) ย้ายไปเก็บที่ `~/faoj-standby/data/postgres/pgdata.e5-20261102` แล้ว build release ของ standby ใหม่ เพราะรุ่นเดิมกำหนด path ตายตัว
> - **ข้อ 5:** `backup-pull` แยกโฟลเดอร์ต่อ path (`pgbackrest-production-gcp`) เพราะ `rclone sync` จะลบสำเนา stanza ของ E5 ใน OneDrive ถ้าใช้โฟลเดอร์เดิม ตั้ง `SOURCE_REPO_PATH` แล้ว ยังไม่ได้ pull (เน็ตมือถือ)
> - **ข้อ 7:** Endpoint URL (Published) เป็น `https://34-15-129-131.sslip.io/broadcasts/` แล้ว ตอบ 200
> - **ข้อ 8:** rich menu ใช้ `miniapp.line.me/<LIFF ID>` จึงตามข้อ 7 ไปเอง ไม่ต้องรันใหม่
> - **ข้อ 9:** healthcheck ชี้ GCP แล้ว (บน E5 เห็นแต่หน้า maintenance จึงไม่เคยเตือน) การเตือนเรื่อง backup ใช้ lag ของ standby (เกิน 15 นาที) แทน

## ทำทันที (G5)

| # | งาน | ใคร | ตรวจว่า |
|---|---|---|---|
| 1 | ทดสอบครบรอบ: requester ส่ง LINE → bot ตอบ → เปิดเรื่องได้ / เจ้าหน้าที่ login (OTP mail) → เห็นเรื่องเก่าและเรื่องใหม่ → ตอบกลับ → เปิดไฟล์แนบเก่าได้ | ทั้งคู่ | ทำได้ครบ |
| 2 | **restore drill ของ backup ใหม่:** `faoj restore-drill` จาก stanza `production-gcp` ลงฐานข้อมูลที่ทิ้งได้ แล้ว `--deep --compare` กับ GCP | ผู้ช่วย | ตรงทุกค่า ถือว่า backup ใหม่ restore ได้จริง |
| 3 | `pgbackrest check` และ `faoj backup status` | ผู้ช่วย | WAL ไป OCI แล้ว และ backup ล่าสุดไม่เกิน 1 ชม. |
| 4 | **Mac standby ชี้ไป GCP:** `standby.sh setup` → `start` ให้อ่าน stanza `production-gcp` | ผู้ช่วย | `standby.sh status` อยู่ใน recovery และตามหลังไม่เกิน 15 นาที |
| 5 | Mac: `make backup-pull` ดึงสำเนาจุด freeze มาเก็บ แล้ว `make backup-pull-schedule` ให้ใช้ path ใหม่ | ผู้ช่วย | มีไฟล์บน Mac |
| 6 | GCP: ตั้ง snapshot schedule รายวันให้ data disk เก็บ 7 วัน (`gcloud compute resource-policies create snapshot-schedule` แล้ว attach) เป็นสำเนาเพิ่มอีกชั้น | เจ้าของ | เห็น snapshot แรก |
| 7 | LINE Developers → LINE MINI App (Published) → Endpoint URL เปลี่ยนเป็น `https://<ip-ใหม่>.sslip.io/broadcasts/` | เจ้าของ | เปิด Broadcast ได้จากมือถือ |
| 8 | ถ้า rich menu มีลิงก์ไปเว็บเจ้าหน้าที่ ให้รัน `gcp-move.sh line-menu` | ผู้ช่วย | กดเมนูแล้วไป host ใหม่ |
| 9 | Mac healthcheck: `STAFF_URL` ใหม่ และแจ้งเตือนเมื่อ backup ล่าสุดเกิน 2 ชม. แล้ว `make health-schedule` | ผู้ช่วย | ใน log ของ launchd เห็นผล OK |
| 10 | UptimeRobot เปลี่ยน URL, แจ้ง URL ใหม่ให้เจ้าหน้าที่ และปิดแบนเนอร์ | เจ้าของ | – |

ข้อ 2 ถึง 4 ต้องผ่านในวันที่ย้าย ถ้าไม่ผ่าน ให้แก้ทันที ห้ามปล่อยข้ามคืน

## ภายใน 24 ชั่วโมง

- ☐ บันทึก ADR ใหม่ "production บน GCP Free Trial ที่กรุงเทพฯ" (แก้ ADR 0015) รวมทั้งกติกาใน [0-data-safety.md](0-data-safety.md)
- ☐ ดูค่าใช้จ่ายวันแรกใน GCP Billing (รวม snapshot) แล้วประมาณว่า $300 จะพอถึงวันไหน

## วันที่ 10 พ.ย. 2569

- ☐ ก่อน E5 หยุด รันซ้ำว่า restore drill บน GCP ยังผ่าน และ Mac standby ยังตามทัน
- ☐ E5 หยุดเองเมื่อเครดิตหมด จากนั้นเปิด PR เอา `faoj-prod-e5` ออกจาก Terraform (แก้ `prevent_destroy` ใน commit ที่ตรวจแล้ว) แล้ว**เจ้าของกด apply** โดยเก็บ bucket, OCIR และ Vault ไว้
- ☐ หมุน Customer Secret Key ของ backup-writer ใส่ key ใหม่บน GCP แล้วลบ key เก่า

## 30 วันหลังย้าย

- ☐ ลบ stanza `production` เดิม และ dump ของรอบย้ายบน GCP ได้ แต่ต้องมี restore drill ที่ผ่านในสัปดาห์นั้นก่อน

## ก่อนกลาง ม.ค. 2570

- ☐ เลือกที่อยู่ถาวรที่ไม่ต้องตัดบัตร (A1 Always Free หรือ VM ของ กฟน.) และย้ายด้วยแผนชุดนี้ก่อน Free Trial หมด ห้ามอัปเกรด GCP เพื่อให้เครื่องรันต่อ เพราะดิสก์บน GCP จะถูกลบ 30 วันหลัง trial หมด
- ☐ ขอ subdomain ของ กฟน. ไว้ การย้ายครั้งหน้า URL จะได้ไม่เปลี่ยน
- ☐ ลบ image เก่าใน OCIR
