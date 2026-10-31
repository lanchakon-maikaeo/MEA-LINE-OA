"""ตอบอิสระ on the web (ADR 0037): the guides page for admins and the knowledge documents page.

Admins write each topic's guide with a reason, see every version with who or what wrote it and
why the guard suite refused one, and roll back in one step (Q22, Q30). Staff upload knowledge
documents; another admin approves or retires them (Q25).
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from operations import answers, chat_guides, free_chat, identity, knowledge, options
from operations.models import AuditEvent, ChatGuide, KnowledgeDoc
from operations.views import _run, admin_required


@admin_required
@require_http_methods(["GET", "HEAD", "POST"])
def guides_page(request):
    if request.method == "POST":
        data = request.POST
        topic, reason = data.get("topic", ""), data.get("reason", "")
        if data.get("op") == "rollback":
            guide = (
                get_object_or_404(ChatGuide, pk=data["guide"], topic=topic)
                if data.get("guide")
                else None
            )
            _run(
                request,
                lambda: chat_guides.rollback(request.user, topic, reason, guide),
                "ย้อนกลับแล้ว",
            )
        else:
            _run(
                request,
                lambda: chat_guides.save(request.user, topic, data.get("text", ""), reason),
                "บันทึกแล้ว ฉบับนี้รอชุดตรวจคืนนี้ก่อนใช้จริง",
            )
        return redirect(f"/console/admin/free-chat/#g-{topic}")
    current = chat_guides.active()
    versions = ChatGuide.objects.select_related("created_by").order_by("topic", "-number")
    topics = [
        {
            "topic": topic,
            "label": label,
            "text": current[topic].text if topic in current else chat_guides.DEFAULTS[topic],
            "active": current.get(topic),
            "versions": [v for v in versions if v.topic == topic][:20],
        }
        for topic, label in ChatGuide.Topic.choices
    ]
    return render(
        request,
        "operations/admin_free_chat.html",
        {
            "topics": topics,
            "mode": options.OPTIONS[options.CHAT_ANSWERS].label_of(answers.mode()),
            "on": free_chat.on(),
            "usable": free_chat.usable(),
            "tuning": options.get(options.FREE_CHAT_TUNING) == "on",
            "max_guide": chat_guides.MAX_GUIDE,
            "history": AuditEvent.objects.filter(action__startswith="chat_guide.")
            .select_related("actor")
            .order_by("-created_at")[:20],
        },
    )


@login_required
@require_http_methods(["GET", "HEAD", "POST"])
def knowledge_page(request):
    scope = identity.staff_scope(request.user)
    if not scope.has_any_access:
        raise PermissionDenied
    if request.method == "POST":
        data = request.POST
        op, reason = data.get("op"), data.get("reason", "")
        doc = get_object_or_404(KnowledgeDoc, pk=data["doc"]) if data.get("doc") else None
        if op == "approve" and doc is not None:
            _run(request, lambda: knowledge.approve(request.user, doc, reason), "รับรองแล้ว")
        elif op == "retire" and doc is not None:
            _run(request, lambda: knowledge.retire(request.user, doc, reason), "เลิกใช้แล้ว")
        else:
            upload = request.FILES.get("file")
            text = data.get("text", "")
            if upload is not None:
                try:
                    text = upload.read(knowledge.MAX_TEXT * 4 + 1).decode("utf-8")
                except UnicodeDecodeError:
                    text = "\0"  # refused below as unreadable
            if "\0" in text:
                messages.error(request, "อ่านไฟล์ไม่ได้ ใช้ไฟล์ข้อความ .txt หรือ .md แบบ UTF-8")
            else:
                _run(
                    request,
                    lambda: knowledge.upload(
                        request.user, data.get("title", ""), text, reason, doc
                    ),
                    "อัปโหลดแล้ว รอแอดมินอีกคนรับรอง",
                )
        return redirect("knowledge")
    return render(
        request,
        "operations/knowledge.html",
        {
            "docs": KnowledgeDoc.objects.select_related("uploaded_by", "approved_by"),
            "is_admin": scope.is_admin,
            "connected": knowledge.enabled(),
            "docs_option": options.get(options.FREE_CHAT_DOCS) == "on",
            "max_text": knowledge.MAX_TEXT,
        },
    )
