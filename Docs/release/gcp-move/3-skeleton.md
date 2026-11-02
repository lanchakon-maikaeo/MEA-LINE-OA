# แผน 3: ขึ้นโครงเปล่า (ระยะ 1)

**ใครทำ:** ผู้ช่วยรัน เจ้าของอนุมัติ **เวลา:** ~45 นาที **รอ:** แผน 1 (VM) และแผน 2 ระยะ 1 (`gcp-move.sh`)

**เป้าหมาย:** GCP รันครบทุก service ด้วยโค้ดล่าสุด แต่เป็นระบบเปล่า ไม่มี key ภายนอก ไม่มี webhook และไม่มีข้อมูลจริง ส่วน E5 ให้บริการตามปกติ

## ขั้นตอน

| # | คำสั่ง | เวลา | ตรวจว่า |
|---|---|---|---|
| 1 | `gcp-move.sh tunnel` | 2' | ssh เข้าได้ |
| 2 | `gcp-move.sh prepare` | 8' | `findmnt /srv/faoj` เป็น data disk 150 GB, `docker version` ใช้ได้, `free -g` ได้ราว 10 GB |
| 3 | `gcp-move.sh source` | 2' | `~/src/.commit` ตรงกับ `git rev-parse --short HEAD` และไม่มี `-dirty` |
| 4 | `gcp-move.sh build` | 10' | ได้ digest ของ `db` และ `app` |
| 5 | `gcp-move.sh bootstrap` | 3' | สร้าง env, TLS, swap และ timer ครบ |
| 6 | `gcp-move.sh skeleton-config` | 2' | `NGROK_DOMAIN` ว่าง, `BACKUP_MODE=dump`, ไม่มี timer ของ backup หรือ quota ที่เปิดอยู่ |
| 7 | `gcp-move.sh deploy` | 15' | `faoj status` healthy ทุก service (ClamAV ใช้เวลาเริ่มราว 5 นาที) และ `faoj smoke` ผ่าน |
| 8 | เปิด `https://<ip-ใหม่>.sslip.io/health/ready/` | 1' | ได้ `ok` และใบรับรอง TLS จาก Caddy ถูกต้อง |
| 9 | `faoj maintenance on` แล้วปล่อยไว้จนถึงแผน 5 | 1' | หน้าเว็บตอบ 503 จึงไม่มีใครกรอกข้อมูลจริงลงระบบเปล่า เพราะข้อมูลพวกนั้นจะถูกล้างตอน restore ([0-data-safety](0-data-safety.md) ข้อ 1) |

approval ของการ deploy: `"owner in chat 1 Nov 2026: empty skeleton on faoj-prod-gcp, no data (gcp-move)"`

## สถานะของระบบเปล่า

| ส่วน | สถานะ |
|---|---|
| LINE | ไม่มี channel secret/token และไม่มี ngrok bot ยังไม่ได้รับข้อความใด ๆ |
| AI, เมล, push | ปิด (`AI_ENABLED`, `OUTBOUND_ENABLED`, `LINE_PUSH_ENABLED`, `STAFF_EMAIL_ENABLED` เป็น `false` ตามค่าเริ่มต้นของ bootstrap) |
| Backup | dump เก็บในเครื่องเท่านั้น ยังไม่ส่งไป Object Storage |
| ข้อมูล | ฐานข้อมูลว่างที่ผ่าน migrate แล้ว และ n8n import workflow จาก release แล้ว |
| Secret | ของ GCP เองจาก bootstrap ค่าที่ผูกกับข้อมูล (เช่น `AI_SECRET_KEY`, `PII_SECRET_KEY`) จะถูกแทนด้วยค่าของ E5 ในแผน 5 |

## G2

- ☐ ข้อ 7, 8 และ 9 ผ่าน
- ☐ `docker ps` บน GCP ไม่มี ngrok และ LINE ยังตอบจาก E5 อยู่
- ☐ ส่ง URL ใหม่ให้เจ้าของไว้เตรียมประกาศ แต่**ยังไม่แจ้งใคร**จนถึงแผน 5

## ถ้าพัง

E5 ไม่ได้รับผลกระทบ ทุกขั้นรันซ้ำได้ ถ้า smoke ล้มให้ดูด้วย `gcp-move.sh ssh 'sudo faoj status'` และดู log ของ web/worker
