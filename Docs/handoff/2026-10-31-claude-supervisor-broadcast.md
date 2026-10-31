# ส่งต่องานให้ Claude: Supervisor LINE และ Broadcast / Mini App

อัปเดต: 31 ตุลาคม 2026 (Asia/Bangkok)

## เป้าหมายและวิธีเริ่ม

เจ้าของสั่ง **ทำงานที่ค้างให้เสร็จทั้งหมด** ตาม `Docs/adr/0040-supervisor-line-work-and-broadcast.md` และอนุญาต **commit ลง dev** แล้ว เป้าหมายยังไม่สำเร็จ เอกสารนี้เป็นจุดส่งต่อเมื่อ Codex ติดลิมิต ไม่ใช่การตัด scope ให้เหลือสิ่งที่ทำแล้ว

เริ่มจากอ่าน ADR 0040, เอกสารนี้, `git status --short`, `git log -5 --oneline` และ diff ปัจจุบัน ใช้ worktree เดิม ตรวจว่ามี AGENTS.md หรือคำสั่งใหม่ก่อนแก้ไข สถานะไฟล์จริงและผลทดสอบล่าสุดมีน้ำหนักมากกว่าเอกสารนี้

ใช้แนวทาง ponytail: เปลี่ยนเท่าที่จำเป็นแต่ทำให้ครบ ตรวจสิทธิ์/validation/การส่งซ้ำจริง อย่าสร้างระบบขนานหรือเพิ่ม framework โดยไม่จำเป็น ไม่ต้องถามยืนยันข้อออกแบบที่เจ้าของตกลงแล้ว

## Workspace และ Git

- Repo: `/Users/lanchakon-maikaeo/Library/CloudStorage/OneDrive-MetropolitanElectricityAuthority/1.ศอค/Project/Chat-flow-working`
- Branch: `dev`
- HEAD ณ จุดส่งต่อ: `e83d156 Add supervisor inbox and broadcast delivery backend`
- ก่อนหน้า: `1431ab5 Restrict new work orders to department supervisors`
- ยังไม่ได้ push หรือ deploy และยังไม่ได้ส่ง Broadcast จริง
- Python ผ่าน `python3 -m uv run --locked --env-file .env ...`; Django 5.2, Python 3.13, PostgreSQL local Docker port 55432
- อย่าแสดง `.env`, token, ID token หรือข้อมูลผู้รับใน log/เอกสาร

**งานแก้เดิมของผู้ใช้ที่ห้ามทับ/เหมารวม commit:**

- `operations/conversation.py` hunk `_general_draft` ที่เพิ่ม `menu_label` (ส่วน inbox routing ของเรา commit แล้ว)
- `operations/problem_pick.py`
- `operations/problems_web.py`
- `operations/tests/test_m13_line_people.py`
- `operations/tests/test_m13_problems.py`
- `operations/tests/test_m25_web_access.py`
- `output/design/rich-menu-committed-f817870/` เป็นภาพเก่าที่ไม่ควรเพิ่มเข้า commit โดยอัตโนมัติ

## ข้อกำหนดที่ต้องรักษา

1. **เฉพาะ Supervisor ฝ่ายสร้าง “สั่งงาน”** ได้ ทั้งเว็บ LINE typed command ปุ่มเก่า ร่างค้าง และ backend ปุ่มอยู่ใน “เมนูเพิ่มเติม” และเมนูนั้นมีเฉพาะสั่งงาน ฟังก์ชันอื่นทำภายหลัง Supervisor กองยังรับและมอบหมายงานเดิมได้
2. ฝ่ายเห็น Case และ WorkOrder ของทุกกอง รวมปัญหา/ข้อเสนอแนะ; กองเห็นเฉพาะขอบเขตของตน ไม่ดึงงานจากชีทมาแทรก
3. งานสั่งที่ได้รับเป็นการ์ดแชต มีรายละเอียด รับเรื่อง ส่งต่อ เลือกกอง และยืนยัน ฝ่ายรับแทนกองได้ ไม่เปลี่ยนผู้รับผิดชอบเป็นผู้กดรับ ไม่รับเรื่องซ้ำ
4. ฝ่ายส่งต่อทันทีหลังยืนยัน ไม่ต้องให้ปลายทางตอบรับการโอน ล้างผู้รับผิดชอบเดิม ใช้ผู้รับผิดชอบตามหน่วยปลายทาง/รอมอบหมาย และเก็บประวัติเดิม การส่งต่อของกองแบบเดิมยังทำงานตามกฎเดิม
5. Supervisor ทั้งสองระดับส่ง Broadcast ได้เท่ากัน: เพื่อน OA ทั้งหมด หรือ Segment ตั้งเองได้ ไม่มีขั้นฝ่ายอนุมัติ
6. เจ้าของประกาศและฝ่ายแก้/ยกเลิกได้ Supervisor คนอื่นอ่านประวัติได้ ใช้ข้อมูล/สิทธิ์เดียวกันในเว็บและ Mini App
7. ข้อความ รูปภาพ วิดีโอ รูปกดลิงก์ การ์ดและปุ่ม; ร่าง ตัวอย่าง ส่งทดสอบถึงตนเอง ส่งทันที ตั้งเวลา ประวัติ ไม่ทำคูปอง/แบบสอบถาม
8. Segment คำนวณสมาชิก ณ เวลาส่งจริง ยอดก่อนส่งเป็นประมาณการ รวมผู้สร้างประกาศหนึ่งครั้งแม้อยู่นอก Segment ผู้สร้างรับประกาศจริงเหมือนผู้ใช้ทั่วไป แยกจากส่งทดสอบ
9. เริ่มแก้รายการตั้งเวลาแล้วต้องพักทันที จนกดยืนยันใหม่ ปิดหน้าไม่ทำให้ส่งต่อเอง
10. ตรวจและสำรองโควตาทั้งกลุ่มก่อนส่ง ไม่ตั้งใจส่ง subset เมื่อโควตาไม่พอ ผลไม่ครบ/ไม่ทราบผลต้องตรงความจริง retry เฉพาะส่วนไม่สำเร็จด้วย payload/recipient/retry key เดิม
11. หน้าจอ Mini App **เขียว–ขาว สวย ใช้ง่าย สุภาพทางการ** เว็บต้องสอดคล้องกัน
12. Rich Menu ตามรูปที่อนุมัติ: 2 tabs บริการ / งานของกอง/ฝ่าย; work มี 2 ช่อง งานสั่งที่ได้รับ + Broadcast และแถบล่าง เมนูเพิ่มเติม; services มี 6 ช่องเดิมและ เรื่องของฉัน / ติดตามเลขเรื่อง
13. อย่าเอา user ID/roles จาก client มาเชื่อ ต้อง verify LINE ID token ฝั่ง server และตรวจสิทธิ์ปัจจุบัน

## สิ่งที่ commit แล้ว

`1431ab5`: จำกัดสร้างสั่งงานเฉพาะฝ่ายทั้งเว็บ/แชต/backend และเอาแท็บสั่งงานออกจาก supervisor menu โดยรักษาสิทธิ์งานเดิมของกอง

`e83d156`:

- `staff_inbox.py`: SQL UNION Case/WorkOrder กรองสิทธิ์ก่อนแบ่งหน้า 5 รายการ ตัวกรองรอรับ/กำลังดำเนินการ/รอมอบหมาย/ทั้งหมด ฝ่ายเลือกกองได้ Flex list/detail
- `approvals.py`, `routing.py`, `web_actions.py`, `work_orders.py`: ฝ่ายรับทั้งเรื่องและส่งต่อ Case; supervisor รับ WorkOrder โดยไม่เปลี่ยน assignee; ไม่เปิดสิทธิ์อนุมัติ/ปฏิเสธคลังเกินขอบเขต
- `broadcasts.py`: Segment validation, draft/revision/editor rules, confirm/cancel/edit pause, content -> LINE payload
- `broadcast_delivery.py`: Outbox prepare/send batches, live quota, reserve all, retry immutable key, unknown cutoff, narrowcast polling, partial results
- `models.py`, migrations 0087/0088: campaigns, segments, batches + request result
- `notifications.py`: shared monthly quota lock/accounting with ordinary notifications, counts unknown/reserved conservatively
- `line_api.py`: stable retry key and HTTP 409 original accepted request ID
- worker/orchestration integration + regression tests
- ก่อน commit ทดสอบ supervisor/chat/broadcast/notification 67 รายการผ่าน, Ruff/diff/migration checks ผ่าน

## งานใหม่ใน worktree (ยังไม่ commit)

### Auth และ API / UI

- `operations/broadcast_auth.py`: POST `/broadcasts/auth/line/` verify token ที่ fixed official endpoint ด้วย `LINE_MINI_CHANNEL_ID`; session เฉพาะ Broadcast ไม่เกิน 1 ชั่วโมง; recheck linkage, active staff, roles, expiry, admin session revocation ทุกครั้ง; CSRF ไม่ยกเว้น; token ไม่ถูกเก็บ
- `config/settings.py`: `LINE_MINI_CHANNEL_ID`, `LINE_LIFF_ID` อ่าน env ยังไม่ได้ตั้งค่าจริง
- `operations/broadcast_web.py`: shared JSON endpoints bootstrap, campaigns list/save/detail, actions edit/cancel/confirm/preview/test/copy, segments; formal errors/no-store; ไม่คืน retry key หรือผู้รับรายคน
- `config/urls.py`: `/broadcasts/`, `/broadcasts/<id>/`, API และ auth routes
- `operations/templates/operations/broadcast.html`, static `broadcast.css`, `broadcast.js`: standalone green-white UI 4 steps, live preview, list/filter/pagination/detail, Segment form, draft/save/schedule/pause/cancel, LIFF login, protected staff web login fallback
- `operations/templates/operations/base.html`: link ประกาศข่าวสารใน drawer สำหรับผู้มี scope.overseen
- `operations/tests/test_broadcast_auth.py`: 11 tests ผ่านก่อนเพิ่ม test/copy actions (rerun ล่าสุดดูด้านล่าง)
- CSP ผ่อนเฉพาะหน้า Broadcast สำหรับ LINE SDK และ HTTPS media ไม่เปลี่ยน policy หน้าอื่น

### ส่งทดสอบ / หลายปุ่ม / copy (กำลังตรวจ)

- `models.py` + migration `0089_broadcast_test_sends.py`: test_source FK, unique nullable test_key UUID, test_revision
- `broadcasts.test_to_self`: lock source, verify editor/revision, snapshot แยก campaign ส่งถึง current sender เท่านั้น, duplicate key คืนงานเดิม, ไม่ confirm/แก้ร่างจริง, enqueue เดิม
- `recipients()` override สำหรับ test_source เป็น LINE identity ของผู้ส่งทดสอบเพียงรายเดียว จึงเข้า shared quota และ batch retry เดิม
- `broadcasts.copy_draft`: สร้าง draft ของผู้กด copy; ไม่คัดลอกเมื่อยังมี UNKNOWN batch เพื่อไม่ชวนส่งซ้ำโดยไม่รู้ผล
- UI เพิ่มส่งทดสอบ/ลิงก์สถานะ, แสดง badge “ทดสอบ”, copy draft, dynamic 1–3 card buttons โดยไม่ทิ้งปุ่มที่มีอยู่เมื่อเริ่มแก้
- `test_broadcasts.py` เพิ่ม test send snapshot/idempotency/one recipient/quota และ copy ownership/no send
- ต้องทดสอบ browser ส่วนที่เพิ่งเพิ่มและ audit API ครบก่อนถือว่าชุดนี้เสร็จ

## อัปเดตหลังทำเอกสารส่งต่อครั้งแรก

- ปรับ Rich Menu `sup-work` เป็น 2 ช่อง + extras เต็มความกว้าง และแท็บ งานของกอง/ฝ่าย แล้ว; `line_menu.py`, `line_menu.js`, `conversation.py`, tests และภาพ `sup-work/sup-report/sup-order.jpg` + manifest มี diff ใหม่
- URI ใช้ miniapp.line.me เมื่อตั้ง LIFF; fallback HTTPS web; postback guard ปฏิเสธ non-supervisor/สิทธิ์ถูกถอน
- `/tmp/broadcast-menu-tests.log`: 36 tests ผ่านเมื่อใช้ test DB ใหม่ (รอบแรก keepdb ล้มเหลวเพราะ seed ถูก flush จาก TransactionTestCase ก่อนหน้า ไม่ใช่ผลที่ยืนยันใช้ได้)
- ข้อ 3 ด้านล่างเปลี่ยนจาก “ต้อง implement” เป็นตรวจ integration/setup/live configuration ต่อ ห้ามทำซ้ำจากข้อความเก่า
- test campaign เก็บ audience/segment เดิมแล้ว แต่ recipients ยังคง override ให้ถึงผู้กดทดสอบเท่านั้น
- UI test request แก้ reset key เมื่อ API ปฏิเสธด้วย 4xx; ถ้าไม่ทราบผลให้ retry key เดิมและแจ้งผู้ใช้ก่อนส่งเนื้อหาใหม่

## งานที่ต้องทำต่อ ตามลำดับ

### 1. ปิดงานทดสอบและหลายปุ่มที่กำลังทำ

- ตรวจผล `/tmp/broadcast-handoff-tests.log` และรันใหม่เมื่อจำเป็น (process/session เก่าไม่ใช่หลักฐานว่ายังรันอยู่)
- ตรวจ test campaign ไม่มีช่องแก้/confirm ที่ทำให้กลายเป็นส่งเพื่อนทั้งหมด; recipient override ต้องอยู่ server เสมอ
- ทดสอบผู้กดทดสอบเป็นฝ่ายที่แก้ร่างของคนอื่น: สำเนาทดสอบไปผู้กด, ประกาศจริงยังรวมผู้สร้างเดิม
- UI retry หลัง network response หายใช้ request key เดิม; ถ้าผู้ใช้แก้ข้อความระหว่างรอผลต้องสื่อสารไม่ส่ง snapshot เก่าโดยไม่รู้ตัว
- API/worker tests ใหม่รวมกัน, Playwright หลายปุ่ม/test/copy/schedule; tests 11 เดิมไม่พิสูจน์ฟีเจอร์ใหม่
- พิจารณาเก็บ audience/segment เดิมใน test campaign เพื่อ copy กลับเป็นร่างได้ไม่สับสน (ปัจจุบัน test row default all แต่ส่งจริงถูก override ถึงตนเอง)

### 2. อัปโหลดสื่อจริง

ปัจจุบัน UI รับ HTTPS URL เท่านั้น ยังไม่ครบความต้องการอัปโหลด

- ทำ BroadcastMedia แยกจาก private case attachments ห้ามนำ private attachment URL มาเปิดสาธารณะ
- reuse `attachments.clamd_scan`/worker scan pattern; ตรวจชนิด/ขนาดจริง ไม่เชื่อ extension/MIME จาก client; clean ก่อน publish URL; quarantine/rejected ไม่เปิดอ่าน
- ภาพ JPEG/PNG และ MP4 (ตรวจ LINE limits จาก official docs ปัจจุบัน); video ต้องมี preview image ที่เหมาะสม
- stream upload/scan ไม่อ่านวิดีโอใหญ่ทั้งหมดเข้า RAM; ถ้าปรับ scanner ให้รับ filelike ต้องไม่พัง bytes callers เดิม
- public media ใช้ UUID URL, safe MIME, nosniff; storage และ API นับสิทธิ์/ขนาด/สถานะ; UI progress/poll scan; ห้าม confirm ระหว่าง pending
- ใช้ HTTPS public base ที่ตั้งค่าได้; localhost HTTP ใช้ทดสอบ UI เท่านั้น ไม่อ้างว่าส่งให้ LINE ดาวน์โหลดได้
- ตรวจ range video preview ถ้าจำเป็น + failure/retry UI

### 3. Rich Menu จริงตามแบบ

- ไฟล์หลัก: `operations/line_menu.py`, `operations/static/operations/line_menu.js`, command `build_line_menu_images.py`, `operations/line_assets/` + manifest, `line_menu_release.py`
- runtime `sup-work` เปลี่ยนเป็น 2 ช่องแล้วใน worktree (ดูอัปเดตด้านบน); staff-work และ legacy sup-order คงพฤติกรรมเดิม
- ปรับ work เป็น 2 ช่อง + extras ตามแบบ user; ปุ่ม Broadcast เปิด LIFF URL/เว็บ fallback ตาม config; role guards สำหรับ non-supervisor และ stale alias
- ใช้บริการ 6 tiles เดิม ห้ามทำ service flows เปลี่ยนโดยไม่ตั้งใจ
- แบบ round7 ที่ใกล้ถูกต้อง: `output/design/rich-menu-mockup-2026-10-31/staff-work-v3.jpg` และ `staff-report-v2.jpg`; `sup-work-v3` แบบเก่า 3 tabs ไม่ถูกต้อง
- รูปแนบ reference ต้นฉบับอยู่ /var/folders/... ชื่อ `codex-clipboard-4de0c53a-8bd9-49e6-bf59-432ab4ea0e1d.png` (work) และ `codex-clipboard-337238f4-51c5-44e9-a4a8-1ab2da5a5d76.png` (services) ค้นหาตามชื่อหาก temp หาย ใช้ context/round7 asset
- regenerate images + verify manifest/hash/size + inspect actual images ห้ามถือว่าทำแล้วเพราะแก้ config อย่างเดียว
- อย่า publish rich menu เข้า OA จริงอัตโนมัติ

### 4. ตรวจ Supervisor workflows ให้ครบ

- ฝ่ายส่งต่อ Case ทำแล้ว แต่ **WorkOrder ยังไม่มี forward ทั่วไป** (reassign เดิมจำกัด sent_back): audit ความต้องการ “แต่ละงานส่งต่อ” และทำให้ครบทั้งสองชนิดถ้าจำเป็น อย่าถือว่า Case tests พิสูจน์ WorkOrder
- ปลายทาง unit assignee, pending handoff/review guards, priority below floor, self-submit restriction, stale/direct role tests
- Detail Flex labels/buttons/URI valid และ mobile LINE-friendly

### 5. Completion audit / setup / commit

- ทดสอบจริงใน local DB ผ่านเว็บ: create segment -> draft -> preview -> test mocked delivery -> schedule -> begin edit pause -> re-confirm -> result; web/Mini sessions เห็นข้อมูลเดียวกัน; concurrent confirm/quotas remain safe
- ตรวจ all-friends count estimate: ยังแสดงไม่ทราบจำนวนพร้อมคำอธิบาย อย่าใช้จำนวน local identities อ้างว่าเป็นเพื่อน OA ทั้งหมด พิจารณา official follower stats API estimate หากบัญชีรองรับ
- ตรวจ full regression suite โดยเฉพาะ notification budgets, worker, staff menu, permission, requester service flows
- migrate demo/local DB หากจำเป็น (test DB apply migration อัตโนมัติไม่ได้แปลว่า local app DB migrate แล้ว)
- จัดทำ env/setup docs: LIFF endpoint, openid scope, correct provider/linked OA, HTTPS public media, worker/scanner. ตัวเลข channel ที่ user ส่งไม่ใช่หลักฐานว่าบัญชีปัจจุบันถูกผูกแล้ว
- Account references: `https://manager.line.biz/account/@705ttrga/broadcast/create`, `https://developers.line.biz/console/channel/2012304618/basics`; ก่อนหน้าตรวจได้แค่ login page ยังไม่ verify configuration จริง
- อัปเดต ADR ด้วยสถานะจริงและ limitations, commit เฉพาะงานของเราลง dev หลังผ่าน checks; ไม่ push หากยังไม่มีคำสั่ง
- อย่าระบุ “เสร็จทั้งหมด” จนทุก requirement มีหลักฐาน ถ้าขาด live config ให้ทำ local งานทั้งหมดก่อน แล้วระบุค่าที่ต้องใช้/ผลที่ยัง verify ไม่ได้อย่างตรงไปตรงมา

## คำสั่งตรวจที่ใช้

```sh
python3 -m uv run --locked --env-file .env python manage.py test operations.tests.test_broadcasts operations.tests.test_broadcast_auth --keepdb --noinput
python3 -m uv run --locked --env-file .env python manage.py makemigrations --check --dry-run
python3 -m uv run --locked ruff check operations/broadcasts.py operations/broadcast_delivery.py operations/broadcast_auth.py operations/broadcast_web.py operations/tests/test_broadcasts.py operations/tests/test_broadcast_auth.py
node --check operations/static/operations/broadcast.js
git diff --check
```

สำหรับ suite แชตที่ fixture เปิด LINE API ให้บังคับปิด outbound/ใช้ localhost เพื่อไม่แตะ OA จริง:

```python
import os
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'
import django
django.setup()
from django.conf import settings
settings.OUTBOUND_ENABLED = False
settings.LINE_API_BASE = 'http://127.0.0.1:1'
from django.core.management import call_command
call_command('test', 'operations.tests.test_supervisor_work', 'operations.tests.test_m13_staff_chat', 'operations.tests.test_m04_orchestration', interactive=False, keepdb=True)
```

อย่ารัน tests หลาย process พร้อมกันบน test DB เดียวกัน `TransactionTestCase` ต้อง APP_ENV local ตอน flush; บาง test method override production ได้ แต่ class override production เคยทำ flush fail

## หลักฐานและภาพ

- `/tmp/commit-dev-tests.log`: 67 tests ก่อน commit e83d156 ผ่าน
- `/tmp/broadcast-auth-tests.log`: 11 tests Auth/API/page ผ่านก่อนเพิ่ม test/copy
- `/tmp/broadcast-handoff-tests.log`: **38 tests ผ่าน** (Broadcast/Auth รวม tests ส่งทดสอบและ copy ใหม่), Ruff และ git diff --check ผ่าน; ยังไม่แทน browser verification ของฟีเจอร์ใหม่
- `/tmp/check-broadcast-ui.py`: Playwright render with mocked API ก่อนเพิ่มหลายปุ่ม/test; 320/390/1280 px ไม่มี pageerror/horizontal overflow
- `output/design/broadcast-mini-app/{recipients,compose}-{320,390,1280}.png`: screenshots ก่อนเพิ่มฟีเจอร์ล่าสุด เป็น mock API render ไม่ใช่หลักฐาน end-to-end LINE
- local UI server port8017 ถูกหยุดหลังตรวจภาพรอบก่อน เริ่มใหม่ได้เมื่อจำเป็น อย่าอ้างว่ากำลังทำงานโดยไม่ตรวจ process

## Sources ทางเทคนิค

- LINE verify ID token: https://developers.line.biz/en/reference/line-login/#verify-id-token
- LIFF server verification: https://developers.line.biz/en/docs/liff/using-user-profile/
- Retry same request key/payload, 409, 24h: https://developers.line.biz/en/docs/messaging-api/retrying-api-request/
- Narrowcast limits/progress: https://developers.line.biz/en/reference/messaging-api/nojs/

Native all-friends narrowcast ใช้ no recipient/filter + `upToRemainingQuota: true` และ `forbidPartialDelivery: true`, cap ด้วยโควตาคงเหลือ; HTTP accepted ไม่ได้แปลว่าทุกคนเปิดอ่าน; retain unknown quota; retry เฉพาะ unfinished batch และอย่าสร้าง key ใหม่หลัง timeout

### ผลตรวจล่าสุดก่อน commit หน้าเว็บและเมนู

- `/tmp/broadcast-web-final-tests.log`: 39 tests ผ่าน (รวม Auth/API/test/copy และ Broadcast backend/concurrency)
- `/tmp/check-broadcast-card-ui.py`: Chromium 390px บันทึก/preview ครบ 3 ปุ่ม ส่ง test request ด้วย revision ของร่างที่บันทึก ไม่มี page errors/overflow ภาพ `output/design/broadcast-mini-app/card-390.png`
- `makemigrations --check --dry-run`: No changes detected
- งานอัปโหลดสื่อ และ audit WorkOrder forward / integration ทั้งระบบ ยังไม่เสร็จ
