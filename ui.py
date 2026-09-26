"""Everything the user sees on Telegram: signal cards, trade updates, menus and dashboards (HTML)."""

from datetime import datetime, timezone
from html import escape

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup

from setups import STYLES, TF_LABEL
from tracker import pips as _pips

LINE = "━━━━━━━━━━━━━━━━━━━━"
DISCLAIMER = "⚠️ Not financial advice. Always use a stop loss and risk 1–2% per trade."
TREND = {"bullish": "🟢 Bullish", "bearish": "🔴 Bearish", None: "⚪ Ranging"}


def p(x: float) -> str:
    return f"{x:,.2f}"


def sym(t: dict) -> str:
    return t.get("symbol_name", "XAU/USD")


def pips(diff: float, t: dict | None = None) -> float:
    return _pips(diff, (t or {}).get("pip", 0.1))


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
        f"{icon} <b>{sym(t)} · {order}</b> {icon}",
        f"<b>{t['style_label']}</b>" + (f"  ·  <i>{escape(t['headline'])}</i>" if t.get("headline") else ""),
        LINE,
        f"📍 <b>Entry:</b>  <code>{p(t['entry'])}</code>",
        f"🛑 <b>Stop Loss:</b>  <code>{p(t['stop_loss'])}</code>  <i>(−{pips(risk, t)} pips)</i>",
    ]
    for i, (tp, rr) in enumerate(zip(t["tps"], t["rr"]), 1):
        lines.append(f"🎯 <b>TP{i}:</b>  <code>{p(tp)}</code>  <i>(+{pips(abs(tp - t['entry']), t)} pips · 1:{rr:g})</i>")
    lines.append(LINE)
    if t.get("engine_only"):
        lines.append(f"🤖 <b>Engine score:</b> {t['score']}%  {bar(t['score'])}")
        lines.append("<i>AI desk offline – rule engine signal only</i>")
    else:
        lines.append(f"🧠 <b>AI Confidence:</b> {t['confidence']}%  {bar(t['confidence'])}")
        agents = "  ".join(f"{r['icon']}{'✅' if r['vote'] == 'TAKE' else '❌' if r['vote'] == 'SKIP' else '⚠️'}"
                           for r in t.get("reports", []))
        lines.append(f"🤖 <b>AI Desk:</b> {t['votes']}/{len(t.get('reports', [])) or 8} agents agree   {agents}")
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
                                  Btn("🧠 AI Desk", callback_data=f"r:{trade_id}"),
                                  Btn("📋 Full Analysis", callback_data=f"f:{trade_id}")]])


def lot_size(balance: float, risk_pct: float, sl_distance: float, contract: float = 100) -> tuple[float, float]:
    """Lots for a given account risk. Gold: 1 lot = `contract` oz, so $1 move = $contract per lot."""
    risk_usd = balance * risk_pct / 100
    lots = int(risk_usd / (sl_distance * contract) * 100) / 100 if sl_distance > 0 else 0.0
    return max(lots, 0.0), risk_usd


def lot_line(user: dict | None, t: dict, contract: float = 100) -> str:
    if not user:
        return ""
    if not user.get("balance"):
        return "💰 Send <code>/balance 1000</code> to get your lot size on every signal"
    sl = abs(t["entry"] - t["stop_loss"])
    contract = t.get("contract", contract)
    lots, risk_usd = lot_size(user["balance"], user.get("risk", 1.0), sl, contract)
    if lots < 0.01:
        real = sl * contract * 0.01
        return f"💰 <b>Lot 0.01</b> (min) – risks ${real:,.2f}, above your {user.get('risk', 1.0):g}% (${risk_usd:,.2f})"
    return f"💰 <b>Your lot: {lots:.2f}</b>  (risk ${risk_usd:,.2f} = {user.get('risk', 1.0):g}% of ${user['balance']:,.0f})"


def signal_caption(t: dict, lot: str = "") -> str:
    """Short version of the signal card that fits a photo caption (Telegram limit: 1024 characters)."""
    bull = t["direction"] == "BUY"
    icon = "🟢" if bull else "🔴"
    order = f"{t['direction']} {'NOW' if t['entry_type'] == 'MARKET' else 'LIMIT'}"
    risk = abs(t["entry"] - t["stop_loss"])
    lines = [f"{icon} <b>{sym(t)} · {order}</b> · {t['style_label']}"]
    if t.get("headline"):
        lines.append(f"<i>{escape(t['headline'])}</i>")
    lines += [LINE,
              f"📍 Entry  <code>{p(t['entry'])}</code>",
              f"🛑 SL  <code>{p(t['stop_loss'])}</code>  (−{pips(risk, t)} pips)"]
    for i, (tp, rr) in enumerate(zip(t["tps"], t["rr"]), 1):
        lines.append(f"🎯 TP{i}  <code>{p(tp)}</code>  (1:{rr:g})")
    lines.append(LINE)
    if t.get("engine_only"):
        lines.append(f"🤖 Engine score {t['score']}% {bar(t['score'])} · AI offline")
    else:
        agents = "".join(f"{r['icon']}{'✅' if r['vote'] == 'TAKE' else '❌' if r['vote'] == 'SKIP' else '⚠️'} "
                         for r in t.get("reports", []))
        lines.append(f"🧠 Confidence {t['confidence']}% {bar(t['confidence'])}")
        lines.append(f"🤖 {t['votes']}/{len(t.get('reports', [])) or 8} agree  {agents}")
    lines += [f"• {escape(c)}" for c in t["confluences"][:3]]
    if lot:
        lines.append(lot)
    if t["entry_type"] == "LIMIT":
        lines.append(f"⏳ Valid until {hhmm(t['expires_at'])}")
    lines.append("🔒 SL → entry after TP1 · <i>Not financial advice</i>")
    text = "\n".join(lines)
    while len(text) > 1000 and len(lines) > 12:
        lines.pop(-4 if t["entry_type"] == "LIMIT" else -3)
        text = "\n".join(lines)
    return text


def event_message(t: dict, ev: dict) -> str:
    head = f"{sym(t)} {t['direction']} · {t['style_label']}"
    k = ev["kind"]
    if k == "filled":
        return f"✅ <b>ENTRY FILLED</b> at <code>{p(ev['price'])}</code>\n{head}\n🛑 SL <code>{p(t['stop_loss'])}</code>"
    if k == "tp":
        gain = pips(abs(ev["price"] - t["entry"]), t)
        msg = f"🎯 <b>TP{ev['n']} HIT!</b>  +{gain} pips  (1:{ev['rr']:g})\n{head}"
        if ev["n"] == 3:
            msg = f"🏆 <b>TP3 HIT – FULL TARGET!</b>  +{gain} pips  (1:{ev['rr']:g})\n{head}\n🎉 Trade closed in profit"
        elif ev.get("new_sl") is not None:
            msg += f"\n🔒 Move SL to <code>{p(ev['new_sl'])}</code>" + (" (breakeven)" if ev["n"] == 1 else "")
        return msg
    if k == "sl":
        return f"🛑 <b>STOP LOSS HIT</b>  −{pips(abs(t['entry'] - ev['price']), t)} pips  (−1R)\n{head}\n" \
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
    lines = [f"<b>{sym(t)} {t['direction']} · {t['style_label']}</b>", f"Status: <b>{status}</b>"]
    if t["status"] == "active" and price:
        diff = (price - t["entry"]) if bull else (t["entry"] - price)
        lines.append(f"Price: <code>{p(price)}</code>  ·  Floating: <b>{'+' if diff >= 0 else '−'}{pips(abs(diff), t)} pips</b>")
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
    lines = [f"🧠 <b>AI Desk Report</b> · {sym(t)} {t['direction']} · {t['style_label']}", LINE]
    last_stage = None
    for r in t["reports"]:
        if r.get("stage") != last_stage:
            last_stage = r.get("stage")
            lines.append({1: "<b>Stage 1 · Analysts</b>", 2: "<b>Stage 2 · Verifiers</b>"}.get(last_stage, ""))
        vote = {"TAKE": "✅ TAKE", "SKIP": "❌ SKIP"}.get(r["vote"], "⚠️ N/A")
        model = f"  <i>[{escape(r['model'])}]</i>" if r.get("model") else ""
        lines.append(f"{r['icon']} <b>{escape(r['name'])}</b> — {vote} ({r['score']}){model}")
        lines.append(f"<i>{escape(r['summary'])}</i>")
        lines += [f"  • {escape(pt)}" for pt in r["points"]]
        if r.get("debate"):
            lines.append(f"  🗣 <i>Debate: {escape(r['debate'])}</i>" + (" (changed vote)" if r.get("changed") else ""))
        lines.append("")
    lines.append(f"👑 <b>Head Trader</b> — confidence {t['confidence']}%")
    lines.append(f"<i>{escape(t.get('reason', ''))}</i>")
    if t.get("audit"):
        lines.append(f"\n✅ <b>Signal Auditor</b> — {'approved' if t['audit']['approve'] else 'vetoed'}")
        if t["audit"].get("note"):
            lines.append(f"<i>{escape(t['audit']['note'])}</i>")
    return "\n".join(lines)


# ---------- menus ----------

def main_menu(user: dict, is_admin: bool) -> tuple[str, InlineKeyboardMarkup]:
    styles = ", ".join(STYLES[s]["label"] for s in STYLES if s in user.get("styles", [])) or "none"
    lot = f"${user['balance']:,.0f} · {user.get('risk', 1.0):g}% risk" if user.get("balance") else "not set"
    text = (
        "🏆 <b>GOLD SMC AI SIGNALS</b> 🏆\n"
        f"{LINE}\n"
        "🤖 13 AI agents scan <b>🥇 Gold (XAU/USD)</b> and <b>₿ Bitcoin (BTC/USD)</b> on M5 → D1\n"
        "💧 Smart Money Concepts · 📊 Volume · 📰 News filter\n"
        "🎯 Auto TP/SL tracking · 📈 Charts · 💰 Lot sizes\n\n"
        f"🔔 Alerts: <b>{'ON' if user.get('subscribed') else 'OFF'}</b>\n"
        f"📡 Signals: {styles}\n"
        f"💰 Account: {lot}\n\n"
        "Choose an option 👇"
    )
    rows = [
        [Btn("📡 Active Trades", callback_data="trades"), Btn("📜 History", callback_data="hist")],
        [Btn("📊 Performance", callback_data="perf"), Btn("🌍 Market Now", callback_data="mkt")],
        [Btn("🧠 AI Market View", callback_data="aiview"), Btn("📰 News", callback_data="news")],
        [Btn("💰 Risk & Lot", callback_data="risk"), Btn("⚙️ Settings", callback_data="set")],
        [Btn(f"🔔 Alerts: {'ON ✅' if user.get('subscribed') else 'OFF ❌'}", callback_data="alert"),
         Btn("ℹ️ How it works", callback_data="help")],
    ]
    if is_admin:
        rows.append([Btn("⚡ Scan Now", callback_data="scan"), Btn("🩺 Status", callback_data="status"),
                     Btn("🧪 Backtest", callback_data="bt")])
        rows.append([Btn("🎓 Practice AI review (live data)", callback_data="practice")])
    return text, InlineKeyboardMarkup(rows)


def back(extra: list | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup((extra or []) + [[Btn("⬅️ Menu", callback_data="menu")]])


def settings(user: dict) -> tuple[str, InlineKeyboardMarkup]:
    rows = [[Btn(f"{'✅' if s in user.get('styles', []) else '❌'} {STYLES[s]['label']}", callback_data=f"sty:{s}")]
            for s in STYLES]
    rows.append([Btn(f"{'✅' if user.get('briefings', True) else '❌'} 🌅 Session briefings", callback_data="tog:briefings")])
    rows.append([Btn(f"{'✅' if user.get('news_alerts', True) else '❌'} 📰 News alerts", callback_data="tog:news_alerts")])
    text = ("⚙️ <b>Settings</b>\nTap to switch on/off:\n\n"
            "⚡ <b>Scalping</b> – M5 entries, H1 bias, quick trades\n"
            "📊 <b>Intraday</b> – M15 entries, H4 bias, same-day trades\n"
            "🌊 <b>Swing</b> – H1 entries, D1 bias, multi-day trades\n"
            "🌅 <b>Briefings</b> – AI outlook + chart at London & New York open\n"
            "📰 <b>News alerts</b> – warning before high-impact USD news")
    return text, back(rows)


def risk_screen(user: dict, contract: float = 100) -> tuple[str, InlineKeyboardMarkup]:
    bal = user.get("balance")
    risk = user.get("risk", 1.0)
    lines = ["💰 <b>Risk & Lot Size</b>", LINE,
             f"Balance: <b>{'$' + format(bal, ',.2f') if bal else 'not set'}</b>",
             f"Risk per trade: <b>{risk:g}%</b>" + (f"  (${bal * risk / 100:,.2f})" if bal else ""), ""]
    if bal:
        lines.append("<b>Lot size by stop-loss distance</b>")
        for sl_pips in (30, 50, 80, 120, 200):
            lots, _ = lot_size(bal, risk, sl_pips / 10, contract)
            lines.append(f"  SL {sl_pips} pips → <b>{max(lots, 0.01):.2f}</b> lot")
        lines.append("")
    lines += ["Set your balance:  <code>/balance 1000</code>",
              "Custom risk:  <code>/risk 1.5</code>",
              "Quick calculator:  <code>/lot 50</code>  (SL in pips)",
              f"<i>1 lot = {contract:g} oz · 1 pip = $0.10</i>"]
    rows = [[Btn(f"{'✅ ' if abs(risk - r) < 1e-9 else ''}{r:g}%", callback_data=f"rk:{r:g}") for r in (0.5, 1, 2, 3)]]
    return "\n".join(lines), back(rows)


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
            float_txt = f"  {'+' if diff >= 0 else '−'}{pips(abs(diff), t)} pips"
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


def market_dashboard(market: dict | None, session: dict, is_open: bool, scanned_at: datetime | None,
                     name: str = "XAU/USD") -> str:
    if not market:
        return f"🌍 <b>{name} Market Now</b>\n\nNo scan yet – waiting for the first market scan…"
    price = market["5min"]["price"]
    lines = [f"🌍 <b>{name} Market Now</b>  ·  <code>{p(price)}</code>",
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
    pats = [x["name"] for x in market["15min"]["smc"].get("patterns", []) if x.get("age", 0) <= 2]
    if pats:
        lines += ["", f"🕯 <b>M15 candles:</b> {', '.join(pats[-3:])}"]
    adr = market.get("adr") or {}
    if adr.get("adr"):
        lines.append(f"📏 ADR {adr['adr']:.2f} · today {adr['today_range']:.2f} ({adr['used_pct']}% used)")
    ind = market["1h"]["ind"]
    if ind["rsi"] is not None:
        lines += ["", f"⚙️ H1 RSI {ind['rsi']:.0f} · ATR(M15) {market['15min']['smc']['atr']:.2f}"]
    if scanned_at:
        lines.append(f"\n🔎 Last scan {scanned_at.strftime('%H:%M UTC')}")
    return "\n".join(lines)


HELP = (
    "ℹ️ <b>How the bot works</b>\n" + LINE + "\n"
    "1️⃣ Every 5 minutes it reads gold on <b>M5, M15, H1, H4, D1</b> (+ PAXG volume).\n"
    "2️⃣ The <b>SMC engine</b> maps BOS/CHoCH, order blocks, fair value gaps, liquidity pools & sweeps, "
    "PDH/PDL, weekly high/low, the Asian range, premium/discount, killzones and volume.\n"
    "3️⃣ A setup needs: higher-timeframe bias + a sweep or structure shift + an OB/FVG to enter from + "
    "a clear path to TP1.\n"
    "4️⃣ A <b>13-agent AI desk</b> works as a pipeline: 8 analysts (structure, liquidity, order blocks, FVGs, "
    "volume, price action, indicators, news) → 3 verifiers who cross-check their reports (confluence, risk, "
    "devil's advocate) → 👑 Head Trader → ✅ Signal Auditor.\n"
    "5️⃣ 📰 No new trades 30 min around high-impact USD news; you get a warning before it.\n"
    "6️⃣ Signals come with a 📈 chart, 💰 your lot size, and are <b>tracked live</b>: entry, TP1/TP2/TP3, "
    "SL, expiry – as replies on the signal.\n"
    "7️⃣ Results are booked honestly: 1/3 closed at each TP, SL to breakeven after TP1.\n\n"
    "<b>Commands</b>: /menu /trades /history /stats /market /news /balance /risk /lot /stop\n\n"
    f"<i>{DISCLAIMER}</i>"
)


def daily_report(s: dict, open_count: int) -> str:
    text = performance(s, title="🌙 <b>Daily Report · Gold & Bitcoin</b>")
    return text + f"\n\n📡 Open trades: {open_count}\n🕒 {datetime.now(timezone.utc).strftime('%d %b %Y')}"


def market_keyboard(key: str = "XAUUSD", instruments: list | None = None) -> InlineKeyboardMarkup:
    rows = [[Btn("📈 M15 Chart", callback_data=f"chart:15min:{key}"), Btn("📈 H1 Chart", callback_data=f"chart:1h:{key}"),
             Btn("📈 H4 Chart", callback_data=f"chart:4h:{key}")],
            [Btn("🔄 Refresh", callback_data=f"mkt:{key}"), Btn("🧠 AI view", callback_data=f"aiview:{key}")]]
    others = [i for i in (instruments or []) if i["key"] != key]
    if others:
        rows.append([Btn(f"{i['icon']} {i['label']} ({i['name']})", callback_data=f"mkt:{i['key']}") for i in others])
    return back(rows)


def news_screen(events: list[dict], blackout: dict | None, error: str | None, headlines: list | None = None) -> str:
    now = datetime.now(timezone.utc)
    lines = ["📰 <b>Economic Calendar · USD</b>", LINE]
    if blackout:
        lines += [f"⛔ <b>News pause active:</b> {escape(blackout['title'])} – no new signals right now", ""]
    if not events:
        lines.append("No high/medium impact USD news in the next 7 days." if not error else
                     "⚠️ Calendar unavailable right now.")
    day = None
    for e in events[:25]:
        d = e["time"].strftime("%a %d %b")
        if d != day:
            lines += ["", f"<b>{d}</b>"]
            day = d
        mins = int((e["time"] - now).total_seconds() // 60)
        when = "now" if -30 <= mins <= 0 else f"in {mins // 60}h {mins % 60}m" if 0 < mins < 24 * 60 else ""
        extra = " · ".join(x for x in (f"F {e['forecast']}" if e["forecast"] else "",
                                       f"P {e['previous']}" if e["previous"] else "") if x)
        lines.append(f"{'🔴' if e['impact'] == 'High' else '🟠'} {e['time'].strftime('%H:%M')}  {escape(e['title'])}"
                     + (f"  <i>{extra}</i>" if extra else "") + (f"  ⏰ {when}" if when else ""))
    if headlines:
        lines += ["", "<b>🗞 Latest gold / USD headlines</b>"]
        lines += [f"{'🥇' if h['gold'] else '•'} {h['time'].strftime('%H:%M') if h['time'] else ''} "
                  f"<a href=\"{escape(h['link'])}\">{escape(h['title'])}</a>" if h["link"] else f"• {escape(h['title'])}"
                  for h in headlines]
    lines += ["", "<i>🔴 high impact: the bot pauses new signals 30 min before/after</i>"]
    return "\n".join(lines)


def news_alert(e: dict, minutes: int) -> str:
    extra = " · ".join(x for x in (f"Forecast {e['forecast']}" if e["forecast"] else "",
                                   f"Previous {e['previous']}" if e["previous"] else "") if x)
    return (f"⚠️ <b>HIGH-IMPACT NEWS in {minutes} min</b>\n"
            f"🔴 {e['country']} · <b>{escape(e['title'])}</b> at {e['time'].strftime('%H:%M UTC')}\n"
            + (f"{extra}\n" if extra else "") +
            "\nGold can move very fast. Consider moving open trades to breakeven or taking partial profit.\n"
            "⛔ New signals are paused around the release.")


def status_screen(info: dict) -> str:
    ok = lambda b: "✅" if b else "⚠️"  # noqa: E731
    lines = ["🩺 <b>Bot Status</b>", LINE,
             f"⏱ Uptime: {info['uptime']}",
             f"{'🟢 Market open' if info['market_open'] else '🔴 Market closed'}",
             f"🔎 Last scan: {info['last_scan']}  ·  scans today: {info['scans_today']}",
             f"{ok(not info['fail_count'])} Scan errors in a row: {info['fail_count']}"
             + (f"\n   <i>{escape(info['last_error'][:150])}</i>" if info["last_error"] else ""),
             f"📈 Twelve Data requests today: {info['td_requests']} / 800",
             f"🧠 Gemini calls today: {info['ai_calls']} (failed {info['ai_failures']})",
             f"{ok(info['volume_ok'])} Volume feed (PAXG): {'OK' if info['volume_ok'] else 'unavailable'}",
             f"{ok(info['news_ok'])} News calendar: {'OK' if info['news_ok'] else 'unavailable'}",
             f"📡 Open trades: {info['open_trades']}  ·  👥 users: {info['users']}",
             "", "<b>Last scan</b>"]
    lines += [f"• {escape(n)}" for n in info["notes"]] or ["• –"]
    return "\n".join(lines)


def backtest_report(r: dict) -> str:
    if r.get("error"):
        return f"🧪 Backtest failed: {escape(r['error'])}"
    s = r["stats"]
    lines = [f"🧪 <b>Backtest · {r['label']}</b>  (engine only, no AI)", LINE,
             f"📅 {r['start']} → {r['end']}  ·  {r['steps']} scans",
             f"📡 Signals: <b>{r['signals']}</b>  ·  expired/cancelled: {s['expired']}"]
    if s["trades"]:
        lines += [f"🎯 Win rate (TP1+): <b>{s['win_rate']}%</b>  {bar(s['win_rate'])}",
                  f"✅ {s['wins']} wins  ❌ {s['losses']} losses  🏆 {s['tp3']} hit TP3",
                  f"💰 Total: <b>{'+' if s['total_r'] >= 0 else ''}{s['total_r']}R</b>  ·  "
                  f"avg {r['avg_r']:+.2f}R/trade",
                  f"📉 Max drawdown: {r['max_dd']}R  ·  profit factor: {r['profit_factor']}"]
    lines.append("\n<i>Past results do not guarantee future results. The live bot also filters with the AI desk"
                 " and news, which the backtest does not.</i>")
    return "\n".join(lines)


def _cfg_text(c: dict) -> str:
    tp1 = f"TP1 ≤ {c['tp1_max_r']:g}R" if c.get("tp1_max_r") else "TP1 at liquidity"
    mode = "🛡 strict" if c.get("strict") else "normal"
    return f"{mode} · score ≥ {c['min_score']} · min 1:{c['min_rr']:g} · {tp1}"


def backtest_menu(params: dict) -> str:
    lines = ["🧪 <b>Backtest & Optimize</b>", LINE,
             "<b>Backtest</b> – replay the last weeks of real gold data with the current settings.",
             "<b>Optimize</b> – test 16 settings (strict/normal, score, R:R, TP1) on the same data and apply the best.", "",
             "<b>Current settings</b>"]
    for s, p_ in params.items():
        state = "✅" if p_["enabled"] else "⛔ OFF"
        lines.append(f"{state} {STYLES[s]['label']}: {_cfg_text(p_)}")
    lines.append("\n<i>M5 history covers ~3 weeks, so scalping results use fewer days than swing.</i>")
    return "\n".join(lines)


def optimize_report(o: dict, current: dict) -> tuple[str, InlineKeyboardMarkup]:
    if o.get("error"):
        return f"🔧 Optimize failed: {escape(o['error'])}", back()
    style = o["style"]
    lines = [f"🔧 <b>Optimizer · {o['label']}</b>  (engine only)", LINE, f"📅 {o['start']} → {o['end']}",
             f"Current: {_cfg_text(current)}", ""]
    ranked = o["ranked"]
    rows = []
    if not ranked:
        lines.append(f"Not enough trades (need ≥ {o['min_trades']}) in any setting to judge this style.")
    medals = ["🥇", "🥈", "🥉", "4.", "5."]
    for i, r in enumerate(ranked[:5]):
        lines.append(f"{medals[i]} {_cfg_text(r['config'])}")
        lines.append(f"    {r['trades']} trades · win {r['win_rate']}% · <b>{'+' if r['total_r'] >= 0 else ''}"
                     f"{r['total_r']}R</b> · PF {r['profit_factor']:g} · DD {r['max_dd']}R")
    best = ranked[0] if ranked else None
    if best and best["total_r"] > 0:
        lines += ["", "✅ Tap a button to use one of these settings for live signals."]
        rows.append([Btn(f"Apply {medals[i]}", callback_data=f"apply:{style}:{i}") for i in range(min(3, len(ranked)))])
    else:
        lines += ["", "⚠️ <b>No profitable setting on this data.</b> Consider switching this style off for now."]
    rows.append([Btn(f"⛔ Switch {STYLES[style]['label'][2:]} off", callback_data=f"soff:{style}"),
                 Btn("↩️ Default settings", callback_data=f"son:{style}")])
    lines.append("\n<i>Optimizing on a few weeks can over-fit. Prefer settings that are also good in the 2nd/3rd"
                 " place, and re-check every week.</i>")
    return "\n".join(lines), back(rows)
