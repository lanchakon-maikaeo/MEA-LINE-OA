# แผน 2: โค้ด

**ใครทำ:** ผู้ช่วยเขียนโค้ด เจ้าของกด merge **รอ:** ไม่ต้องรอแผนไหน

## ระยะ 1: `deploy/gcp-move.sh` สำหรับขึ้นโครง (~40 นาที ทำพร้อมแผน 1)

สคริปต์นี้รันจาก Mac ไม่อยู่ใน image โครงเหมือน `e5-move.sh` แต่ปลายทางเป็น GCP

| คำสั่ง | ทำอะไร |
|---|---|
| `tunnel` | IAP tunnel ไป `faoj-prod-gcp:22` แล้ว ssh ด้วย `~/.ssh/faoj_gcp_ed25519` |
| `prepare` | ติดตั้ง docker.io, docker-compose-v2, python3, openssl, unattended-upgrades; format และ mount `/dev/disk/by-id/google-faoj-data` ที่ `/srv/faoj` (label `faojdata`, fstab `nofail`); เขียน `/etc/faoj/host.env` |
| `source` | คัดลอก working tree ที่ commit แล้ว (ไม่รวม `.env`, Docs, media) เหมือน `e5-move.sh source` แต่ถ้า tree ยัง dirty ให้หยุด |
| `build` | build image `db` และ `app` บนเครื่องลง registry `127.0.0.1:5001` เหมือน `e5-move.sh build` |
| `bootstrap` | `bootstrap-host.sh --env production --public-host <ip>.sslip.io` (สร้าง secret ของตัวเอง) |
| `skeleton-config` | `BACKUP_MODE=dump`, `PG_ARCHIVE_MODE=off`, `METRICS_SINK=stdout`, `NGROK_DOMAIN=` ค่าว่าง; ปิด timer `faoj-backup-{full,diff,files}` และ `faoj-quota-check` (ยังไม่มี key ของ Object Storage และไม่มี instance principal ของ OCI) |
| `deploy` | `faoj deploy <digest จาก build> --approval "…"` |
| `ssh`, `status` | เข้าเครื่องหรือดู `faoj status` |

☐ ทดสอบด้วย `bash -n` และ `shellcheck` แล้ว commit ลง `dev` ส่วนการรันจริงอยู่ในแผน 3

> **2 พ.ย.:** ทำแล้ว 2.1, 2.2 (`22bbd33`) และ `escrow-check`, `dry-run`, `keys` (อยู่ใน `move`), `move`, `back-to-e5` ฝั่ง E5 ใช้แค่การอ่านแบบ stream ผ่าน Mac ยกเว้นตอน freeze ใน `move` ซึ่งต้องพิมพ์ YES ก่อน

## ระยะ 2: สิ่งที่ต้องมีก่อนย้าย (~1.5 ชม. รวม test ทำหลังระยะ 1 และก่อนแผน 4)

### 2.1 path ของ backup ตั้งเองได้ (ต้องอยู่ใน image)

ฐานข้อมูลที่ restore จาก dump บน GCP จะได้ system-id ใหม่ จึงทำ `stanza-create` ทับ stanza `/pgbackrest/production` ของ E5 ไม่ได้

- ☐ เพิ่ม `BACKUP_REPO_PATH` ถ้าไม่ตั้ง ให้ใช้ค่าเดิม `/pgbackrest/<env>` เพื่อให้ E5 และ Mac standby ทำงานเหมือนเดิม จุดที่ต้องแก้คือ `deploy/server/compose.yaml:65`, `deploy/server/bin/faoj:544` (restore-drill) และ `deploy/backup-pull.sh:39`
- ☐ `make check` และ test ของ deploy

### 2.2 ตรวจข้อมูลให้ละเอียดขึ้น (ต้องอยู่ใน image) ตาม [0-data-safety.md](0-data-safety.md)

ตอนนี้ `move_manifest` เทียบแค่จำนวนแถว เลข W/R/P, audit และไฟล์แนบที่ DB อ้างถึง ถ้ามีการ UPDATE จำนวนแถวไม่เปลี่ยน จึงจับไม่ได้

- ☐ `move_manifest --deep`: digest ของเนื้อหาทุกตาราง (md5 ของแต่ละแถว เรียงด้วย `COLLATE "C"` และตั้ง TimeZone เป็น UTC ทั้งสองฝั่ง) และค่า sequence ทุกตัว ค่าเดิมที่ไม่ใช้ `--deep` ทำงานเหมือนเดิม
- ☐ `decrypt-check` (manage command): ถอดรหัส API key ที่เก็บใน DB ด้วย `AI_SECRET_KEY` และอีเมลใน roster ด้วย `PII_SECRET_KEY` ได้อย่างน้อยอย่างละ 1 รายการ ถ้าไม่ได้ให้ exit 1 และห้ามแสดงค่าที่ถอดได้
- ☐ test สำหรับ `--deep` ทั้งกรณีตรงกัน และกรณีที่มี UPDATE แถวเดียวแล้วต้องจับได้

### 2.3 คำสั่งย้ายใน `gcp-move.sh`

| คำสั่ง | ทำอะไร |
|---|---|
| `escrow-check` | แสดง hash 16 ตัวของ key ที่ผูกกับข้อมูล ให้เจ้าของเทียบกับตัวจัดการรหัสผ่าน |
| `dry-run` | dump จาก E5 ไป restore ในฐานข้อมูลที่ทิ้งได้บน GCP แล้วเทียบด้วย `--deep` และ sha256 ของไฟล์ทั้งโฟลเดอร์ |
| `keys` | อ่าน env ของ E5 แล้ว merge เข้า GCP ตามตารางในแผน 5 ส่งผ่าน pipe ไม่เขียนลง Mac และไม่แสดงค่า (แสดงแค่ชื่อ key และ hash 16 ตัว) |
| `move` | ลำดับทั้งหมดในแผน 5: freeze (read-only + `pg_stat_activity`), backup ณ จุด freeze, `--deep` ก่อนและหลัง dump, sha256 ของ dump และ media, `decrypt-check` ข้อใดไม่ผ่านให้หยุดทันที |
| `back-to-e5` | ใช้ได้เฉพาะเมื่อ GCP ยังไม่เคยเปิดรับงาน ถ้าเคยเปิดแล้วให้ปฏิเสธ |
| `reverse-move` | ย้ายจาก GCP กลับ E5 ด้วยขั้นตอนและการตรวจชุดเดียวกัน **(ยังไม่ได้เขียน 2 พ.ย. ต้องเขียนก่อนวันย้าย ส่วน `back-to-e5` ใช้ได้เฉพาะเมื่อ GCP ยังไม่เปิดรับงาน)** |
| `line-menu`, `manage` | เหมือนใน e5-move |

- ☐ คำสั่งที่ลบข้อมูลได้ (`DROP SCHEMA`, ล้างโฟลเดอร์) ต้องเช็ก `FAOJ_HOST` ของเครื่องปลายทางก่อน และสคริปต์ห้ามลบ dump เอง
- ☐ Mac healthcheck แจ้งเตือนเมื่อ backup ล่าสุดเกิน 2 ชม. เพราะบน GCP ใช้ `faoj-quota-check` ไม่ได้

### 2.4 ขึ้น main

- ☐ เปิด PR `dev → main` ซึ่งจะพา 2 commit ที่แก้ deploy (เช็กพื้นที่ image), 2.1 และ 2.2 ไปด้วย CI ผ่านแล้ว**เจ้าของกด merge** จากนั้น Release workflow ต้องผ่าน blocking scans

## ถ้าพัง

- **CI หรือ scan ล้ม:** แก้ผ่าน PR แล้วเลื่อนระยะ 2 ออกไป ระบบเปล่าบน GCP รอได้
