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
    """[(day, new, ended)] → two cumulative lines, ticks, hit columns and end dots.

    The lines add up from the first day of the period (15 ต.ค. 2569 Q8), so the gap between them
    is what came in during the period and has not ended. Each column keeps its own day's counts
    for the tooltip and the table. `link(metric, day)` returns the drill-down URL of one day."""
    count = len(points)
    totals, new_sum, ended_sum = [], 0, 0
    for d, new, ended in points:
        new_sum, ended_sum = new_sum + new, ended_sum + ended
        totals.append((d, new_sum, ended_sum))
    plot_w, plot_h = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM
    top = nice_ceiling(max([new_sum, ended_sum, 0]))
    step = plot_w / max(count, 1)

    def x(i):
        return round(LEFT + (i + 0.5) * step, 1)

    def y(value):
        return round(TOP + plot_h * (1 - value / top), 1)

    def path(index):
        return " ".join(
            f"{'M' if i == 0 else 'L'}{x(i)} {y(p[index])}" for i, p in enumerate(totals)
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
            "new_total": totals[i][1],
            "ended_total": totals[i][2],
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
            {"x": x(last), "y": y(new_sum), "series": "new"},
            {"x": x(last), "y": y(ended_sum), "series": "ended"},
        ]
        if count
        else [],
        "total_new": new_sum,
        "total_ended": ended_sum,
    }
