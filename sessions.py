"""Trading sessions and killzones for gold (UTC, approximate; ignores DST shifts)."""

from datetime import datetime, timezone

SESSIONS = {"Asia": (0, 8), "London": (7, 16), "New York": (12, 21)}
KILLZONES = {"London Open": (7, 10), "New York Open": (12, 15), "London Close": (15, 16)}


def is_market_open(now: datetime | None = None) -> bool:
    """Gold trades from Sunday ~22:00 UTC to Friday ~21:00 UTC."""
    now = now or datetime.now(timezone.utc)
    weekday, hour = now.weekday(), now.hour  # Monday = 0 ... Sunday = 6
    if weekday == 5:
        return False
    if weekday == 4 and hour >= 21:
        return False
    if weekday == 6 and hour < 22:
        return False
    return True


def current(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    h = now.hour
    active = [name for name, (a, b) in SESSIONS.items() if a <= h < b]
    killzone = next((name for name, (a, b) in KILLZONES.items() if a <= h < b), None)
    return {"sessions": active or ["Off-hours"], "killzone": killzone, "utc_time": now.strftime("%H:%M")}
