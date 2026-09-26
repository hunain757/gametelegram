"""Gold (XAU/USD) Smart-Money AI signal bot for Telegram.

Every few minutes: fetch M5..D1 gold candles (+ PAXG volume) -> SMC engine finds setups for
scalping / intraday / swing -> 5 AI specialists + Head Trader review them -> approved trades
are sent with live TP/SL tracking.
"""

import asyncio
import logging
import time
from html import escape
from datetime import datetime, time as dtime, timedelta, timezone

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, NetworkError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import sessions
import tracker
import ui
from agents import TradingDesk
from config import Config, load_config
from market_data import MarketData
from setups import STYLES, analyze_market, find_setup
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


class GoldBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.storage = Storage(cfg.data_file)
        self.data = MarketData(cfg.twelvedata_api_key, cfg.symbol, cfg.volume_symbol)
        self.desk = TradingDesk(cfg.gemini_api_key, cfg.gemini_model, cfg.min_risk_reward,
                                cfg.min_confidence, cfg.min_agent_votes, cfg.gemini_fallback_models,
                                cfg.gemini_rpm_per_model)
        self.scan_lock = asyncio.Lock()
        self.market: dict | None = None
        self.last_scan: datetime | None = None
        self.last_notes: list[str] = []
        self._ai_view: tuple[float, str] = (0.0, "")

    # ================= scanning =================

    async def scan(self, bot, manual: bool = False) -> list[str]:
        """One full cycle: data -> track open trades -> find & review setups -> publish."""
        if not manual and not sessions.is_market_open():
            return ["Market closed"]
        if self.scan_lock.locked():
            return ["A scan is already running"]

        async with self.scan_lock:
            candles, volumes = await self.data.get()
            self.market = analyze_market(candles, volumes)
            self.last_scan = datetime.now(timezone.utc)
            session = sessions.current()
            m5 = candles["5min"]

            for trade in self.storage.open_trades():
                for ev in tracker.update(trade, m5):
                    await self.notify(bot, trade, ev)
            self.storage.save()

            notes = []
            open_trades = self.storage.open_trades()
            for style in self.cfg.styles:
                label = STYLES[style]["label"]
                setup = find_setup(style, self.market, session, self.cfg.min_risk_reward)
                if not setup:
                    notes.append(f"{label}: no setup")
                    continue
                if setup["score"] < self.cfg.min_engine_score:
                    notes.append(f"{label}: weak {setup['direction']} setup ({setup['score']})")
                    continue
                if self.storage.seen(setup["key"]):
                    notes.append(f"{label}: setup already reviewed")
                    continue
                if any(t["style"] == style and t["direction"] == setup["direction"] for t in open_trades):
                    notes.append(f"{label}: already in a {setup['direction']} trade")
                    continue

                self.storage.mark_seen(setup["key"])
                log.info("Reviewing %s %s setup (score %s)", style, setup["direction"], setup["score"])
                verdict = await self.desk.review(setup, self.market, session)
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

            self.last_notes = notes
            log.info("Scan done: %s", " | ".join(notes))
            return notes

    async def scheduled_scan(self, context: ContextTypes.DEFAULT_TYPE):
        try:
            await self.scan(context.bot)
        except Exception:
            log.exception("Scheduled scan failed")

    def _targets(self, style: str | None) -> list:
        targets = self.storage.subscribers(style)
        if self.cfg.channel_id:
            targets.append(self.cfg.channel_id)
        return targets

    async def publish(self, bot, trade: dict):
        self.storage.add_trade(trade)
        text, kb = ui.signal_card(trade), ui.signal_keyboard(trade["id"])
        for chat_id in self._targets(trade["style"]):
            try:
                msg = await bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=kb)
                trade["messages"][str(chat_id)] = msg.message_id
            except Exception as e:
                log.warning("Could not send signal to %s: %s", chat_id, e)
        self.storage.save()

    async def notify(self, bot, trade: dict, ev: dict):
        text = ui.event_message(trade, ev)
        for chat_id, msg_id in trade["messages"].items():
            try:
                await bot.send_message(chat_id, text, parse_mode=HTML, reply_to_message_id=msg_id,
                                       allow_sending_without_reply=True)
            except Exception as e:
                log.warning("Could not send update to %s: %s", chat_id, e)

    async def daily_report(self, context: ContextTypes.DEFAULT_TYPE):
        since = datetime.now(timezone.utc) - timedelta(days=1)
        closed = self.storage.closed_trades(since)
        if not closed and not self.storage.open_trades():
            return
        text = ui.daily_report(stats(closed), len(self.storage.open_trades()))
        for chat_id in self._targets(None):
            try:
                await context.bot.send_message(chat_id, text, parse_mode=HTML)
            except Exception as e:
                log.warning("Could not send daily report to %s: %s", chat_id, e)

    # ================= commands & buttons =================

    def is_admin(self, user_id: int) -> bool:
        return not self.cfg.admin_ids or user_id in self.cfg.admin_ids

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        self.storage.set_subscribed(chat_id, True)
        text, kb = ui.main_menu(self.storage.user(chat_id), self.is_admin(update.effective_user.id))
        await update.message.reply_text(text, parse_mode=HTML, reply_markup=kb)

    async def cmd_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text, kb = ui.main_menu(self.storage.user(update.effective_chat.id), self.is_admin(update.effective_user.id))
        await update.message.reply_text(text, parse_mode=HTML, reply_markup=kb)

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.storage.set_subscribed(update.effective_chat.id, False)
        await update.message.reply_text("🔕 Alerts OFF. Send /start to switch them on again.")

    async def cmd_simple(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/trades, /stats, /market, /help – same screens as the menu buttons."""
        key = {"trades": "trades", "stats": "perf", "market": "mkt", "help": "help", "history": "hist"}[
            update.message.text.split()[0].lstrip("/").split("@")[0]]
        text, kb = await self.screen(key, update.effective_chat.id, update.effective_user.id)
        await update.message.reply_text(text, parse_mode=HTML, reply_markup=kb)

    async def cmd_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ Only the bot admin can run a manual scan.")
            return
        msg = await update.message.reply_text("🔎 AI desk is scanning XAU/USD on M5 → D1…")
        await msg.edit_text(await self._manual_scan_text(context.bot), parse_mode=HTML)

    async def _manual_scan_text(self, bot) -> str:
        try:
            notes = await self.scan(bot, manual=True)
        except Exception as e:
            log.exception("Manual scan failed")
            return f"❌ Scan failed: {e}"
        return "🔎 <b>Scan complete</b>\n" + "\n".join(f"• {n}" for n in notes)

    async def screen(self, key: str, chat_id: int, user_id: int):
        user = self.storage.user(chat_id)
        price = self.market["5min"]["price"] if self.market else None
        if key == "menu":
            return ui.main_menu(user, self.is_admin(user_id))
        if key == "trades":
            return ui.trades_list(self.storage.open_trades(), price)
        if key == "hist":
            return ui.history(self.storage.closed_trades())
        if key == "perf":
            return ui.performance(stats(self.storage.closed_trades())), ui.back()
        if key == "mkt":
            text = ui.market_dashboard(self.market, sessions.current(), sessions.is_market_open(), self.last_scan)
            return text, ui.back()
        if key == "set":
            return ui.settings(user)
        if key == "help":
            return ui.HELP, ui.back()
        return ui.main_menu(user, self.is_admin(user_id))

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        data = q.data or ""
        chat = q.message.chat
        user_id = q.from_user.id

        # Buttons under a signal (also work in channels, where we answer with a popup).
        if data[:2] in ("t:", "r:"):
            trade = self.storage.trade(data[2:])
            if not trade:
                await q.answer("Trade not found", show_alert=True)
                return
            price = self.market["5min"]["price"] if self.market else None
            text = ui.trade_status(trade, price) if data[0] == "t" else ui.ai_report(trade)
            if chat.type == ChatType.CHANNEL:
                plain = text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
                plain = plain.replace("<code>", "").replace("</code>", "")
                await q.answer(plain[:195], show_alert=True)
            else:
                await q.answer()
                await q.message.reply_text(text, parse_mode=HTML)
            return

        if data.startswith("sty:"):
            self.storage.toggle_style(chat.id, data[4:])
            data = "set"
        elif data == "alert":
            u = self.storage.user(chat.id)
            self.storage.set_subscribed(chat.id, not u.get("subscribed"))
            data = "menu"
        elif data == "scan":
            if not self.is_admin(user_id):
                await q.answer("Only the admin can scan manually", show_alert=True)
                return
            await q.answer("Scanning…")
            await q.message.reply_text(await self._manual_scan_text(context.bot), parse_mode=HTML)
            return
        elif data == "aiview":
            await q.answer("AI analyst is reading the chart…")
            await q.message.reply_text(await self._ai_market_view(), parse_mode=HTML)
            return

        await q.answer()
        text, kb = await self.screen(data, chat.id, user_id)
        try:
            await q.edit_message_text(text, parse_mode=HTML, reply_markup=kb)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    async def _ai_market_view(self) -> str:
        if not self.market:
            return "🧠 No market data yet – wait for the first scan."
        cached_at, text = self._ai_view
        if time.time() - cached_at > 600:
            try:
                text = await self.desk.market_view(self.market, sessions.current())
                self._ai_view = (time.time(), text)
            except Exception as e:
                log.warning("AI market view failed: %s", e)
                return "🧠 AI analyst is busy right now, try again in a minute."
        return f"🧠 <b>AI Market View · XAU/USD</b>\n{ui.LINE}\n{escape(text)}\n\n<i>{ui.DISCLAIMER}</i>"

    async def on_startup(self, app: Application):
        for admin in self.cfg.admin_ids:
            try:
                await app.bot.send_message(admin, "✅ <b>Gold AI bot is online</b> – scanning every "
                                                  f"{self.cfg.scan_interval_minutes} min.", parse_mode=HTML)
            except Exception:
                pass


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
    app.add_handler(CommandHandler(["trades", "stats", "market", "help", "history"], bot.cmd_simple))
    app.add_handler(CommandHandler("scan", bot.cmd_scan))
    app.add_handler(CallbackQueryHandler(bot.on_button))

    app.job_queue.run_repeating(bot.scheduled_scan, interval=cfg.scan_interval_minutes * 60, first=10)
    # Mon-Fri (python-telegram-bot counts Sunday as 0).
    app.job_queue.run_daily(bot.daily_report, time=dtime(cfg.daily_report_hour, 5, tzinfo=timezone.utc),
                            days=(1, 2, 3, 4, 5))

    log.info("Gold SMC AI bot started: styles=%s, scan every %s min", ",".join(cfg.styles),
             cfg.scan_interval_minutes)
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except NetworkError as e:  # includes TimedOut
        raise SystemExit(f"\n{NETWORK_HELP}\n(error: {e})")


if __name__ == "__main__":
    main()
