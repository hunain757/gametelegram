"""Chart images (PNG) for signals and the market dashboard: candles, order blocks, FVGs,
liquidity / key levels and the trade's entry, stop loss and targets."""

import io
import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

BG, PANEL, GRID, TEXT = "#0b0e14", "#11151d", "#1c2230", "#c9d1d9"
UP, DOWN = "#26a69a", "#ef5350"
BLUE, GOLD, GREY = "#42a5f5", "#f5c542", "#7d8590"

_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200d\u20BF]")


def plain(text: str) -> str:
    return _EMOJI.sub("", text or "").strip()


KEY_LEVEL_NAMES = ("PDH", "PDL", "PWH", "PWL", "Asia High", "Asia Low")


def _setup_axes(title: str, subtitle: str):
    fig, ax = plt.subplots(figsize=(11, 6), dpi=110)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(PANEL)
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=GREY, labelsize=8)
    ax.grid(color=GRID, linewidth=0.5)
    ax.yaxis.tick_right()
    fig.text(0.015, 0.955, title, color=GOLD, fontsize=15, fontweight="bold", ha="left")
    fig.text(0.015, 0.915, subtitle, color=TEXT, fontsize=9, ha="left")
    ax.text(0.5, 0.5, "GOLD SMC AI", transform=ax.transAxes, fontsize=46, color="white", alpha=0.04,
            ha="center", va="center", fontweight="bold")
    return fig, ax


def _draw_candles(ax, candles):
    rng = max(c["high"] for c in candles) - min(c["low"] for c in candles)
    for i, c in enumerate(candles):
        color = UP if c["close"] >= c["open"] else DOWN
        ax.vlines(i, c["low"], c["high"], color=color, linewidth=0.9)
        low = min(c["open"], c["close"])
        ax.add_patch(Rectangle((i - 0.36, low), 0.72, max(abs(c["close"] - c["open"]), rng * 0.002),
                               color=color, linewidth=0))


def _zones(ax, smc_read, offset, right, lo, hi):
    for z in smc_read.get("order_blocks", [])[-4:]:
        _zone(ax, z, offset, right, lo, hi, UP if z["direction"] == "bullish" else DOWN, 0.20, "OB")
    for z in smc_read.get("breakers", [])[-3:]:
        _zone(ax, z, offset, right, lo, hi, "#ab47bc", 0.16, "BRK")
    for z in smc_read.get("fvgs", [])[-4:]:
        _zone(ax, z, offset, right, lo, hi, "#66bb6a" if z["direction"] == "bullish" else "#ff8a65", 0.11, "FVG")


def _zone(ax, z, offset, right, lo, hi, color, alpha, label):
    if z["top"] < lo or z["bottom"] > hi:
        return
    x = max(z["idx"] - offset, 0)
    ax.add_patch(Rectangle((x, z["bottom"]), right - x, z["top"] - z["bottom"], color=color, alpha=alpha, lw=0))
    ax.text(x + 0.5, z["top"], label, color=color, fontsize=7, va="bottom", alpha=0.9)


def _hline(ax, y, right, color, label, style="-", width=1.2):
    ax.hlines(y, 0, right, colors=color, linestyles=style, linewidth=width)
    ax.text(right + 0.3, y, f" {label}  {y:,.2f} ", color=BG if style == "-" else color, fontsize=8, va="center",
            ha="left", fontweight="bold",
            bbox=dict(facecolor=color if style == "-" else BG, edgecolor=color, boxstyle="round,pad=0.25", lw=0.8))


def _time_ticks(ax, candles):
    n = len(candles)
    step = max(n // 8, 1)
    idx = list(range(0, n, step))
    ax.set_xticks(idx)
    ax.set_xticklabels([candles[i]["time"][5:16].replace("-", "/") for i in idx], rotation=0)


def _finish(fig) -> bytes:
    fig.subplots_adjust(left=0.02, right=0.86, top=0.88, bottom=0.07)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()


def signal_chart(candles: list[dict], trade: dict, smc_read: dict, levels: dict, tf_label: str) -> bytes:
    window = candles[-90:]
    offset = len(candles) - len(window)
    n = len(window)
    right = n + 14
    bull = trade["direction"] == "BUY"
    prices = [c["high"] for c in window] + [c["low"] for c in window] + [trade["stop_loss"], trade["tps"][-1]]
    lo, hi = min(prices), max(prices)
    pad = (hi - lo) * 0.06

    order = f"{trade['direction']} {'NOW' if trade['entry_type'] == 'MARKET' else 'LIMIT'}"
    fig, ax = _setup_axes(f"{trade.get('symbol_name', 'XAU/USD')}  {order}",
                          f"{plain(trade['style_label'])}  ·  {tf_label} chart  ·  confidence {trade.get('confidence', 0)}%"
                          f"  ·  {trade.get('headline', '')}")
    _zones(ax, smc_read, offset, right, lo - pad, hi + pad)
    _draw_candles(ax, window)

    # Risk / reward boxes to the right of the last candle.
    x0 = n + 1
    ax.add_patch(Rectangle((x0, min(trade["entry"], trade["stop_loss"])), right - x0,
                           abs(trade["entry"] - trade["stop_loss"]), color=DOWN, alpha=0.22, lw=0))
    ax.add_patch(Rectangle((x0, min(trade["entry"], trade["tps"][-1])), right - x0,
                           abs(trade["tps"][-1] - trade["entry"]), color=UP, alpha=0.16, lw=0))

    for name in KEY_LEVEL_NAMES:
        y = levels.get(name)
        if y is not None and lo - pad <= y <= hi + pad:
            ax.hlines(y, 0, right, colors=GREY, linestyles=":", linewidth=0.9)
            ax.text(1, y, name, color=GREY, fontsize=7, va="bottom")

    _hline(ax, trade["entry"], right, BLUE, "ENTRY")
    _hline(ax, trade["stop_loss"], right, DOWN, "SL")
    for i, tp in enumerate(trade["tps"], 1):
        _hline(ax, tp, right, UP, f"TP{i}", style="--" if i > 1 else "-")
    ax.annotate("", xy=(x0 + 2, trade["tps"][0]), xytext=(x0 + 2, trade["entry"]),
                arrowprops=dict(arrowstyle="-|>", color=UP if bull else DOWN, lw=2))

    ax.set_xlim(-1, right)
    ax.set_ylim(lo - pad, hi + pad)
    _time_ticks(ax, window)
    return _finish(fig)


def market_chart(candles: list[dict], smc_read: dict, levels: dict, tf_label: str, name: str = "XAU/USD") -> bytes:
    window = candles[-120:]
    offset = len(candles) - len(window)
    n = len(window)
    right = n + 10
    lo = min(c["low"] for c in window)
    hi = max(c["high"] for c in window)
    pad = (hi - lo) * 0.05
    trend = smc_read.get("trend") or "ranging"
    price = window[-1]["close"]
    fig, ax = _setup_axes(f"{name}  {tf_label}  ·  {price:,.2f}",
                          f"Structure: {trend}  ·  zone: {smc_read['range']['zone']}  ·  "
                          "green/red boxes = order blocks & FVGs, dotted = key levels, dashed = liquidity")
    _zones(ax, smc_read, offset, right, lo - pad, hi + pad)
    _draw_candles(ax, window)
    liq = smc_read["liquidity"]
    for y in liq["buy_side"][:2] + liq["sell_side"][:2]:
        if lo - pad <= y <= hi + pad:
            ax.hlines(y, 0, right, colors=GOLD, linestyles="--", linewidth=0.8, alpha=0.8)
            ax.text(right - 1, y, "liquidity", color=GOLD, fontsize=7, va="bottom", ha="right")
    for name in KEY_LEVEL_NAMES:
        y = levels.get(name)
        if y is not None and lo - pad <= y <= hi + pad:
            _hline(ax, y, right, GREY, name, style=":", width=0.9)
    _hline(ax, price, right, BLUE, "PRICE", width=0.8)
    ax.set_xlim(-1, right)
    ax.set_ylim(lo - pad, hi + pad)
    _time_ticks(ax, window)
    return _finish(fig)
