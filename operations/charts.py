"""Server-built SVG geometry for the report dashboard.

The pages carry a strict CSP (no inline style or script), so charts are SVG whose sizes are
attributes, coloured by classes in app.css; app.js adds the hover tooltip and crosshair. Every
chart also has a table view, so nothing is readable only by hovering.
"""

import math

WIDTH, HEIGHT = 960, 260
LEFT, RIGHT, TOP, BOTTOM = 44, 16, 14, 32


def nice_ceiling(value):
    """1, 2, 5 × 10^k at or above `value` (at least 1), for round axis ticks."""
    if value <= 1:
        return 1
    power = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 5, 10):
        if value <= step * power:
            return step * power
    return 10 * power


def trend(points, link):
    """Two series per day: [(day, new, ended)] → lines, ticks, hit columns and end dots.

    `link(metric, day)` returns the drill-down URL of one day's cases."""
    count = len(points)
    plot_w, plot_h = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM
    top = nice_ceiling(max([n for _, n, _ in points] + [e for _, _, e in points] + [0]))
    step = plot_w / max(count, 1)

    def x(i):
        return round(LEFT + (i + 0.5) * step, 1)

    def y(value):
        return round(TOP + plot_h * (1 - value / top), 1)

    def path(index):
        return " ".join(
            f"{'M' if i == 0 else 'L'}{x(i)} {y(p[index])}" for i, p in enumerate(points)
        )

    ticks = [{"y": y(v), "label": f"{v:,}"} for v in sorted({0, top // 2 if top >= 2 else 0, top})]
    labels = [
        {"x": x(i), "label": f"{d.day}/{d.month}"}
        for i, (d, _, _) in enumerate(points)
        if (count - 1 - i) % 7 == 0
    ]
    columns = [
        {
            "x": round(LEFT + i * step, 1),
            "width": round(step, 1),
            "center": x(i),
            "day": f"{d.day}/{d.month}/{d.year + 543}",
            "new": new,
            "ended": ended,
            "href": link("new", d.isoformat()),
            "ended_href": link("ended", d.isoformat()),
        }
        for i, (d, new, ended) in enumerate(points)
    ]
    last = count - 1
    return {
        "width": WIDTH,
        "height": HEIGHT,
        "left": LEFT,
        "right": WIDTH - RIGHT,
        "top": TOP,
        "bottom": HEIGHT - BOTTOM,
        "plot_h": plot_h,
        "tick_x": LEFT - 6,
        "label_y": HEIGHT - 8,
        "new_path": path(1),
        "ended_path": path(2),
        "ticks": ticks,
        "labels": labels,
        "columns": columns,
        "ends": [
            {"x": x(last), "y": y(points[last][1]), "series": "new"},
            {"x": x(last), "y": y(points[last][2]), "series": "ended"},
        ]
        if count
        else [],
        "total_new": sum(n for _, n, _ in points),
        "total_ended": sum(e for _, _, e in points),
    }
