"""Everything the user sees on Telegram: signal cards, trade updates, menus and dashboards (HTML)."""

from datetime import datetime, timezone
from html import escape

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup

from setups import STYLES, TF_LABEL
from tracker import pips

LINE = "━━━━━━━━━━━━━━━━━━━━"
DISCLAIMER = "⚠️ Not financial advice. Always use a stop loss and risk 1–2% per trade."
TREND = {"bullish": "🟢 Bullish", "bearish": "🔴 Bearish", None: "⚪ Ranging"}


def p(x: float) -> str:
    return f"{x:,.2f}"


def bar(pct: int, width: int = 10) -> str:
    n = max(0, min(width, round(pct / 100 * width)))
    return "▰" * n + "▱" * (width - n)


def hhmm(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%d %b %H:%M UTC")


# ---------- signals ----------

def signal_card(t: dict) -> str:
    bull = t["direction"] == "BUY"
    icon = "🟢" if bull else "🔴"
    order = f"{t['direction']} {'NOW' if t['entry_type'] == 'MARKET' else 'LIMIT'}"
    risk = abs(t["entry"] - t["stop_loss"])
    lines = [
        f"{icon} <b>XAU/USD · {order}</b> {icon}",
        f"<b>{t['style_label']}</b>" + (f"  ·  <i>{escape(t['headline'])}</i>" if t.get("headline") else ""),
        LINE,
        f"📍 <b>Entry:</b>  <code>{p(t['entry'])}</code>",
        f"🛑 <b>Stop Loss:</b>  <code>{p(t['stop_loss'])}</code>  <i>(−{pips(risk)} pips)</i>",
    ]
    for i, (tp, rr) in enumerate(zip(t["tps"], t["rr"]), 1):
        lines.append(f"🎯 <b>TP{i}:</b>  <code>{p(tp)}</code>  <i>(+{pips(abs(tp - t['entry']))} pips · 1:{rr:g})</i>")
    lines.append(LINE)
    if t.get("engine_only"):
        lines.append(f"🤖 <b>Engine score:</b> {t['score']}%  {bar(t['score'])}")
        lines.append("<i>AI desk offline – rule engine signal only</i>")
    else:
        lines.append(f"🧠 <b>AI Confidence:</b> {t['confidence']}%  {bar(t['confidence'])}")
        agents = "  ".join(f"{r['icon']}{'✅' if r['vote'] == 'TAKE' else '❌' if r['vote'] == 'SKIP' else '⚠️'}"
                           for r in t.get("reports", []))
        lines.append(f"🤖 <b>AI Desk:</b> {t['votes']}/5 agents agree   {agents}")
    lines.append("")
    lines.append("📌 <b>Confluences</b>")
    lines += [f"  • {escape(c)}" for c in t["confluences"][:7]]
    if t.get("reason"):
        lines += ["", f"💬 <i>{escape(t['reason'])}</i>"]
    lines.append("")
    tfs = t["timeframes"]
    lines.append(f"🕒 {hhmm(t['created_at'])}  ·  📐 {TF_LABEL[tfs['bias']]} → {TF_LABEL[tfs['confirm']]} → "
                 f"{TF_LABEL[tfs['entry']]}")
    if t["entry_type"] == "LIMIT":
        lines.append(f"⏳ Limit order valid until {hhmm(t['expires_at'])}")
    lines += ["🔒 After TP1 move SL to entry (breakeven)", "", f"<i>{DISCLAIMER}</i>"]
    return "\n".join(lines)


def signal_keyboard(trade_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[Btn("📍 Live Status", callback_data=f"t:{trade_id}"),
                                  Btn("🧠 AI Desk Report", callback_data=f"r:{trade_id}")]])


def event_message(t: dict, ev: dict) -> str:
    head = f"XAU/USD {t['direction']} · {t['style_label']}"
    k = ev["kind"]
    if k == "filled":
        return f"✅ <b>ENTRY FILLED</b> at <code>{p(ev['price'])}</code>\n{head}\n🛑 SL <code>{p(t['stop_loss'])}</code>"
    if k == "tp":
        gain = pips(abs(ev["price"] - t["entry"]))
        msg = f"🎯 <b>TP{ev['n']} HIT!</b>  +{gain} pips  (1:{ev['rr']:g})\n{head}"
        if ev["n"] == 3:
            msg = f"🏆 <b>TP3 HIT – FULL TARGET!</b>  +{gain} pips  (1:{ev['rr']:g})\n{head}\n🎉 Trade closed in profit"
        elif ev.get("new_sl") is not None:
            msg += f"\n🔒 Move SL to <code>{p(ev['new_sl'])}</code>" + (" (breakeven)" if ev["n"] == 1 else "")
        return msg
    if k == "sl":
        return f"🛑 <b>STOP LOSS HIT</b>  −{pips(abs(t['entry'] - ev['price']))} pips  (−1R)\n{head}\n" \
               "<i>Losses are part of trading. Next setup loading…</i>"
    if k == "protected_stop":
        return f"🔒 <b>Closed at protected stop</b> <code>{p(ev['price'])}</code> after TP{ev['stage']}\n{head}\n" \
               "✅ Profit secured"
    if k == "expired":
        return f"⌛ <b>Order expired</b> – price never came back to <code>{p(t['entry'])}</code>\n{head}\n" \
               "❎ Cancel the pending order"
    if k == "cancelled":
        return f"❎ <b>Cancelled</b> – price reached TP1 without filling the entry\n{head}\nCancel the pending order"
    if k == "timeout":
        return f"⏱ <b>Closed after {7} days</b> at <code>{p(ev['price'])}</code>\n{head}"
    return head


def trade_status(t: dict, price: float | None) -> str:
    bull = t["direction"] == "BUY"
    status = {"pending": "⏳ Waiting for entry", "active": "🟢 Running", "closed": "🏁 Closed"}[t["status"]]
    lines = [f"<b>XAU/USD {t['direction']} · {t['style_label']}</b>", f"Status: <b>{status}</b>"]
    if t["status"] == "active" and price:
        diff = (price - t["entry"]) if bull else (t["entry"] - price)
        lines.append(f"Price: <code>{p(price)}</code>  ·  Floating: <b>{'+' if diff >= 0 else '−'}{pips(abs(diff))} pips</b>")
    lines.append(f"📍 Entry <code>{p(t['entry'])}</code>   🛑 SL <code>{p(t['stop_loss'])}</code>")
    for i, tp in enumerate(t["tps"], 1):
        lines.append(f"{'✅' if t['stage'] >= i else '⬜️'} TP{i} <code>{p(tp)}</code>")
    if t["status"] == "closed":
        r = t.get("result_r") or 0
        lines.append(f"Result: <b>{t['outcome'].upper()}</b>  ({'+' if r >= 0 else ''}{r:g}R)")
    lines.append(f"🕒 Opened {hhmm(t['created_at'])}")
    return "\n".join(lines)


def ai_report(t: dict) -> str:
    if t.get("engine_only") or not t.get("reports"):
        return "🤖 This signal came from the rule engine only (AI desk was offline)."
    lines = [f"🧠 <b>AI Desk Report</b> · XAU/USD {t['direction']} · {t['style_label']}", LINE]
    for r in t["reports"]:
        vote = {"TAKE": "✅ TAKE", "SKIP": "❌ SKIP"}.get(r["vote"], "⚠️ N/A")
        model = f"  <i>[{escape(r['model'])}]</i>" if r.get("model") else ""
        lines.append(f"{r['icon']} <b>{escape(r['name'])}</b> — {vote} ({r['score']}){model}")
        lines.append(f"<i>{escape(r['summary'])}</i>")
        lines += [f"  • {escape(pt)}" for pt in r["points"]]
        lines.append("")
    lines.append(f"👑 <b>Head Trader</b> — confidence {t['confidence']}%")
    lines.append(f"<i>{escape(t.get('reason', ''))}</i>")
    return "\n".join(lines)


# ---------- menus ----------

def main_menu(user: dict, is_admin: bool) -> tuple[str, InlineKeyboardMarkup]:
    styles = ", ".join(STYLES[s]["label"] for s in STYLES if s in user.get("styles", [])) or "none"
    text = (
        "🏆 <b>GOLD SMC AI SIGNALS</b> 🏆\n"
        f"{LINE}\n"
        "🤖 6 AI agents scan <b>XAU/USD</b> 24/5 on M5 → D1\n"
        "💧 Smart Money Concepts · 📊 Volume · 🎯 Auto TP/SL tracking\n\n"
        f"🔔 Alerts: <b>{'ON' if user.get('subscribed') else 'OFF'}</b>\n"
        f"📡 Signal types: {styles}\n\n"
        "Choose an option 👇"
    )
    rows = [
        [Btn("📡 Active Trades", callback_data="trades"), Btn("📜 History", callback_data="hist")],
        [Btn("📊 Performance", callback_data="perf"), Btn("🌍 Market Now", callback_data="mkt")],
        [Btn("🧠 AI Market View", callback_data="aiview"), Btn("⚙️ Signal Types", callback_data="set")],
        [Btn(f"🔔 Alerts: {'ON ✅' if user.get('subscribed') else 'OFF ❌'}", callback_data="alert"),
         Btn("ℹ️ How it works", callback_data="help")],
    ]
    if is_admin:
        rows.append([Btn("⚡ Scan Market Now", callback_data="scan")])
    return text, InlineKeyboardMarkup(rows)


def back(extra: list | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup((extra or []) + [[Btn("⬅️ Menu", callback_data="menu")]])


def settings(user: dict) -> tuple[str, InlineKeyboardMarkup]:
    rows = [[Btn(f"{'✅' if s in user.get('styles', []) else '❌'} {STYLES[s]['label']}", callback_data=f"sty:{s}")]
            for s in STYLES]
    text = ("⚙️ <b>Signal Types</b>\nTap to switch on/off:\n\n"
            "⚡ <b>Scalping</b> – M5 entries, H1 bias, quick trades\n"
            "📊 <b>Intraday</b> – M15 entries, H4 bias, same-day trades\n"
            "🌊 <b>Swing</b> – H1 entries, D1 bias, multi-day trades")
    return text, back(rows)


def trades_list(trades: list[dict], price: float | None) -> tuple[str, InlineKeyboardMarkup]:
    if not trades:
        return "📡 <b>Active Trades</b>\n\nNo open trades right now. The AI desk is scanning… 🔎", back()
    lines = ["📡 <b>Active Trades</b>", LINE]
    rows = []
    for t in trades[-8:]:
        state = "⏳" if t["status"] == "pending" else "🟢"
        float_txt = ""
        if t["status"] == "active" and price:
            diff = (price - t["entry"]) if t["direction"] == "BUY" else (t["entry"] - price)
            float_txt = f"  {'+' if diff >= 0 else '−'}{pips(abs(diff))} pips"
        lines.append(f"{state} {t['direction']} @ {p(t['entry'])} · {t['style_label']} · TP {t['stage']}/3{float_txt}")
        rows.append([Btn(f"{t['direction']} {p(t['entry'])} – details", callback_data=f"t:{t['id']}")])
    return "\n".join(lines), back(rows)


def history(trades: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    if not trades:
        return "📜 <b>History</b>\n\nNo closed trades yet.", back()
    icon = {"win": "✅", "loss": "❌", "breakeven": "➖", "expired": "⌛", "cancelled": "❎"}
    lines = ["📜 <b>Last 10 trades</b>", LINE]
    for t in trades[-10:][::-1]:
        r = t.get("result_r") or 0
        lines.append(f"{icon.get(t['outcome'], '•')} {t['direction']} {p(t['entry'])} · {t['style_label']} · "
                     f"TP {t['stage']}/3 · {'+' if r >= 0 else ''}{r:g}R · {hhmm(t['created_at'])[:6]}")
    return "\n".join(lines), back()


def performance(s: dict, title: str = "📊 <b>Performance</b>") -> str:
    if not s["trades"]:
        return f"{title}\n\nNo finished trades yet."
    lines = [
        title, LINE,
        f"🎯 Win rate: <b>{s['win_rate']}%</b>  {bar(s['win_rate'])}",
        f"✅ Wins: <b>{s['wins']}</b>   ❌ Losses: <b>{s['losses']}</b>   🏆 TP3: <b>{s['tp3']}</b>",
        f"💰 Total: <b>{'+' if s['total_r'] >= 0 else ''}{s['total_r']}R</b>",
        "",
    ]
    for key, v in s["by_style"].items():
        if v["trades"]:
            lines.append(f"{STYLES[key]['label']}: {v['wins']}/{v['trades']} wins ({v['win_rate']}%)")
    lines.append("\n<i>A trade counts as a win once TP1 is hit.</i>")
    return "\n".join(lines)


def market_dashboard(market: dict | None, session: dict, is_open: bool, scanned_at: datetime | None) -> str:
    if not market:
        return "🌍 <b>Market Now</b>\n\nNo scan yet – waiting for the first market scan…"
    price = market["5min"]["price"]
    lines = [f"🌍 <b>XAU/USD Market Now</b>  ·  <code>{p(price)}</code>",
             f"{'🟢 Market open' if is_open else '🔴 Market closed'} · {', '.join(session['sessions'])}"
             + (f" · 🔥 {session['killzone']}" if session.get("killzone") else ""), LINE, "<b>Structure</b>"]
    for tf in ("1day", "4h", "1h", "15min", "5min"):
        s = market[tf]["smc"]
        ev = s["last_event"]
        lines.append(f"{TF_LABEL[tf]:>3}: {TREND[s['trend']]}" + (f" · last {ev['type']}" if ev else "")
                     + f" · {s['range']['zone']}")
    h1 = market["1h"]["smc"]["liquidity"]
    lines += ["", "<b>💧 Liquidity (H1)</b>",
              f"Above: {', '.join(p(x) for x in h1['buy_side'][:3]) or '—'}",
              f"Below: {', '.join(p(x) for x in h1['sell_side'][:3]) or '—'}"]
    obs = [z for z in market["15min"]["smc"]["order_blocks"]][-3:]
    if obs:
        lines += ["", "<b>🧱 Order Blocks (M15)</b>"]
        lines += [f"{'🟢' if z['direction'] == 'bullish' else '🔴'} {p(z['bottom'])} – {p(z['top'])}" for z in obs]
    fvgs = market["15min"]["smc"]["fvgs"][-2:]
    if fvgs:
        lines += ["", "<b>⚡ Fair Value Gaps (M15)</b>"]
        lines += [f"{'🟢' if z['direction'] == 'bullish' else '🔴'} {p(z['bottom'])} – {p(z['top'])}" for z in fvgs]
    v = market["15min"]["volume"]
    lines += ["", "<b>📊 Volume (M15, PAXG)</b>"]
    if v.get("available"):
        lines.append(f"Pressure: {v['pressure']} (delta {v['delta']:+.2f}) · RVOL {v['relative_volume_last_closed']}x")
        lines.append(f"POC {p(v['poc'])} · Value area {p(v['value_area_low'])} – {p(v['value_area_high'])}")
    else:
        lines.append("Not available right now")
    ind = market["1h"]["ind"]
    if ind["rsi"] is not None:
        lines += ["", f"⚙️ H1 RSI {ind['rsi']:.0f} · ATR(M15) {market['15min']['smc']['atr']:.2f}"]
    if scanned_at:
        lines.append(f"\n🔎 Last scan {scanned_at.strftime('%H:%M UTC')}")
    return "\n".join(lines)


HELP = (
    "ℹ️ <b>How the bot works</b>\n" + LINE + "\n"
    "1️⃣ Every 5 minutes it pulls gold candles on <b>M5, M15, H1, H4, D1</b>.\n"
    "2️⃣ The <b>SMC engine</b> maps structure (BOS/CHoCH), order blocks, fair value gaps, "
    "liquidity pools & sweeps, premium/discount, sessions and <b>volume</b> (PAXG).\n"
    "3️⃣ When a setup appears, <b>5 AI specialists</b> review it in parallel:\n"
    "   🏗 Structure · 💧 Liquidity/OB · 📊 Volume · ⚙️ Momentum · 🛡 Risk\n"
    "4️⃣ The 👑 <b>Head Trader AI</b> reads their reports and makes the final call.\n"
    "5️⃣ Only strong trades are sent. SL goes beyond the order block / sweep, "
    "TPs sit at the next liquidity pools.\n"
    "6️⃣ Every trade is <b>tracked live</b>: entry fill, TP1/TP2/TP3, SL, expiry – "
    "you get a reply on the signal each time.\n\n"
    f"<i>{DISCLAIMER}</i>"
)


def daily_report(s: dict, open_count: int) -> str:
    text = performance(s, title="🌙 <b>Daily Report · XAU/USD</b>")
    return text + f"\n\n📡 Open trades: {open_count}\n🕒 {datetime.now(timezone.utc).strftime('%d %b %Y')}"
