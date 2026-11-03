"""Server-built SVG geometry for the report dashboard.

The pages carry a strict CSP (no inline style or script), so charts are SVG whose sizes are
attributes, coloured by classes in app.css; app.js adds the hover tooltip and crosshair. Every
chart also has a table view, so nothing is readable only by hovering.
"""

import math

WIDTH, HEIGHT = 960, 280
LEFT, RIGHT, TOP, BOTTOM = 64, 36, 16, 52
HEADROOM = 1.08  # the highest line stays clear of the top edge, as matplotlib's margins do


def tick_step(value):
    """1, 2, 5 × 10^k so that 0..`value` takes at most five steps, for round axis ticks."""
    if value <= 5:
        return 1
    power = 10 ** math.floor(math.log10(value / 5))
    for step in (1, 2, 5, 10):
        if value <= 5 * step * power:
            return step * power
    return 10 * power


def signed(n):
    return f"+{n:,}" if n > 0 else f"−{-n:,}" if n < 0 else "0"


def trend(points, link):
    """[(day, new, ended)] → two cumulative step lines, the gap between them, ticks and hit columns.

    The lines add up from the first day of the period (15 ต.ค. 2569 Q8), so the gap between them
    is what came in during the period and has not ended. A count changes on its day, so each line
    steps at the day's edge instead of sloping across days. Each column keeps its own day's counts
    for the tooltip and the table. `link(metric, day)` returns the drill-down URL of one day."""
    count = len(points)
    totals, new_sum, ended_sum = [], 0, 0
    for d, new, ended in points:
        new_sum, ended_sum = new_sum + new, ended_sum + ended
        totals.append((d, new_sum, ended_sum))
    plot_w, plot_h = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM
    peak = max(new_sum, ended_sum, 1)
    unit, ceiling = tick_step(peak), peak * HEADROOM
    step = plot_w / max(count, 1)

    def x(i):
        return round(LEFT + i * step + step / 2, 1)

    def y(value):
        return round(TOP + plot_h * (1 - value / ceiling), 1)

    def steps(index):
        if not count:
            return []
        corners = [(x(0), y(totals[0][index]))]
        for i in range(1, count):
            edge = round(LEFT + i * step, 1)
            corners += [(edge, y(totals[i - 1][index])), (edge, y(totals[i][index]))]
        return corners + [(x(count - 1), y(totals[-1][index]))]

    def path(corners, close=False):
        if not corners:
            return ""
        return "M" + " L".join(f"{px} {py}" for px, py in corners) + (" Z" if close else "")

    new_line, ended_line = steps(1), steps(2)
    ticks = [{"y": y(v), "label": f"{v:,}"} for v in range(0, int(ceiling) + 1, unit)]
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
    ends = []
    if count:
        new_y, ended_y = y(new_sum), y(ended_sum)
        # The end values sit beside the dots; two close values move apart so they never overlap.
        middle, apart = (new_y + ended_y) / 2, abs(new_y - ended_y) < 16
        ends = [
            {
                "x": x(count - 1),
                "y": new_y,
                "series": "new",
                "value": f"{new_sum:,}",
                "label_y": round(middle - 8, 1) if apart else new_y,
            },
            {
                "x": x(count - 1),
                "y": ended_y,
                "series": "ended",
                "value": f"{ended_sum:,}",
                "label_y": round(middle + 8, 1) if apart else ended_y,
            },
        ]
    return {
        "width": WIDTH,
        "height": HEIGHT,
        "left": LEFT,
        "right": WIDTH - RIGHT,
        "top": TOP,
        "bottom": HEIGHT - BOTTOM,
        "plot_h": plot_h,
        "tick_x": LEFT - 10,
        "label_y": HEIGHT - BOTTOM + 22,
        "axis_x": round(LEFT + plot_w / 2, 1),
        "axis_x_y": HEIGHT - 6,
        "axis_y_x": 16,
        "axis_y_y": round(TOP + plot_h / 2, 1),
        "end_x": x(count - 1) + 10 if count else 0,
        "new_path": path(new_line),
        "ended_path": path(ended_line),
        "gap_path": path(new_line + ended_line[::-1], close=True),
        "ticks": ticks,
        "labels": labels,
        "columns": columns,
        "ends": ends,
        "total_new": new_sum,
        "total_ended": ended_sum,
        "gap": signed(new_sum - ended_sum),
    }
