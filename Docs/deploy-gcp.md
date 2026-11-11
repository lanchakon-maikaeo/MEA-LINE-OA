# คู่มือ deploy และดูแล production บน GCP

ปรับปรุงวันที่ 3 พ.ย. 2569 production ย้ายจาก OCI E5 มาอยู่ที่ GCP ตั้งแต่ 2 พ.ย. 2569 เวลา 21:48 น. ตาม [ADR 0043](adr/0043-production-on-gcp-free-trial.md) ขั้นตอนตอนย้ายอยู่ใน [แผนย้าย](release/gcp-move/README.md) ส่วนเอกสารนี้ใช้กับงานประจำหลังย้าย

แต่ละขั้นในคู่มือมีคำสั่งที่ต้องรัน และย่อหน้า **ถ้าถูกต้อง** ที่บอกว่าควรเห็นอะไร ถ้าเห็นไม่ตรง ให้ไปดูส่วน [ปัญหาที่อาจเจอและวิธีแก้](#ปัญหาที่อาจเจอและวิธีแก้) ท้ายเอกสาร

## ระบบตอนนี้

| | ค่า |
|---|---|
| เครื่อง | `faoj-prod-gcp` (`n4-custom-2-10240`, 2 vCPU แรม 10 GB) ใน project `mea-ded-center-line-oa` โซน `asia-southeast3-a` IP 34.15.129.131 ข้อมูลอยู่ที่ `/srv/faoj` (ดิสก์ 150 GB) |
| เว็บเจ้าหน้าที่ | `https://dedcenter-381083094942.asia-southeast3.run.app/` เป็น Cloud Run ที่ส่งต่อไปยังเครื่อง ใช้ได้ในเครือข่าย กฟน. ส่วน `https://34-15-129-131.sslip.io/` เข้าเครื่องตรง แต่ FortiGate ของ กฟน. บล็อกไว้ |
| LINE webhook | `https://crabmeat-lake-lunar.ngrok-free.dev/webhooks/line` ผ่าน container ngrok บนเครื่อง เปิด Webhook redelivery ไว้แล้ว |
| Mini App | Endpoint (Published) คือ `https://dedcenter-381083094942.asia-southeast3.run.app/broadcasts/` |
| image | Artifact Registry `asia-southeast3-docker.pkg.dev/mea-ded-center-line-oa/faoj/app@sha256:…` จาก Release workflow เครื่องดึงด้วย service account ของ VM เอง (ไม่มี token เก็บไว้) ตั้งแต่ 11 พ.ย. 2569 เพราะ OCIR ไม่รับ push หลังบัญชี OCI กลายเป็น free tier |
| backup | pgBackRest ไป OCI Object Storage ที่ path `/pgbackrest/production-gcp` ส่วนไฟล์แนบและ n8n ไป restic repo เดียวกับที่ E5 ใช้ แยกกันด้วย host tag `production-gcp` |
| เครื่องสำรอง | Mac (`~/faoj-standby`) ดึง WAL ของ `production-gcp` มาทุก 5 นาที |
| เครดิต | Free Trial ของ GCP หมดราวปลาย ม.ค. 2570 ห้ามกด Upgrade และห้ามผูก billing account อื่น ต้องย้ายออกให้ได้ภายในกลาง ม.ค. |

เจ้าของลบ VM `faoj-prod-e5` (E5) แล้วเมื่อ 5 พ.ย. 2569 จึงย้ายกลับ E5 (`gcp-move.sh reverse-move`) ไม่ได้อีก และ `168-107-70-170.sslip.io` เปิดไม่ได้แล้ว ที่ OCI ยังเก็บ data volume `faoj-prod-e5-data` (ฐานข้อมูล E5 ตอน freeze), boot volume, reserved IP 168.107.70.170, bucket `faoj-backup` และ `faoj-files` (GCP ยัง backup ลงที่นี่) และ OCIR (image ก่อน 11 พ.ย. 2569 รวมรุ่นที่ใช้ rollback) ห้ามลบของเหล่านี้

## ก่อนเริ่ม

1. เจ้าของรัน `deploy/gcp-move.sh login` บน Mac เบราว์เซอร์จะเปิดหน้า login ของ Google ให้เลือกบัญชี `mea.amr2026@gmail.com` แล้วกด Allow จากนั้นคำสั่งของ `gcp-move.sh` จะทำงานในนามของ service account `faoj-deployer` ได้ราว 55 นาที เกินนั้นต้อง `login` ใหม่ ถ้าเสร็จงานก่อนให้รัน `deploy/gcp-move.sh logout`

   บัญชีของเจ้าของเป็น Owner ของโปรเจกต์ `login` จึงไม่เก็บ token ของบัญชีนี้ไว้ แต่ขอ token อายุ 1 ชั่วโมงของ `faoj-deployer` แล้วยกเลิก token ของเจ้าของทันที `faoj-deployer` ssh ผ่าน IAP และ deploy Cloud Run ได้ แต่แก้ IAM ไม่ได้ เมื่อครบชั่วโมง Google ปิด token เอง สคริปต์ agent หรือมัลแวร์บน Mac จะเข้า production ไม่ได้อีกจนกว่าเจ้าของจะ login ใหม่ ส่วน `logout` ลบแค่ไฟล์ token บน Mac ตัว token ที่ Google ยังใช้ได้จนครบชั่วโมง

   อย่ารัน `gcloud auth login` ตรงๆ เพราะจะเก็บ refresh token ของ Owner ไว้บน Mac ถาวร ถ้าจำเป็นต้องใช้ ให้รัน `gcloud auth revoke --all` ทันทีที่เสร็จ

   `faoj-deployer` สร้างไว้แล้วเมื่อ 5 พ.ย. 2569 ด้วย `deploy/gcp-move.sh deployer-setup` ถ้าต้องสร้างใหม่หรือเพิ่มสิทธิ์ เจ้าของรันคำสั่งนี้เอง คำสั่งจะ login ผ่านเบราว์เซอร์ ตั้งสิทธิ์ แล้วยกเลิก token ของเจ้าของเมื่อจบ สิทธิ์ใหม่ใช้เวลา 1 ถึง 2 นาทีกว่าจะมีผล ถ้า login เร็วกว่านั้นจะได้ `no token for faoj-deployer`
2. ทุกคำสั่งรันจาก repository บน Mac ผ่าน `deploy/gcp-move.sh` ซึ่งเข้าเครื่องผ่าน IAP ด้วย key `~/.ssh/faoj_gcp_ed25519` เครื่องไม่เปิดพอร์ต SSH สู่อินเทอร์เน็ต และไม่ต้องแก้ allow list เมื่อ IP ของ Mac เปลี่ยน (ต่างจาก Bastion ของ E5)
3. ถ้าต้องการ shell บนเครื่อง ใช้ `deploy/gcp-move.sh ssh` ถ้าต้องการรันคำสั่งเดียว ใช้ `deploy/gcp-move.sh ssh '<คำสั่ง>'` คำสั่ง `faoj` ต้องใช้ `sudo`

ทดสอบว่าเข้าเครื่องได้

```bash
deploy/gcp-move.sh status
```

**ถ้าถูกต้อง** จะเห็นรายชื่อ service ครบ 7 ตัว และบรรทัดสุดท้ายเป็น `{"status": "ok"}` ส่วนบรรทัด `WARNING:` ที่ว่างอยู่ด้านบนมาจาก gcloud ไม่มีผลอะไร

```
env: production  host: 34-15-129-131.sslip.io  maintenance: off
current: ec9a92d9f70e
SERVICE   STATUS
caddy     Up 8 hours
clamav    Up 17 hours (healthy)
db        Up 8 hours (healthy)
n8n       Up 8 hours (healthy)
ngrok     Up 10 hours
web       Up 8 hours (healthy)
worker    Up 8 hours (healthy)
off
{"status": "ok"}
```

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

`make ci` รัน 5 ขั้นตามลำดับ คือ `migrate`, `check`, `security`, `test` และ `test-e2e` แล้วเขียนผลแต่ละขั้นไว้ใน `reports/` ขั้น `security` อยู่ก่อนชุดทดสอบยาว ถ้ามี secret หลุดเข้า commit จะรู้ภายในนาทีแรก

**ถ้าถูกต้อง** ทุกขั้นขึ้น `PASS`

```
migrate: PASS
check: PASS
security: PASS
test: PASS
test-e2e: PASS
```

ผ่านครบแล้วจึง push

```bash
git push origin dev
```

## 2. เปิด PR และ merge

```bash
gh pr create --base main --head dev --title "<สรุปสั้น>" --body "<มีอะไรเปลี่ยน และตรวจอะไรแล้ว>"
```

**ถ้าถูกต้อง** คำสั่งจะพิมพ์ลิงก์ของ PR และในหน้า PR จะมี check ชื่อ `foundation` ของ CI กำลังรัน CI รัน `make ci` ขั้นเดียวกันและเก็บรายงานไว้ใน artifact `foundation-reports` ถ้า push เข้า `dev` อีกระหว่างที่ CI ยังรันอยู่ รอบเดิมจะถูกยกเลิกและเริ่มใหม่ จึงควรรวมงานให้เสร็จก่อน push

เจ้าของเป็นคน merge ในหน้า PR ให้เลือก **Create a merge commit** CI ของ `main` จะเห็นว่าโค้ดตรงกับที่ PR ผ่านมาแล้ว จึงไม่รันซ้ำ ถ้าเลือก Squash หรือ Rebase CI จะรันใหม่ทั้งชุดอีกราว 25 นาทีก่อน Release เริ่ม

**ถ้าถูกต้อง** CI ของ `main` จบในไม่ถึงนาที และมีข้อความ notice ว่า

```
Tree <hash> passed every stage in pull request run <run id>; not tested again.
```

## 3. Release

Release workflow เริ่มเองเมื่อ CI ของ `main` ผ่าน ขั้นตอนคือ

1. build image `faoj/db` และ `faoj/app` ทั้ง amd64 และ arm64 แล้ว push ขึ้น Artifact Registry `faoj` ตาม digest โดย login เป็น `faoj-ci` ผ่าน Workload Identity Federation (ไม่มี key ใน GitHub) ซึ่งเชื่อเฉพาะ branch `main` ของ repo นี้
2. สแกน app และ db ด้วย Trivy ถ้าพบช่องโหว่ระดับ HIGH หรือ CRITICAL ที่มีตัวแก้แล้ว หรือพบ secret รุ่นนั้นจะไม่ผ่าน image ของผู้อื่นที่ pin ไว้ (n8n, ClamAV, Caddy, backup) สแกนแล้วรายงานอย่างเดียว
3. ทำ SBOM และ `release.json` ซึ่งมี `app_image` (digest ที่ใช้ deploy) และ `release_ok`
4. ติด tag `faoj/app:main` เฉพาะรุ่นที่สแกนผ่าน

ก่อน deploy ให้อ่าน digest และผลสแกนจาก artifact `release-<commit>`

```bash
id=$(gh run list --workflow release.yml --branch main --limit 1 --json databaseId --jq '.[0].databaseId'); gh run watch "$id" --exit-status && d=$(mktemp -d) && gh run download "$id" -D "$d" && python3 -c 'import json,glob,sys; m=json.load(open(glob.glob(sys.argv[1]+"/release-*/release.json")[0])); print(m["app_image"], "release_ok:", m["release_ok"])' "$d"
```

**ถ้าถูกต้อง** จะได้ digest หนึ่งบรรทัด ตามด้วย `release_ok: True` และหน้าสรุปของ Release run เขียนว่า `Blocking scans: passed`

```
asia-southeast3-docker.pkg.dev/mea-ded-center-line-oa/faoj/app@sha256:ec9a92d9f70e… release_ok: True
```

ทุกวันจันทร์ 03:00 workflow `scheduled.yml` จะสแกน release ของ `main` ล่าสุดซ้ำ เพื่อจับช่องโหว่ที่ประกาศหลังวันที่ build

## 4. Deploy บน GCP

ใช้ได้เฉพาะ image ที่ Release workflow สร้างและสแกนแล้ว ห้ามใช้ image ที่ build บนเครื่องหรือบน Mac

1. Login (ถ้ายังไม่ได้ login หรือเกินเวลาแล้ว) จากนั้น deploy โดยใส่ว่าใครอนุมัติเมื่อไร ข้อความนี้จะอยู่ใน release record

   ```bash
   deploy/gcp-move.sh login
   ```

   ```bash
   GCP_APPROVAL="owner in chat <วันที่>: <PR หรือเหตุผล>" deploy/gcp-move.sh deploy asia-southeast3-docker.pkg.dev/mea-ded-center-line-oa/faoj/app@sha256:<digest>
   ```

   คำสั่งนี้รัน `faoj` ของรุ่นใหม่ด้วย `systemd-run` บนเครื่อง ถ้า IAP หลุดกลางทาง deploy ก็ยังทำต่อจนเสร็จ ขั้นตอนบนเครื่องคือ backup, เปิด maintenance, migrate, นำเข้า n8n workflow ของรุ่นใหม่, สลับ container และ smoke ถ้า smoke ไม่ผ่าน ระบบจะกลับไปใช้รุ่นก่อนเอง

2. ติดตามผล

   ```bash
   deploy/gcp-move.sh ssh 'sudo tail -f /srv/faoj/run/deploy-<12 ตัวแรกของ digest>.log'
   ```

   **ถ้าถูกต้อง** log จะเดินตามลำดับนี้ และจบด้วย `deployed <digest>` (กด Ctrl+C เพื่อออกจาก `tail`) 

   ```
   [17:20:12] release ec9a92d9f70e (previous: 7359950aa629)
   [17:20:19] maintenance ON (Caddy answers 503 with the fallback channels)
   [17:20:22] restore point: pgBackRest incremental backup
   [...] migrate (owner role, one job) and narrow runtime grants
   [17:21:34] maintenance OFF
   edge smoke ok
   [17:21:34] deployed ec9a92d9f70e
   ```

3. ตรวจหลัง deploy

   ```bash
   deploy/gcp-move.sh status
   ```

   ```bash
   curl -s https://dedcenter-381083094942.asia-southeast3.run.app/health/ready/
   ```

   **ถ้าถูกต้อง** `current:` เป็น 12 ตัวแรกของ digest ใหม่ maintenance เป็น `off` ทุก service ขึ้น `Up` และ `curl` ได้ `{"status": "ok"}` worker ที่เพิ่งเริ่มจะเผยแพร่ Rich Menu ของรุ่นใหม่ทันที

4. Logout เมื่อเสร็จ

   ```bash
   deploy/gcp-move.sh logout
   ```

`faoj auto-promote` และ `faoj auto-deploy` ปิดอยู่บน GCP ระบบจึงไม่รับรุ่นใหม่เอง ทุกรุ่นต้อง deploy ด้วยมือตามขั้นข้างบน

## ย้อนรุ่น

```bash
deploy/gcp-move.sh ssh 'sudo faoj rollback'
```

คำสั่งนี้กลับไปรุ่นก่อนหน้าที่ deploy สำเร็จ ถ้าต้องการรุ่นที่เจาะจง ให้ใส่ digest ต่อท้าย (`sudo faoj rollback <image@sha256:…>`) ดูรายการรุ่นได้จาก `sudo faoj releases` การย้อนรุ่นเปลี่ยนแค่ image ไม่ย้อน migration และไม่ restore ฐานข้อมูล ถ้า migration ทำข้อมูลเสียให้ใช้ RB05 ใน `PROJECT.md` ส่วน E.9

**ถ้าถูกต้อง** คำสั่งจบด้วย `rolled back` และ `deploy/gcp-move.sh status` แสดง `current:` เป็นรุ่นเดิม

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

**ถ้าถูกต้อง** หน้าเว็บขึ้นว่า "เผยแพร่ <ชื่อ flow> รุ่น <เลข> แล้ว" และผู้แจ้งที่เริ่มคุยหลังจากนั้นจะได้ flow รุ่นใหม่ ผู้แจ้งที่คุยค้างอยู่ยังใช้รุ่นเดิมจนจบหรือจนร่างหมดอายุใน 7 วัน ถ้าเปลี่ยนหัวข้อหรือช่องที่ AI อ่าน ต้องประเมิน AI ใหม่ ระหว่างนั้นแชตจะถามทีละขั้น

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
4. หลัง deploy worker จะเผยแพร่เมนูทันทีที่เริ่ม และตรวจซ้ำทุก 5 นาที

**ถ้าถูกต้อง** เปิด LINE OA บนโทรศัพท์แล้วเห็นเมนูใหม่ตามสิทธิ์ของบัญชีนั้น (ถ้ายังเห็นเมนูเดิม ให้ปิดแล้วเปิดห้องแชตใหม่) และ log ของ worker ไม่มีข้อความ "Automatic LINE menu update incomplete"

```bash
deploy/gcp-move.sh ssh 'sudo docker logs --since 15m faoj-worker-1 2>&1 | tail -30'
```

ถ้าเมนูใน LINE ไม่ตรงกับรุ่นที่รันอยู่ (เช่น การเผยแพร่ค้างกลางทาง) ให้เผยแพร่ซ้ำโดยไม่ต้อง deploy คำสั่งนี้ทำต่อจากจุดที่ค้าง

```bash
deploy/gcp-move.sh line-menu
```

**ถ้าถูกต้อง** จะเห็น `LINE menu synchronization completed (or outbound LINE is disabled).`

ปุ่ม Broadcast ในเมนูเปิด Mini App ผ่าน `miniapp.line.me/<LIFF ID>` จึงไม่ต้องสร้างเมนูใหม่เมื่อ Endpoint ของ Mini App เปลี่ยน

## งานย่อย

| งาน | คำสั่ง | ถ้าถูกต้อง |
|---|---|---|
| รัน `manage.py` หนึ่งคำสั่ง | `deploy/gcp-move.sh manage <คำสั่ง> [args…]` | ผลของคำสั่งนั้น เช่น `manage check` ได้ `System check identified no issues` |
| ปิดหรือเปิดระบบชั่วคราว | `deploy/gcp-move.sh ssh 'sudo faoj maintenance on'` (หรือ `off`) | `maintenance ON` หรือ `maintenance OFF` |
| ตั้ง Cloud Run ของเว็บเจ้าหน้าที่ใหม่ (เช่น IP ของเครื่องเปลี่ยน) | `deploy/gcp-move.sh staff-proxy` | `staff web: https://dedcenter-….run.app/` และค่า `CSRF_TRUSTED_ORIGINS` ที่มีชื่อ run.app |

## Backup และการเฝ้าดู

เวลาในตารางเป็นเวลาไทย เครื่องตั้งเวลาเป็น UTC

| งาน | เมื่อไร | ที่ไหน |
|---|---|---|
| WAL ของ Postgres | ตลอดเวลา | OCI Object Storage `/pgbackrest/production-gcp` |
| full backup | วันจันทร์ 03:00 | ที่เดียวกัน |
| diff backup | วันอังคารถึงอาทิตย์ 03:00 | ที่เดียวกัน |
| ไฟล์แนบและ n8n | ทุกวัน 03:30 | restic host `production-gcp` |
| standby บน Mac | ทุก 5 นาที | `~/faoj-standby` แจ้งเตือนบน Mac เมื่อตามหลังเกิน 15 นาที |
| healthcheck บน Mac | ทุกนาที | ตรวจ `34-15-129-131.sslip.io` และ LINE webhook ล้มติดกัน 3 ครั้งจะแจ้งเตือนบน Mac |
| สำเนานอก OCI | วันจันทร์ 09:00 | OneDrive โฟลเดอร์ `pgbackrest-production-gcp` และ `restic` |

ดูผลบนเครื่อง

```bash
deploy/gcp-move.sh ssh 'sudo faoj backup status 2>&1 | tail -1'
```

**ถ้าถูกต้อง** ตัวเลขต้องไม่เกิน 93,600 วินาที (26 ชั่วโมง) เพราะ backup รันทุกคืน

```
last database backup: 18294s ago
```

ดูผลบน Mac

```bash
deploy/standby.sh status
```

**ถ้าถูกต้อง** ฐานข้อมูลตามหลังไม่เกินราว 10 นาที

```
database: standby, 2 min 12 s behind production
files: last copy 2026-11-02 23:13
```

บน GCP ตั้ง `METRICS_SINK=stdout` ไว้ metric จึงไม่ขึ้น OCI Monitoring และ alarm ของ OCI ไม่เห็นเครื่องนี้ การเตือนตอนนี้มีแค่งานบน Mac กับ UptimeRobot ถ้า Mac ปิดอยู่ จะเหลือแค่ UptimeRobot ส่วน `faoj quota-check` เป็นงบของ OCI และปิดไว้บนเครื่องนี้

`faoj-restore-drill.timer` ปิดอยู่บน GCP การซ้อมกู้ต้องสั่งเอง (`sudo faoj restore-drill`) ระหว่างซ้อมระบบจะเข้า maintenance และหยุดชั่วคราว ครั้งล่าสุดวันที่ 2 พ.ย. ใช้เวลา 88 วินาที จึงควรซ้อมนอกเวลางาน

## ปัญหาที่อาจเจอและวิธีแก้

แต่ละตารางเรียงตามขั้นในคู่มือ ข้อความในคอลัมน์แรกคือสิ่งที่จะเห็นบนจอหรือใน log

### เข้าเครื่องไม่ได้

| เห็นอะไร | แปลว่า | แก้อย่างไร |
|---|---|---|
| `not signed in, or the hour is up` | ยังไม่ได้ login หรือเกินเวลาแล้ว | เจ้าของรัน `deploy/gcp-move.sh login` แล้วลองใหม่ |
| `no token for faoj-deployer` | ยังไม่ได้สร้าง `faoj-deployer` หรือสิทธิ์ยังไม่มีผล | เจ้าของรัน `deploy/gcp-move.sh deployer-setup` รอ 2 นาที แล้วรัน `login` ใหม่ |
| gcloud ขึ้น `Permission denied` หรือ `does not have … permission` ทั้งที่เพิ่ง login | `faoj-deployer` ขาดสิทธิ์สำหรับคำสั่งนั้น | ดูชื่อ permission ในข้อความ เพิ่ม role ใน `deployer_setup` ของ `gcp-move.sh` แล้วให้เจ้าของรัน `deployer-setup` ใหม่ |
| ssh ผ่าน IAP ค้างหรือต่อไม่ติด | IAP หรือเครื่องยังไม่พร้อม หรือเครื่องหยุด | ลองใหม่อีกครั้ง ถ้ายังไม่ได้ ดูสถานะเครื่องด้วย `gcloud compute instances list --project mea-ded-center-line-oa` |
| เครื่องหยุด (`TERMINATED` หรือ `SUSPENDED`) และทุกอย่างล่ม | เครดิต Free Trial อาจหมด | เปิดหน้า Billing ของ GCP ดูเครดิต ห้ามกด Upgrade และแจ้งเจ้าของทันที ข้อมูลยังอยู่ใน backup ที่ OCI และ standby บน Mac |

### CI

| เห็นอะไร | แปลว่า | แก้อย่างไร |
|---|---|---|
| ขั้นใดขั้นหนึ่งของ `make ci` ขึ้น `FAIL` | โค้ดมีปัญหาในขั้นนั้น | อ่าน `reports/<ขั้น>.log` บน Mac หรือดาวน์โหลด artifact `foundation-reports` ของ CI แก้แล้วรันใหม่ |
| CI ของ PR เป็น cancelled | มี push ใหม่เข้า `dev` ระหว่างที่ CI รัน | ไม่ต้องทำอะไร รอบใหม่เริ่มให้เองแล้ว |
| CI ของ `main` รันนาน 25 นาที | merge แบบ Squash หรือ Rebase | รอให้จบ ครั้งหน้าใช้ Create a merge commit |

### Release

| เห็นอะไร | แปลว่า | แก้อย่างไร |
|---|---|---|
| Release ไม่เริ่มหลัง merge | CI ของ `main` ไม่ผ่าน | ดู CI ของ `main` ก่อน แก้แล้วเปิด PR ใหม่ |
| notice `GAR_REGISTRY/GAR_REPOSITORY not set yet` | ตัวแปรของ repository หาย | เจ้าของรัน `deploy/gcp-move.sh registry-setup` แล้วรัน Release ใหม่ |
| `google-github-actions/auth` ล้ม (`unauthorized_client` หรือ `Permission 'iam.serviceAccounts.getAccessToken' denied`) | Release ไม่ได้รันจาก `main` หรือ IAM ยังไม่ครบ | รันจาก `main` เท่านั้น ถ้ายังล้ม เจ้าของรัน `deploy/gcp-move.sh registry-setup` ซ้ำ (รันซ้ำได้) |
| `release_ok: False` หรือ `Blocking scans: FAILED` | Trivy พบช่องโหว่ที่มีตัวแก้แล้ว หรือพบ secret ใน image | ห้าม deploy ดูตารางผลสแกนในหน้าสรุป อัปเดต package หรือ base image แล้วเปิด PR ใหม่ |

### Deploy

| เห็นอะไร | แปลว่า | แก้อย่างไร |
|---|---|---|
| `give the Release workflow's app image` | digest ไม่ได้อยู่ในรูป `asia-southeast3-docker.pkg.dev/mea-ded-center-line-oa/faoj/app@sha256:…` | คัดลอก `app_image` จากขั้น Release มาใส่ทั้งบรรทัด |
| `last database backup is …s old; run 'faoj backup diff' first` | backup ล่าสุดเก่ากว่า 26 ชั่วโมง ระบบไม่ยอม migrate | รัน `deploy/gcp-move.sh ssh 'sudo faoj backup diff'` แล้ว deploy ใหม่ ถ้า backup ไม่สำเร็จ ให้หยุดและหาสาเหตุก่อน |
| `another deploy holds …` | มี deploy อื่นค้างอยู่ | ดูว่ามี deploy รันอยู่จริงไหมด้วย `deploy/gcp-move.sh ssh 'systemctl list-units "faoj-deploy-*"'` ถ้ารันอยู่ให้รอ ลบ lock เฉพาะเมื่อแน่ใจว่าไม่มี deploy รัน |
| ดึง image ไม่ได้ (`unauthorized` หรือ `denied`) | service account ของ VM ไม่มีสิทธิ์อ่าน repo `faoj` | เจ้าของรัน `deploy/gcp-move.sh registry-setup` ซ้ำ แล้ว deploy ใหม่ (image เก่าบน OCIR ใช้ `pull-token` หรือ `ocir-login`) |
| `migration failed (RB07); maintenance stays on` | migration ล้ม ระบบกลับไปรุ่นเดิมแต่ยังปิดรับงาน | อ่าน log หาสาเหตุ ถ้ารุ่นเดิมทำงานปกติ ใช้ `sudo faoj maintenance off` แล้วแก้โค้ดในรอบหน้า ถ้าข้อมูลเสีย ใช้ RB05 |
| `smoke failed` แล้วตามด้วย `rolling back to …` | รุ่นใหม่ไม่ผ่าน smoke ระบบย้อนกลับเอง | ตรวจ `deploy/gcp-move.sh status` ว่ารุ่นเดิมกลับมาและ maintenance เป็น `off` แล้วหาสาเหตุจาก log ก่อนลองใหม่ |
| maintenance ยังเป็น `on` หลัง deploy จบ | deploy หรือ rollback ไม่ผ่านครบ | ดู `deploy/gcp-move.sh status` ถ้าทุก service `Up` และ `healthy` ให้ `sudo faoj maintenance off` ถ้าไม่ ใช้ `sudo faoj rollback` |

### หลัง deploy

| เห็นอะไร | แปลว่า | แก้อย่างไร |
|---|---|---|
| run.app ตอบช้าราว 1 วินาทีในครั้งแรก | Cloud Run เพิ่งปลุก instance (ตั้งให้เหลือ 0 ได้เมื่อไม่มีคนใช้) | ปกติ ไม่ต้องแก้ |
| เจ้าหน้าที่ใน กฟน. เปิด `sslip.io` ไม่ได้ และเห็นหน้า FortiGate | FortiGate บล็อก `sslip.io` เป็น Dynamic DNS | ใช้ URL run.app |
| login แล้วได้ 403 (CSRF) ผ่าน run.app | `CSRF_TRUSTED_ORIGINS` ไม่มีชื่อ run.app | รัน `deploy/gcp-move.sh staff-proxy` ใหม่ คำสั่งนี้เติมค่าให้และ restart web กับ worker |
| LINE webhook ได้ 400 | `ALLOWED_HOSTS` ใน `django.env` ไม่มีชื่อ ngrok (เกิดตอนย้ายวันที่ 2 พ.ย.) | เติม `crabmeat-lake-lunar.ngrok-free.dev` ใน `ALLOWED_HOSTS` แล้ว recreate web เปิด Webhook redelivery ไว้ LINE จึงส่งข้อความที่พลาดซ้ำให้ |
| webhook ตอบ 405 เมื่อเปิดด้วยเบราว์เซอร์ | webhook รับเฉพาะ POST | ปกติ |
| ทุกหน้าขึ้น 503 หน้าปิดปรับปรุง | maintenance ยังเปิดอยู่ | ดูแถว maintenance ในตาราง Deploy |

### LINE flow และ Rich Menu

| เห็นอะไร | แปลว่า | แก้อย่างไร |
|---|---|---|
| "ใส่เหตุผลก่อนเผยแพร่" | ช่องเหตุผลว่าง | ใส่เหตุผลแล้วกดเผยแพร่อีกครั้ง |
| "ยังมีจุดที่ต้องแก้ก่อนเผยแพร่" | ร่างยังไม่ผ่านการตรวจ | แก้ตามจุดที่ระบบบอกในหน้าแก้ flow แล้วบันทึก |
| "เผยแพร่ได้เฉพาะร่าง" | กำลังกดเผยแพร่รุ่นที่เผยแพร่แล้ว | เปิดร่างใหม่ก่อน |
| log ของ worker มี "ภาพเมนูไม่ตรงรุ่นโปรแกรม ต้อง build_line_menu_images ก่อนปล่อยรุ่น" | ภาพเมนูใน release ไม่ตรงกับโค้ด เมนูเดิมยังอยู่ใน LINE | รัน `build_line_menu_images` บน Mac, commit ภาพและ `menu-manifest.json` แล้วทำตามขั้น 1 ถึง 4 |
| log ของ worker มี "Automatic LINE menu update incomplete; retry in five minutes" | เผยแพร่เมนูไม่ครบ | worker ลองใหม่เองใน 5 นาที ถ้าอยากให้ทำทันที ใช้ `deploy/gcp-move.sh line-menu` |
| เมนูในโทรศัพท์ยังเป็นรุ่นเดิม | LINE ในโทรศัพท์อาจยังแสดงเมนูเดิมค้างไว้ | ปิดแล้วเปิดห้องแชต OA ใหม่ ถ้ายังไม่เปลี่ยน ดู log ของ worker |

### Backup และ standby

| เห็นอะไร | แปลว่า | แก้อย่างไร |
|---|---|---|
| Mac แจ้งเตือนว่า standby ตามหลังเกิน 15 นาที | Mac ดึง WAL ไม่ได้ หรือ production ส่ง WAL ขึ้น OCI ไม่ได้ | ดู `deploy/standby.sh status` ก่อน ถ้าเน็ตของ Mac ปกติ ให้ดูว่า Postgres บนเครื่องส่ง WAL ผ่านไหมด้วย `deploy/gcp-move.sh ssh 'sudo docker logs --since 30m faoj-db-1 2>&1 \| grep -i archive \| tail'` |
| Mac แจ้งเตือนว่าระบบล่ม | healthcheck ล้ม 3 ครั้งติดกัน | ดู `deploy/gcp-move.sh status` แล้วทำตาม RB01 ใน `PROJECT.md` ส่วน E.9 หน้าปิดปรับปรุง (503) ไม่นับเป็นล่ม |
| `last database backup` เกิน 26 ชั่วโมง | backup คืนที่ผ่านมาไม่สำเร็จ | ดู `deploy/gcp-move.sh ssh 'systemctl status faoj-backup-diff'` แล้วรัน `sudo faoj backup diff` เอง |

## เมื่อต้องย้ายออกจาก GCP

ต้องย้ายก่อนกลาง ม.ค. 2570 ไปเครื่องที่ไม่ต้องตัดบัตร (A1 Always Free หรือ VM ของ กฟน.) ใช้ขั้นตอนชุดเดียวกับ [แผนย้าย](release/gcp-move/README.md) และ RB08 ตอนย้ายมีสองอย่างที่ต้องเปลี่ยนตาม

- ชื่อ run.app ผูกกับ project GCP นี้ เจ้าหน้าที่และ Endpoint ของ Mini App ต้องเปลี่ยน URL อีกรอบ ถ้าได้ subdomain ของ กฟน. ก่อนย้าย URL จะไม่ต้องเปลี่ยนอีก
- ngrok domain เปิดได้ทีละเครื่อง ต้องหยุด ngrok บนเครื่องเดิมก่อนเปิดที่เครื่องใหม่
