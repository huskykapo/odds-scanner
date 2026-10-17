"""Active hours: only poll a source during part of the day (saves metered API requests overnight).

``"08:00-23:00"`` is local time on the machine running the scanner. A window that crosses midnight
(``"22:00-06:00"``) works too. ``None`` / not set = poll around the clock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta

_WINDOW = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


@dataclass(frozen=True)
class Window:
    start: time
    end: time

    def contains(self, t: time) -> bool:
        if self.start == self.end:
            return True  # 00:00-00:00 or equal bounds: always on
        if self.start < self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end  # crosses midnight

    def seconds_until_open(self, now: datetime) -> float:
        """0 if open at ``now`` (a datetime in the window's own local time), else seconds to the next opening."""
        if self.contains(now.time()):
            return 0.0
        opens = now.replace(hour=self.start.hour, minute=self.start.minute, second=0, microsecond=0)
        if opens <= now:
            opens += timedelta(days=1)
        return (opens - now).total_seconds()

    def __str__(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"


def parse_window(text: str) -> Window:
    """``"08:00-23:00"`` -> Window. Raises ValueError with a clear message."""
    match = _WINDOW.match(str(text))
    if not match:
        raise ValueError(f"active_hours must look like 08:00-23:00, got {text!r}")
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    if not (0 <= h1 < 24 and 0 <= h2 < 24 and 0 <= m1 < 60 and 0 <= m2 < 60):
        raise ValueError(f"active_hours has an impossible time: {text!r}")
    return Window(time(h1, m1), time(h2, m2))
