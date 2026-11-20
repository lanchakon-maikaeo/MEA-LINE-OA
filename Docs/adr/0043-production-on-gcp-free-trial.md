---
status: accepted
---

# production ชั่วคราวบน GCP Free Trial จนถึงกลาง ม.ค. 2570

แทนที่ส่วนเครื่อง prod และทางเข้าของ [ADR 0015](0015-temporary-e5-production-on-trial-credit.md) เครดิตทดลองของ OCI ที่ใช้รัน `faoj-prod-e5` หมดวันที่ 10 พ.ย. 2569 และยังสร้างเครื่อง A1 ไม่ได้ เจ้าของระบบจึงสั่งวันที่ 1 พ.ย. ให้ย้ายไป GCP โดยมีเงื่อนไขว่าระบบต้องใช้งานได้ต่อเนื่อง ข้อมูลเดิมห้ามหาย image ต้องเป็นรุ่นล่าสุด และห้ามตัดบัตร ย้ายเสร็จวันที่ 2 พ.ย. 2569 เวลา 21:42 ถึง 21:48 น. ขั้นตอนอยู่ใน [แผนย้าย](../release/gcp-move/README.md) ส่วนวิธี deploy อยู่ใน [คู่มือ](../deploy-gcp.md)

- **เครื่อง:** `faoj-prod-gcp` เป็น `n4-custom-2-10240` (2 vCPU แรม 10 GB) ใน `asia-southeast3` กรุงเทพฯ ดิสก์ข้อมูล 150 GB ใช้เครดิต Free Trial อย่างเดียว ค่าใช้จ่าย 90 วันประมาณ ฿9,780 ถึง ฿10,170 ขณะที่เครดิตมี ฿10,133 จึงอาจหมดก่อนครบ 90 วันไม่กี่วัน ถ้าเครดิตหมด เครื่องจะหยุดโดยไม่มีการเก็บเงิน และ backup ยังอยู่ที่ OCI
- **ทางเข้า:** LINE ยังใช้ ngrok static domain เดิม URL ของ webhook จึงไม่เปลี่ยน เว็บเจ้าหน้าที่ใช้ `sslip.io` ไม่ได้เพราะ FortiGate ของ กฟน. จัด `sslip.io` เป็น Dynamic DNS แล้วบล็อก จึงตั้ง Cloud Run ชื่อ `dedcenter` ที่ `https://dedcenter-381083094942.asia-southeast3.run.app/` (FortiGuard จัด `run.app` เป็น Information Technology) ให้รัน Caddy ส่งต่อไปที่เครื่อง Endpoint ของ Mini App ก็ใช้ชื่อนี้
- **backup:** pgBackRest ใช้ bucket เดิมใน OCI Object Storage แต่แยก path เป็น `/pgbackrest/production-gcp` สำเนาของ E5 ที่ `/pgbackrest/production` จึงไม่ถูกแตะ restic ใช้ repo เดิมและแยกกันด้วย host tag (`BACKUP_HOST_TAG`) แต่ละเครื่องลบได้เฉพาะ snapshot ของตัวเอง Mac ยังเป็น standby และดึงสำเนาไป OneDrive ทุกสัปดาห์ แยกโฟลเดอร์ตาม path
- **การ deploy:** deploy เฉพาะ digest จาก Release workflow ผ่าน `deploy/gcp-move.sh deploy` ซึ่งรัน `faoj deploy` แบบ detached บนเครื่อง เข้าเครื่องผ่าน IAP จึงไม่ต้องแก้ allow list เมื่อ IP ของ Mac เปลี่ยน auto-promote ปิดไว้ และเครื่องดึง image จาก OCIR ด้วย auth token ที่ pull ได้อย่างเดียว
- **การเฝ้าดู:** metric ส่งออก stdout (`METRICS_SINK=stdout`) ไม่ขึ้น OCI Monitoring การเตือนมีแค่งานบน Mac (healthcheck ทุกนาที และงาน standby ที่เตือนเมื่อตามหลังเกิน 15 นาที) กับ UptimeRobot
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

## Backup ย้ายไป Cloud Storage (20 พ.ย. 2569)

ตั้งแต่ 15:03 น. ของวันที่ 20 พ.ย. OCI Object Storage ปฏิเสธการเขียนทุกครั้งด้วย `StorageLimitExceeded` เพราะ tenancy เหลือแค่ Always Free ซึ่งมีที่ 20 GB รวม OCIR และ restic WAL จึงค้างใน `pg_wal` (ฐานข้อมูล 116 MB แต่สร้าง WAL ราว 13 GB ต่อวัน) และกู้ถึงจุดเวลาได้แค่ 15:03 น. การเตือนเรื่องโควตา (`faoj quota-check` ที่ 16 GB) ต้องใช้ instance principal ของ OCI ซึ่งเครื่อง GCP ไม่มี จึงไม่มีอะไรเตือนก่อนเต็ม ตอน 18:54 น. deploy PR #61 ล้มที่ขั้นทำจุดกู้คืน script ออกโดยไม่ปิด maintenance และไม่เปิด n8n กับ worker คืน ระบบจึงหยุดรับงาน 16 นาทีจนเจ้าของรันคำสั่งกู้เอง เจ้าของสั่งให้ย้าย backup ไป GCP และลบ OCI ทั้งหมดหลังจากนั้น

- **ที่เก็บ:** bucket `mea-ded-center-line-oa-faoj-backup` ใน `asia-southeast3` (กรุงเทพฯ) แบบ private ทั้ง bucket ค่าใช้จ่ายหักจากเครดิต Free Trial pgBackRest ใช้ path เดิม `/pgbackrest/production-gcp` (`BACKUP_REPO_TYPE=gcs`) restic ใช้ `gs:<bucket>:/restic` และ host tag เดิม
- **สิทธิ์:** VM ได้ scope `devstorage.read_only` ซึ่งเปลี่ยนได้เฉพาะตอนหยุดเครื่อง เจ้าของเลือกไม่หยุดเครื่อง จึงใช้ key ของ service account `faoj-backup` ที่มีแค่ `storage.objectAdmin` กับ `storage.legacyBucketReader` บน bucket นี้ (restic ต้องอ่านข้อมูล bucket ก่อน init) key อยู่ที่ `/srv/faoj/gcs/faoj-backup.json` (root:999, 0640) `deploy/gcp-move.sh backup-setup` ที่เจ้าของรันสร้างทุกอย่าง ส่ง key จาก Google ไปที่เครื่องโดยไม่ค้างบน Mac ลบ key เก่าของบัญชีนี้ ตั้งค่าบนเครื่อง และลองอ่าน bucket จากเครื่อง รันซ้ำเพื่อหมุน key ได้
- **deploy ที่สลับ:** container ของฐานข้อมูลรุ่นเดิมยังชี้ไป OCI จุดกู้คืนก่อน migrate จึงใช้ `--restore-point dump` (pg_dump บนดิสก์ของเครื่อง) ครั้งเดียว dump ที่ทำก่อน migrate เป็นจุดกู้คืนที่ใหม่อยู่แล้ว การ deploy แบบนี้จึงไม่ตรวจอายุ backup ล่าสุดใน pgBackRest (backup ล่าสุดบน OCI คือ 19 พ.ย. 20:12Z ซึ่งจะเกิน 26 ชั่วโมง) container รุ่นใหม่สร้าง stanza ใน Cloud Storage แล้วส่ง WAL ที่ค้างขึ้นไป จากนั้นทำ full backup และ backup ไฟล์ทันที
- **`faoj deploy`:** ถ้าทำจุดกู้คืนไม่ได้ จะเปิด n8n กับ worker คืน ปิด maintenance บันทึกว่า failed แล้วหยุด โดยยังไม่เปลี่ยนอะไร ถ้า stanza-create ล้มหลังสลับรุ่นแล้ว deploy ทำต่อ เพราะ WAL รอใน `pg_wal` ได้ ดีกว่าค้างครึ่งทางในโหมด maintenance
- **ซ้อมกู้:** `faoj restore-drill` อ่านจาก Cloud Storage เมื่อ `BACKUP_REPO_TYPE=gcs` ต้องซ้อมกู้ผ่านก่อนลบ OCI
- **ยังต้องทำ:** `standby.sh` และ `backup-pull.sh` บน Mac ยังอ่านจาก OCI จึงหยุดได้ข้อมูลใหม่ตั้งแต่ 15:03 น. ต้องเปลี่ยนให้อ่าน Cloud Storage ก่อนลบ OCI backup อยู่ผู้ให้บริการเดียวกับ production สำเนานอก GCP จึงเหลือแค่สำเนาบน OneDrive ของ `backup-pull.sh`
