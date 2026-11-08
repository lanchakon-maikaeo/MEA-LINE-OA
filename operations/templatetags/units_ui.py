from django import template
from django.utils.html import format_html, format_html_join

from operations import roster, units

register = template.Library()


@register.filter
def unit_path(user):
    """ป้ายสังกัด (ADR 0031 Q13): หน่วยงาน › กอง › แผนก › กลุ่มงาน, N/A where missing; the full
    names show on hover. Owner, 8 พ.ย. 2569: an account on ผังบุคลากร shows its row's path as
    the LINE list does; one off the chart shows the system's units under the หน่วยงาน the
    roster gives that กอง."""
    profile = getattr(user, "staff_profile", None) if user is not None else None
    row = profile.roster_row if profile is not None else None
    if row is not None:
        labels = roster.employee_path(row)
        names = [roster.path_of(row) or units.NA]
    else:
        top = roster.department_of(profile.division) if profile is not None else ""
        labels = [top or units.NA, *units.path_labels(profile)]
        names = [top or units.NA, *units.path_names(profile)]
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
