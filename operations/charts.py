"""Server-built SVG geometry for the report dashboard.

The pages carry a strict CSP (no inline style or script), so charts are SVG whose sizes are
attributes, coloured by classes in app.css; app.js adds the hover tooltip and crosshair. Every
chart also has a table view, so nothing is readable only by hovering.
"""

import math
import unicodedata
from itertools import pairwise

WIDTH, HEIGHT = 960, 280
LEFT, RIGHT, TOP, BOTTOM = 64, 36, 16, 52
MARGIN = 0.05  # seaborn (matplotlib) axes margins: the lines stay clear of the frame on every side
LEGEND_ROW, LEGEND_PAD = 20, 7


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


def text_width(text, size=13):
    """Rough rendered width; Thai marks above or below a letter take no room of their own."""
    return sum(0 if unicodedata.category(ch) == "Mn" else size * 0.62 for ch in text)


def trend(points, link):
    """[(day, new, ended)] → two cumulative step lines, the gap between them, ticks and hit columns.

    The lines add up from the first day of the period (15 ต.ค. 2569 Q8), so the gap between them
    is what came in during the period and has not ended. A count changes on its day, so each line
    steps halfway between two days instead of sloping across them. Each column keeps its own day's
    counts for the tooltip and the table. `link(metric, day)` returns the drill-down URL of one day.
    The look follows seaborn's whitegrid style (user, 3 Nov 2569)."""
    count = len(points)
    totals, new_sum, ended_sum = [], 0, 0
    for d, new, ended in points:
        new_sum, ended_sum = new_sum + new, ended_sum + ended
        totals.append((d, new_sum, ended_sum))
    plot_w, plot_h = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM
    peak = max(new_sum, ended_sum, 1)
    unit, low, high = tick_step(peak), -MARGIN * peak, (1 + MARGIN) * peak
    span = max(count - 1, 1)
    dx = plot_w / (span * (1 + 2 * MARGIN))

    def x(i):
        return round(LEFT + (MARGIN * span + i) * dx, 1)

    def edge(i):
        """Between day i - 1 and day i."""
        return round(LEFT + (MARGIN * span + i - 0.5) * dx, 1)

    def y(value):
        return round(TOP + plot_h * (high - value) / (high - low), 1)

    def steps(index):
        if not count:
            return []
        corners = [(x(0), y(totals[0][index]))]
        for i in range(1, count):
            corners += [(edge(i), y(totals[i - 1][index])), (edge(i), y(totals[i][index]))]
        return corners + [(x(count - 1), y(totals[-1][index]))]

    def path(corners, close=False):
        if not corners:
            return ""
        return "M" + " L".join(f"{px} {py}" for px, py in corners) + (" Z" if close else "")

    new_line, ended_line = steps(1), steps(2)
    ticks = [{"y": y(v), "label": f"{v:,}"} for v in range(0, int(high) + 1, unit)]
    labels = [
        {"x": x(i), "label": f"{d.day}/{d.month}"}
        for i, (d, _, _) in enumerate(points)
        if (count - 1 - i) % 7 == 0
    ]
    columns = []
    for i, (d, new, ended) in enumerate(points):
        start = LEFT if i == 0 else edge(i)
        end = WIDTH - RIGHT if i == count - 1 else edge(i + 1)
        columns.append(
            {
                "x": start,
                "width": round(end - start, 1),
                "center": x(i),
                "day": f"{d.day}/{d.month}/{d.year + 543}",
                "new": new,
                "ended": ended,
                "new_total": totals[i][1],
                "ended_total": totals[i][2],
                "href": link("new", d.isoformat()),
                "ended_href": link("ended", d.isoformat()),
            }
        )
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
        "plot_w": plot_w,
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
        "legend": legend(
            [
                ("new", f"เข้า {new_sum:,}"),
                ("ended", f"จบ {ended_sum:,}"),
                ("gap", f"ส่วนต่าง {signed(new_sum - ended_sum)}"),
            ],
            [(x(i), y(t[k])) for i, t in enumerate(totals) for k in (1, 2)],
        ),
        "total_new": new_sum,
        "total_ended": ended_sum,
        "gap": signed(new_sum - ended_sum),
    }


def legend(items, marks):
    """A framed legend inside the plot, at the first place that covers the fewest points of the
    lines, tried in matplotlib's loc="best" order (upper right, upper left, lower left, ...)."""
    width = round(52 + max(text_width(label) for _, label in items))
    height = 2 * LEGEND_PAD + LEGEND_ROW * len(items)
    left, right = LEFT + 10, WIDTH - RIGHT - 10 - width
    upper, lower = TOP + 10, HEIGHT - BOTTOM - 10 - height
    middle_x, middle_y = round((left + right) / 2), round((upper + lower) / 2)
    corners = [
        (right, upper),
        (left, upper),
        (left, lower),
        (right, lower),
        (right, middle_y),
        (left, middle_y),
        (middle_x, lower),
        (middle_x, upper),
        (middle_x, middle_y),
    ]

    def covered(corner):
        bx, by = corner
        return sum(
            bx - 6 <= mx <= bx + width + 6 and by - 6 <= my <= by + height + 6 for mx, my in marks
        )

    bx, by = min(corners, key=covered)
    rows = [by + LEGEND_PAD + LEGEND_ROW * k + LEGEND_ROW / 2 for k in range(len(items))]
    return {
        "x": bx,
        "y": by,
        "width": width,
        "height": height,
        "key_x": bx + 10,
        "key_end": bx + 32,
        "text_x": bx + 40,
        "items": [
            {"series": series, "label": label, "y": row, "box_y": row - 5.5}
            for (series, label), row in zip(items, rows, strict=True)
        ],
    }


PIE_R = 90
PIE_PAD_X, PIE_PAD_Y = 52, 16  # room around the pie for the % labels on their leader lines
PIE_SLICES = 5  # past five divisions the smallest fold into อื่นๆ; a sixth hue would blur
LABEL_GAP = 16  # the least height between two labels on the same side


def pie(rows, other_href=""):
    """[(label, n, href)] → pie slices with each one's share, largest first from 12 o'clock.

    Each division/centre's share of the open work (user, 3 Nov 2569). Slices take seaborn's
    deep hues in a fixed order whose neighbours stay apart for colour-blind readers; อื่นๆ is
    grey. Divisions with nothing open are left out; one division alone is a whole disc. Each
    slice's % sits outside the pie at the end of a leader line from the slice (user, 3 Nov
    2569)."""
    rows = sorted((r for r in rows if r[1]), key=lambda r: -r[1])
    if len(rows) > PIE_SLICES + 1:
        rest = rows[PIE_SLICES:]
        rows = rows[:PIE_SLICES] + [("อื่นๆ", sum(r[1] for r in rest), other_href)]
    total = sum(n for _, n, _ in rows)
    cx, cy = PIE_PAD_X + PIE_R, PIE_PAD_Y + PIE_R
    slices, start = [], 0
    for i, (label, n, href) in enumerate(rows):
        share = n / total
        end = start + share
        other = label == "อื่นๆ" and i == len(rows) - 1
        percent = round(share * 100)
        slices.append(
            {
                "label": label,
                "n": n,
                "href": href,
                "percent": f"{percent}%" if percent else "<1%",
                "tone": "other" if other else f"c{i + 1}",
                "path": _wedge(cx, cy, start, end),
                "middle": (start + end) / 2,
            }
        )
        start = end
    _leaders(slices, cx, cy)
    return {
        "width": 2 * (PIE_PAD_X + PIE_R),
        "height": 2 * (PIE_PAD_Y + PIE_R),
        "total": total,
        "slices": slices,
    }


def _angle_point(cx, cy, share, radius):
    angle = 2 * math.pi * share - math.pi / 2
    return cx + radius * math.cos(angle), cy + radius * math.sin(angle)


def _wedge(cx, cy, start, end):
    """SVG path of the share start..end (0..1, clockwise from 12 o'clock) around (cx, cy)."""
    if end - start >= 1:  # one division has all the work: a whole disc (an arc can't close)
        half = f"A{PIE_R} {PIE_R} 0 1 1"
        return f"M{cx} {cy - PIE_R} {half} {cx} {cy + PIE_R} {half} {cx} {cy - PIE_R} Z"
    x1, y1 = _angle_point(cx, cy, start, PIE_R)
    x2, y2 = _angle_point(cx, cy, end, PIE_R)
    large = 1 if end - start > 0.5 else 0
    return f"M{cx} {cy} L{x1:.2f} {y1:.2f} A{PIE_R} {PIE_R} 0 {large} 1 {x2:.2f} {y2:.2f} Z"


def _leaders(slices, cx, cy):
    """A line out from the middle of each slice's rim, bending level to its % label. Labels on
    one side that would touch are pushed apart top to bottom, then back up if they overflow."""
    top, bottom = 8, 2 * (PIE_PAD_Y + PIE_R) - 8
    for s in slices:
        s["right"] = math.sin(2 * math.pi * s["middle"]) >= 0
        s["rim"] = _angle_point(cx, cy, s["middle"], PIE_R - 4)
        s["knee"] = _angle_point(cx, cy, s["middle"], PIE_R + 10)
        s["label_y"] = min(max(s["knee"][1], top), bottom)
    for right in (True, False):
        side = sorted((s for s in slices if s["right"] == right), key=lambda s: s["label_y"])
        for above, below in pairwise(side):
            below["label_y"] = max(below["label_y"], above["label_y"] + LABEL_GAP)
        if side and side[-1]["label_y"] > bottom:
            side[-1]["label_y"] = bottom
            for below, above in pairwise(side[::-1]):
                above["label_y"] = min(above["label_y"], below["label_y"] - LABEL_GAP)
    for s in slices:
        sign = 1 if s["right"] else -1
        end_x = cx + sign * (PIE_R + 22)
        (rx, ry), (kx, _) = s["rim"], s["knee"]
        y = round(s["label_y"], 1)
        s["leader"] = f"{rx:.1f},{ry:.1f} {kx:.1f},{y} {end_x:.1f},{y}"
        s["label_x"] = round(end_x + sign * 4, 1)
        s["anchor"] = "start" if s["right"] else "end"
        s["label_y"] = y
        for key in ("rim", "knee", "right", "middle"):
            del s[key]
