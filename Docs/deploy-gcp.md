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

## จากโค้ดถึง production

| ขั้น | ใครทำ | ใช้เวลา (รอบล่าสุด) |
|---|---|---|
| 1. แก้โค้ดบน branch `dev` แล้วรัน `make ci` บน Mac | ผู้พัฒนา | ขึ้นกับเครื่อง |
| 2. push `dev` แล้วเปิด PR จาก `dev` เข้า `main` | ผู้พัฒนา | ทันที |
| 3. CI ของ PR | GitHub Actions | 24 ถึง 26 นาที |
| 4. merge PR ด้วย Create a merge commit | เจ้าของ | ทันที |
| 5. CI ของ `main` ใช้ผลของ PR ซ้ำ | GitHub Actions | ไม่ถึง 1 นาที |
| 6. Release สร้าง image สแกน และติด tag | GitHub Actions | 10 ถึง 48 นาที |
| 7. deploy digest ของ Release บน GCP | ผู้พัฒนา เมื่อเจ้าของสั่ง | ราว 3 นาที ระบบหยุดรับงาน 75 วินาที |
| 8. ตรวจหลัง deploy | ผู้พัฒนา | 1 ถึง 2 นาที |

## 1. แก้โค้ดและ push

งานทั้งหมดทำบน branch `dev` ก่อน push ให้รัน CI ชุดเดียวกับบน GitHub ในเครื่อง

```bash
make ci
```

`make ci` รัน 5 ขั้นตามลำดับ คือ `migrate`, `check`, `security`, `test` และ `test-e2e` แล้วเขียนผลแต่ละขั้นไว้ใน `reports/` ขั้น `security` อยู่ก่อนชุดทดสอบยาว ถ้ามี secret หลุดเข้า commit จะรู้ภายในนาทีแรก ผ่านครบแล้วจึง push

```bash
git push origin dev
```

## 2. เปิด PR และ merge

```bash
gh pr create --base main --head dev --title "<สรุปสั้น>" --body "<มีอะไรเปลี่ยน และตรวจอะไรแล้ว>"
```

CI บน GitHub รัน `make ci` ขั้นเดียวกันและเก็บรายงานไว้ใน artifact `foundation-reports` ถ้า push เข้า `dev` อีกระหว่างที่ CI ของ PR ยังรันอยู่ รอบเดิมจะถูกยกเลิกและเริ่มใหม่ จึงควรรวมงานให้เสร็จก่อน push

เจ้าของเป็นคน merge ในหน้า PR ให้เลือก **Create a merge commit** CI ของ `main` จะเห็นว่าโค้ดตรงกับที่ PR ผ่านมาแล้ว จึงไม่รันซ้ำและ Release เริ่มได้ภายในนาทีเดียว ถ้าเลือก Squash หรือ Rebase CI จะรันใหม่ทั้งชุดอีกราว 25 นาทีก่อน Release เริ่ม

## 3. Release

Release workflow เริ่มเองเมื่อ CI ของ `main` ผ่าน ขั้นตอนคือ

1. build image `faoj/db` และ `faoj/app` ทั้ง amd64 และ arm64 แล้ว push ขึ้น OCIR ตาม digest
2. สแกน app และ db ด้วย Trivy ถ้าพบช่องโหว่ระดับ HIGH หรือ CRITICAL ที่มีตัวแก้แล้ว หรือพบ secret รุ่นนั้นจะไม่ผ่าน image ของผู้อื่นที่ pin ไว้ (n8n, ClamAV, Caddy, backup) สแกนแล้วรายงานอย่างเดียว
3. ทำ SBOM และ `release.json` ซึ่งมี `app_image` (digest ที่ใช้ deploy) และ `release_ok`
4. ติด tag `faoj/app:main` เฉพาะรุ่นที่สแกนผ่าน

ก่อน deploy ให้อ่าน digest และผลสแกนจาก artifact `release-<commit>`

```bash
id=$(gh run list --workflow release.yml --branch main --limit 1 --json databaseId --jq '.[0].databaseId'); gh run watch "$id" --exit-status && d=$(mktemp -d) && gh run download "$id" -D "$d" && python3 -c 'import json,glob,sys; m=json.load(open(glob.glob(sys.argv[1]+"/release-*/release.json")[0])); print(m["app_image"], "release_ok:", m["release_ok"])' "$d"
```

`release_ok` ต้องเป็น `True` ถ้าเป็น `False` ห้าม deploy ให้ดูตารางผลสแกนในหน้าสรุปของ Release run แก้แล้วเปิด PR ใหม่ ทุกวันจันทร์ 03:00 workflow `scheduled.yml` จะสแกน release ของ `main` ล่าสุดซ้ำ เพื่อจับช่องโหว่ที่ประกาศหลังวันที่ build

## 4. Deploy บน GCP

ใช้ได้เฉพาะ image ที่ Release workflow สร้างและสแกนแล้ว ห้ามใช้ image ที่ build บนเครื่องหรือบน Mac

1. Deploy โดยใส่ว่าใครอนุมัติเมื่อไร ข้อความนี้จะอยู่ใน release record

   ```bash
   GCP_APPROVAL="owner in chat <วันที่>: <PR หรือเหตุผล>" deploy/gcp-move.sh deploy ap-singapore-1.ocir.io/axif86tn0tbj/faoj/app@sha256:<digest>
   ```

   คำสั่งนี้รัน `faoj` ของรุ่นใหม่ด้วย `systemd-run` บนเครื่อง ถ้า IAP หลุดกลางทาง deploy ก็ยังทำต่อจนเสร็จ ขั้นตอนบนเครื่องคือ backup, เปิด maintenance, migrate, นำเข้า n8n workflow ของรุ่นใหม่, สลับ container และ smoke ถ้า smoke ไม่ผ่าน ระบบจะกลับไปใช้รุ่นก่อนเอง

2. ติดตามผล

   ```bash
   deploy/gcp-move.sh ssh 'sudo tail -f /srv/faoj/run/deploy-<12 ตัวแรกของ digest>.log'
   ```

   บรรทัด "OCIR login with the instance principal failed; using Docker's saved login" เป็นเรื่องปกติบน GCP เพราะเครื่องนี้ไม่มี instance principal ของ OCI และดึง image ด้วย login ที่บันทึกไว้ deploy เสร็จเมื่อ log จบด้วย `deployed <digest>`

3. ตรวจหลัง deploy

   ```bash
   deploy/gcp-move.sh status
   ```

   ```bash
   curl -s https://dedcenter-381083094942.asia-southeast3.run.app/health/ready/
   ```

   ต้องได้ maintenance `off` ทุก service ทำงาน และ `/health/ready/` ตอบ `ok` worker ที่เพิ่งเริ่มจะเผยแพร่ Rich Menu ของรุ่นใหม่ทันที

`faoj auto-promote` และ `faoj auto-deploy` ปิดอยู่บน GCP ระบบจึงไม่รับรุ่นใหม่เอง ทุกรุ่นต้อง deploy ด้วยมือตามขั้นข้างบน

## ย้อนรุ่น

```bash
deploy/gcp-move.sh ssh 'sudo faoj rollback'
```

คำสั่งนี้กลับไปรุ่นก่อนหน้าที่ deploy สำเร็จ ถ้าต้องการรุ่นที่เจาะจง ให้ใส่ digest ต่อท้าย (`sudo faoj rollback <image@sha256:…>`) ดูรายการรุ่นได้จาก `sudo faoj releases` การย้อนรุ่นเปลี่ยนแค่ image ไม่ย้อน migration และไม่ restore ฐานข้อมูล ถ้า migration ทำข้อมูลเสียให้ใช้ RB05 ใน `PROJECT.md` ส่วน E.9

## Flow ของ LINE

การสนทนาใน LINE มาจากสามที่ และแต่ละที่เปลี่ยนคนละทาง

| ส่วน | อยู่ที่ไหน | เปลี่ยนอย่างไร |
|---|---|---|
| flow ฝั่งผู้แจ้ง (ปุ่มใน Rich Menu, ปัญหาและข้อเสนอแนะ, เรื่องของฉัน) | ข้อมูลในฐานข้อมูล ([ADR 0023](adr/0023-requester-flows-are-data-published-from-the-admin-web.md)) | แอดมินแก้และเผยแพร่เองในหน้าแอดมิน ไม่ต้อง deploy |
| การลงทะเบียน การยืนยันตัวตน และงานของเจ้าหน้าที่ | โค้ด | แก้โค้ด แล้วทำตามขั้น 1 ถึง 4 |
| n8n workflow | ไฟล์ JSON ใน `workflows/n8n/` ซึ่งอยู่ใน image | แก้ไฟล์ แล้วทำตามขั้น 1 ถึง 4 การ deploy นำเข้าให้เองและ restart n8n |

ขั้นตอนแก้ flow ฝั่งผู้แจ้ง

1. เปิด `https://dedcenter-381083094942.asia-southeast3.run.app/console/admin/flows/` ด้วยบัญชีแอดมิน แล้วเลือก flow
2. แก้ร่างบนผัง ทุกครั้งที่บันทึก ระบบจะตรวจร่างและบอกจุดที่ต้องแก้
3. กด "ลองคุย" เพื่อเล่นร่างเหมือนคุยใน LINE
4. กดเผยแพร่และใส่เหตุผล ระบบไม่ให้เผยแพร่ถ้ายังมีจุดที่ต้องแก้ รุ่นก่อนหน้ายังเก็บไว้ และการเผยแพร่ทุกครั้งบันทึกใน audit

ผู้แจ้งที่คุยค้างอยู่ยังใช้ flow รุ่นเดิมจนจบหรือจนร่างหมดอายุใน 7 วัน ถ้าเปลี่ยนหัวข้อหรือช่องที่ AI อ่าน ต้องประเมิน AI ใหม่ ระหว่างนั้นแชตจะถามทีละขั้น

flow ตั้งต้นเก็บเป็นไฟล์ใน `operations/fixtures/` ถ้าต้องการนำไฟล์เข้าเป็นร่าง (ไม่แตะรุ่นที่เผยแพร่อยู่) ใช้

```bash
deploy/gcp-move.sh manage seed_line_flows --replace-draft
```

## Rich Menu ของ LINE

Rich Menu มี 5 หน้า (`user`, `employee-report`, `sup-report`, `sup-work`, `sup-order`) ระบบเลือกหน้าตามสิทธิ์ของแต่ละบัญชี ปุ่มและหัวข้อกำหนดในโค้ดที่ `operations/line_menu_catalog.py` ส่วนภาพของแต่ละหน้าอยู่ใน `operations/line_assets/` พร้อม `menu-manifest.json` ที่ผูกภาพกับโค้ดรุ่นนั้น ไม่มีหน้าเว็บสำหรับแก้เมนู

ขั้นตอนแก้ปุ่มหรือภาพ

1. แก้ `operations/line_menu_catalog.py` (หรือส่วนวาดใน `operations/line_menu.py`)
2. สร้างภาพใหม่บน Mac คำสั่งนี้ใช้ Playwright browser ของเครื่องพัฒนา และไม่เรียก LINE

   ```bash
   python3 -m uv run --locked --env-file .env python manage.py build_line_menu_images
   ```

3. commit ภาพและ `menu-manifest.json` พร้อมโค้ด แล้วทำตามขั้น 1 ถึง 4
4. หลัง deploy worker จะเผยแพร่เมนูทันทีที่เริ่ม และตรวจซ้ำทุก 5 นาที ถ้าภาพไม่ตรงกับโค้ด worker จะไม่อัปโหลด และ log ของ worker จะมีข้อความ "ภาพเมนูไม่ตรงรุ่นโปรแกรม ต้อง build_line_menu_images ก่อนปล่อยรุ่น"

ถ้าเมนูใน LINE ไม่ตรงกับรุ่นที่รันอยู่ (เช่น การเผยแพร่ค้างกลางทาง) ให้เผยแพร่ซ้ำโดยไม่ต้อง deploy คำสั่งนี้ทำต่อจากจุดที่ค้าง

```bash
deploy/gcp-move.sh line-menu
```

ปุ่ม Broadcast ในเมนูเปิด Mini App ผ่าน `miniapp.line.me/<LIFF ID>` จึงไม่ต้องสร้างเมนูใหม่เมื่อ Endpoint ของ Mini App เปลี่ยน ดูผลการเผยแพร่ได้จาก log ของ worker

```bash
deploy/gcp-move.sh ssh 'sudo docker logs --since 15m faoj-worker-1 2>&1 | tail -30'
```

## งานย่อย

| งาน | คำสั่ง |
|---|---|
| รัน `manage.py` หนึ่งคำสั่ง | `deploy/gcp-move.sh manage <คำสั่ง> [args…]` |
| ปิดหรือเปิดระบบชั่วคราว | `deploy/gcp-move.sh ssh 'sudo faoj maintenance on'` (หรือ `off`) |
| ตั้ง Cloud Run ของเว็บเจ้าหน้าที่ใหม่ (เช่น IP ของเครื่องเปลี่ยน) | `deploy/gcp-move.sh staff-proxy` |

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
