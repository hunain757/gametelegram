"""Key institutional levels: previous day / week high & low, Asian session range, daily & weekly open."""

from datetime import datetime


def _dt(s: str) -> datetime:
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")


def key_levels(daily: list[dict] | None, m5: list[dict] | None = None) -> dict:
    out = {}
    if daily and len(daily) >= 2:
        today = daily[-1]
        # Skip the short Sunday-evening candle some feeds print.
        prev = next((c for c in reversed(daily[:-1]) if _dt(c["time"]).weekday() != 6), daily[-2])
        out.update({"PDH": prev["high"], "PDL": prev["low"], "Daily Open": today["open"]})

        weeks: dict[tuple, list[dict]] = {}
        for c in daily:
            weeks.setdefault(_dt(c["time"]).isocalendar()[:2], []).append(c)
        order = sorted(weeks)
        if len(order) >= 2:
            pw = weeks[order[-2]]
            out.update({"PWH": max(c["high"] for c in pw), "PWL": min(c["low"] for c in pw),
                        "Weekly Open": weeks[order[-1]][0]["open"]})

    if m5:
        day = m5[-1]["time"][:10]
        asia = [c for c in m5 if c["time"][:10] == day and int(c["time"][11:13]) < 7]
        if asia:
            out.update({"Asia High": max(c["high"] for c in asia), "Asia Low": min(c["low"] for c in asia)})
    return {k: round(v, 2) for k, v in out.items()}


# Levels that hold resting liquidity above (buy-side) and below (sell-side) the market.
BUY_SIDE = ("PDH", "PWH", "Asia High")
SELL_SIDE = ("PDL", "PWL", "Asia Low")
