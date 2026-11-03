---
status: accepted
---

# production ชั่วคราวบน GCP Free Trial จนถึงกลาง ม.ค. 2570

แทนที่ส่วนเครื่อง prod และทางเข้าของ [ADR 0015](0015-temporary-e5-production-on-trial-credit.md) เครดิตทดลองของ OCI ที่ใช้รัน `faoj-prod-e5` หมดวันที่ 10 พ.ย. 2569 และยังสร้างเครื่อง A1 ไม่ได้ เจ้าของระบบจึงสั่งวันที่ 1 พ.ย. ให้ย้ายไป GCP โดยมีเงื่อนไขว่าระบบต้องใช้งานได้ต่อเนื่อง ข้อมูลเดิมห้ามหาย image ต้องเป็นรุ่นล่าสุด และห้ามตัดบัตร ย้ายเสร็จวันที่ 2 พ.ย. 2569 เวลา 21:42 ถึง 21:48 น. ขั้นตอนอยู่ใน [แผนย้าย](../release/gcp-move/README.md) ส่วนวิธี deploy อยู่ใน [คู่มือ](../deploy-gcp.md)

- **เครื่อง:** `faoj-prod-gcp` เป็น `n4-custom-2-10240` (2 vCPU แรม 10 GB) ใน `asia-southeast3` กรุงเทพฯ ดิสก์ข้อมูล 150 GB ใช้เครดิต Free Trial อย่างเดียว ค่าใช้จ่าย 90 วันประมาณ ฿9,780 ถึง ฿10,170 ขณะที่เครดิตมี ฿10,133 จึงอาจหมดก่อนครบ 90 วันไม่กี่วัน ถ้าเครดิตหมด เครื่องจะหยุดโดยไม่มีการเก็บเงิน และ backup ยังอยู่ที่ OCI
- **ทางเข้า:** LINE ยังใช้ ngrok static domain เดิม URL ของ webhook จึงไม่เปลี่ยน เว็บเจ้าหน้าที่ใช้ `sslip.io` ไม่ได้เพราะ FortiGate ของ กฟน. จัด `sslip.io` เป็น Dynamic DNS แล้วบล็อก จึงตั้ง Cloud Run ชื่อ `dedcenter` ที่ `https://dedcenter-381083094942.asia-southeast3.run.app/` (FortiGuard จัด `run.app` เป็น Information Technology) ให้รัน Caddy ส่งต่อไปที่เครื่อง Endpoint ของ Mini App ก็ใช้ชื่อนี้
- **backup:** pgBackRest ใช้ bucket เดิมใน OCI Object Storage แต่แยก path เป็น `/pgbackrest/production-gcp` สำเนาของ E5 ที่ `/pgbackrest/production` จึงไม่ถูกแตะ restic ใช้ repo เดิมและแยกกันด้วย host tag (`BACKUP_HOST_TAG`) แต่ละเครื่องลบได้เฉพาะ snapshot ของตัวเอง Mac ยังเป็น standby และดึงสำเนาไป OneDrive ทุกสัปดาห์ แยกโฟลเดอร์ตาม path
- **การ deploy:** deploy เฉพาะ digest จาก Release workflow ผ่าน `deploy/gcp-move.sh deploy` ซึ่งรัน `faoj deploy` แบบ detached บนเครื่อง เข้าเครื่องผ่าน IAP จึงไม่ต้องแก้ allow list เมื่อ IP ของ Mac เปลี่ยน auto-promote ปิดไว้ และเครื่องดึง image จาก OCIR ด้วย auth token ที่ pull ได้อย่างเดียว
- **การเฝ้าดู:** metric ส่งออก stdout (`METRICS_SINK=stdout`) ไม่ขึ้น OCI Monitoring การเตือนมีแค่ healthcheck บน Mac (ทุกนาที และเตือนเมื่อ standby ตามหลังเกิน 15 นาที) กับ UptimeRobot
- **E5:** เก็บไว้แบบอ่านอย่างเดียวจนถึงวันที่ 10 พ.ย. (ฐานข้อมูลเป็น `default_transaction_read_only=on` หยุด web, worker, n8n และ ngrok) ทุกหน้าบอกที่อยู่ใหม่ การเขียนอะไรบน E5 ต้องให้เจ้าของอนุมัติทีละครั้ง

## Considered Options

- **GCP Always Free:** ไม่เลือก ได้แค่ e2-micro แรม 1 GB และมีเฉพาะ region ในสหรัฐฯ แต่ container ทั้งชุดตั้ง `mem_limit` รวมราว 5.6 GB
- **อัปเกรดบัญชีหรือจ่ายเงิน (OCI PAYG หรือ GCP แบบจ่าย):** ไม่เลือก เจ้าของสั่งห้ามตัดบัตรเด็ดขาด ทั้งตอนนี้และสำหรับที่อยู่ถาวร
- **รอ A1:** ไม่เลือก ยังไม่มี capacity และรอเลยวันที่ 7 พ.ย. ไม่ได้
- **ใช้ `sslip.io` ต่อแล้วขอให้ กฟน. ปลดบล็อก:** ไม่เลือกเป็นทางหลัก ต้องรอฝ่ายอื่น ส่วน run.app ใช้ได้ทันที

## Consequences

- ต้องย้ายออกภายในกลาง ม.ค. 2570 ไปที่ที่ไม่ต้องตัดบัตร (A1 Always Free หรือ VM ของ กฟน.) ใช้แผนย้ายชุดเดิม และห้ามอัปเกรด GCP เพื่อให้เครื่องรันต่อ เพราะดิสก์จะถูกลบ 30 วันหลัง Free Trial หมด
- การย้ายครั้งต่อไปต้องทำตาม [กติกาข้อมูลห้ามหาย](../release/gcp-move/0-data-safety.md) ทุกข้อ ได้แก่ เทียบ manifest แบบ deep, ทำฐานข้อมูลเดิมให้ read-only ก่อนย้าย, ตรวจว่าถอดรหัส backup ได้, เก็บ dump ไว้ 30 วัน และถ้าเครื่องใหม่รับงานไปแล้วให้ย้ายกลับด้วย reverse-move ห้ามสลับกลับเฉย ๆ
- ชื่อ run.app ผูกกับ project นี้ ตอนย้ายออก URL ของเว็บเจ้าหน้าที่และ Mini App จะเปลี่ยนอีกรอบ เว้นแต่ได้ subdomain ของ กฟน. ก่อน
- วันที่ 10 พ.ย.: ซ้อมกู้อีกครั้ง ลบ E5 ออกจาก Terraform ใน PR ที่ตรวจแล้ว (ต้องแก้ `prevent_destroy` เจ้าของเป็นคน apply) และหมุน backup-writer key
- ไม่มี alarm จาก OCI Monitoring สำหรับเครื่องนี้ ถ้า Mac ปิดอยู่ จะเหลือแค่ UptimeRobot
- ข้อมูลจริงอยู่ที่กรุงเทพฯ แต่ backup อยู่ที่ Singapore และ LINE ผ่าน ngrok ยังต้องให้เจ้าของข้อมูลรับรองตาม G02 เหมือน ADR 0015

## สิ่งที่ทำแล้ว (2 ถึง 3 พ.ย. 2569)

- ย้ายด้วย `deploy/gcp-move.sh move` ระบบหยุดราว 5 นาที `move_manifest` deep compare และ decrypt check ผ่าน ไม่มีข้อมูลหาย LINE webhook ได้ 400 ราว 4 นาที (21:48 ถึง 21:52 น.) เพราะ `ALLOWED_HOSTS` ยังไม่มี ngrok domain แต่ LINE ส่งซ้ำให้ครบเพราะเปิด redelivery ไว้
- ซ้อมกู้ผ่านสองรอบ รอบล่าสุด RTO 88 วินาที
- `gcp-move.sh staff-proxy`, `manage` และ `line-menu` ส่วน `standby.sh`, `backup-pull.sh` และ `healthcheck.sh` ตามเครื่อง GCP แล้ว
- release record สองรุ่นแรกหลังย้าย (`79d3c22` และ `86cfdc5`) บันทึก approval เป็นข้อความของระยะขึ้นโครง เพราะค่า default ของ `GCP_APPROVAL` ยังเป็นของระยะนั้น แก้ค่า default แล้ว บันทึกเดิมไม่แก้ย้อนหลัง
