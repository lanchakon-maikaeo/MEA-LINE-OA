from django import template

from operations import koj_tracking
from operations.models import KojRecipient

register = template.Library()


@register.inclusion_tag("operations/_koj_recipients.html")
def koj_recipients(user, back=""):
    current = set(KojRecipient.objects.filter(user=user).values_list("group", flat=True))
    return {
        "target": user,
        "back": back,
        "choices": [(key, label, key in current) for key, label in koj_tracking.GROUPS.items()],
    }
