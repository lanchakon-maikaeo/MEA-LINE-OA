"""คำที่ระบบจำ: the page of the words that send a case to a division (ADR 0013, Q36).

Admins and ส่วนกลาง (ฝอจ.) see every word and cancel it or move it to another division, with a
reason; other staff see the words that point to their own divisions, read only.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from operations import identity, options, routing_words
from operations.models import AuditEvent, Division, RoutingWord


@login_required
@require_http_methods(["GET", "HEAD", "POST"])
def words(request):
    scope = identity.staff_scope(request.user)
    if not scope.has_any_access:
        raise PermissionDenied
    manage = routing_words.may_manage(request.user)
    if request.method == "POST":
        if not manage:
            raise PermissionDenied
        row = get_object_or_404(
            RoutingWord, pk=request.POST.get("word_id") or 0, cancelled_at__isnull=True
        )
        reason = request.POST.get("reason", "")
        try:
            if request.POST.get("action") == "cancel":
                routing_words.cancel(request.user, row, reason)
                done = f"ยกเลิกคำ '{row.word}' แล้ว"
            else:
                division = Division.objects.filter(code=request.POST.get("division")).first()
                routing_words.change_division(request.user, row, division, reason)
                done = f"คำ '{row.word}' ชี้ไป {division.code} แล้ว"
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, done)
        return redirect("routing-words")
    terms = routing_words.policy_terms()
    rows = [
        {
            "row": row,
            "often": row.moves >= routing_words.OFTEN_MOVED,
            "in_rules": any(t in row.word.lower() or row.word.lower() in t for t in terms),
        }
        for row in routing_words.visible(request.user)
    ]
    return render(
        request,
        "operations/routing_words.html",
        {
            "rows": rows,
            "manage": manage,
            "is_admin": scope.is_admin,
            "mode": options.OPTIONS[options.LEARNED_ROUTES].label_of(routing_words.mode()),
            "divisions": Division.objects.filter(is_active=True).order_by("code"),
            "history": (
                AuditEvent.objects.filter(action__startswith="routing_word.")
                .select_related("actor")
                .order_by("-created_at")[:30]
                if manage
                else []
            ),
        },
    )
