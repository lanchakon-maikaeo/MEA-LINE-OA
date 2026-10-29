from django import template
from django.utils.html import format_html, format_html_join

from operations import units

register = template.Library()


@register.filter
def unit_path(user):
    """ป้ายสังกัด (ADR 0031 Q13): กอง › แผนก › กลุ่มงาน in short names, N/A where missing; the
    full names show on hover."""
    profile = getattr(user, "staff_profile", None) if user is not None else None
    labels, names = units.path_labels(profile), units.path_names(profile)
    parts = format_html_join(
        format_html('<span class="unit-path__sep" aria-hidden="true">›</span>'),
        '<span class="unit-path__part{}">{}</span>',
        ((" unit-path__part--na" if label == units.NA else "", label) for label in labels),
    )
    return format_html(
        '<span class="unit-path" title="{}" aria-label="สังกัด {}">{}</span>',
        " › ".join(names),
        " › ".join(labels),
        parts,
    )


@register.filter
def nth(values, index):
    try:
        return values[int(index)]
    except (IndexError, TypeError, ValueError):
        return None


@register.filter
def thai_day(value):
    return units.thai_date(value) if value else ""
