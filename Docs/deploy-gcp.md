# คู่มือ deploy และดูแล production บน GCP

ปรับปรุงวันที่ 3 พ.ย. 2569 production ย้ายจาก OCI E5 มาอยู่ที่ GCP ตั้งแต่ 2 พ.ย. 2569 เวลา 21:48 น. ตาม [ADR 0043](adr/0043-production-on-gcp-free-trial.md) ขั้นตอนตอนย้ายอยู่ใน [แผนย้าย](release/gcp-move/README.md) ส่วนเอกสารนี้ใช้กับงานประจำหลังย้าย

## ระบบตอนนี้

| | ค่า |
|---|---|
| เครื่อง | `faoj-prod-gcp` (`n4-custom-2-10240`, 2 vCPU แรม 10 GB) ใน project `mea-ded-center-line-oa` โซน `asia-southeast3-a` IP 34.15.129.131 ข้อมูลอยู่ที่ `/srv/faoj` (ดิสก์ 150 GB) |
| เว็บเจ้าหน้าที่ | `https://dedcenter-381083094942.asia-southeast3.run.app/` เป็น Cloud Run ที่ส่งต่อไปยังเครื่อง ใช้ได้ในเครือข่าย กฟน. ส่วน `https://34-15-129-131.sslip.io/` เข้าเครื่องตรง แต่ FortiGate ของ กฟน. บล็อกไว้ |
| LINE webhook | `https://crabmeat-lake-lunar.ngrok-free.dev/webhooks/line` ผ่าน container ngrok บนเครื่อง เปิด Webhook redelivery ไว้แล้ว |
| Mini App | Endpoint (Published) คือ `https://dedcenter-381083094942.asia-southeast3.run.app/broadcasts/` |
| image | OCIR `ap-singapore-1.ocir.io/axif86tn0tbj/faoj/app@sha256:…` จาก Release workflow เครื่องดึงด้วย auth token ที่ pull ได้อย่างเดียว |
| backup | pgBackRest ไป OCI Object Storage ที่ path `/pgbackrest/production-gcp` ส่วนไฟล์แนบและ n8n ไป restic repo เดียวกับที่ E5 ใช้ แยกกันด้วย host tag `production-gcp` |
| เครื่องสำรอง | Mac (`~/faoj-standby`) ดึง WAL ของ `production-gcp` มาทุก 5 นาที |
| เครดิต | Free Trial ของ GCP หมดราวปลาย ม.ค. 2570 ห้ามกด Upgrade และห้ามผูก billing account อื่น ต้องย้ายออกให้ได้ภายในกลาง ม.ค. |

E5 (`168-107-70-170.sslip.io`) หยุดรับงานแล้ว ฐานข้อมูลเป็น read-only และทุกหน้าแสดงข้อความว่าย้ายไปที่อยู่ใหม่ เก็บไว้ถึงวันที่ 10 พ.ย. ห้าม deploy หรือแก้อะไรบน E5 ถ้าเจ้าของไม่ได้สั่ง

## ก่อนเริ่ม

1. เจ้าของ login ด้วย `gcloud auth login` บน Mac
2. ทุกคำสั่งรันจาก repository บน Mac ผ่าน `deploy/gcp-move.sh` ซึ่งเข้าเครื่องผ่าน IAP ด้วย key `~/.ssh/faoj_gcp_ed25519` เครื่องไม่เปิดพอร์ต SSH สู่อินเทอร์เน็ต และไม่ต้องแก้ allow list เมื่อ IP ของ Mac เปลี่ยน (ต่างจาก Bastion ของ E5)
3. ถ้าต้องการ shell บนเครื่อง ใช้ `deploy/gcp-move.sh ssh` ถ้าต้องการรันคำสั่งเดียว ใช้ `deploy/gcp-move.sh ssh '<คำสั่ง>'` คำสั่ง `faoj` ต้องใช้ `sudo`

## Deploy รุ่นใหม่

ใช้ได้เฉพาะ image ที่ Release workflow สร้างและสแกนแล้ว ห้ามใช้ image ที่ build บนเครื่องหรือบน Mac

1. เจ้าของ merge PR เข้า `main`
2. รอ Release workflow ของ `main` ผ่าน แล้วอ่าน `app_image` จาก artifact `release-<commit>`

   ```bash
   id=$(gh run list --workflow release.yml --branch main --limit 1 --json databaseId --jq '.[0].databaseId'); gh run watch "$id" --exit-status && d=$(mktemp -d) && gh run download "$id" -D "$d" && python3 -c 'import json,glob,sys; print(json.load(open(glob.glob(sys.argv[1]+"/release-*/release.json")[0]))["app_image"])' "$d"
   ```

3. Deploy โดยใส่ว่าใครอนุมัติเมื่อไร ข้อความนี้จะอยู่ใน release record

   ```bash
   GCP_APPROVAL="owner in chat <วันที่>: <PR หรือเหตุผล>" deploy/gcp-move.sh deploy ap-singapore-1.ocir.io/axif86tn0tbj/faoj/app@sha256:<digest>
   ```

   คำสั่งนี้รัน `faoj` ของรุ่นใหม่ด้วย `systemd-run` บนเครื่อง ถ้า IAP หลุดกลางทาง deploy ก็ยังทำต่อจนเสร็จ ขั้นตอนบนเครื่องคือ backup, เปิด maintenance, migrate, สลับ container, smoke และเผยแพร่ Rich Menu ถ้า smoke ไม่ผ่าน ระบบจะกลับไปใช้รุ่นก่อนเอง

4. ติดตามผล

   ```bash
   deploy/gcp-move.sh ssh 'sudo tail -f /srv/faoj/run/deploy-<12 ตัวแรกของ digest>.log'
   ```

5. ตรวจหลัง deploy

   ```bash
   deploy/gcp-move.sh status
   ```

   ```bash
   curl -s https://dedcenter-381083094942.asia-southeast3.run.app/health/ready/
   ```

   ต้องได้ maintenance `off` ทุก service ทำงาน และ `/health/ready/` ตอบ `ok`

`faoj auto-promote` และ `faoj auto-deploy` ปิดอยู่บน GCP ระบบจึงไม่รับรุ่นใหม่เอง ทุกรุ่นต้อง deploy ด้วยมือตามขั้นข้างบน

## ย้อนรุ่น

```bash
deploy/gcp-move.sh ssh 'sudo faoj rollback'
```

คำสั่งนี้กลับไปรุ่นก่อนหน้าที่ deploy สำเร็จ ถ้าต้องการรุ่นที่เจาะจง ให้ใส่ digest ต่อท้าย (`sudo faoj rollback <image@sha256:…>`) ดูรายการรุ่นได้จาก `sudo faoj releases` การย้อนรุ่นเปลี่ยนแค่ image ไม่ย้อน migration และไม่ restore ฐานข้อมูล ถ้า migration ทำข้อมูลเสียให้ใช้ RB05 ใน `PROJECT.md` ส่วน E.9

## งานย่อย

| งาน | คำสั่ง |
|---|---|
| เผยแพร่ Rich Menu ใหม่จากรุ่นที่รันอยู่ (ไม่ deploy, ไม่ restart) | `deploy/gcp-move.sh line-menu` |
| รัน `manage.py` หนึ่งคำสั่ง | `deploy/gcp-move.sh manage <คำสั่ง> [args…]` |
| ปิดหรือเปิดระบบชั่วคราว | `deploy/gcp-move.sh ssh 'sudo faoj maintenance on'` (หรือ `off`) |
| ตั้ง Cloud Run ของเว็บเจ้าหน้าที่ใหม่ (เช่น IP ของเครื่องเปลี่ยน) | `deploy/gcp-move.sh staff-proxy` |

worker ตรวจ Rich Menu จากรุ่นที่รันอยู่ทุก 5 นาที ถ้าแก้ภาพหรือปุ่มเมนู ให้ deploy รุ่นปกติ `line-menu` ใช้เมื่อเมนูใน LINE ไม่ตรงกับรุ่นที่รันอยู่

## Backup และการเฝ้าดู

เวลาในตารางเป็นเวลาไทย เครื่องตั้งเวลาเป็น UTC

| งาน | เมื่อไร | ที่ไหน |
|---|---|---|
| WAL ของ Postgres | ตลอดเวลา | OCI Object Storage `/pgbackrest/production-gcp` |
| full backup | วันจันทร์ 03:00 | ที่เดียวกัน |
| diff backup | วันอังคารถึงอาทิตย์ 03:00 | ที่เดียวกัน |
| ไฟล์แนบและ n8n | ทุกวัน 03:30 | restic host `production-gcp` |
| standby บน Mac | ทุก 5 นาที | `~/faoj-standby` |
| healthcheck บน Mac | ทุกนาที | ตรวจ `https://34-15-129-131.sslip.io/health/ready/` |
| สำเนานอก OCI | วันจันทร์ 09:00 | OneDrive โฟลเดอร์ `pgbackrest-production-gcp` และ `restic` |

ดูผลได้ด้วย `sudo faoj backup status` บนเครื่อง และ `deploy/standby.sh status` บน Mac (ถ้าตามหลังเกิน 15 นาที healthcheck จะเตือน)

บน GCP ตั้ง `METRICS_SINK=stdout` ไว้ metric จึงไม่ขึ้น OCI Monitoring และ alarm ของ OCI ไม่เห็นเครื่องนี้ การเตือนตอนนี้มีแค่ healthcheck บน Mac กับ UptimeRobot ส่วน `faoj quota-check` เป็นงบของ OCI และปิดไว้บนเครื่องนี้

`faoj-restore-drill.timer` ปิดอยู่บน GCP การซ้อมกู้ต้องสั่งเอง (`sudo faoj restore-drill`) ระหว่างซ้อมระบบจะเข้า maintenance และหยุดชั่วคราว ครั้งล่าสุดวันที่ 2 พ.ย. ใช้เวลา 88 วินาที จึงควรซ้อมนอกเวลางาน

## เมื่อต้องย้ายออกจาก GCP

ต้องย้ายก่อนกลาง ม.ค. 2570 ไปเครื่องที่ไม่ต้องตัดบัตร (A1 Always Free หรือ VM ของ กฟน.) ใช้ขั้นตอนชุดเดียวกับ [แผนย้าย](release/gcp-move/README.md) และ RB08 ตอนย้ายมีสองอย่างที่ต้องเปลี่ยนตาม

- ชื่อ run.app ผูกกับ project GCP นี้ เจ้าหน้าที่และ Endpoint ของ Mini App ต้องเปลี่ยน URL อีกรอบ ถ้าได้ subdomain ของ กฟน. ก่อนย้าย URL จะไม่ต้องเปลี่ยนอีก
- ngrok domain เปิดได้ทีละเครื่อง ต้องหยุด ngrok บนเครื่องเดิมก่อนเปิดที่เครื่องใหม่
