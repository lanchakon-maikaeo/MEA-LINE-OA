# แผน 6: หลังย้าย (ระยะ 2)

**ใครทำ:** เจ้าของ (console ของ LINE และ UptimeRobot) และผู้ช่วย (Mac และเครื่อง) **เวลา:** ~60 นาที วันเดียวกับที่ย้าย **รอ:** แผน 5 เสร็จ

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
