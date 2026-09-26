"""Everything the user sees on Telegram: signal cards, trade updates, menus and dashboards (HTML).

House style: no emoji. Clean headers in capitals, aligned numbers in monospace blocks, plain-word buttons.
"""

import re
from datetime import datetime, timezone
from html import escape

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup

from setups import STYLES, TF_LABEL
from tracker import pips as _pips

LINE = "────────────────────────"
DISCLAIMER = "Not financial advice. Always use a stop loss and risk 1–2% per trade."
TREND = {"bullish": "Bullish", "bearish": "Bearish", None: "Ranging"}

_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍⃣₿]")


def plain(text: str) -> str:
    """Text without emoji (older stored trades still carry emoji style labels)."""
    return re.sub(r"\s{2,}", " ", _EMOJI.sub("", text or "")).strip()


def style_name(s: str) -> str:
    return plain(STYLES[s]["label"]) if s in STYLES else s


def p(x: float) -> str:
    return f"{x:,.2f}"


def sym(t: dict) -> str:
    return t.get("symbol_name", "XAU/USD")


def label(t: dict) -> str:
    return plain(t.get("style_label", ""))


def pips(diff: float, t: dict | None = None) -> float:
    return _pips(diff, (t or {}).get("pip", 0.1))


def bar(pct: int | float | None, width: int = 10) -> str:
    n = max(0, min(width, round((pct or 0) / 100 * width)))
    return "█" * n + "░" * (width - n)


def hhmm(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%d %b %H:%M UTC")


def signed(x: float, unit: str = "") -> str:
    return f"{'+' if x >= 0 else '−'}{abs(x):g}{unit}"


def _order(t: dict) -> str:
    return f"{t['direction']} {'MARKET' if t['entry_type'] == 'MARKET' else 'LIMIT'}"


# ---------- signals ----------

def _levels_block(t: dict) -> str:
    risk = abs(t["entry"] - t["stop_loss"])
    rows = [f"ENTRY   {p(t['entry']):>11}",
            f"STOP    {p(t['stop_loss']):>11}   −{pips(risk, t)} pips"]
    for i, (tp, rr) in enumerate(zip(t["tps"], t["rr"]), 1):
        rows.append(f"TP{i}     {p(tp):>11}   +{pips(abs(tp - t['entry']), t)} pips  1:{rr:g}")
    return "<pre>" + "\n".join(rows) + "</pre>"


def _desk_block(t: dict) -> str:
    if t.get("engine_only"):
        return f"<pre>ENGINE SCORE  {t['score']:>3}%  {bar(t['score'])}\nAI DESK       offline</pre>"
    rows = [f"CONFIDENCE  {t['confidence']:>4}%  {bar(t['confidence'])}"]
    if t.get("conviction") is not None:
        rows.append(f"CONVICTION  {t['conviction']:>4}%  {bar(t['conviction'])}")
    if t.get("smc_grade"):
        ck = t.get("smc_checklist") or {}
        rows.append(f"SMC GRADE   {t['smc_grade']:>5}  {sum(ck.values())}/{len(ck)} checks")
    d, v, b = t.get("per_desk"), t.get("desk_votes") or {}, t.get("board") or {}
    if d:
        rows += [f"TECHNICAL   {str(d.get('tech')):>5}", f"STRATEGY    {str(d.get('strategy')):>5}",
                 f"MACRO       {str(d.get('macro')):>5}",
                 f"DESK LEADS  {str(v.get('leads')) + '/3':>5}", f"VERIFIERS   {str(v.get('verifiers')) + '/3':>5}"]
        if b.get("agrees") is not None:
            rows.append(f"STRATEGIES  {b['agrees']:>2} for / {b['against']} against")
    else:
        rows.append(f"AGENTS      {t.get('votes', 0)}/{len(t.get('reports', [])) or 8} agree")
    return "<pre>" + "\n".join(rows) + "</pre>"


def desk_line(t: dict) -> str:
    """How every layer of the AI desk voted, in one line."""
    reports = t.get("reports", [])
    if not t.get("per_desk"):
        return f"{t.get('votes', 0)}/{len(reports) or 8} agents agree"
    d, v, b = t["per_desk"], t.get("desk_votes") or {}, t.get("board") or {}
    out = (f"Technical {d.get('tech')} · Strategy {d.get('strategy')} · Macro {d.get('macro')} · "
           f"Leads {v.get('leads')}/3 · Verifiers {v.get('verifiers')}/3")
    if b.get("agrees") is not None:
        out += f" · Strategies {b['agrees']}/{b['agrees'] + b['against']}"
    return out


def signal_card(t: dict) -> str:
    tfs = t["timeframes"]
    lines = [f"<b>{sym(t)}  ·  {_order(t)}  ·  {label(t).upper()}</b>"]
    if t.get("headline"):
        lines.append(f"<i>{escape(t['headline'])}</i>")
    lines += [_levels_block(t), "<b>DESK VERDICT</b>", _desk_block(t)]
    if t.get("explain") or t.get("reason"):
        lines += ["<b>WHY THIS TRADE</b>", f"<blockquote>{escape(t.get('explain') or t['reason'])}</blockquote>"]
    ev = [f"+ {escape(x)}" for x in t.get("evidence_for") or []] + [f"− {escape(x)}" for x in t.get("evidence_against") or []]
    if ev:
        lines += ["<b>EVIDENCE</b>"] + ev
    if t.get("invalidation"):
        lines += ["<b>INVALIDATION</b>", escape(t["invalidation"])]
    if t.get("management"):
        lines += ["<b>MANAGEMENT</b>", escape(t["management"])]
    if t.get("risks"):
        lines += ["<b>RISKS</b>"] + [f"– {escape(x)}" for x in t["risks"]]
    if t.get("smc_checklist"):
        ck = t["smc_checklist"]
        lines += ["<b>SMC CHECKLIST</b>", "<pre>" + "\n".join(f"[{'x' if v else ' '}] {k}" for k, v in ck.items()) + "</pre>"]
    lines.append("<b>CONFLUENCE</b>")
    lines += [f"– {escape(plain(c))}" for c in t["confluences"][:7]]
    lines += ["", f"Timeframes  {TF_LABEL[tfs['bias']]} › {TF_LABEL[tfs['confirm']]} › {TF_LABEL[tfs['entry']]}",
              f"Issued  {hhmm(t['created_at'])}"]
    if t["entry_type"] == "LIMIT":
        lines.append(f"Limit valid until  {hhmm(t['expires_at'])}")
    lines += ["Move the stop to entry once TP1 is hit.", f"<i>{DISCLAIMER}</i>"]
    return "\n".join(lines)


def signal_keyboard(trade_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[Btn("Live status", callback_data=f"t:{trade_id}"),
                                  Btn("Desk report", callback_data=f"r:{trade_id}"),
                                  Btn("Full analysis", callback_data=f"f:{trade_id}")]])


def lot_size(balance: float, risk_pct: float, sl_distance: float, contract: float = 100) -> tuple[float, float]:
    """Lots for a given account risk. Gold: 1 lot = `contract` oz, so $1 move = $contract per lot."""
    risk_usd = balance * risk_pct / 100
    lots = int(risk_usd / (sl_distance * contract) * 100) / 100 if sl_distance > 0 else 0.0
    return max(lots, 0.0), risk_usd


def lot_line(user: dict | None, t: dict, contract: float = 100) -> str:
    if not user:
        return ""
    if not user.get("balance"):
        return "Position size: send <code>/balance 1000</code> to get your lot size on every signal"
    sl = abs(t["entry"] - t["stop_loss"])
    contract = t.get("contract", contract)
    lots, risk_usd = lot_size(user["balance"], user.get("risk", 1.0), sl, contract)
    if lots < 0.01:
        real = sl * contract * 0.01
        return (f"<b>Position size: 0.01 lot</b> (minimum) – risks ${real:,.2f}, above your "
                f"{user.get('risk', 1.0):g}% (${risk_usd:,.2f})")
    return (f"<b>Position size: {lots:.2f} lot</b> – risk ${risk_usd:,.2f} "
            f"({user.get('risk', 1.0):g}% of ${user['balance']:,.0f})")


def signal_caption(t: dict, lot: str = "") -> str:
    """Short version of the signal card that fits a photo caption (Telegram limit: 1024 characters)."""
    lines = [f"<b>{sym(t)}  ·  {_order(t)}  ·  {label(t).upper()}</b>"]
    if t.get("headline"):
        lines.append(f"<i>{escape(t['headline'])}</i>")
    lines.append(_levels_block(t))
    if t.get("engine_only"):
        lines.append(f"Engine score {t['score']}% · AI desk offline")
    else:
        lines.append(f"<b>Confidence {t['confidence']}%</b>  {bar(t['confidence'])}")
        lines.append(desk_line(t))
    lines += [f"– {escape(plain(c))}" for c in t["confluences"][:3]]
    if t.get("invalidation"):
        lines.append(f"<i>Invalid if: {escape(t['invalidation'][:150])}</i>")
    if lot:
        lines.append(lot)
    if t["entry_type"] == "LIMIT":
        lines.append(f"Valid until {hhmm(t['expires_at'])}")
    lines.append("Stop to entry after TP1 · <i>Not financial advice</i>")
    text = "\n".join(lines)
    while len(text) > 1000 and len(lines) > 6:
        lines.pop(-4 if t["entry_type"] == "LIMIT" else -3)
        text = "\n".join(lines)
    return text


def event_message(t: dict, ev: dict) -> str:
    head = f"{sym(t)} {t['direction']} · {label(t)}"
    k = ev["kind"]
    if k == "filled":
        return f"<b>ENTRY FILLED</b>  <code>{p(ev['price'])}</code>\n{head}\nStop <code>{p(t['stop_loss'])}</code>"
    if k == "tp":
        gain = pips(abs(ev["price"] - t["entry"]), t)
        if ev["n"] == 3:
            return f"<b>TP3 HIT – FULL TARGET</b>  +{gain} pips  (1:{ev['rr']:g})\n{head}\nTrade closed in profit."
        msg = f"<b>TP{ev['n']} HIT</b>  +{gain} pips  (1:{ev['rr']:g})\n{head}"
        if ev.get("new_sl") is not None:
            msg += f"\nMove stop to <code>{p(ev['new_sl'])}</code>" + (" (breakeven)" if ev["n"] == 1 else "")
        return msg
    if k == "sl":
        return (f"<b>STOP LOSS HIT</b>  −{pips(abs(t['entry'] - ev['price']), t)} pips  (−1R)\n{head}\n"
                "<i>Losses are part of the process. The desk keeps scanning.</i>")
    if k == "protected_stop":
        return (f"<b>CLOSED AT PROTECTED STOP</b>  <code>{p(ev['price'])}</code> after TP{ev['stage']}\n{head}\n"
                "Profit secured.")
    if k == "expired":
        return (f"<b>ORDER EXPIRED</b> – price never returned to <code>{p(t['entry'])}</code>\n{head}\n"
                "Cancel the pending order.")
    if k == "cancelled":
        return f"<b>CANCELLED</b> – price reached TP1 without filling the entry\n{head}\nCancel the pending order."
    if k == "timeout":
        return f"<b>CLOSED AFTER 7 DAYS</b>  <code>{p(ev['price'])}</code>\n{head}"
    return head


def trade_status(t: dict, price: float | None) -> str:
    bull = t["direction"] == "BUY"
    status = {"pending": "Waiting for entry", "active": "Running", "closed": "Closed"}[t["status"]]
    lines = [f"<b>{sym(t)}  ·  {t['direction']}  ·  {label(t).upper()}</b>", f"Status: <b>{status}</b>"]
    if t["status"] == "active" and price:
        diff = (price - t["entry"]) if bull else (t["entry"] - price)
        lines.append(f"Price <code>{p(price)}</code> · floating <b>{'+' if diff >= 0 else '−'}{pips(abs(diff), t)} pips</b>")
    rows = [f"ENTRY  {p(t['entry']):>11}", f"STOP   {p(t['stop_loss']):>11}"]
    rows += [f"TP{i}    {p(tp):>11}   {'hit' if t['stage'] >= i else 'open'}" for i, tp in enumerate(t["tps"], 1)]
    lines.append("<pre>" + "\n".join(rows) + "</pre>")
    if t["status"] == "closed":
        r = t.get("result_r") or 0
        lines.append(f"Result: <b>{t['outcome'].upper()}</b>  ({signed(r, 'R')})")
    lines.append(f"Opened {hhmm(t['created_at'])}")
    return "\n".join(lines)


def ai_report(t: dict) -> str:
    if t.get("engine_only") or not t.get("reports"):
        return "This signal came from the rule engine only (the AI desk was offline)."
    lines = [f"<b>AI DESK REPORT</b>  ·  {sym(t)} {t['direction']} · {label(t)}", LINE]
    names = ({1: "STAGE 1 · ANALYSTS", 2: "STAGE 2 · DESK LEADS", 3: "STAGE 3 · VERIFIERS"} if t.get("per_desk")
             else {1: "STAGE 1 · ANALYSTS", 2: "STAGE 2 · VERIFIERS"})
    last_stage = None
    for r in t["reports"]:
        if r.get("stage") != last_stage:
            last_stage = r.get("stage")
            lines.append(f"\n<b>{names.get(last_stage, '')}</b>")
        vote = {"TAKE": "TAKE", "SKIP": "SKIP", "NEUTRAL": "NEUT"}.get(r["vote"], "N/A")
        if t.get("per_desk") and r.get("stage") == 1:  # 18 analysts: one compact line each
            lines.append(f"<code>{vote:<4} {r['score']:>3}</code>  {escape(r['name'])}"
                         + (" · debated" + (", changed vote" if r.get("changed") else "") if r.get("debate") else "")
                         + f" – <i>{escape(r['summary'][:110])}</i>")
            continue
        model = f"  <i>[{escape(r['model'])}]</i>" if r.get("model") else ""
        lines.append(f"<code>{vote:<4} {r['score']:>3}</code>  <b>{escape(r['name'])}</b>{model}")
        lines.append(f"<i>{escape(r['summary'])}</i>")
        lines += [f"  – {escape(plain(pt))}" for pt in r["points"]]
        if r.get("debate"):
            lines.append(f"  Debate: <i>{escape(r['debate'])}</i>" + (" (changed vote)" if r.get("changed") else ""))
    lines += ["", f"<b>HEAD TRADER</b>  confidence {t['confidence']}%", f"<i>{escape(t.get('explain') or t.get('reason', ''))}</i>"]
    lines += [f"  + {escape(x)}" for x in t.get("evidence_for") or []]
    lines += [f"  − {escape(x)}" for x in t.get("evidence_against") or []]
    if t.get("invalidation"):
        lines.append(f"  Invalidation: {escape(t['invalidation'])}")
    if t.get("audit"):
        lines.append(f"\n<b>SIGNAL AUDITOR</b>  {'approved' if t['audit']['approve'] else 'vetoed'}")
        if t["audit"].get("note"):
            lines.append(f"<i>{escape(t['audit']['note'])}</i>")
    text = "\n".join(lines)
    while len(text) > 4000 and len(lines) > 5:  # Telegram message limit: drop detail lines from the middle
        lines.pop(len(lines) // 2)
        text = "\n".join(lines)
    return text


def desk_record(records: dict, agents: list[dict], closed: int) -> str:
    """How often each agent was right on finished signals (the desk weighs votes by this)."""
    lines = ["<b>AI DESK TRACK RECORD</b>", LINE,
             f"Finished signals analysed: <b>{closed}</b>",
             "An agent is right when it said TAKE and the trade reached TP1, or SKIP and it lost.", ""]
    rows = [(a, records.get(a["key"])) for a in agents]
    rows = sorted([x for x in rows if x[1] and x[1]["n"]], key=lambda x: -x[1]["accuracy"])
    if not rows:
        lines.append("No finished signals with AI votes yet. The record builds up automatically.")
        return "\n".join(lines)
    table = [f"{'AGENT':<22}{'RIGHT':>6}{'N':>4}{'WEIGHT':>8}"]
    table += [f"{a['name'][:21]:<22}{r['accuracy']:>5}%{r['n']:>4}{r['weight']:>8.2f}" for a, r in rows[:26]]
    lines.append("<pre>" + "\n".join(table) + "</pre>")
    lines.append("<i>Weight 1.00 = neutral. Reliable agents count more in the weighted agreement.</i>")
    return "\n".join(lines)


# ---------- menus ----------

def _on(flag: bool) -> str:
    return "ON" if flag else "OFF"


def main_menu(user: dict, is_admin: bool) -> tuple[str, InlineKeyboardMarkup]:
    styles = ", ".join(style_name(s) for s in STYLES if s in user.get("styles", [])) or "none"
    lot = f"${user['balance']:,.0f} · {user.get('risk', 1.0):g}% risk" if user.get("balance") else "not set"
    text = (
        "<b>GOLD & BITCOIN · SMC AI DESK</b>\n"
        f"{LINE}\n"
        "26 AI agents in three desks review every setup on <b>XAU/USD</b> and <b>BTC/USD</b>, M5 to D1.\n"
        "Smart Money Concepts, 17-strategy board, volume, news and market regime.\n"
        "Every signal is tracked live: entry, TP1–TP3, stop, expiry.\n\n"
        f"<pre>ALERTS    {_on(user.get('subscribed'))}\n"
        f"SIGNALS   {styles}\n"
        f"ACCOUNT   {lot}</pre>"
    )
    rows = [
        [Btn("Open trades", callback_data="trades"), Btn("History", callback_data="hist")],
        [Btn("Performance", callback_data="perf"), Btn("Market now", callback_data="mkt")],
        [Btn("AI market view", callback_data="aiview"), Btn("News", callback_data="news")],
        [Btn("AI desk record", callback_data="desk"), Btn("Risk & lot size", callback_data="risk")],
        [Btn(f"Alerts: {_on(user.get('subscribed'))}", callback_data="alert"), Btn("Settings", callback_data="set")],
        [Btn("How it works", callback_data="help")],
    ]
    if is_admin:
        rows.append([Btn("Scan now", callback_data="scan"), Btn("Status", callback_data="status"),
                     Btn("Backtest", callback_data="bt")])
        rows.append([Btn("Practice AI review (live data)", callback_data="practice")])
    return text, InlineKeyboardMarkup(rows)


def back(extra: list | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup((extra or []) + [[Btn("‹ Menu", callback_data="menu")]])


def settings(user: dict) -> tuple[str, InlineKeyboardMarkup]:
    rows = [[Btn(f"{style_name(s)}: {_on(s in user.get('styles', []))}", callback_data=f"sty:{s}")] for s in STYLES]
    rows.append([Btn(f"Session briefings: {_on(user.get('briefings', True))}", callback_data="tog:briefings")])
    rows.append([Btn(f"News alerts: {_on(user.get('news_alerts', True))}", callback_data="tog:news_alerts")])
    text = ("<b>SETTINGS</b>\nTap to switch on or off.\n\n"
            "<b>Scalping</b> – M5 entries, H1 bias, quick trades\n"
            "<b>Intraday</b> – M15 entries, H4 bias, same-day trades\n"
            "<b>Swing</b> – H1 entries, D1 bias, multi-day trades\n"
            "<b>Briefings</b> – AI outlook and chart at the London and New York open\n"
            "<b>News alerts</b> – warning before high-impact USD news")
    return text, back(rows)


def risk_screen(user: dict, contract: float = 100) -> tuple[str, InlineKeyboardMarkup]:
    bal = user.get("balance")
    risk = user.get("risk", 1.0)
    lines = ["<b>RISK & POSITION SIZE</b>", LINE,
             f"<pre>BALANCE   {'$' + format(bal, ',.2f') if bal else 'not set'}\n"
             f"RISK      {risk:g}%" + (f"  (${bal * risk / 100:,.2f})" if bal else "") + "</pre>"]
    if bal:
        rows = [f"{'STOP (pips)':<12}{'LOTS':>6}"]
        for sl_pips in (30, 50, 80, 120, 200):
            lots, _ = lot_size(bal, risk, sl_pips / 10, contract)
            rows.append(f"{sl_pips:<12}{max(lots, 0.01):>6.2f}")
        lines += ["<b>Gold lot size by stop distance</b>", "<pre>" + "\n".join(rows) + "</pre>"]
    lines += ["Set balance: <code>/balance 1000</code>",
              "Custom risk: <code>/risk 1.5</code>",
              "Quick calculator: <code>/lot 50</code> (stop in pips)",
              f"<i>Gold: 1 lot = {contract:g} oz · 1 pip = $0.10. Bitcoin signals size their own lot.</i>"]
    rows = [[Btn(f"{'› ' if abs(risk - r) < 1e-9 else ''}{r:g}%", callback_data=f"rk:{r:g}") for r in (0.5, 1, 2, 3)]]
    return "\n".join(lines), back(rows)


def trades_list(trades: list[dict], price: float | None) -> tuple[str, InlineKeyboardMarkup]:
    if not trades:
        return "<b>OPEN TRADES</b>\n\nNo open trades right now. The desk is scanning.", back()
    lines = ["<b>OPEN TRADES</b>", LINE]
    rows = []
    for t in trades[-8:]:
        state = "PENDING" if t["status"] == "pending" else "RUNNING"
        float_txt = ""
        if t["status"] == "active" and price and t.get("instrument", "XAUUSD") == "XAUUSD":
            diff = (price - t["entry"]) if t["direction"] == "BUY" else (t["entry"] - price)
            float_txt = f" · {'+' if diff >= 0 else '−'}{pips(abs(diff), t)} pips"
        lines.append(f"<code>{state:<8}</code> {sym(t)} {t['direction']} @ {p(t['entry'])} · {label(t)} · "
                     f"TP {t['stage']}/3{float_txt}")
        rows.append([Btn(f"{sym(t)} {t['direction']} {p(t['entry'])}", callback_data=f"t:{t['id']}")])
    return "\n".join(lines), back(rows)


def history(trades: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    if not trades:
        return "<b>HISTORY</b>\n\nNo closed trades yet.", back()
    lines = ["<b>LAST 10 TRADES</b>", LINE]
    table = []
    for t in trades[-10:][::-1]:
        r = t.get("result_r") or 0
        table.append(f"{hhmm(t['created_at'])[:6]}  {sym(t)[:3]} {t['direction']:<4} {t['outcome'][:9].upper():<9} "
                     f"TP{t['stage']}  {signed(r, 'R'):>6}")
    lines.append("<pre>" + "\n".join(table) + "</pre>")
    return "\n".join(lines), back()


def performance(s: dict, title: str = "<b>PERFORMANCE</b>") -> str:
    if not s["trades"]:
        return f"{title}\n\nNo finished trades yet."
    rows = [f"WIN RATE   {s['win_rate']:>4}%  {bar(s['win_rate'])}",
            f"WINS       {s['wins']:>5}",
            f"LOSSES     {s['losses']:>5}",
            f"FULL TP3   {s['tp3']:>5}",
            f"TOTAL      {signed(s['total_r'], 'R'):>6}"]
    lines = [title, LINE, "<pre>" + "\n".join(rows) + "</pre>"]
    by = [f"{style_name(k):<10}{v['wins']:>3}/{v['trades']:<3} {v['win_rate']:>4}%"
          for k, v in s["by_style"].items() if v["trades"]]
    if by:
        lines += ["<b>By style</b>", "<pre>" + "\n".join(by) + "</pre>"]
    lines.append("<i>A trade counts as a win once TP1 is hit. Results book 1/3 at each target.</i>")
    return "\n".join(lines)


def market_dashboard(market: dict | None, session: dict, is_open: bool, scanned_at: datetime | None,
                     name: str = "XAU/USD") -> str:
    if not market:
        return f"<b>{name} · MARKET NOW</b>\n\nNo scan yet – waiting for the first market scan."
    price = market["5min"]["price"]
    lines = [f"<b>{name} · MARKET NOW</b>  <code>{p(price)}</code>",
             f"{'Market open' if is_open else 'Market closed'} · {', '.join(session['sessions'])}"
             + (f" · {session['killzone']} killzone" if session.get("killzone") else ""), LINE]
    rows = [f"{'TF':<4}{'TREND':<9}{'REGIME':<12}{'ZONE':<12}LAST"]
    for tf in ("1day", "4h", "1h", "15min", "5min"):
        s = market[tf]["smc"]
        ev = s["last_event"]
        reg = (market[tf].get("regime") or {}).get("regime", "–")
        rows.append(f"{TF_LABEL[tf]:<4}{TREND[s['trend']]:<9}{reg:<12}{s['range']['zone']:<12}{ev['type'] if ev else '–'}")
    lines += ["<b>Structure</b>", "<pre>" + "\n".join(rows) + "</pre>"]
    h1 = market["1h"]["smc"]["liquidity"]
    lines += ["<b>Liquidity (H1)</b>",
              f"Above: {', '.join(p(x) for x in h1['buy_side'][:3]) or '–'}",
              f"Below: {', '.join(p(x) for x in h1['sell_side'][:3]) or '–'}"]
    zones = [f"OB  {z['direction'][:4]}  {p(z['bottom'])} – {p(z['top'])}" for z in market["15min"]["smc"]["order_blocks"][-3:]]
    zones += [f"FVG {z['direction'][:4]}  {p(z['bottom'])} – {p(z['top'])}" for z in market["15min"]["smc"]["fvgs"][-2:]]
    if zones:
        lines += ["<b>Zones (M15)</b>", "<pre>" + "\n".join(zones) + "</pre>"]
    v = market["15min"]["volume"]
    lines.append("<b>Volume (M15)</b>")
    if v.get("available"):
        lines.append(f"Pressure {v['pressure']} (delta {v['delta']:+.2f}) · RVOL {v['relative_volume_last_closed']}x")
        lines.append(f"POC {p(v['poc'])} · value area {p(v['value_area_low'])} – {p(v['value_area_high'])}")
    else:
        lines.append("Not available right now")
    pats = [x["name"] for x in market["15min"]["smc"].get("patterns", []) if x.get("age", 0) <= 2]
    if pats:
        lines.append(f"<b>M15 candles:</b> {', '.join(pats[-3:])}")
    adr = market.get("adr") or {}
    if adr.get("adr"):
        lines.append(f"ADR {adr['adr']:.2f} · today {adr['today_range']:.2f} ({adr['used_pct']}% used)")
    ind = market["1h"]["ind"]
    if ind["rsi"] is not None:
        lines.append(f"H1 RSI {ind['rsi']:.0f} · ATR(M15) {market['15min']['smc']['atr']:.2f}")
    piv = market.get("pivots") or {}
    if piv:
        lines.append("Pivots  " + " · ".join(f"{k} {p(v)}" for k, v in piv.items()))
    if scanned_at:
        lines.append(f"\n<i>Last scan {scanned_at.strftime('%H:%M UTC')}</i>")
    return "\n".join(lines)


HELP = (
    "<b>HOW THE DESK WORKS</b>\n" + LINE + "\n"
    "<b>1. Data.</b> Every 5 minutes gold and bitcoin are read on M5, M15, H1, H4 and D1 with real volume.\n"
    "<b>2. Engine.</b> The SMC engine maps BOS/CHoCH, order blocks, breakers, fair value gaps, liquidity and "
    "sweeps, PDH/PDL, weekly high/low, the Asian range, premium/discount, killzones and the market regime.\n"
    "<b>3. Setup.</b> Needs higher-timeframe bias, a sweep or structure shift, a zone to enter from and a "
    "clear path to TP1.\n"
    "<b>4. Strategy board.</b> 17 classic strategies vote; only those that fit the current regime count.\n"
    "<b>5. AI desk.</b> 26 agents in five stages: 18 analysts in a technical, a strategy and a macro desk; 3 desk "
    "leads who check their evidence and challenge them; 3 verifiers (confluence, risk, devil's advocate); the "
    "Head Trader; the Signal Auditor. Votes are weighted by each agent's own track record. A signal needs "
    "every layer to agree.\n"
    "<b>6. News.</b> No new trades 30 minutes around high-impact USD news; you get a warning before it.\n"
    "<b>7. Tracking.</b> Every signal comes with a chart and your lot size and is followed live as replies.\n"
    "<b>8. Honest results.</b> 1/3 closed at each target, stop to breakeven after TP1.\n\n"
    "<b>Commands</b>: /menu /trades /history /stats /market /news /balance /risk /lot /stop\n\n"
    f"<i>{DISCLAIMER}</i>"
)


def daily_report(s: dict, open_count: int) -> str:
    text = performance(s, title="<b>DAILY REPORT · GOLD & BITCOIN</b>")
    return text + f"\n\nOpen trades: {open_count} · {datetime.now(timezone.utc).strftime('%d %b %Y')}"


def market_keyboard(key: str = "XAUUSD", instruments: list | None = None) -> InlineKeyboardMarkup:
    rows = [[Btn("M15 chart", callback_data=f"chart:15min:{key}"), Btn("H1 chart", callback_data=f"chart:1h:{key}"),
             Btn("H4 chart", callback_data=f"chart:4h:{key}")],
            [Btn("Refresh", callback_data=f"mkt:{key}"), Btn("AI view", callback_data=f"aiview:{key}")]]
    others = [i for i in (instruments or []) if i["key"] != key]
    if others:
        rows.append([Btn(f"Switch to {i['label']} ({i['name']})", callback_data=f"mkt:{i['key']}") for i in others])
    return back(rows)


def news_screen(events: list[dict], blackout: dict | None, error: str | None, headlines: list | None = None) -> str:
    now = datetime.now(timezone.utc)
    lines = ["<b>ECONOMIC CALENDAR · USD</b>", LINE]
    if blackout:
        lines += [f"<b>News pause active:</b> {escape(blackout['title'])} – no new signals right now", ""]
    if not events:
        lines.append("No high or medium impact USD news in the next 7 days." if not error else
                     "Calendar unavailable right now.")
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
        lines.append(f"<code>{e['time'].strftime('%H:%M')} {'HIGH' if e['impact'] == 'High' else 'MED '}</code> "
                     f"{escape(e['title'])}" + (f"  <i>{extra}</i>" if extra else "") + (f"  · {when}" if when else ""))
    if headlines:
        lines += ["", "<b>Latest headlines</b>"]
        for h in headlines:
            cats = "/".join(h.get("categories") or []) or "news"
            tag = f"<code>{cats.upper()[:12]:<12}</code>"
            title = (f"<a href=\"{escape(h['link'])}\">{escape(h['title'])}</a>" if h.get("link")
                     else escape(h["title"]))
            lines.append(f"{tag} {h['time'].strftime('%H:%M') if h.get('time') else ''} {title}")
    lines += ["", "<i>HIGH impact: the bot pauses new signals 30 minutes before and after.</i>"]
    return "\n".join(lines)


def news_alert(e: dict, minutes: int) -> str:
    extra = " · ".join(x for x in (f"Forecast {e['forecast']}" if e["forecast"] else "",
                                   f"Previous {e['previous']}" if e["previous"] else "") if x)
    return (f"<b>HIGH-IMPACT NEWS IN {minutes} MIN</b>\n"
            f"{e['country']} · <b>{escape(e['title'])}</b> at {e['time'].strftime('%H:%M UTC')}\n"
            + (f"{extra}\n" if extra else "") +
            "\nGold and bitcoin can move very fast. Consider moving open trades to breakeven or taking partial "
            "profit.\nNew signals are paused around the release.")


def status_screen(info: dict) -> str:
    ok = lambda b: "OK  " if b else "WARN"  # noqa: E731
    rows = [f"UPTIME        {info['uptime']}",
            f"MARKET        {'open' if info['market_open'] else 'closed'}",
            f"LAST SCAN     {info['last_scan']} ({info['scans_today']} today)",
            f"SCAN ERRORS   {info['fail_count']} in a row  [{ok(not info['fail_count'])}]",
            f"TWELVE DATA   {info['td_requests']} / 800 requests",
            f"GEMINI        {info['ai_calls']} calls, {info['ai_failures']} failed",
            f"VOLUME FEED   {'available' if info['volume_ok'] else 'unavailable'}  [{ok(info['volume_ok'])}]",
            f"NEWS FEED     {'available' if info['news_ok'] else 'unavailable'}  [{ok(info['news_ok'])}]",
            f"OPEN TRADES   {info['open_trades']}",
            f"USERS         {info['users']}"]
    lines = ["<b>BOT STATUS</b>", LINE, "<pre>" + "\n".join(rows) + "</pre>"]
    if info["last_error"]:
        lines.append(f"Last error: <i>{escape(info['last_error'][:150])}</i>")
    lines.append("<b>Last scan</b>")
    lines += [f"– {escape(plain(n))}" for n in info["notes"]] or ["– none"]
    return "\n".join(lines)


def backtest_report(r: dict) -> str:
    if r.get("error"):
        return f"Backtest failed: {escape(r['error'])}"
    s = r["stats"]
    lines = [f"<b>BACKTEST · {escape(plain(r['label']).upper())}</b>  (engine only, no AI)", LINE,
             f"{r['start']} → {r['end']} · {r['steps']} scans"]
    rows = [f"SIGNALS        {r['signals']}", f"EXPIRED/CANCEL {s['expired']}"]
    if s["trades"]:
        rows += [f"WIN RATE       {s['win_rate']}%  {bar(s['win_rate'])}",
                 f"WINS / LOSSES  {s['wins']} / {s['losses']}",
                 f"FULL TP3       {s['tp3']}",
                 f"TOTAL          {signed(s['total_r'], 'R')}",
                 f"AVG / TRADE    {r['avg_r']:+.2f}R",
                 f"MAX DRAWDOWN   {r['max_dd']}R",
                 f"PROFIT FACTOR  {r['profit_factor']}"]
    lines.append("<pre>" + "\n".join(rows) + "</pre>")
    lines.append("<i>Past results do not guarantee future results. The live bot also filters with the AI desk"
                 " and news, which the backtest does not.</i>")
    return "\n".join(lines)


def _cfg_text(c: dict) -> str:
    tp1 = f"TP1 ≤ {c['tp1_max_r']:g}R" if c.get("tp1_max_r") else "TP1 at liquidity"
    mode = "strict" if c.get("strict") else "normal"
    return f"{mode} · score ≥ {c['min_score']} · min 1:{c['min_rr']:g} · {tp1}"


def backtest_menu(params: dict) -> str:
    lines = ["<b>BACKTEST & OPTIMIZE</b>", LINE,
             "<b>Backtest</b> – replay the last weeks of real data with the current settings.",
             "<b>Optimize</b> – test 16 settings (strict/normal, score, R:R, TP1) on the same data and apply the best.",
             "", "<b>Current settings</b>"]
    for s, p_ in params.items():
        lines.append(f"{style_name(s)}: {'ON' if p_['enabled'] else 'OFF'} · {_cfg_text(p_)}")
    lines.append("\n<i>M5 history covers about 3 weeks, so scalping results use fewer days than swing.</i>")
    return "\n".join(lines)


def optimize_report(o: dict, current: dict) -> tuple[str, InlineKeyboardMarkup]:
    if o.get("error"):
        return f"Optimize failed: {escape(o['error'])}", back()
    style = o["style"]
    lines = [f"<b>OPTIMIZER · {escape(plain(o['label']).upper())}</b>  (engine only)", LINE,
             f"{o['start']} → {o['end']}", f"Current: {_cfg_text(current)}", ""]
    ranked = o["ranked"]
    rows = []
    if not ranked:
        lines.append(f"Not enough trades (need ≥ {o['min_trades']}) in any setting to judge this style.")
    for i, r in enumerate(ranked[:5]):
        lines.append(f"<b>#{i + 1}</b> {_cfg_text(r['config'])}")
        lines.append(f"    {r['trades']} trades · win {r['win_rate']}% · <b>{signed(r['total_r'], 'R')}</b> · "
                     f"PF {r['profit_factor']:g} · DD {r['max_dd']}R")
    best = ranked[0] if ranked else None
    if best and best["total_r"] > 0:
        lines += ["", "Tap a button to use one of these settings for live signals."]
        rows.append([Btn(f"Apply #{i + 1}", callback_data=f"apply:{style}:{i}") for i in range(min(3, len(ranked)))])
    else:
        lines += ["", "<b>No profitable setting on this data.</b> Consider switching this style off for now."]
    rows.append([Btn(f"Switch {style_name(style)} off", callback_data=f"soff:{style}"),
                 Btn("Default settings", callback_data=f"son:{style}")])
    lines.append("\n<i>Optimizing on a few weeks can over-fit. Prefer settings that are also good in second and "
                 "third place, and re-check every week.</i>")
    return "\n".join(lines), back(rows)
