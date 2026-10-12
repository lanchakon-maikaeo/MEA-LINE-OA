# Release candidate m13-rc1

- commit `9c0a4c39205d984a95b3d54d17717f55775d81db`
- image `localhost:5001/faoj/app@sha256:8dc6a2f001cc7a1d6a694278150e1d95a4465d8696456c041bd57eeb25f6b3b6`
- source digest `0b3b49fb8aff`
- ผล: **ยังไม่พร้อม production**

## AI

| งาน | รุ่น | โมเดล | ผลประเมินผูกกับโค้ดนี้ |
| --- | --- | --- | --- |
| extract | v11 | typhoon-v2.5-30b-a3b-instruct → typhoon-s-thaillm-8b-instruct → OpenThaiGPT-ThaiLLM-8B-Instruct-v7.2 | ใช่ |
| triage | - | หยุดใช้ v13: M13 release candidate ไม่รวม AI คัดกรอง (ผู้ใช้ตัดสิน 12 ต.ค. 2569) เจ้าหน้าที่คัดกรองเอง เปิดใช้ภายหลังต้องประเมินใหม่ | - |
| answer | v14 | typhoon-v2.5-30b-a3b-instruct | ใช่ |

## Gates G01–G10

| Gate | สถานะ | ผู้รับรอง | ยังขาด |
| --- | --- | --- | --- |
| G01 ขอบเขตและระดับบริการ | รอหลักฐานภายนอก | เจ้าของกระบวนการ + ผู้ดูแลระบบ | วัดบน faoj-prod และเจ้าของระบบรับรองตัวเลข E.9; ผู้ใช้เลือกกลุ่มเขต pilot และตัวแทนรับปัญหา |
| G02 ตัวตน/ข้อมูล/อำนาจ | รอหลักฐานภายนอก | ฝอจ. + เจ้าของตัวตน/ข้อมูล | ฝอจ. ส่งข้อมูลจริง (Q7, Q10, Q12); เจ้าของตัวตน/ข้อมูลและเจ้าของกติการับรอง; data owner รับรอง |
| G03 ธุรกรรมและ routing | รอหลักฐานภายนอก | QA + เจ้าของกระบวนการ | QA และเจ้าของกระบวนการ |
| G04 Security | รอหลักฐานภายนอก | ผู้รับผิดชอบ security | ผู้รับผิดชอบ security |
| G05 AI | รอหลักฐานภายนอก | ผู้รับผิดชอบ AI + ฝอจ. | endpoint/key/โควตาจริง ประเมินซ้ำ และ ฝอจ. ทบทวนผล; ไม่อยู่ใน candidate นี้ ผู้ใช้ตัดสินภายหลัง ต้องประเมินใหม่ |
| G06 Integration | รอหลักฐานภายนอก | เจ้าของระบบเชื่อมต่อ | ทำซ้ำบน faoj-staging กับ OA ทดสอบ; เจ้าของระบบทะเบียนยืนยันเอกสารหรือ API |
| G07 CI/CD | รอหลักฐานภายนอก | ผู้ดูแล release | รันจริงบน GitHub และ OCI |
| G08 Operations | รอหลักฐานภายนอก | ผู้ดูแลระบบ/DB | วัดบน faoj-prod และกู้ลง faoj-staging; OCI Monitoring/Notifications และรายชื่อเวร |
| G09 UAT | รอหลักฐานภายนอก | ตัวแทนผู้ใช้ | ตัวแทนผู้ใช้ทำตาม Docs/release/uat-script.md |
| G10 Release | รอหลักฐานภายนอก | เจ้าของโครงการ + release owner | ลงนามใน Docs/release/go-no-go.md |

## Acceptance ของ M13

| ข้อ | สิ่งที่ต้องมี | สถานะ | หลักฐาน |
| --- | --- | --- | --- |
| A01 | LINE OA ส่วนตัวจริง → n8n container → ThaiLLM API จริง → เว็บเฉพาะกอง/แอดมิน → ผล LINE ถึงเจ้าของเรื่อง ครบสามงาน | รอหลักฐานภายนอก: ทำซ้ำบน faoj-staging กับ OA ทดสอบเมื่อมี OCI (G06) | live-line, simulate-line, ci, เทสต์ 4 รายการ |
| A02 | P0: แจ้งอันตรายได้ P0 และคำแนะนำทันที แม้ AI ล่ม | ผ่าน | simulate-line, เทสต์ 4 รายการ |
| A03 | unknown: ไม่รู้ของ ไม่รู้ยอด หรือระบบภายนอกไม่ตอบ ไม่เดาและไม่ทำซ้ำ | ผ่าน | เทสต์ 3 รายการ |
| A04 | partial: อนุมัติ จ่าย และคืนบางส่วน แล้วยุติส่วนที่เหลือ | ผ่าน | เทสต์ 4 รายการ |
| A05 | override: เจ้าหน้าที่เลือกต่างจากข้อเสนอได้พร้อมเหตุผล และข้อเสนอเก่าไม่ทับการตัดสิน | ผ่าน | เทสต์ 4 รายการ |
| A06 | own-requester: ผู้แจ้งเห็นและกดได้เฉพาะเรื่องของตัวเอง แม้อยู่เขตเดียวกัน | ผ่าน | simulate-line, เทสต์ 5 รายการ |
| A07 | division scope: เจ้าหน้าที่เห็นเฉพาะกองตน ทั้งหน้า URL ตรง ไฟล์แนบ และ export | ผ่าน | เทสต์ 5 รายการ |
| A08 | admin scope: แอดมินเห็นทุกกองแบบมี audit สิทธิ์ผู้พัฒนาแยก และ export ทุกกองเฉพาะแอดมิน | ผ่าน | เทสต์ 4 รายการ |
| A09 | LINE pending → approved: ลงทะเบียนแล้วรอรับรอง ไม่ได้สิทธิ์จนกว่าจะอนุมัติ | ผ่าน | live-line, เทสต์ 4 รายการ |
| A10 | role changes: ถอนบทบาท ย้ายกอง ระงับบัญชี มีผลกับ session ที่เปิดอยู่ทันที | ผ่าน | เทสต์ 4 รายการ |
| A11 | model/key changes ทั้งหน้าเว็บและ configuration: ทดสอบ → ประเมิน → เปลี่ยน, key ไม่รั่ว, หยุดใช้ได้ | ผ่าน | ai-extract, ai-answer, เทสต์ 5 รายการ |
| A12 | AI down: แชตถามทีละช่อง ตอบจากคำสำคัญ กติกายังเสนอกอง และหยุดใช้ AI รายงานได้ | ผ่าน | เทสต์ 6 รายการ |
| A13 | ส่งไม่สำเร็จ: ผลส่ง failed/unknown/deferred ถูกบันทึก ไม่ส่งซ้ำมั่ว งบข้อความกันไว้ให้เรื่องสำคัญ และแอดมินส่งซ้ำได้ | ผ่าน | เทสต์ 5 รายการ |
| A14 | load + restore + rollback มีหลักฐาน | รอหลักฐานภายนอก: วัดซ้ำบน faoj-prod (A1 1 OCPU/8 GB) และ restore ลง faoj-staging บน OCI (G08) | load-rehearsal, restore-drill, rollback-rehearsal, rehearsal-deploy, เทสต์ 1 รายการ |
| A15 | รายงาน push ต่อเรื่องและ forecast โควตา LINE พร้อมข้อเสนอแพ็กเกจ | รอหลักฐานภายนอก: ตัวเลขจริงจาก pilot อย่างน้อยสองเดือน แล้วเจ้าของงบตัดสินแพ็กเกจ | line-usage, เทสต์ 2 รายการ |
| A16 | gate ภายนอกที่ยังไม่มีหลักฐานไม่ถูกเปลี่ยนเป็นผ่านด้วย fixture | ผ่าน | เทสต์ 1 รายการ |
| A17 | pilot rollout และ go/no-go ที่ผู้รับผิดชอบรับรอง | รอหลักฐานภายนอก: เจ้าของโครงการและ release owner ลงนามใน Docs/release/go-no-go.md |  |

## หลักฐาน

| หลักฐาน | ผล | ไฟล์ (sha256) |
| --- | --- | --- |
| ci | ผ่าน | ci.json `05ed2257b132`<br>migrate.log `14b0c6097093`<br>check.log `46732ec0eb1e`<br>test.log `10cfb35f7e90`<br>test-e2e.log `a74d4eebc4e9`<br>security.log `baf6b1aecd39` |
| tests | ผ่าน | test.log `10cfb35f7e90` |
| acceptance-tests | ผ่าน | acceptance-tests.log `a8fbf04ff2e2` |
| simulate-line | ผ่าน | sim-seed1.json `e29f0faa70de`<br>sim-seed7.json `6a6199343540` |
| ai-extract | ผ่าน | ai-extract.json `a8f0a7a0f4bf` |
| ai-answer | ผ่าน | ai-answer.json `f0f8654a917d` |
| image-scan | ผ่าน | image-scan.json `84033244c551`<br>trivy-app.log `9a52a41c19ae`<br>trivy-db.log `55afe0a14939` |
| rehearsal-deploy | ผ่าน | history.jsonl `1051eb0d07f5` |
| rollback-rehearsal | ผ่าน | history.jsonl `1051eb0d07f5` |
| restore-drill | ผ่าน | restore-drill.json `eba00d0a9168` |
| load-rehearsal | ผ่าน | load/k6-summary.json `b301d894b1fa`<br>load/inside.json `d2aacb091c6a` |
| line-usage | ผ่าน | line-usage.json `11ca3a67f1d0` |
| live-line | ผ่าน | live-line.json `e2ea7a44af09` |
