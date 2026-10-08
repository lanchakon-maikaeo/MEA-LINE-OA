"""Service calendar (ปฏิทินบริการ, M09 Q7/Q17): business minutes on the policy's periods.

The active policy names the time zone, working weekdays, daily periods (07:30–12:00 and
13:00–15:30 in the synthetic DEMO-PE-3) and holidays. P0 always counts continuous time, and a
policy without a calendar (DEMO-PE-1/2) counts everything continuously as M06 did. All values are
synthetic until the service owner sets real ones.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

GUARD_DAYS = 3660  # never search more than ten years ahead


def _time(value):
    return time.fromisoformat(value)


class Calendar:
    def __init__(self, spec=None):
        spec = spec or {}
        self.continuous = not spec
        self.zone = ZoneInfo(spec.get("timezone", "Asia/Bangkok"))
        self.weekdays = set(spec.get("weekdays", [1, 2, 3, 4, 5]))
        self.periods = [(_time(a), _time(b)) for a, b in spec.get("periods", [])]
        self.holidays = {date.fromisoformat(d) for d in spec.get("holidays", [])}
        self.continuous_priorities = set(spec.get("continuous_priorities", ["P0"]))

    def counts_continuously(self, priority):
        return self.continuous or not self.periods or priority in self.continuous_priorities

    def windows(self, day):
        """Working spans of one local day (none on weekends and holidays)."""
        if day.isoweekday() not in self.weekdays or day in self.holidays:
            return []
        return [
            (
                datetime.combine(day, start, tzinfo=self.zone),
                datetime.combine(day, end, tzinfo=self.zone),
            )
            for start, end in self.periods
        ]

    def add(self, start, minutes, priority=""):
        """The moment `minutes` of service time after `start`."""
        if self.counts_continuously(priority):
            return start + timedelta(minutes=minutes)
        remaining = timedelta(minutes=minutes)
        local = start.astimezone(self.zone)
        day = local.date()
        for _ in range(GUARD_DAYS):
            for begin, end in self.windows(day):
                if end <= local:
                    continue
                begin = max(begin, local)
                span = end - begin
                if remaining <= span:
                    return begin + remaining
                remaining -= span
            day += timedelta(days=1)
        raise ValueError("No working time in the calendar.")

    def minutes_between(self, start, end, priority=""):
        """Service minutes from `start` to `end` (0 when `end` is not later)."""
        if end <= start:
            return 0
        if self.counts_continuously(priority):
            return int((end - start).total_seconds() // 60)
        total = timedelta(0)
        day = start.astimezone(self.zone).date()
        last = end.astimezone(self.zone).date()
        while day <= last:
            for begin, finish in self.windows(day):
                lower, upper = max(begin, start), min(finish, end)
                if upper > lower:
                    total += upper - lower
            day += timedelta(days=1)
        return int(total.total_seconds() // 60)


def validate(spec):
    """Problems with a calendar definition (empty list when it is usable)."""
    problems = []
    try:
        ZoneInfo(spec.get("timezone", "Asia/Bangkok"))
    except Exception:  # noqa: BLE001 - any invalid zone name
        problems.append("Unknown time zone.")
    weekdays = spec.get("weekdays", [1, 2, 3, 4, 5])
    if not weekdays or not set(weekdays) <= set(range(1, 8)):
        problems.append("weekdays are ISO numbers 1-7.")
    try:
        periods = [(_time(a), _time(b)) for a, b in spec.get("periods", [])]
    except (TypeError, ValueError):
        return [*problems, "periods are [start, end] times like 07:30."]
    if not periods or any(a >= b for a, b in periods):
        problems.append("Each period ends after it starts.")
    if any(prev[1] > nxt[0] for prev, nxt in zip(periods, periods[1:], strict=False)):
        problems.append("Periods must be in order without overlapping.")
    try:
        [date.fromisoformat(d) for d in spec.get("holidays", [])]
    except (TypeError, ValueError):
        problems.append("holidays are YYYY-MM-DD dates.")
    return problems
