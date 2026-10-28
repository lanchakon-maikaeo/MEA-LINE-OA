"""ผู้รับแจ้งเรื่องฉุกเฉิน (grill กคว. รอบใหม่ Q33): staff an admin picks to hear of a new
emergency case of their division. Since 28 ต.ค. 2569 (Q7, Q12) the setting lives on the person's
LINE account page only, so a contact always has LINE linked."""

from django.core.exceptions import ValidationError
from django.db import transaction

from operations import identity
from operations.models import ChannelIdentity, Division, EmergencyContact, RoleGrant


def divisions_of(user):
    """The active divisions where this person holds an active grant."""
    ids = RoleGrant.objects.filter(
        user=user, revoked_at__isnull=True, division__isnull=False
    ).values_list("division_id", flat=True)
    return Division.objects.filter(pk__in=ids, is_active=True).order_by("code")


def choices(user):
    chosen = set(EmergencyContact.objects.filter(user=user).values_list("division_id", flat=True))
    return [(division, division.pk in chosen) for division in divisions_of(user)]


@transaction.atomic
def set_for(actor, user, division_ids):
    """Make the person a contact of exactly the chosen divisions among their own."""
    if not identity.staff_scope(actor).is_admin:
        raise ValidationError("เฉพาะแอดมินตั้งผู้รับแจ้งเรื่องฉุกเฉินได้")
    if not ChannelIdentity.objects.filter(staff_user=user).exists():
        raise ValidationError("ผูก LINE กับบัญชีเจ้าหน้าที่ก่อน จึงจะตั้งเป็นผู้รับแจ้งได้")
    own = {d.pk for d in divisions_of(user)}
    wanted = {int(x) for x in division_ids if str(x).isdigit()} & own
    current = set(EmergencyContact.objects.filter(user=user).values_list("division_id", flat=True))
    EmergencyContact.objects.filter(user=user, division_id__in=current - wanted).delete()
    for division_id in sorted(wanted - current):
        EmergencyContact.objects.create(division_id=division_id, user=user, added_by=actor)
    identity.audit(actor, "staff.emergency_contact", user, divisions=sorted(wanted))
    return wanted


def by_division():
    """{division id: [(name, LINE account page requester id or None)]} for the divisions page."""
    out = {}
    rows = EmergencyContact.objects.select_related("user__staff_profile").order_by(
        "division_id", "user__username"
    )
    pages = dict(
        ChannelIdentity.objects.filter(
            requester__isnull=False, staff_user__isnull=False
        ).values_list("staff_user_id", "requester_id")
    )
    for row in rows:
        profile = getattr(row.user, "staff_profile", None)
        name = profile.display_name if profile else row.user.username
        out.setdefault(row.division_id, []).append((name, pages.get(row.user_id)))
    return out
