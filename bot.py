"""Gold (XAU/USD) AI signal bot for Telegram.

Every few minutes it pulls gold candles on 15min/1h/4h, asks Google Gemini whether
there is a high-probability trade, and only if there is (and the levels pass the
sanity checks) sends a signal to subscribers and the optional channel.
"""

import asyncio
import html
import logging
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import NetworkError
from telegram.ext import Application, CommandHandler, ContextTypes

from analyzer import Analyzer, validate_signal
from config import Config, load_config
from market_data import fetch_all_timeframes, is_market_open
from storage import Storage

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("goldbot")

NETWORK_HELP = (
    "Could not reach Telegram (api.telegram.org). Telegram is probably blocked on this internet.\n"
    "Fix: turn on a VPN (e.g. Cloudflare WARP / 1.1.1.1) and run again, or set PROXY_URL in .env\n"
    "(a SOCKS5/HTTP proxy - the MTProto proxy used by the Telegram app will NOT work)."
)

DISCLAIMER = "⚠️ Not financial advice. Trade at your own risk and always use a stop loss."


class GoldSignalBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.storage = Storage(cfg.data_file)
        self.analyzer = Analyzer(cfg.gemini_api_key, cfg.gemini_model, cfg.symbol, cfg.min_risk_reward)
        self.scan_lock = asyncio.Lock()
        self.last_scan: dict = {"time": None, "result": "no scan yet"}

    # ---------- scanning ----------

    async def scan(self, force: bool = False) -> tuple[dict | None, str]:
        """Run one market scan. Returns (signal or None, human-readable status)."""
        if not force and not is_market_open():
            return None, "Gold market is closed"
        if self.scan_lock.locked():
            return None, "A scan is already running"

        async with self.scan_lock:
            candles = await fetch_all_timeframes(self.cfg.twelvedata_api_key, self.cfg.symbol)
            sig = await self.analyzer.analyze(candles)
            ok, reason = validate_signal(sig, self.cfg.min_confidence, self.cfg.min_risk_reward)
            status = f"price {sig['price']:.2f} — {reason}"
            if ok and not force and self._in_cooldown():
                ok, status = False, f"{status} (skipped: cooldown after last signal)"
            self.last_scan = {"time": datetime.now(timezone.utc), "result": status, "ai_reason": sig.get("reason")}
            log.info("Scan: %s | AI: %s", status, sig.get("reason"))
            return (sig if ok else None), status

    def _in_cooldown(self) -> bool:
        last = self.storage.last_signal
        if not last:
            return False
        sent_at = datetime.fromisoformat(last["time"])
        return datetime.now(timezone.utc) - sent_at < timedelta(minutes=self.cfg.cooldown_minutes)

    async def scheduled_scan(self, context: ContextTypes.DEFAULT_TYPE):
        try:
            sig, _ = await self.scan()
        except Exception:
            log.exception("Scheduled scan failed")
            return
        if sig:
            await self.broadcast(context, sig)

    async def broadcast(self, context: ContextTypes.DEFAULT_TYPE, sig: dict):
        self.storage.set_last_signal({**sig, "time": datetime.now(timezone.utc).isoformat()})
        text = format_signal(sig, self.cfg.symbol)
        targets = self.storage.subscribers + ([self.cfg.channel_id] if self.cfg.channel_id else [])
        for chat_id in targets:
            try:
                await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
            except Exception as e:
                log.warning("Could not send to %s: %s", chat_id, e)

    # ---------- commands ----------

    def _is_admin(self, update: Update) -> bool:
        return not self.cfg.admin_ids or update.effective_user.id in self.cfg.admin_ids

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.storage.subscribe(update.effective_chat.id)
        await update.message.reply_text(
            "👋 Welcome to the Gold AI Signal Bot!\n\n"
            f"I scan {self.cfg.symbol} every {self.cfg.scan_interval_minutes} minutes with AI "
            "and send a signal ONLY when there is a strong trade.\n\n"
            "✅ You are now subscribed.\n\n"
            "/status – bot status and last scan\n"
            "/last – last signal sent\n"
            "/scan – scan the market now\n"
            "/stop – stop receiving signals\n\n" + DISCLAIMER
        )

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.storage.unsubscribe(update.effective_chat.id)
        await update.message.reply_text("🔕 Unsubscribed. Send /start to subscribe again.")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        scan_time = self.last_scan["time"]
        await update.message.reply_text(
            f"📊 Market: {'OPEN 🟢' if is_market_open() else 'CLOSED 🔴'}\n"
            f"⏱ Scan every: {self.cfg.scan_interval_minutes} min\n"
            f"🎯 Min confidence: {self.cfg.min_confidence}% | Min R:R: {self.cfg.min_risk_reward}\n"
            f"👥 Subscribers: {len(self.storage.subscribers)}\n\n"
            f"🔎 Last scan: {scan_time.strftime('%Y-%m-%d %H:%M UTC') if scan_time else '-'}\n"
            f"Result: {self.last_scan['result']}\n"
            f"AI view: {self.last_scan.get('ai_reason') or '-'}"
        )

    async def cmd_last(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        last = self.storage.last_signal
        if not last:
            await update.message.reply_text("No signal has been sent yet.")
            return
        await update.message.reply_text(
            f"🕒 Sent at {last['time'][:16].replace('T', ' ')} UTC\n\n" + format_signal(last, self.cfg.symbol),
            parse_mode=ParseMode.HTML,
        )

    async def cmd_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            await update.message.reply_text("⛔ Only the bot admin can run a manual scan.")
            return
        await update.message.reply_text("🔎 Scanning the gold market with AI, please wait...")
        try:
            sig, status = await self.scan(force=True)
        except Exception as e:
            log.exception("Manual scan failed")
            await update.message.reply_text(f"❌ Scan failed: {e}")
            return
        if sig:
            await self.broadcast(context, sig)
        else:
            reason = self.last_scan.get("ai_reason") or ""
            await update.message.reply_text(f"😴 No trade right now.\n{status}\n\n{reason}")


def format_signal(sig: dict, symbol: str) -> str:
    emoji = "🟢" if sig["direction"] == "BUY" else "🔴"
    return (
        f"{emoji} <b>{symbol} {sig['direction']}</b> {emoji}\n\n"
        f"📍 Entry: <b>{sig['entry']:.2f}</b>\n"
        f"🛑 Stop Loss: <b>{sig['stop_loss']:.2f}</b>\n"
        f"🎯 TP1: <b>{sig['take_profit_1']:.2f}</b>\n"
        f"🎯 TP2: <b>{sig['take_profit_2']:.2f}</b>\n\n"
        f"📈 Confidence: {sig['confidence']}% | R:R 1:{sig['risk_reward']}\n"
        f"🧠 {html.escape(str(sig.get('reason', '')))}\n\n"
        f"<i>{DISCLAIMER}</i>"
    )


def main():
    cfg = load_config()
    bot = GoldSignalBot(cfg)
    builder = Application.builder().token(cfg.telegram_token).connect_timeout(20).read_timeout(20)
    if cfg.proxy_url:
        builder = builder.proxy(cfg.proxy_url).get_updates_proxy(cfg.proxy_url)
    app = builder.build()

    app.add_handler(CommandHandler(["start", "subscribe"], bot.cmd_start))
    app.add_handler(CommandHandler(["stop", "unsubscribe"], bot.cmd_stop))
    app.add_handler(CommandHandler("status", bot.cmd_status))
    app.add_handler(CommandHandler("last", bot.cmd_last))
    app.add_handler(CommandHandler("scan", bot.cmd_scan))

    app.job_queue.run_repeating(bot.scheduled_scan, interval=cfg.scan_interval_minutes * 60, first=10)

    log.info("Gold signal bot started (scan every %s min)", cfg.scan_interval_minutes)
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except NetworkError as e:  # includes TimedOut
        raise SystemExit(f"\n{NETWORK_HELP}\n(error: {e})")


if __name__ == "__main__":
    main()
