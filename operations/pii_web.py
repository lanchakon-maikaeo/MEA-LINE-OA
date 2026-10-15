"""ปิดข้อมูลก่อนส่ง AI: the staff page of the masking (ADR 0014; 14 Oct 2569 Q30, Q34).

Every staff member, not only admins (Q34):
- tries the masking on a text and sees what the AI would get, with no AI call and nothing kept;
- sees how much the masking hid in the last 30 days, never what it hid;
- adds a word the AI must never see (คำที่ต้องปิดเสมอ), with a reason.
Only an admin removes a word, since that lets it reach the AI again. Staff other than admins see
the listed words only as hints (ส**** ม***), because the list itself holds people's names.
"""

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from operations import identity, pii
from operations.models import MaskCount, MaskedWord

PREVIEW_MAX = 4000  # characters tried at once
DAYS = 30


@login_required
@require_http_methods(["GET", "HEAD", "POST"])
def page(request):
    scope = identity.staff_scope(request.user)
    if not scope.has_any_access:
        raise PermissionDenied
    tried = None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "preview":
            text = request.POST.get("text", "")[:PREVIEW_MAX]
            masked, counts = pii.preview(text)
            tried = {"text": text, "masked": masked, "counts": _named(counts.items())}
        elif action == "add":
            try:
                pii.add_word(request.user, request.POST.get("word"), request.POST.get("reason"))
            except ValidationError as exc:
                messages.error(request, " ".join(exc.messages))
            else:
                messages.success(request, "เพิ่มคำแล้ว ระบบจะปิดคำนี้ทุกครั้งก่อนส่งให้ AI")
            return redirect("pii")
        elif action == "remove":
            if not scope.is_admin:
                raise PermissionDenied
            row = get_object_or_404(
                MaskedWord, pk=request.POST.get("word_id") or 0, removed_at__isnull=True
            )
            try:
                pii.remove_word(request.user, row, request.POST.get("reason"))
            except ValidationError as exc:
                messages.error(request, " ".join(exc.messages))
            else:
                messages.success(request, "เอาคำออกจากรายการแล้ว")
            return redirect("pii")
    since = timezone.localdate() - timedelta(days=DAYS - 1)
    real = MaskCount.objects.filter(day__gte=since).exclude(task="eval")
    by_kind = real.values_list("kind").annotate(total=Sum("count")).order_by("-total")
    by_task = real.values_list("task").annotate(total=Sum("count")).order_by("-total")
    words = MaskedWord.objects.filter(removed_at__isnull=True).select_related(
        "added_by__staff_profile"
    )
    return render(
        request,
        "operations/pii.html",
        {
            "scope": scope,
            "tried": tried,
            "kinds": [label for _, label in pii.KINDS.values()],
            "by_kind": _named(by_kind),
            "by_task": [(pii.TASKS.get(task, task), total) for task, total in by_task],
            "days": DAYS,
            "words": [
                {
                    "row": w,
                    "shown": w.word if scope.is_admin else pii.hint(w.word),
                    "by": getattr(w.added_by, "staff_profile", None) or w.added_by.username,
                }
                for w in words
            ],
            "preview_max": PREVIEW_MAX,
        },
    )


def _named(pairs):
    return [(pii.KINDS[kind][1] if kind in pii.KINDS else kind, n) for kind, n in pairs]
