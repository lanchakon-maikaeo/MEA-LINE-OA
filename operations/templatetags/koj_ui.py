from django import template

from operations import koj_tracking
from operations.models import KojRecipient

register = template.Library()


@register.inclusion_tag("operations/_koj_recipients.html")
def koj_recipients(user):
    current = set(KojRecipient.objects.filter(user=user).values_list("group", flat=True))
    return {
        "target": user,
        "choices": [(key, label, key in current) for key, label in koj_tracking.GROUPS.items()],
    }


@register.simple_tag
def koj_access(user):
    return bool(user.is_authenticated and koj_tracking.staff_can_read(user))
