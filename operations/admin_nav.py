"""เมนูแอดมิน (15 ต.ค. 2569, the user: too many tabs, hard to use).

The settings pages, grouped by what the admin is doing, in one list. The sidebar, the admin home
page and their "waiting" badges all read it, so a new page is one entry here.
"""

from dataclasses import dataclass

from django.urls import reverse


@dataclass(frozen=True)
class Page:
    url_name: str
    label: str
    hint: str
    also: tuple = ()  # url names of the page's detail and action views
    badge: str = ""  # key of badges(): how many things wait on this page
    developer: bool = False  # แอดมินผู้พัฒนา only


@dataclass(frozen=True)
class Group:
    label: str
    hint: str
    pages: tuple


GROUPS = (
    Group(
        "คนและสิทธิ์",
        "บัญชีเจ้าหน้าที่ ผู้ใช้ LINE OA และทะเบียนพนักงาน",
        (
            Page(
                "admin-users",
                "บัญชีเจ้าหน้าที่",
                "บัญชีเจ้าหน้าที่ กอง และบทบาท",
                ("admin-user-new", "admin-user-detail", "admin-user-action"),
            ),
            Page(
                "admin-registrations",
                "ผู้ใช้ LINE OA",
                "คำขอลงทะเบียนจาก LINE และผู้ใช้ที่รับรองแล้ว",
                (
                    "admin-registration-action",
                    "admin-requester-detail",
                    "admin-requester-messages",
                    "admin-requester-action",
                    "admin-requester-new",
                ),
                badge="registrations",
            ),
            Page("admin-employees", "ทะเบียนพนักงาน", "รายชื่อพนักงานที่ใช้ยืนยันตัวตน"),
        ),
    ),
    Group(
        "กองและการส่งงาน",
        "เรื่องไปถึงกองไหน แต่ละกองดูแลอะไร และใครอนุมัติได้",
        (
            Page("admin-divisions", "กอง", "กอง แผนก และผู้ประสาน", ("admin-division-action",)),
            Page("admin-duties", "หน้าที่ของกอง", "งานที่แต่ละกองดูแล ใช้ส่งเรื่องให้ถูกกอง"),
            Page("routing-words", "คำที่ระบบจำ", "คำที่เคยส่งเรื่องไปกองใดกองหนึ่ง"),
            Page(
                "admin-policy",
                "กติกาและอำนาจ",
                "ความด่วน เวลาตอบรับ และผู้มีอำนาจอนุมัติ",
                ("admin-policy-action",),
            ),
            Page("admin-holidays", "วันหยุด", "วันที่ไม่นับเป็นวันทำการ และขึ้นในปฏิทินนัด"),
            Page("admin-inventory", "คลังของ", "รายการของและยอดคลัง", ("admin-inventory-action",)),
            Page("admin-items", "รายการอุปกรณ์", "หมวดเมนู LINE และรหัสพัสดุ SAP ของแต่ละรายการ"),
            Page(
                "koj-sources",
                "ติดตามงาน กอจ.",
                "ชีทสถานะงานที่ระบบอ่าน และการสั่งอ่านใหม่",
                ("koj-history",),
            ),
        ),
    ),
    Group(
        "บอท LINE และ AI",
        "คำตอบของบอท คำถามที่ยังตอบไม่ได้ และการปิดข้อมูลก่อนส่ง AI",
        (
            Page(
                "admin-flows",
                "flow LINE",
                "ขั้นสนทนาของผู้แจ้ง แก้แบบลากวาง ตรวจ แล้วเผยแพร่",
                ("admin-flow", "admin-flow-start", "admin-flow-save", "admin-flow-publish"),
            ),
            Page(
                "admin-requirements",
                "สิ่งที่แต่ละกองต้องการ",
                "ความต้องการของแต่ละกองที่ผูกกับกล่องใน flow กดไปดูกล่องนั้นได้",
            ),
            Page("admin-answers", "คลังคำตอบ", "คำตอบที่บอทใช้ตอบคำถามทั่วไป"),
            Page(
                "admin-questions",
                "คำถามที่บอทตอบไม่ได้",
                "คำถามที่รอเพิ่มคำตอบ",
                badge="questions",
            ),
            Page("pii", "ปิดข้อมูลก่อนส่ง AI", "ลองปิดข้อมูล และคำที่ต้องปิดเสมอ"),
            Page(
                "admin-ai",
                "AI",
                "รุ่น AI การประเมิน และการเปิดใช้",
                ("admin-ai-action", "admin-ai-version-action"),
            ),
            Page(
                "admin-monitoring",
                "ติดตามระบบและ AI",
                "ตัวเลขรายวันของ AI คลังคำตอบ และระบบ",
                ("admin-monitoring-report",),
                badge="monitoring",
            ),
        ),
    ),
    Group(
        "ระบบและการติดตาม",
        "การแจ้งเตือน LINE งานค้าง เรื่องแจ้งปัญหา และตัวเลือกระบบ",
        (
            Page(
                "admin-line",
                "LINE และงานค้าง",
                "งบข้อความแจ้งเตือน การแจ้งเตือนที่ส่งไม่ได้ และงานค้าง",
                ("admin-requeue",),
                badge="line",
            ),
            Page(
                "admin-problems",
                "ปัญหาและข้อเสนอแนะ",
                "ทุกเรื่องแจ้งปัญหาและข้อเสนอแนะ ดาวน์โหลด CSV ได้",
                ("admin-problems-csv",),
            ),
            Page("admin-options", "ตัวเลือกระบบ", "เปลี่ยนวิธีทำงานโดยไม่ต้องแก้โปรแกรม"),
        ),
    ),
)

URL_NAMES = frozenset(
    {"admin-home"} | {name for g in GROUPS for p in g.pages for name in (p.url_name, *p.also)}
)


def badges():
    """What waits for an admin: registrations to decide, questions to answer, sends that failed,
    and Monitoring's alerts of the latest day."""
    from operations import monitoring
    from operations.models import (
        NotificationDelivery,
        OutboxEvent,
        RegistrationRequest,
        UnansweredQuestion,
    )

    return {
        "registrations": RegistrationRequest.objects.filter(
            status=RegistrationRequest.Status.PENDING
        ).count(),
        "questions": UnansweredQuestion.objects.filter(
            status=UnansweredQuestion.Status.OPEN
        ).count(),
        "line": NotificationDelivery.objects.filter(status__in=["unknown", "failed"]).count()
        + OutboxEvent.objects.filter(status=OutboxEvent.Status.FAILED).count(),
        "monitoring": monitoring.latest_alerts(),
    }


def menu(scope, url_name, waiting=None):
    """The groups this admin sees, each page with its link, badge and whether it is open now."""
    waiting = badges() if waiting is None else waiting
    groups, current = [], None
    for group in GROUPS:
        pages = []
        for page in group.pages:
            if page.developer and not scope.is_developer_admin:
                continue
            is_current = url_name == page.url_name or url_name in page.also
            item = {
                "label": page.label,
                "hint": page.hint,
                "url": reverse(page.url_name),
                "current": is_current,
                "badge": waiting.get(page.badge, 0) if page.badge else 0,
            }
            pages.append(item)
            if is_current:
                current = item
        groups.append({"label": group.label, "hint": group.hint, "pages": pages})
    return {
        "groups": groups,
        "current": current,
        "home": url_name == "admin-home",
        "waiting": sum(waiting.values()),
    }
