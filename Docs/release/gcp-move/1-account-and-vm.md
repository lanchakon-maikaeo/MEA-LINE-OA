# แผน 1: บัญชีและ VM (ระยะ 1)

> **ทำแล้ว 1 พ.ย. 2569:** project `MEA-DED-CENTER-LINE-OA` (ID `mea-ded-center-line-oa`, org `mea-amr2026-org`) ผูกกับ billing `0169F1-04067F-7B1870` ซึ่งมีเครดิต Free Trial ฿10,132.96 หมดอายุ 31 ม.ค. 2570 (ผ่าน G0) budget alert ตั้งที่ 50/80/95% ปิด SSH/RDP ที่เปิดให้ทุก IP แล้ว VM `faoj-prod-gcp` อยู่ที่ `asia-southeast3-a` เป็น `n4-custom-2-10240` boot 30 GB + data 150 GB (`google-faoj-data`, ยังไม่ได้ format) ทั้งสองลูกใช้ Hyperdisk ที่ 3000 IOPS / 140 MiB/s ซึ่งไม่มีค่าใช้จ่ายเพิ่ม IP `34.15.129.131` ดังนั้น URL ใหม่คือ `https://34-15-129-131.sslip.io/` และเข้า SSH ผ่าน IAP ได้แล้ว

**ใครทำ:** เจ้าของ **เวลา:** ~40 นาที **รอ:** ไม่ต้องรอแผนไหน

**ผลลัพธ์ที่ส่งต่อให้แผน 3:** `PROJECT`, `ZONE`, IP ภายนอก และ key `~/.ssh/faoj_gcp_ed25519` (ค่าเหล่านี้ไม่ใช่ secret ยกเว้น private key ซึ่งอยู่บน Mac เท่านั้น)

## ขั้นตอน

1. ☐ สมัคร Free Trial ที่ https://cloud.google.com/free ตอนสมัครธนาคารอาจกันวงเงินไว้ $0–1 เพื่อยืนยันบัตร ยอดนี้ไม่ใช่การเก็บเงิน และจะถูกคืนเอง
2. ☐ **ตรวจว่าเป็น Free Trial จริง (G0):** เปิด Console → Billing → Overview ต้องเห็นเครดิต $300 พร้อมจำนวนวันที่เหลือ และต้องมีปุ่ม "Activate" อยู่ (แปลว่ายังไม่ได้อัปเกรด) ถ้าเป็นบัญชีที่เคยใช้ trial ไปแล้ว Google อาจสร้าง billing account แบบเสียเงินให้แทน กรณีนี้**ห้ามสร้างอะไรเลย** ให้หยุดแผนแล้วกลับมาคุยกัน

   **ห้ามตัดบัตรเด็ดขาด:**
   - ห้ามกด "Activate", "Upgrade" หรือ "Activate full account" ทุกที่ ถ้ากด หลังเครดิตหมดบัตรจะถูกเก็บเงิน
   - ห้ามผูก project กับ billing account อื่นนอกจาก Free Trial
   - ห้ามใช้ Marketplace, GPU หรือ Windows (ช่วง trial ถูกบล็อกอยู่แล้ว)
   - ถ้าเครดิตหมดหรือครบ 90 วัน เครื่องจะหยุดเองโดยไม่มีการเก็บเงิน
3. ☐ ติดตั้ง gcloud: `brew install --cask google-cloud-sdk` แล้ว `gcloud auth login`
4. ☐ สร้าง project ผูก billing และเปิด API

   ```bash
   PROJECT=faoj-prod-$(date +%m%d)
   gcloud projects create $PROJECT --name="MEA FAOJ"
   gcloud billing accounts list
   gcloud billing projects link $PROJECT --billing-account=<ACCOUNT_ID>
   gcloud config set project $PROJECT
   gcloud services enable compute.googleapis.com iap.googleapis.com
   ```

5. ☐ ตั้ง budget alert ใน Billing → Budgets & alerts วงเงิน $300 แจ้งเตือนที่ 50% และ 80% ส่งเข้าอีเมลเจ้าของ
6. ☐ เช็กราคาของ `n4-custom-2-10240` + Hyperdisk Balanced 180 GB + static IP ใน Pricing Calculator ยอดรวม 90 วันต้องไม่เกิน $300
7. ☐ **เช็ก quota (G1)** ต้องได้ N4 CPU ≥ 2, Hyperdisk ≥ 180 GB และ IN_USE_ADDRESSES ≥ 1 ใน `asia-southeast3`

   ```bash
   gcloud compute regions describe asia-southeast3 --flatten=quotas \
     --format="table(quotas.metric,quotas.limit,quotas.usage)" | grep -E 'N4|^CPUS|HYPERDISK|IN_USE_ADDRESSES'
   gcloud compute machine-types list --filter="name=n4-standard-2 AND zone~asia-southeast3"
   ```

   ถ้าไม่พอ (ช่วงระหว่าง trial ขอเพิ่ม quota ไม่ได้) ให้ใช้ `REGION=asia-southeast1 ZONE=asia-southeast1-b TYPE=e2-custom-2-10240 DISK=pd-balanced` แทน

8. ☐ ทำ network ให้ปลอดภัย: เปิด 80/443 ให้เว็บ และเปิด SSH ให้เฉพาะช่วง IP ของ IAP

   ```bash
   gcloud compute firewall-rules delete default-allow-ssh default-allow-rdp --quiet
   gcloud compute firewall-rules create faoj-web --allow=tcp:80,tcp:443 --target-tags=faoj
   gcloud compute firewall-rules create faoj-iap-ssh --allow=tcp:22 \
     --source-ranges=35.235.240.0/20 --target-tags=faoj
   ```

9. ☐ จอง IP และสร้าง SSH key

   ```bash
   REGION=asia-southeast3 ZONE=asia-southeast3-a TYPE=n4-custom-2-10240 DISK=hyperdisk-balanced
   gcloud compute addresses create faoj-web --region=$REGION
   ssh-keygen -t ed25519 -f ~/.ssh/faoj_gcp_ed25519 -C faoj-gcp -N ''
   ```

10. ☐ สร้าง VM: boot 30 GB (Docker image อยู่บน boot) และ data 150 GB (`/srv/faoj`) data disk ตั้ง `auto-delete=no` ไว้ เครื่องเปิด deletion protection

   ```bash
   gcloud compute instances create faoj-prod-gcp --zone=$ZONE --machine-type=$TYPE \
     --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
     --boot-disk-size=30GB --boot-disk-type=$DISK \
     --create-disk=name=faoj-prod-gcp-data,size=150GB,type=$DISK,device-name=faoj-data,auto-delete=no \
     --address=faoj-web --tags=faoj --deletion-protection \
     --metadata=block-project-ssh-keys=TRUE,ssh-keys="ubuntu:$(cat ~/.ssh/faoj_gcp_ed25519.pub)"
   gcloud compute addresses describe faoj-web --region=$REGION --format='value(address)'
   ```

11. ☐ ทดลองเข้าเครื่อง: `gcloud compute ssh ubuntu@faoj-prod-gcp --zone=$ZONE --tunnel-through-iap --ssh-key-file ~/.ssh/faoj_gcp_ed25519 --command 'free -g; lsblk'` ต้องเห็นแรมราว 10 GB และดิสก์ 150 GB

## เสร็จเมื่อ

เข้า SSH ผ่าน IAP ได้, แรมและดิสก์ตรงตามสเปค และ budget alert ตั้งเสร็จ จากนั้นส่งค่า `PROJECT`, `ZONE` และ IP ให้ผู้ช่วย

## ถ้าพัง

- **บัตรถูกปฏิเสธ:** ลองบัตรอื่น ระหว่างนี้ E5 ยังให้บริการอยู่จนถึง 7 พ.ย.
- **Zone `-a` ไม่มี N4:** ลอง `-b` หรือ `-c`
- **ดิสก์ไม่พอทีหลัง:** ขยายได้โดยไม่ต้องหยุดเครื่อง ด้วย `gcloud compute disks resize` แล้ว `resize2fs`
