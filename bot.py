"""Gold (XAU/USD) Smart-Money AI signal bot for Telegram.

Every few minutes: fetch M5..D1 gold candles (+ PAXG volume) -> SMC engine finds setups for
scalping / intraday / swing -> news filter -> 5 AI specialists + Head Trader review them ->
approved trades are sent with a chart, per-user lot size and live TP/SL tracking.
"""

import asyncio
import logging
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from html import escape

from telegram import ReplyParameters, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, NetworkError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import backtest
import chart
import sessions
import tracker
import ui
from agents import TradingDesk
from config import Config, load_config
import instruments
from market_data import MarketData
from news import Headlines, NewsCalendar
from setups import STYLES, TF_LABEL, analyze_market, find_setup
from storage import Storage, stats

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
log = logging.getLogger("goldbot")

NETWORK_HELP = (
    "Could not reach Telegram (api.telegram.org). Telegram is probably blocked on this internet.\n"
    "Fix: turn on a VPN (e.g. Cloudflare WARP / 1.1.1.1) and run again, or set PROXY_URL in .env\n"
    "(a SOCKS5/HTTP proxy - the MTProto proxy used by the Telegram app will NOT work)."
)
HTML = ParseMode.HTML
FAILS_BEFORE_ALERT = 3


def failure_hint(error: str) -> str:
    e = error.lower()
    if "apikey" in e or "api key" in e or "api_key" in e:
        return "Check TWELVEDATA_API_KEY – Twelve Data rejected the key."
    if "credits" in e or "limit" in e or "429" in e:
        return "Twelve Data limit reached – wait, or raise SCAN_INTERVAL_MINUTES."
    if "connect" in e or "timeout" in e or "name or service" in e:
        return "Internet / connection problem on the server."
    return "See the server logs for details."


class GoldBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.storage = Storage(cfg.data_file)
        self.instruments = [instruments.get(k) for k in cfg.markets]
        self.feeds = {}
        for inst in self.instruments:
            gold = inst["key"] == "XAUUSD"
            self.feeds[inst["key"]] = MarketData(cfg.twelvedata_api_key, cfg.symbol if gold else inst["symbol"],
                                                 cfg.volume_symbol if gold else inst["volume_symbol"], inst["source"])
        self.candles_by: dict[str, dict] = {}
        self.volumes_by: dict[str, dict] = {}
        self.markets: dict[str, dict] = {}
        self.desk = TradingDesk(cfg.gemini_api_keys, cfg.gemini_model, cfg.min_risk_reward,
                                cfg.min_confidence, cfg.min_agent_votes, cfg.gemini_fallback_models,
                                cfg.gemini_rpm_per_model)
        self.news = NewsCalendar(cfg.news_currencies)
        self.headlines = Headlines(cfg.news_feeds)
        self.scan_lock = asyncio.Lock()
        self.started = time.time()
        self._live: dict[str, tuple[float, dict | None]] = {}
        self.last_scan: datetime | None = None
        self.last_notes: list[str] = []
        self.scans = {"day": None, "count": 0}
        self.fail_count = 0
        self.last_error = ""
        self._ai_down_alert_at = 0.0
        self._ai_view: tuple[float, str] = (0.0, "")
        self.backtest_running = False
        self.app_bot = None
        self.last_optimize: dict[str, dict] = {}

    # ================= instruments =================
    # The first instrument in MARKETS is the "primary" one used by single-market screens.

    @property
    def primary(self) -> str:
        return self.instruments[0]["key"]

    @property
    def data(self) -> MarketData:
        return self.feeds[self.primary]

    @property
    def candles(self) -> dict | None:
        return self.candles_by.get(self.primary)

    @candles.setter
    def candles(self, value):
        self.candles_by[self.primary] = value

    @property
    def volumes(self) -> dict:
        return self.volumes_by.get(self.primary, {})

    @property
    def market(self) -> dict | None:
        return self.markets.get(self.primary)

    @market.setter
    def market(self, value):
        self.markets[self.primary] = value

    def inst(self, key: str | None) -> dict:
        return next((i for i in self.instruments if i["key"] == key), self.instruments[0])

    async def load(self, key: str):
        candles, volumes = await self.feeds[key].get()
        self.candles_by[key], self.volumes_by[key] = candles, volumes
        self.markets[key] = analyze_market(candles, volumes)
        return candles

    # ================= helpers =================

    def admins(self) -> list[int]:
        if self.cfg.admin_ids:
            return self.cfg.admin_ids
        return [self.storage.owner] if self.storage.owner else []

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admins()

    async def tell_admins(self, bot, text: str):
        for admin in self.admins():
            try:
                await bot.send_message(admin, text, parse_mode=HTML)
            except Exception as e:
                log.warning("Could not message admin %s: %s", admin, e)

    def _targets(self, style: str | None = None, flag: str | None = None) -> list:
        targets = self.storage.subscribers(style, flag)
        if self.cfg.channel_id:
            targets.append(self.cfg.channel_id)
        return targets

    async def broadcast(self, bot, text: str, flag: str | None = None, photo: bytes | None = None):
        file_id = None
        for chat_id in self._targets(flag=flag):
            try:
                if photo:
                    msg = await bot.send_photo(chat_id, photo=file_id or photo, caption=text, parse_mode=HTML)
                    if not file_id and getattr(msg, "photo", None):
                        file_id = msg.photo[-1].file_id
                else:
                    await bot.send_message(chat_id, text, parse_mode=HTML)
            except Exception as e:
                log.warning("Could not send to %s: %s", chat_id, e)

    # ================= scanning =================

    async def scan(self, bot, manual: bool = False) -> list[str]:
        """One full cycle: data -> track trades -> news -> find & review setups -> publish."""
        open_now = [i for i in self.instruments if manual or instruments.is_open(i)]
        if not open_now:
            stale = not self.last_scan or datetime.now(timezone.utc) - self.last_scan > timedelta(hours=1)
            if stale and not self.scan_lock.locked():
                await self.refresh_market()
            return ["Market closed"]
        if self.scan_lock.locked():
            return ["A scan is already running"]

        mon = self.desk.monitor
        async with self.scan_lock:
            mon.set_phase("scanning")
            mon.event("🔎 Scan started" + (" (manual)" if manual else ""))
            try:
                return await self._scan(bot, mon, open_now)
            except Exception as e:
                mon.event(f"❌ Scan failed: {str(e)[:160]}", "error")
                raise
            finally:
                mon.set_phase("idle")

    async def refresh_market(self, keys: list[str] | None = None):
        """Market closed: keep charts, dashboard and menus up to date without looking for trades."""
        async with self.scan_lock:
            for key in keys or [i["key"] for i in self.instruments]:
                try:
                    candles = await self.load(key)
                except Exception as e:
                    self.desk.monitor.event(f"⚠️ {self.inst(key)['name']} data refresh failed: {str(e)[:120]}", "error")
                    continue
                self.desk.monitor.event(f"💤 {self.inst(key)['name']} closed – chart & market data refreshed "
                                        f"({candles['5min'][-1]['close']:,.2f})")
            self.last_scan = datetime.now(timezone.utc)
            await self.news.refresh()
            await self.headlines.refresh()

    def style_params(self, style: str) -> dict:
        """Per-style settings chosen by the owner (e.g. from the optimizer), else the .env defaults."""
        ss = self.storage.style_settings(style)
        return {"enabled": ss.get("enabled", True),
                "min_score": ss.get("min_score", self.cfg.min_engine_score),
                "min_rr": ss.get("min_rr", self.cfg.min_risk_reward),
                "tp1_max_r": ss.get("tp1_max_r"),
                "strict": ss.get("strict", self.cfg.strict_mode)}

    async def _scan(self, bot, mon, insts: list[dict]) -> list[str]:
        now = datetime.now(timezone.utc)
        self.last_scan = now
        if self.scans["day"] != date.today():
            self.scans = {"day": date.today(), "count": 0}
        self.scans["count"] += 1
        await self.news.refresh()
        await self.headlines.refresh()
        session = sessions.current(now)
        session["news"] = self.news.brief(now)
        session["headlines"] = self.headlines.brief()

        # Warn before high-impact news (once, for everybody).
        for ev in self.news.due_alerts(now, 20):
            mins = max(int((ev["time"] - now).total_seconds() // 60), 0)
            await self.broadcast(bot, ui.news_alert(ev, mins), flag="news_alerts")
        blackout = self.news.blackout(now, self.cfg.news_blackout_min, self.cfg.news_blackout_min) \
            if self.cfg.news_blackout_min else None

        notes = []
        for inst in insts:
            try:
                notes += await self._scan_instrument(bot, mon, inst, session, blackout)
            except Exception as e:
                if len(insts) == 1:
                    raise
                notes.append(f"{inst['icon']} {inst['name']}: scan failed – {str(e)[:100]}")
                mon.event(notes[-1], "error")
        # Keep closed markets' charts fresh too.
        closed = [i["key"] for i in self.instruments if i not in insts and i["key"] not in self.markets]
        for key in closed:
            try:
                await self.load(key)
            except Exception as e:
                log.warning("Could not refresh %s: %s", key, e)

        self.last_notes = notes
        log.info("Scan done: %s", " | ".join(notes))
        return notes

    async def _scan_instrument(self, bot, mon, inst: dict, session: dict, blackout: dict | None) -> list[str]:
        key, icon = inst["key"], inst["icon"]
        candles = await self.load(key)
        market = self.markets[key]
        m5 = candles["5min"]
        feed = self.feeds[key]
        mon.event(f"📥 {inst['name']} data loaded: M5→D1, {m5[-1]['close']:,.2f} · "
                  f"volume {'✓' if feed.volume_ok else '✗'} · news {'✓' if self.news.error is None else '✗'}")

        # 1) live tracking of this instrument's open trades
        for trade in self.storage.open_trades():
            if trade.get("instrument", "XAUUSD") != key:
                continue
            for ev in tracker.update(trade, m5):
                mon.event(f"📍 {inst['name']} {trade['direction']} {trade['entry']} ({trade['style_label']}): "
                          f"{ev['kind']}" + (f" TP{ev['n']}" if ev.get("n") else ""), "signal")
                await self.notify(bot, trade, ev)
        self.storage.save()

        # 2) no new trades around high-impact USD news (moves gold and crypto alike)
        if blackout:
            note = f"{icon} {inst['name']}: 📰 news pause – {blackout['title']} at {blackout['time'].strftime('%H:%M UTC')}"
            mon.event(note, "skip")
            return [note.replace(f"{icon} {inst['name']}: 📰 news pause", "📰 News pause")]

        # 3) find, review and publish setups
        notes = []
        open_trades = [t for t in self.storage.open_trades() if t.get("instrument", "XAUUSD") == key]
        for style in self.cfg.styles:
            label = f"{icon} {inst['name']} {STYLES[style]['label']}"
            sp = self.style_params(style)
            if not sp["enabled"]:
                notes.append(f"{label}: switched off by owner")
                continue
            setup = find_setup(style, market, session, sp["min_rr"], sp["tp1_max_r"], sp["strict"])
            if not setup:
                notes.append(f"{label}: no setup")
                continue
            setup.update(instrument=key, symbol_name=inst["name"], pip=inst["pip"],
                         contract=self.cfg.contract_size if key == "XAUUSD" else inst["contract"],
                         key=f"{key}:{setup['key']}")
            if setup["score"] < sp["min_score"]:
                notes.append(f"{label}: weak {setup['direction']} setup ({setup['score']})")
                continue
            if self.storage.seen(setup["key"]):
                notes.append(f"{label}: setup already reviewed")
                continue
            if any(t["style"] == style and t["direction"] == setup["direction"] for t in open_trades):
                notes.append(f"{label}: already in a {setup['direction']} trade")
                continue

            self.storage.mark_seen(setup["key"])
            log.info("Reviewing %s %s %s setup (score %s)", key, style, setup["direction"], setup["score"])
            mon.event(f"🎯 {label}: engine found {setup['direction']} setup, score {setup['score']} – "
                      + "; ".join(setup["confluences"][:3]))
            verdict = await self.desk.review(setup, market, session, self.track_record(style, key),
                                             instrument=inst["ai_name"])
            if verdict.get("ai_down"):
                await self._alert_ai_down(bot)
            if not verdict["approved"]:
                if verdict.get("ai_down") and self.cfg.engine_only_score and \
                        setup["score"] >= self.cfg.engine_only_score:
                    verdict["engine_only"] = True
                else:
                    why = verdict.get("reject_reason") or verdict.get("reason")
                    notes.append(f"{label}: {setup['direction']} rejected by AI desk – {why}")
                    continue

            trade = tracker.new_trade(setup, verdict, m5[-1]["time"])
            await self.publish(bot, trade)
            open_trades.append(trade)
            notes.append(f"{label}: 🚀 {trade['direction']} signal sent")

        for n in notes:
            mon.event(n, "signal" if "signal sent" in n else "info")
        return notes

    async def _alert_ai_down(self, bot):
        if time.time() - self._ai_down_alert_at > 7200:
            self._ai_down_alert_at = time.time()
            await self.tell_admins(bot, "⚠️ <b>Gemini AI is not responding</b> (quota or overload). Setups are "
                                        "skipped until it recovers. Check your GEMINI_API_KEY quota.")

    async def scheduled_scan(self, context: ContextTypes.DEFAULT_TYPE):
        try:
            await self.scan(context.bot)
        except Exception as e:
            self.fail_count += 1
            self.last_error = str(e)
            log.exception("Scheduled scan failed (%s in a row)", self.fail_count)
            if self.fail_count == FAILS_BEFORE_ALERT:
                await self.tell_admins(context.bot, f"⚠️ <b>Bot problem:</b> the last {FAILS_BEFORE_ALERT} market scans "
                                                    f"failed.\n<code>{escape(self.last_error[:300])}</code>\n\n"
                                                    f"💡 {failure_hint(self.last_error)}")
            return
        if self.fail_count >= FAILS_BEFORE_ALERT:
            await self.tell_admins(context.bot, "✅ <b>Recovered</b> – market scans are working again.")
        self.fail_count, self.last_error = 0, ""

    async def publish(self, bot, trade: dict):
        self.storage.add_trade(trade)
        png = None
        try:
            tf = trade["timeframes"]["entry"]
            key = trade.get("instrument", self.primary)
            png = await asyncio.to_thread(chart.signal_chart, self.candles_by[key][tf], trade,
                                          self.markets[key][tf]["smc"], self.markets[key].get("levels", {}),
                                          TF_LABEL[tf])
        except Exception:
            log.exception("Signal chart failed; sending text only")

        kb = ui.signal_keyboard(trade["id"])
        file_id = None
        for chat_id in self._targets(trade["style"]):
            user = self.storage.data["users"].get(str(chat_id))
            lot = ui.lot_line(user, trade, self.cfg.contract_size)
            try:
                if png:
                    msg = await bot.send_photo(chat_id, photo=file_id or png, caption=ui.signal_caption(trade, lot),
                                               parse_mode=HTML, reply_markup=kb)
                    if not file_id and getattr(msg, "photo", None):
                        file_id = msg.photo[-1].file_id
                else:
                    text = ui.signal_card(trade) + (f"\n\n{lot}" if lot else "")
                    msg = await bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=kb)
                trade["messages"][str(chat_id)] = msg.message_id
            except Exception as e:
                log.warning("Could not send signal to %s: %s", chat_id, e)
        self.storage.save()

    async def notify(self, bot, trade: dict, ev: dict):
        text = ui.event_message(trade, ev)
        for chat_id, msg_id in trade["messages"].items():
            try:
                await bot.send_message(chat_id, text, parse_mode=HTML,
                                       reply_parameters=ReplyParameters(msg_id, allow_sending_without_reply=True))
            except Exception as e:
                log.warning("Could not send update to %s: %s", chat_id, e)

    async def daily_report(self, context: ContextTypes.DEFAULT_TYPE):
        since = datetime.now(timezone.utc) - timedelta(days=1)
        closed = self.storage.closed_trades(since)
        if not closed and not self.storage.open_trades():
            return
        await self.broadcast(context.bot, ui.daily_report(stats(closed), len(self.storage.open_trades())))

    async def briefing(self, context: ContextTypes.DEFAULT_TYPE):
        """AI outlook + chart at the London and New York opens."""
        name = context.job.data
        if not self.cfg.briefings or not sessions.is_market_open() or not self.market:
            return
        try:
            text = await self.desk.market_view(self.market, {**sessions.current(), "news": self.news.brief()})
            png = await asyncio.to_thread(chart.market_chart, self.candles["15min"], self.market["15min"]["smc"],
                                          self.market.get("levels", {}), "M15", self.inst(self.primary)["name"])
        except Exception:
            log.exception("Briefing failed")
            return
        caption = f"🌅 <b>{name} Open Briefing · {self.inst(self.primary)['name']}</b>\n{ui.LINE}\n{escape(text)}"
        if len(caption) > 1000:
            caption = caption[:990].rsplit(" ", 1)[0] + "…"
        await self.broadcast(context.bot, caption, flag="briefings", photo=png)

    # ================= commands =================

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id, user_id = update.effective_chat.id, update.effective_user.id
        self.storage.set_subscribed(chat_id, True)
        if not self.cfg.admin_ids and self.storage.claim_owner(user_id):
            await update.message.reply_text("👑 <b>You are the owner of this bot.</b> Admin tools (scan, status, "
                                            "backtest, error alerts) are unlocked for you.", parse_mode=HTML)
        text, kb = ui.main_menu(self.storage.user(chat_id), self.is_admin(user_id))
        await update.message.reply_text(text, parse_mode=HTML, reply_markup=kb)

    async def cmd_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text, kb = ui.main_menu(self.storage.user(update.effective_chat.id), self.is_admin(update.effective_user.id))
        await update.message.reply_text(text, parse_mode=HTML, reply_markup=kb)

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.storage.set_subscribed(update.effective_chat.id, False)
        await update.message.reply_text("🔕 Alerts OFF. Send /start to switch them on again.")

    async def cmd_simple(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/trades /stats /market /help /history /news /status – same screens as the menu buttons."""
        cmd = update.message.text.split()[0].lstrip("/").split("@")[0]
        key = {"trades": "trades", "stats": "perf", "market": "mkt", "help": "help", "history": "hist",
               "news": "news", "status": "status"}[cmd]
        text, kb = await self.screen(key, update.effective_chat.id, update.effective_user.id)
        await update.message.reply_text(text, parse_mode=HTML, reply_markup=kb)

    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            value = float(context.args[0].replace(",", "").replace("$", ""))
            assert value > 0
        except (IndexError, ValueError, AssertionError):
            await update.message.reply_text("Usage: <code>/balance 1000</code>", parse_mode=HTML)
            return
        self.storage.set_field(update.effective_chat.id, "balance", value)
        text, kb = ui.risk_screen(self.storage.user(update.effective_chat.id), self.cfg.contract_size)
        await update.message.reply_text("✅ Balance saved.\n\n" + text, parse_mode=HTML, reply_markup=kb)

    async def cmd_risk(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            value = float(context.args[0].rstrip("%"))
            assert 0 < value <= 10
        except (IndexError, ValueError, AssertionError):
            await update.message.reply_text("Usage: <code>/risk 1</code>  (percent per trade, 0.1–10)", parse_mode=HTML)
            return
        self.storage.set_field(update.effective_chat.id, "risk", value)
        text, kb = ui.risk_screen(self.storage.user(update.effective_chat.id), self.cfg.contract_size)
        await update.message.reply_text("✅ Risk saved.\n\n" + text, parse_mode=HTML, reply_markup=kb)

    async def cmd_lot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = self.storage.user(update.effective_chat.id)
        try:
            sl_pips = float(context.args[0])
            assert sl_pips > 0
        except (IndexError, ValueError, AssertionError):
            await update.message.reply_text("Usage: <code>/lot 50</code>  (stop loss in pips; 1 pip = $0.10)",
                                            parse_mode=HTML)
            return
        if not user.get("balance"):
            await update.message.reply_text("Set your balance first: <code>/balance 1000</code>", parse_mode=HTML)
            return
        lots, risk_usd = ui.lot_size(user["balance"], user.get("risk", 1.0), sl_pips / 10, self.cfg.contract_size)
        await update.message.reply_text(
            f"💰 SL <b>{sl_pips:g} pips</b> with {user.get('risk', 1.0):g}% risk (${risk_usd:,.2f})\n"
            f"➡️ Lot size: <b>{max(lots, 0.01):.2f}</b>" + ("  (minimum lot – risk is higher)" if lots < 0.01 else ""),
            parse_mode=HTML)

    async def cmd_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ Only the bot owner can run a manual scan.")
            return
        msg = await update.message.reply_text("🔎 AI desk is scanning XAU/USD on M5 → D1…")
        await msg.edit_text(await self._manual_scan_text(context.bot), parse_mode=HTML)

    async def cmd_backtest(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ Only the bot owner can run a backtest.")
            return
        style = context.args[0] if context.args else "intraday"
        key = "BTCUSD" if any(a.upper().startswith("BTC") for a in context.args[1:]) else None
        if style not in STYLES:
            await update.message.reply_text(f"Usage: <code>/backtest intraday</code>  ({', '.join(STYLES)})",
                                            parse_mode=HTML)
            return
        await self.run_backtest(context.bot, update.effective_chat.id, style, key)

    async def _history(self, style: str, key: str):
        inst = self.inst(key)
        symbol = self.cfg.symbol if inst["key"] == "XAUUSD" else inst["symbol"]
        return await backtest.fetch_history(self.cfg.twelvedata_api_key, symbol, style, inst["source"])

    async def run_backtest(self, bot, chat_id: int, style: str, key: str | None = None):
        inst = self.inst(key or self.primary)
        if self.backtest_running:
            await bot.send_message(chat_id, "🧪 A backtest is already running, please wait.")
            return
        self.backtest_running = True
        try:
            await bot.send_message(chat_id, f"🧪 Backtesting {inst['icon']} {inst['name']} {STYLES[style]['label']} "
                                            "on recent history… this takes 1–5 minutes.")
            data = await self._history(style, inst["key"])
            sp = self.style_params(style)
            result = await asyncio.to_thread(backtest.run, data, style, sp["min_rr"], sp["min_score"],
                                             sp["tp1_max_r"], sp["strict"])
        except Exception as e:
            log.exception("Backtest failed")
            result = {"error": str(e)[:200]}
        finally:
            self.backtest_running = False
        if not result.get("error"):
            result["label"] = f"{inst['icon']} {inst['name']} {result['label']}"
        await bot.send_message(chat_id, ui.backtest_report(result), parse_mode=HTML)

    async def run_optimize(self, bot, chat_id: int, style: str, key: str | None = None):
        inst = self.inst(key or self.primary)
        if self.backtest_running:
            await bot.send_message(chat_id, "🧪 A backtest is already running, please wait.")
            return
        self.backtest_running = True
        try:
            await bot.send_message(chat_id, f"🔧 Optimizing {inst['icon']} {inst['name']} {STYLES[style]['label']}: "
                                            f"testing {len(backtest.GRID)} settings on the same history… 2–6 minutes.")
            data = await self._history(style, inst["key"])
            result = await asyncio.to_thread(backtest.optimize, data, style)
        except Exception as e:
            log.exception("Optimize failed")
            result = {"error": str(e)[:200]}
        finally:
            self.backtest_running = False
        self.last_optimize[style] = result
        text, kb = ui.optimize_report(result, self.style_params(style))
        await bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=kb)

    async def _manual_scan_text(self, bot) -> str:
        try:
            notes = await self.scan(bot, manual=True)
        except Exception as e:
            log.exception("Manual scan failed")
            return f"❌ Scan failed: {escape(str(e)[:300])}\n💡 {failure_hint(str(e))}"
        closed = "" if sessions.is_market_open() else "\n\n<i>Market is closed – setups are rare until it reopens.</i>"
        return "🔎 <b>Scan complete</b>\n" + "\n".join(f"• {escape(n)}" for n in notes) + closed

    def status_info(self) -> dict:
        up = int(time.time() - self.started)
        return {
            "uptime": f"{up // 86400}d {up % 86400 // 3600}h {up % 3600 // 60}m",
            "market_open": sessions.is_market_open(),
            "last_scan": self.last_scan.strftime("%H:%M UTC") if self.last_scan else "–",
            "scans_today": self.scans["count"] if self.scans["day"] == date.today() else 0,
            "fail_count": self.fail_count,
            "last_error": self.last_error,
            "td_requests": sum(f.requests_today for f in self.feeds.values()),
            "ai_calls": self.desk.usage["calls"],
            "ai_failures": self.desk.usage["failures"],
            "volume_ok": self.data.volume_ok,
            "news_ok": self.news.error is None,
            "open_trades": len(self.storage.open_trades()),
            "users": len(self.storage.subscribers()),
            "notes": self.last_notes,
        }

    # ================= screens & buttons =================

    async def screen(self, key: str, chat_id: int, user_id: int):
        user = self.storage.user(chat_id)
        price = self.market["5min"]["price"] if self.market else None
        if key == "trades":
            return ui.trades_list(self.storage.open_trades(), price)
        if key == "hist":
            return ui.history(self.storage.closed_trades())
        if key == "perf":
            return ui.performance(stats(self.storage.closed_trades())), ui.back()
        if key == "mkt" or key.startswith("mkt:"):
            inst = self.inst(key[4:] or self.primary)
            text = ui.market_dashboard(self.markets.get(inst["key"]), sessions.current(), instruments.is_open(inst),
                                       self.last_scan, f"{inst['icon']} {inst['name']}")
            return text, ui.market_keyboard(inst["key"], self.instruments)
        if key == "set":
            return ui.settings(user)
        if key == "risk":
            return ui.risk_screen(user, self.cfg.contract_size)
        if key == "news":
            await self.news.refresh()
            now = datetime.now(timezone.utc)
            await self.headlines.refresh()
            return ui.news_screen(self.news.upcoming(now, 24 * 7), self.news.blackout(now), self.news.error,
                                  self.headlines.latest(6)), ui.back()
        if key == "status" and self.is_admin(user_id):
            return ui.status_screen(self.status_info()), ui.back()
        if key == "bt" and self.is_admin(user_id):
            from telegram import InlineKeyboardButton as Btn
            rows = []
            for inst in self.instruments:
                rows.append([Btn(f"🧪 {inst['icon']} {STYLES[s]['label'][2:]}", callback_data=f"bt:{s}:{inst['key']}")
                             for s in STYLES])
                rows.append([Btn(f"🔧 {inst['icon']} Opt. {STYLES[s]['label'][2:]}", callback_data=f"opt:{s}:{inst['key']}")
                             for s in STYLES])
            return ui.backtest_menu({s: self.style_params(s) for s in STYLES}), ui.back(rows)
        if key == "help":
            return ui.HELP, ui.back()
        return ui.main_menu(user, self.is_admin(user_id))

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        data = q.data or ""
        chat = q.message.chat
        user_id = q.from_user.id

        # Buttons under a signal (also work in channels, where we answer with a popup).
        if data[:2] in ("t:", "r:", "f:"):
            trade = self.storage.trade(data[2:])
            if not trade:
                await q.answer("Trade not found", show_alert=True)
                return
            price = self.market["5min"]["price"] if self.market else None
            text = {"t": lambda: ui.trade_status(trade, price), "r": lambda: ui.ai_report(trade),
                    "f": lambda: ui.signal_card(trade)}[data[0]]()
            if chat.type == ChatType.CHANNEL:
                plain = text
                for tag in ("<b>", "</b>", "<i>", "</i>", "<code>", "</code>"):
                    plain = plain.replace(tag, "")
                await q.answer(plain[:195], show_alert=True)
            else:
                await q.answer()
                await q.message.reply_text(text, parse_mode=HTML)
            return

        if data.startswith("chart:"):
            _, tf, key = (data + ":").split(":")[:3]
            inst = self.inst(key or self.primary)
            candles, market = self.candles_by.get(inst["key"]), self.markets.get(inst["key"])
            if not candles or tf not in candles:
                await q.answer("No market data yet – wait for the first scan", show_alert=True)
                return
            await q.answer("Drawing chart…")
            png = await asyncio.to_thread(chart.market_chart, candles[tf], market[tf]["smc"],
                                          market.get("levels", {}), TF_LABEL[tf], inst["name"])
            await q.message.reply_photo(png, caption=f"📈 {inst['name']} {TF_LABEL[tf]} · {market[tf]['price']:,.2f}")
            return

        if data.startswith(("opt:", "apply:", "soff:", "son:")):
            if not self.is_admin(user_id):
                await q.answer("Only the owner can change strategy settings", show_alert=True)
                return
            kind, _, rest = data.partition(":")
            if kind == "opt":
                style_, _, key_ = rest.partition(":")
                await q.answer("Optimizer started")
                asyncio.create_task(self.run_optimize(context.bot, chat.id, style_, key_ or None))
                return
            style, _, idx = rest.partition(":")
            if kind == "apply":
                ranked = self.last_optimize.get(style, {}).get("ranked", [])
                if not idx.isdigit() or int(idx) >= len(ranked):
                    await q.answer("Run the optimizer again first", show_alert=True)
                    return
                cfg = ranked[int(idx)]["config"]
                self.storage.set_style_settings(style, enabled=True, **cfg)
                await q.answer("Settings applied ✅", show_alert=True)
            elif kind == "soff":
                self.storage.set_style_settings(style, enabled=False)
                await q.answer(f"{STYLES[style]['label']} switched off", show_alert=True)
            else:
                self.storage.reset_style_settings(style)
                await q.answer(f"{STYLES[style]['label']} back to default settings", show_alert=True)
            text, kb = await self.screen("bt", chat.id, user_id)
            await q.message.reply_text(text, parse_mode=HTML, reply_markup=kb)
            return

        if data.startswith("bt:"):
            if not self.is_admin(user_id):
                await q.answer("Only the owner can run backtests", show_alert=True)
                return
            style_, _, key_ = data[3:].partition(":")
            await q.answer("Backtest started")
            asyncio.create_task(self.run_backtest(context.bot, chat.id, style_, key_ or None))
            return

        if data.startswith("sty:"):
            self.storage.toggle_style(chat.id, data[4:])
            data = "set"
        elif data.startswith("tog:"):
            self.storage.toggle_field(chat.id, data[4:])
            data = "set"
        elif data.startswith("rk:"):
            self.storage.set_field(chat.id, "risk", float(data[3:]))
            data = "risk"
        elif data == "alert":
            u = self.storage.user(chat.id)
            self.storage.set_subscribed(chat.id, not u.get("subscribed"))
            data = "menu"
        elif data == "scan":
            if not self.is_admin(user_id):
                await q.answer("Only the owner can scan manually", show_alert=True)
                return
            await q.answer("Scanning…")
            await q.message.reply_text(await self._manual_scan_text(context.bot), parse_mode=HTML)
            return
        elif data == "practice":
            if not self.is_admin(user_id):
                await q.answer("Only the owner can run a practice review", show_alert=True)
                return
            await q.answer("Practice review started – watch it on the dashboard")
            verdict = await self.practice_review()
            if verdict:
                votes = " ".join(r["icon"] + ("✅" if r["vote"] == "TAKE" else "❌" if r["vote"] == "SKIP" else "⚠️")
                                 for r in verdict["reports"])
                await q.message.reply_text(
                    f"🧪 <b>Practice review</b> (no signal sent)\n{votes}\n"
                    f"Result: {'✅ would be SENT' if verdict['approved'] else '❌ would be SKIPPED'} · "
                    f"confidence {verdict['confidence']}%\n<i>{escape(verdict.get('reject_reason') or verdict.get('reason') or '')}</i>",
                    parse_mode=HTML)
            return
        elif data == "aiview" or data.startswith("aiview:"):
            await q.answer("AI analyst is reading the chart…")
            await q.message.reply_text(await self._ai_market_view(data[7:] or None), parse_mode=HTML)
            return

        await q.answer()
        text, kb = await self.screen(data, chat.id, user_id)
        try:
            await q.edit_message_text(text, parse_mode=HTML, reply_markup=kb)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    async def _ai_market_view(self, key: str | None = None) -> str:
        inst = self.inst(key or self.primary)
        market = self.markets.get(inst["key"])
        if not market:
            return "🧠 No market data yet – wait for the first scan."
        cached_at, text = self._ai_view.get(inst["key"], (0.0, "")) if isinstance(self._ai_view, dict) else (0.0, "")
        if time.time() - cached_at > 600:
            try:
                text = await self.desk.market_view(market, {**sessions.current(), "news": self.news.brief()},
                                                   inst["ai_name"])
                if not isinstance(self._ai_view, dict):
                    self._ai_view = {}
                self._ai_view[inst["key"]] = (time.time(), text)
            except Exception as e:
                log.warning("AI market view failed: %s", e)
                return "🧠 AI analyst is busy right now, try again in a minute."
        return f"🧠 <b>AI Market View · {inst['icon']} {inst['name']}</b>\n{ui.LINE}\n{escape(text)}\n\n<i>{ui.DISCLAIMER}</i>"

    # ================= local dashboard =================

    ROLES = {"structure": "Trend, BOS/CHoCH on every timeframe", "liquidity": "Sweeps, resting liquidity, stop hunts",
             "orderblocks": "Order blocks & breaker blocks", "imbalance": "FVGs, imbalance, premium/discount",
             "volume": "Volume profile, delta, volume bubbles", "price_action": "Candles, patterns, rejections",
             "indicators": "EMA, RSI, MACD, ADX, Supertrend, StochRSI, BB, VWAP",
             "session_news": "Killzone, ADR, calendar & headlines",
             "confluence": "Cross-checks all 8 analyst reports", "risk": "Verifies stop, targets, R:R",
             "devil": "Attacks the trade with the analysts' findings",
             "head": "Reads all 11 reports → TAKE/SKIP, confidence, levels",
             "auditor": "Final check of the signal – can veto"}

    def _market_summary(self, key: str) -> dict | None:
        market = self.markets.get(key)
        if not market:
            return None
        tfs = []
        for tf in ("1day", "4h", "1h", "15min", "5min"):
            if tf not in market:
                continue
            m = market[tf]
            ev = m["smc"]["last_event"]
            tfs.append({"label": TF_LABEL[tf], "trend": m["smc"]["trend"], "zone": m["smc"]["range"]["zone"],
                        "event": f"{ev['type']} {ev['direction']} @ {ev['level']:.2f}" if ev else None,
                        "rsi": round(m["ind"]["rsi"]) if m["ind"]["rsi"] is not None else None,
                        "atr": round(m["smc"]["atr"], 2),
                        "patterns": ", ".join(x["name"].split(" (")[0] for x in m["smc"].get("patterns", [])
                                              if x.get("age", 0) <= 1)[:60]})
        return {"price": market["5min"]["price"], "levels": market.get("levels", {}),
                "adr": market.get("adr", {}), "tfs": tfs}

    def dashboard_state(self) -> dict:
        from agents import PIPELINE

        mon = self.desk.monitor
        agents = [{"key": a["key"], "name": a["name"], "icon": a["icon"], "role": self.ROLES.get(a["key"], ""),
                   "stage": stage_no, **dict(mon.agents.get(a["key"], {}))}
                  for stage_no, (_, members) in enumerate(PIPELINE, 1) for a in members]
        stages = [name for name, _ in PIPELINE]
        flows = list(mon.flows)[-250:]
        markets = {i["key"]: self._market_summary(i["key"]) for i in self.instruments}
        next_in = None
        if self.last_scan:
            elapsed = (datetime.now(timezone.utc) - self.last_scan).total_seconds()
            next_in = max(int(self.cfg.scan_interval_minutes * 60 - elapsed), 0)
        return {
            "status": self.status_info(),
            "phase": mon.phase,
            "next_scan_in": next_in if any(instruments.is_open(i) for i in self.instruments) else None,
            "instruments": [{"key": i["key"], "name": i["name"], "label": i["label"], "icon": i["icon"],
                             "open": instruments.is_open(i)} for i in self.instruments],
            "agents": agents,
            "stages": stages,
            "flows": flows,
            "data_age": self.data_age(),
            "data_ages": {i["key"]: self.data_age(i["key"]) for i in self.instruments},
            "ai_models": self.desk.pool.status(),
            "news_error": self.headlines.error if not self.headlines.items else None,
            "calendar": [{"title": e["title"], "impact": e["impact"], "time": e["time"].strftime("%a %H:%M UTC"),
                          "forecast": e["forecast"], "previous": e["previous"]}
                         for e in self.news.upcoming(datetime.now(timezone.utc), 48)][:10],
            "headlines": [{"title": h["title"], "source": h["source"], "link": h["link"], "gold": h["gold"],
                           "time": h["time"].strftime("%H:%M UTC") if h["time"] else ""} for h in self.headlines.latest(10)],
            "log": list(mon.log)[::-1][:200],
            "reviews": list(mon.reviews),
            "market": markets.get(self.primary),
            "markets": markets,
            "trades": [{**{k: t[k] for k in ("direction", "style_label", "entry", "stop_loss", "tps", "status", "stage")},
                        "symbol": t.get("symbol_name", "XAU/USD")} for t in self.storage.open_trades()],
            "performance": stats(self.storage.closed_trades()),
            "settings": {"styles": self.cfg.styles, "min_confidence": self.cfg.min_confidence,
                         "min_votes": self.cfg.min_agent_votes},
            "chart_version": str(self.last_scan) if self.last_scan else "",
        }

    def live_price(self, key: str | None = None) -> dict | None:
        """Near real-time price for the dashboard (called from the dashboard thread, cached 5 s).

        Bitcoin comes straight from Binance. Gold candles come from Twelve Data every few minutes, so between
        scans the price is moved with PAXG/USDT (tokenized gold) calibrated to the last XAU/USD M5 close.
        """
        inst = self.inst(key or self.primary)
        k = inst["key"]
        cached_at, value = self._live.get(k, (0.0, None))
        if time.time() - cached_at < 5 and value:
            return value
        candles = self.candles_by.get(k)
        if not candles or not candles.get("5min"):
            return None
        last = candles["5min"][-1]
        value = {"price": round(last["close"], 2), "source": f"{inst['name']} last scan", "time": last["time"]}
        if not instruments.is_open(inst):
            value["source"] = "market closed – last real price"
            self._live[k] = (time.time(), value)
            return value
        vol_symbol = self.cfg.volume_symbol if k == "XAUUSD" else inst["volume_symbol"]
        ref_candles = (self.volumes_by.get(k) or {}).get("5min")
        if vol_symbol and (ref_candles or inst["source"] == "binance"):
            try:
                import httpx
                r = httpx.get("https://data-api.binance.vision/api/v3/ticker/price",
                              params={"symbol": vol_symbol}, timeout=4)
                r.raise_for_status()
                ticker = float(r.json()["price"])
                if inst["source"] == "binance":
                    value = {"price": round(ticker, 2), "source": "live (Binance)",
                             "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}
                else:
                    ref = next((c for c in reversed(ref_candles) if c["time"] == last["time"]), ref_candles[-1])
                    offset = last["close"] - ref["close"]
                    value = {"price": round(ticker + offset, 2), "source": "live (PAXG-calibrated)",
                             "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                             "offset": round(offset, 2)}
            except Exception as e:
                log.debug("Live price unavailable: %s", e)
        self._live[k] = (time.time(), value)
        return value

    def chart_data(self, tf: str, key: str | None = None) -> dict | None:
        """Candles and SMC overlays for the dashboard's live chart."""
        inst = self.inst(key or self.primary)
        all_candles, market = self.candles_by.get(inst["key"]), self.markets.get(inst["key"])
        if not all_candles or tf not in all_candles or not market or tf not in market:
            return None

        def ts(t: str) -> int:
            return int(datetime.strptime(t[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())

        candles = all_candles[tf][-300:]
        smc_read = market[tf]["smc"]
        offset = len(all_candles[tf]) - len(candles)
        first = ts(candles[0]["time"])

        def zone(z, kind):
            i = max(z["idx"] - offset, 0)
            return {"kind": kind, "dir": z["direction"], "top": z["top"], "bottom": z["bottom"],
                    "from": ts(candles[min(i, len(candles) - 1)]["time"])}

        price, a = candles[-1]["close"], smc_read["atr"] or 1.0

        def near(zs, n):
            zs = [z for z in zs if abs((z["top"] + z["bottom"]) / 2 - price) <= 6 * a]
            return sorted(zs, key=lambda z: abs((z["top"] + z["bottom"]) / 2 - price))[:n]
        zones = [zone(z, "OB") for z in near(smc_read["order_blocks"], 3)]
        zones += [zone(z, "FVG") for z in near(smc_read["fvgs"], 3)]
        zones += [zone(z, "Breaker") for z in near(smc_read.get("breakers", []), 2)]
        markers = [{"time": ts(e["time"]), "position": "belowBar" if e["direction"] == "bullish" else "aboveBar",
                    "color": "#26c281" if e["direction"] == "bullish" else "#ef5350",
                    "shape": "arrowUp" if e["direction"] == "bullish" else "arrowDown", "text": e["type"]}
                   for e in smc_read["events"] if ts(e["time"]) >= first]
        markers += [{"time": ts(w["time"]), "position": "belowBar" if w["direction"] == "bullish" else "aboveBar",
                     "color": "#f5c542", "shape": "circle", "text": "sweep"}
                    for w in smc_read["liquidity"]["sweeps"] if ts(w["time"]) >= first]
        seen, unique = set(), []
        for m in markers:  # several sweeps on one candle -> one marker
            if (m["time"], m["text"], m["position"]) not in seen:
                seen.add((m["time"], m["text"], m["position"]))
                unique.append(m)
        markers = unique
        vol = market[tf]["volume"]
        bubbles = [{"time": ts(b["time"]), "price": b["price"], "x": b["x_avg"], "dir": b["direction"]}
                   for b in vol.get("bubbles", []) if ts(b["time"]) >= first] if vol.get("available") else []
        return {
            "tf": tf,
            "candles": [{"time": ts(c["time"]), "open": c["open"], "high": c["high"], "low": c["low"],
                         "close": c["close"]} for c in candles],
            "markers": sorted(markers, key=lambda m: m["time"]),
            "zones": zones,
            "levels": {k: v for k, v in market.get("levels", {}).items() if abs(v - price) <= 10 * a},
            "liquidity": {"buy": smc_read["liquidity"]["buy_side"][:2], "sell": smc_read["liquidity"]["sell_side"][:2]},
            "market_open": instruments.is_open(inst),
            "symbol": inst["name"],
            "bubbles": bubbles,
            "profile": vol.get("profile", []) if vol.get("available") else [],
            "poc": vol.get("poc"),
            "trades": [{k: t[k] for k in ("direction", "entry", "stop_loss", "tps", "style_label")}
                       for t in self.storage.open_trades() if t.get("instrument", "XAUUSD") == inst["key"]],
        }

    def data_age(self, key: str | None = None) -> dict | None:
        """How fresh the market data is: last M5 candle vs now (UTC)."""
        inst = self.inst(key or self.primary)
        candles = self.candles_by.get(inst["key"])
        if not candles or not candles.get("5min"):
            return None
        last = datetime.strptime(candles["5min"][-1]["time"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        source = f"Twelve Data {self.cfg.symbol}" if inst["source"] == "twelvedata" else f"Binance {inst['symbol']}"
        return {"last_candle": last.strftime("%Y-%m-%d %H:%M UTC"),
                "minutes": int((datetime.now(timezone.utc) - last).total_seconds() // 60),
                "source": source, "open": instruments.is_open(inst)}

    async def dashboard_scan(self):
        try:
            await self.scan(self.app_bot, manual=True)
        except Exception:
            log.exception("Dashboard scan failed")

    def track_record(self, style: str, key: str | None = None) -> str:
        """Short summary of how this style's recent signals ended, given to the Head Trader."""
        closed = [t for t in self.storage.closed_trades() if t["style"] == style
                  and (key is None or t.get("instrument", "XAUUSD") == key)][-10:]
        if not closed:
            return "no closed trades yet"
        s = stats(closed)
        last = ", ".join(f"{t['direction']} {t['outcome']} {t.get('result_r') or 0:+g}R" for t in closed[-5:])
        return (f"last {s['trades']} finished {STYLES[style]['label'][2:]} trades: win rate {s['win_rate']}%, "
                f"total {s['total_r']:+g}R; most recent: {last}")

    def practice_setup(self, key: str | None = None) -> dict | None:
        """Best setup the engine can see right now (normal mode), or a probe trade in the higher-timeframe
        direction – only for practice reviews, never sent as a signal."""
        inst = self.inst(key or self.primary)
        market = self.markets[inst["key"]]
        extra = {"instrument": inst["key"], "symbol_name": inst["name"], "pip": inst["pip"], "contract": inst["contract"]}
        session = sessions.current()
        for style in self.cfg.styles:
            setup = find_setup(style, market, session, self.cfg.min_risk_reward)
            if setup:
                return {**setup, **extra}
        m = market["15min"]
        trend = market["4h"]["smc"]["trend"] or market["1h"]["smc"]["trend"] or "bullish"
        bull = trend == "bullish"
        price, a = m["price"], m["smc"]["atr"] or 1.0
        risk = 1.5 * a
        sign = 1 if bull else -1
        return {"style": "intraday", "style_label": STYLES["intraday"]["label"], "direction": "BUY" if bull else "SELL",
                "entry_type": "MARKET", "entry": round(price, 2), "stop_loss": round(price - sign * risk, 2),
                "tps": [{"price": round(price + sign * r * risk, 2), "rr": r, "source": "R-multiple"} for r in (1.5, 2.5, 4)],
                "price": round(price, 2), "atr": round(a, 2), "score": 0,
                "confluences": [f"Practice probe: H4 {trend} bias, market entry at the current price"],
                "poi": {"kind": "none", "top": price, "bottom": price, "time": ""}, "key": "practice",
                "expiry_min": 240, "timeframes": {"entry": "15min", "confirm": "1h", "bias": "4h"}, **extra}

    async def practice_review(self, key: str | None = None) -> dict | None:
        """Run the whole AI desk on live data right now so the owner can watch it work (no signal is sent)."""
        mon = self.desk.monitor
        if self.scan_lock.locked():
            mon.event("🧪 Practice review skipped – a scan is running", "skip")
            return None
        inst = self.inst(key or self.primary)
        if inst["key"] not in self.markets:
            await self.refresh_market([inst["key"]])
        async with self.scan_lock:
            setup = self.practice_setup(inst["key"])
            session = {**sessions.current(), "news": self.news.brief(), "headlines": self.headlines.brief()}
            try:
                return await self.desk.review(setup, self.markets[inst["key"]], session,
                                              self.track_record(setup["style"], inst["key"]), practice=True,
                                              instrument=inst["ai_name"])
            finally:
                mon.set_phase("idle")

    async def dashboard_practice(self, key: str | None = None):
        try:
            await self.practice_review(key)
        except Exception as e:
            log.exception("Practice review failed")
            self.desk.monitor.event(f"❌ Practice review failed: {str(e)[:150]}", "error")

    async def dashboard_ping(self):
        try:
            await self.desk.ping()
        except Exception:
            log.exception("Agent test failed")

    async def on_startup(self, app: Application):
        self.app_bot = app.bot
        if self.cfg.dashboard_port:
            try:
                from dashboard import Dashboard
                dash = Dashboard(self, asyncio.get_running_loop(), self.cfg.dashboard_host, self.cfg.dashboard_port)
                dash.start()
                self.desk.monitor.event(f"🖥 Dashboard ready at {dash.url}")
                print(f"\n  >>> Live dashboard: {dash.url}  (open it in your browser)\n", flush=True)
                if self.cfg.dashboard_open:
                    import webbrowser
                    webbrowser.open(dash.url)
            except OSError as e:
                log.warning("Dashboard could not start on port %s: %s", self.cfg.dashboard_port, e)
        self.desk.monitor.set_phase("idle")
        self.desk.monitor.event("✅ Bot online – first scan in a few seconds")
        await self.tell_admins(app.bot, f"✅ <b>Gold AI bot is online</b> – scanning every "
                                        f"{self.cfg.scan_interval_minutes} min.")


def main():
    cfg = load_config()
    bot = GoldBot(cfg)
    builder = Application.builder().token(cfg.telegram_token).connect_timeout(20).read_timeout(30)
    if cfg.proxy_url:
        builder = builder.proxy(cfg.proxy_url).get_updates_proxy(cfg.proxy_url)
    app = builder.post_init(bot.on_startup).build()

    app.add_handler(CommandHandler(["start", "subscribe"], bot.cmd_start))
    app.add_handler(CommandHandler("menu", bot.cmd_menu))
    app.add_handler(CommandHandler(["stop", "unsubscribe"], bot.cmd_stop))
    app.add_handler(CommandHandler(["trades", "stats", "market", "help", "history", "news", "status"], bot.cmd_simple))
    app.add_handler(CommandHandler("balance", bot.cmd_balance))
    app.add_handler(CommandHandler("risk", bot.cmd_risk))
    app.add_handler(CommandHandler("lot", bot.cmd_lot))
    app.add_handler(CommandHandler("scan", bot.cmd_scan))
    app.add_handler(CommandHandler("backtest", bot.cmd_backtest))
    app.add_handler(CallbackQueryHandler(bot.on_button))

    jq = app.job_queue
    jq.run_repeating(bot.scheduled_scan, interval=cfg.scan_interval_minutes * 60, first=10)
    weekdays = (1, 2, 3, 4, 5)  # python-telegram-bot counts Sunday as 0
    jq.run_daily(bot.daily_report, time=dtime(cfg.daily_report_hour, 5, tzinfo=timezone.utc), days=weekdays)
    jq.run_daily(bot.briefing, time=dtime(7, 2, tzinfo=timezone.utc), days=weekdays, data="London")
    jq.run_daily(bot.briefing, time=dtime(12, 32, tzinfo=timezone.utc), days=weekdays, data="New York")

    log.info("Gold SMC AI bot started: styles=%s, scan every %s min", ",".join(cfg.styles),
             cfg.scan_interval_minutes)
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except NetworkError as e:  # includes TimedOut
        raise SystemExit(f"\n{NETWORK_HELP}\n(error: {e})")


if __name__ == "__main__":
    main()
