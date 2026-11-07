"""Template helpers of the M10 design system: icons, Thai dates, durations, level badges."""

from django import template
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()

# Stroke icons (24px grid). Inline SVG is markup, not script or style, so the CSP allows it.
ICONS = {
    "menu": '<path d="M4 7h16M4 12h16M4 17h16"/>',
    "sliders": (
        '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/>'
        '<circle cx="9" cy="17" r="2"/>'
    ),
    "search": '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4 4"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "alert": '<path d="M12 4l9 16H3z"/><path d="M12 10v4.5M12 17.5v.01"/>',
    "clock": '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    "user": (
        '<circle cx="12" cy="8.5" r="3.5"/><path d="M5 20c1.2-3.6 3.8-5.5 7-5.5s5.8 1.9 7 5.5"/>'
    ),
    "logout": '<path d="M14 5h4a1 1 0 011 1v12a1 1 0 01-1 1h-4M10 16l4-4-4-4M14 12H4"/>',  # noqa: E501
    "key": (
        '<circle cx="8" cy="14" r="3.5"/><path d="M10.5 11.5L19 3M15 7l2.5 2.5M17.5 4.5L20 7"/>'
    ),
    "file": '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/>',
    "clip": (
        '<path d="M20 11.5l-7.8 7.8a5 5 0 01-7-7L13 4.5a3.3 3.3 0 014.7 4.7'
        'l-7.8 7.8a1.7 1.7 0 01-2.4-2.4l7.1-7.1"/>'
    ),
    "back": '<path d="M15 5l-7 7 7 7"/>',
    "info": '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5M12 8v.01"/>',
    "refresh": '<path d="M19 8a7.5 7.5 0 10.9 6"/><path d="M19.5 3.5V8H15"/>',
    "grid": '<path d="M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z"/>',
    "lock": '<rect x="5" y="11" width="14" height="9" rx="1"/><path d="M8 11V8a4 4 0 018 0v3"/>',
    "pause": '<path d="M9 6v12M15 6v12"/>',
    "spark": (
        '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18'
        'M18 6l-2.5 2.5M8.5 15.5L6 18"/>'
    ),
    "chat": (
        '<path d="M5 5h14a1 1 0 011 1v9a1 1 0 01-1 1H10l-4 3.5V16H5a1 1 0 01-1-1V6a1 1 0 011-1z"/>'
    ),
    "send": '<path d="M4 12l16-7-6 16-3-6.5z"/><path d="M11 14.5L20 5"/>',
    "copy": '<rect x="9" y="9" width="11" height="11" rx="1.5"/><path d="M15 5H5v10"/>',
}


@register.simple_tag
def icon(name, size=""):
    paths = ICONS.get(name, "")
    css = "icon icon--sm" if size == "sm" else "icon"
    return mark_safe(  # noqa: S308 - fixed markup from ICONS, no user input
        f'<svg class="{css}" viewBox="0 0 24 24" aria-hidden="true" focusable="false">{paths}</svg>'
    )


def _local(value):
    if value is None or value == "":
        return None
    if hasattr(value, "tzinfo") and timezone.is_aware(value):
        return timezone.localtime(value)
    return value


@register.filter
def thai_datetime(value):
    """07/10/2569 15:30 น. (Buddhist era, as in LINE)."""
    local = _local(value)
    if local is None:
        return "-"
    return f"{local.day:02d}/{local.month:02d}/{local.year + 543} {local:%H:%M} น."


@register.filter
def thai_date(value):
    local = _local(value)
    if local is None:
        return "-"
    return f"{local.day:02d}/{local.month:02d}/{local.year + 543}"


@register.filter
def thai_short(value):
    """07/10 15:30 for dense rows."""
    local = _local(value)
    if local is None:
        return "-"
    return f"{local.day:02d}/{local.month:02d} {local:%H:%M}"


@register.filter
def duration(minutes):
    """Service minutes as ชม./นาที (business time is not divided into calendar days)."""
    if minutes is None:
        return "-"
    minutes = abs(int(minutes))  # overdue clocks pass negative minutes
    if minutes == 0:
        return "ไม่ถึง 1 นาที"
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours:,} ชม. {rest} นาที"
    if hours:
        return f"{hours:,} ชม."
    return f"{rest} นาที"


@register.filter
def days_since(moment):
    """Calendar age in Thai (M11 ส่วนต่างทะเบียน: "ค้าง 6 วัน")."""
    if moment is None:
        return "-"
    days = (timezone.now() - moment).days
    if days >= 1:
        return f"{days:,} วัน"
    hours = int((timezone.now() - moment).total_seconds() // 3600)
    return f"{hours} ชม." if hours else "ไม่ถึง 1 ชม."


@register.simple_tag
def prio(level):
    from operations.queues import LEVEL_NAMES, LEVEL_TITLES

    if level in LEVEL_NAMES:
        return format_html(
            '<span class="prio prio--{}" title="{}">{}</span>',
            level,
            LEVEL_TITLES[level],
            LEVEL_NAMES[level],
        )
    return format_html('<span class="prio prio--none" title="ยังไม่ได้กำหนดระดับ">รอคัดกรอง</span>')


@register.filter
def level_name(level):
    from operations.queues import level_name as name

    return name(level)


CLOSED = {"closed"}
ENDED = {"rejected", "cancelled"}


@register.simple_tag
def status_tag(case):
    css = "tag"
    if case.status in CLOSED:
        css += " tag--closed"
    elif case.status in ENDED:
        css += " tag--muted"
    return format_html('<span class="{}">{}</span>', css, case.get_status_display())


@register.filter
def lines(values):
    """One value per line for a textarea. Django tags cannot span lines, so the old
    `join:"<newline>"` left the tag itself in the box (and a save would store it)."""
    return "\n".join(str(value) for value in values or [])


@register.filter
def initial(text):
    # Skip Thai leading vowels (เ แ โ ใ ไ) so "เมธี" shows ม, not เ.
    text = (text or "").strip().lstrip("เแโใไ")
    return text[:1].upper() if text else "?"


@register.filter
def lookup(mapping, key):
    try:
        return mapping.get(key, "")
    except AttributeError:
        return ""


@register.filter
def action_label(action):
    """Thai name of a case action (the report of การทำแทนกอง)."""
    from operations.casepage import action_label as label

    return label(action)
