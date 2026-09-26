"""Gold & Bitcoin SMC AI desk - runs on this PC, everything is shown on the local website.

Every few minutes: fetch M5..D1 candles (+ real volume) -> SMC engine finds setups for scalping / intraday /
swing -> news filter -> strategy board -> 26-agent AI desk reviews them -> approved signals appear on the
website (http://localhost:8080) and are tracked live: entry fill, TP1-TP3, stop loss, expiry.
"""

import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone

import backtest
import instruments
import sessions
import tracker
from agents import TradingDesk, agent_records
from config import Config, load_config
from labels import label, lot_size, plain, style_name
from market_data import MarketData
from news import Headlines, NewsCalendar
from setups import STYLES, TF_LABEL, analyze_market, find_setup
from storage import Storage, stats

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("goldbot")

FAILS_BEFORE_ALERT = 3


def failure_hint(error: str) -> str:
    e = error.lower()
    if "api credits" in e or "429" in e or "limit" in e:
        return "Twelve Data free limit reached (800/day, 8/min) - raise SCAN_INTERVAL_MINUTES or wait for the reset."
    if "apikey" in e or "api key" in e or "401" in e:
        return "Twelve Data key rejected - check TWELVEDATA_API_KEY in .env."
    if "timed out" in e or "connect" in e or "network" in e:
        return "Internet / connection problem on this PC."
    return "See the console window for details."


def event_text(t: dict, ev: dict) -> str:
    """One line for the website's update feed when a tracked trade changes."""
    head = f"{t.get('symbol_name', 'XAU/USD')} {t['direction']} {label(t)}"
    pip = t.get("pip", 0.1)
    k = ev["kind"]
    if k == "filled":
        return f"{head}: entry filled at {ev['price']:,.2f} - stop {t['stop_loss']:,.2f}"
    if k == "tp":
        gain = tracker.pips(abs(ev["price"] - t["entry"]), pip)
        text = f"{head}: TP{ev['n']} hit +{gain} pips (1:{ev['rr']:g})"
        if ev["n"] == 3:
            return text + " - full target, trade closed in profit"
        if ev.get("new_sl") is not None:
            text += f" - move stop to {ev['new_sl']:,.2f}" + (" (breakeven)" if ev["n"] == 1 else "")
        return text
    if k == "sl":
        return f"{head}: stop loss hit -{tracker.pips(abs(t['entry'] - ev['price']), pip)} pips (-1R)"
    if k == "protected_stop":
        return f"{head}: closed at the protected stop {ev['price']:,.2f} after TP{ev['stage']} - profit secured"
    if k == "expired":
        return f"{head}: limit order expired - price never returned to {t['entry']:,.2f}"
    if k == "cancelled":
        return f"{head}: cancelled - price reached TP1 without filling the entry"
    if k == "timeout":
        return f"{head}: closed after 7 days at {ev['price']:,.2f}"
    return f"{head}: {k}"


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
                                cfg.gemini_rpm_per_model, providers=cfg.ai_providers)
        self.desk.pool.restore(self.storage.data.get("ai_pool"))
        self.desk.language = cfg.explain_language
        self.desk.min_conviction = cfg.min_conviction
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
        self.ai_views: dict[str, dict] = {}
        self.lab: dict = {"running": False, "kind": None, "style": None, "symbol": None, "result": None}
        self.last_optimize: dict[str, dict] = {}
        self.dashboard = None

    # ================= instruments =================
    # The first instrument in MARKETS is the "primary" one.

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

    # ================= scanning =================

    async def scan(self, manual: bool = False) -> list[str]:
        """One full cycle: data -> track trades -> news -> find & review setups -> publish on the website."""
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
            mon.event("Scan started" + (" (manual)" if manual else ""))
            try:
                return await self._scan(mon, open_now)
            except Exception as e:
                mon.event(f"Scan failed: {str(e)[:160]}", "error")
                raise
            finally:
                mon.set_phase("idle")

    async def refresh_market(self, keys: list[str] | None = None):
        """Market closed: keep the charts and market panels up to date without looking for trades."""
        async with self.scan_lock:
            for key in keys or [i["key"] for i in self.instruments]:
                try:
                    candles = await self.load(key)
                except Exception as e:
                    self.desk.monitor.event(f"{self.inst(key)['name']} data refresh failed: {str(e)[:120]}", "error")
                    continue
                self.desk.monitor.event(f"{self.inst(key)['name']} closed - chart & market data refreshed "
                                        f"({candles['5min'][-1]['close']:,.2f})")
            self.last_scan = datetime.now(timezone.utc)
            await self.news.refresh()
            await self.headlines.refresh()

    def style_params(self, style: str) -> dict:
        """Per-style settings chosen on the website (e.g. from the optimizer), else the .env defaults."""
        ss = self.storage.style_settings(style)
        return {"enabled": ss.get("enabled", True),
                "min_score": ss.get("min_score", self.cfg.min_engine_score),
                "min_rr": ss.get("min_rr", self.cfg.min_risk_reward),
                "tp1_max_r": ss.get("tp1_max_r"),
                "strict": ss.get("strict", self.cfg.strict_mode)}

    async def _scan(self, mon, insts: list[dict]) -> list[str]:
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
        session["headlines_by_cat"] = self.headlines.by_category()

        # Warn on the website before high-impact news (once per event).
        for ev in self.news.due_alerts(now, 20):
            mins = max(int((ev["time"] - now).total_seconds() // 60), 0)
            mon.signal("news", f"High-impact news in {mins} min: {ev['country']} {ev['title']} at "
                               f"{ev['time'].strftime('%H:%M UTC')} - consider breakeven or partial profit")
        blackout = self.news.blackout(now, self.cfg.news_blackout_min, self.cfg.news_blackout_min) \
            if self.cfg.news_blackout_min else None

        notes = []
        for inst in insts:
            try:
                notes += await self._scan_instrument(mon, inst, session, blackout)
            except Exception as e:
                if len(insts) == 1:
                    raise
                notes.append(f"{inst['name']}: scan failed - {str(e)[:100]}")
                mon.event(notes[-1], "error")
        # Keep closed markets' charts fresh too.
        for key in [i["key"] for i in self.instruments if i not in insts and i["key"] not in self.markets]:
            try:
                await self.load(key)
            except Exception as e:
                log.warning("Could not refresh %s: %s", key, e)

        self.last_notes = notes
        self.save_ai_state()
        log.info("Scan done: %s", " | ".join(notes))
        return notes

    async def _scan_instrument(self, mon, inst: dict, session: dict, blackout: dict | None) -> list[str]:
        key = inst["key"]
        candles = await self.load(key)
        market = self.markets[key]
        m5 = candles["5min"]
        feed = self.feeds[key]
        mon.event(f"{inst['name']} data loaded: M5→D1, {m5[-1]['close']:,.2f} · "
                  f"volume {'ok' if feed.volume_ok else 'unavailable'} · news {'ok' if self.news.error is None else 'unavailable'}")

        # 1) live tracking of this instrument's open trades
        for trade in self.storage.open_trades():
            if trade.get("instrument", "XAUUSD") != key:
                continue
            for ev in tracker.update(trade, m5):
                self.notify(trade, ev)
        self.storage.save()

        # 2) no new trades around high-impact USD news (moves gold and crypto alike)
        if blackout:
            note = f"{inst['name']}: news pause - {blackout['title']} at {blackout['time'].strftime('%H:%M UTC')}"
            mon.event(note, "skip")
            return [note.replace(f"{inst['name']}: news pause", "News pause")]

        # 3) find, review and publish setups
        notes = []
        open_trades = [t for t in self.storage.open_trades() if t.get("instrument", "XAUUSD") == key]
        for style in self.cfg.styles:
            name = f"{inst['name']} {style_name(style)}"
            sp = self.style_params(style)
            if not sp["enabled"]:
                notes.append(f"{name}: switched off in settings")
                continue
            setup = find_setup(style, market, session, sp["min_rr"], sp["tp1_max_r"], sp["strict"])
            if not setup:
                notes.append(f"{name}: no setup")
                continue
            setup.update(instrument=key, symbol_name=inst["name"], pip=inst["pip"],
                         contract=self.cfg.contract_size if key == "XAUUSD" else inst["contract"],
                         key=f"{key}:{setup['key']}")
            if setup["score"] < sp["min_score"]:
                notes.append(f"{name}: weak {setup['direction']} setup ({setup['score']})")
                continue
            if self.storage.seen(setup["key"]):
                notes.append(f"{name}: setup already reviewed")
                continue
            if any(t["style"] == style and t["direction"] == setup["direction"] for t in open_trades):
                notes.append(f"{name}: already in a {setup['direction']} trade")
                continue

            self.storage.mark_seen(setup["key"])
            log.info("Reviewing %s %s %s setup (score %s)", key, style, setup["direction"], setup["score"])
            mon.event(f"{name}: engine found {setup['direction']} setup, score {setup['score']}, "
                      f"SMC grade {setup.get('smc_grade', '-')} - " + "; ".join(setup["confluences"][:3]))
            self.desk.records = agent_records(self.storage.closed_trades())
            verdict = await self.desk.review(setup, market, {**session, "intermarket": self.intermarket(key)},
                                             self.track_record(style, key), instrument=inst["ai_name"])
            if verdict.get("ai_down"):
                self._alert_ai_down()
            if not verdict["approved"]:
                if verdict.get("ai_down") and self.cfg.engine_only_score and \
                        setup["score"] >= self.cfg.engine_only_score:
                    verdict["engine_only"] = True
                else:
                    why = verdict.get("reject_reason") or verdict.get("reason")
                    notes.append(f"{name}: {setup['direction']} rejected by AI desk - {why}")
                    continue

            trade = tracker.new_trade(setup, verdict, m5[-1]["time"])
            self.publish(trade)
            open_trades.append(trade)
            notes.append(f"{name}: {trade['direction']} signal published")

        for n in notes:
            mon.event(n, "signal" if "signal published" in n else "info")
        return notes

    def _alert_ai_down(self):
        if time.time() - self._ai_down_alert_at > 7200:
            self._ai_down_alert_at = time.time()
            self.desk.monitor.signal("alert", "The AI desk is not responding (quota or overload) - setups are skipped "
                                              "until it recovers. Check the AI keys in .env and their quota.")

    async def scheduled_scan(self):
        try:
            await self.scan()
        except Exception as e:
            self.fail_count += 1
            self.last_error = str(e)
            log.exception("Scheduled scan failed (%s in a row)", self.fail_count)
            if self.fail_count == FAILS_BEFORE_ALERT:
                self.desk.monitor.signal("alert", f"The last {FAILS_BEFORE_ALERT} market scans failed: "
                                                  f"{self.last_error[:200]} - {failure_hint(self.last_error)}")
            return
        if self.fail_count >= FAILS_BEFORE_ALERT:
            self.desk.monitor.signal("alert", "Recovered - market scans are working again.")
        self.fail_count, self.last_error = 0, ""

    def publish(self, trade: dict):
        """A new approved signal: store it and announce it on the website (feed, sound, notification)."""
        self.storage.add_trade(trade)
        self.desk.monitor.signal(
            "new", f"{trade.get('symbol_name', 'XAU/USD')} {trade['direction']} "
                   f"{'LIMIT' if trade['entry_type'] == 'LIMIT' else 'MARKET'} @ {trade['entry']:,.2f} · SL "
                   f"{trade['stop_loss']:,.2f} · TP1 {trade['tps'][0]:,.2f} · {label(trade)}", trade["id"])

    def notify(self, trade: dict, ev: dict):
        text = event_text(trade, ev)
        self.desk.monitor.event(text, "signal")
        self.desk.monitor.signal(ev["kind"], text, trade["id"])

    # ================= backtest & optimizer (website "Lab") =================

    async def _history(self, style: str, key: str):
        inst = self.inst(key)
        symbol = self.cfg.symbol if inst["key"] == "XAUUSD" else inst["symbol"]
        return await backtest.fetch_history(self.cfg.twelvedata_api_key, symbol, style, inst["source"])

    async def run_lab(self, kind: str, style: str, key: str | None = None):
        """Backtest or optimize one style on recent history; the result is shown in the website's Lab panel."""
        if style not in STYLES or kind not in ("backtest", "optimize"):
            return
        inst = self.inst(key or self.primary)
        if self.lab["running"]:
            self.desk.monitor.event("A backtest is already running - please wait", "skip")
            return
        self.lab = {"running": True, "kind": kind, "style": style, "symbol": inst["name"], "result": None,
                    "started": datetime.now(timezone.utc).strftime("%H:%M UTC")}
        self.desk.monitor.event(f"Lab: {kind} {inst['name']} {style_name(style)} started (1-6 minutes)")
        try:
            data = await self._history(style, inst["key"])
            if kind == "backtest":
                sp = self.style_params(style)
                result = await asyncio.to_thread(backtest.run, data, style, sp["min_rr"], sp["min_score"],
                                                 sp["tp1_max_r"], sp["strict"])
            else:
                result = await asyncio.to_thread(backtest.optimize, data, style)
                self.last_optimize[style] = result
        except Exception as e:
            log.exception("Lab %s failed", kind)
            result = {"error": str(e)[:200]}
        if not result.get("error"):
            result["label"] = f"{inst['name']} {plain(result.get('label', style_name(style)))}"
            result.pop("trades", None)
        self.lab.update(running=False, result=result)
        self.desk.monitor.event(f"Lab: {kind} finished" + (f" - {result['error']}" if result.get("error") else ""),
                                "error" if result.get("error") else "info")

    def apply_setting(self, body: dict) -> dict:
        """Settings changed on the website: account for lot sizes, per-style switches and optimizer results."""
        if "account" in body:
            a = body["account"] or {}
            self.storage.set_account(balance=float(a["balance"]) if a.get("balance") not in (None, "") else 0.0,
                                     risk=float(a["risk"]) if a.get("risk") not in (None, "") else None)
            return {"ok": True}
        style, action = body.get("style"), body.get("action")
        if style not in STYLES:
            return {"ok": False, "error": "unknown style"}
        if action == "on":
            self.storage.set_style_settings(style, enabled=True)
        elif action == "off":
            self.storage.set_style_settings(style, enabled=False)
        elif action == "reset":
            self.storage.reset_style_settings(style)
        elif action == "apply":
            ranked = self.last_optimize.get(style, {}).get("ranked", [])
            i = int(body.get("index", -1))
            if not 0 <= i < len(ranked):
                return {"ok": False, "error": "run the optimizer first"}
            self.storage.set_style_settings(style, enabled=True, **ranked[i]["config"])
        else:
            return {"ok": False, "error": "unknown action"}
        self.desk.monitor.event(f"Settings: {style_name(style)} {action}")
        return {"ok": True}

    # ================= AI market view =================

    async def ai_market_view(self, key: str | None = None):
        inst = self.inst(key or self.primary)
        market = self.markets.get(inst["key"])
        if not market:
            return
        cached = self.ai_views.get(inst["key"])
        if cached and time.time() - cached["ts"] < 600:
            return
        self.ai_views[inst["key"]] = {"ts": time.time(), "time": datetime.now(timezone.utc).strftime("%H:%M UTC"),
                                      "text": "The AI analyst is reading the chart…", "busy": True}
        try:
            text = await self.desk.market_view(market, {**sessions.current(), "news": self.news.brief()},
                                               inst["ai_name"])
        except Exception as e:
            log.warning("AI market view failed: %s", e)
            text = "The AI analyst is busy right now - try again in a minute."
            self.ai_views[inst["key"]]["ts"] = 0
        self.ai_views[inst["key"]].update(text=text.strip(), busy=False)

    # ================= website state =================

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
            "notes": self.last_notes,
        }

    def _slot_label(self, agent_key: str) -> str:
        slots = self.desk.slots_for(agent_key)
        return self.desk.label(slots[0]) if slots else ""

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
                        "regime": (m.get("regime") or {}).get("regime"),
                        "event": f"{ev['type']} {ev['direction']} @ {ev['level']:.2f}" if ev else None,
                        "rsi": round(m["ind"]["rsi"]) if m["ind"]["rsi"] is not None else None,
                        "atr": round(m["smc"]["atr"], 2),
                        "patterns": ", ".join(x["name"].split(" (")[0] for x in m["smc"].get("patterns", [])
                                              if x.get("age", 0) <= 1)[:60]})
        return {"price": market["5min"]["price"], "levels": market.get("levels", {}), "pivots": market.get("pivots", {}),
                "adr": market.get("adr", {}), "tfs": tfs}

    def dashboard_state(self) -> dict:
        from agents import DESKS, PIPELINE, inputs, links

        mon = self.desk.monitor
        link = links()
        closed = self.storage.closed_trades()
        records = agent_records(closed)
        agents = [{"key": a["key"], "name": a["name"], "icon": a["icon"], "role": a.get("focus", ""),
                   "desk": a.get("desk"), "inputs": inputs(a) if stage_no == 1 else [],
                   "receives": link[a["key"]]["from"], "sends": link[a["key"]]["to"],
                   "home_key": self.desk.home_key(a["key"]), "stage": stage_no, "slot": self._slot_label(a["key"]),
                   "record": records.get(a["key"]), **dict(mon.agents.get(a["key"], {}))}
                  for stage_no, (_, members) in enumerate(PIPELINE, 1) for a in members]
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
            "stages": [name for name, _ in PIPELINE],
            "session": sessions.current(),
            "desks": DESKS,
            "board": mon.board,
            "keys": len(self.desk.key_names()),
            "key_names": self.desk.key_names(),
            "flows": list(mon.flows)[-250:],
            "data_age": self.data_age(),
            "data_ages": {i["key"]: self.data_age(i["key"]) for i in self.instruments},
            "ai_models": self.desk.pool.status(),
            "current": mon.current,
            "news_error": self.headlines.error if not self.headlines.items else None,
            "calendar": [{"title": e["title"], "impact": e["impact"], "time": e["time"].strftime("%a %H:%M UTC"),
                          "forecast": e["forecast"], "previous": e["previous"]}
                         for e in self.news.upcoming(datetime.now(timezone.utc), 48)][:10],
            "headlines": [{"title": h["title"], "source": h["source"], "link": h["link"],
                           "cats": h.get("categories", []),
                           "time": h["time"].strftime("%H:%M UTC") if h["time"] else ""} for h in self.headlines.latest(18)],
            "log": list(mon.log)[::-1][:200],
            "reviews": list(mon.reviews),
            "market": markets.get(self.primary),
            "markets": markets,
            "signals": self._signals(),
            "signal_feed": list(mon.signal_feed)[:40],
            "performance": stats(closed),
            "account": self.storage.account,
            "styles": {s: {**self.style_params(s), "label": style_name(s), "active": s in self.cfg.styles}
                       for s in STYLES},
            "lab": self.lab,
            "optimizer": {s: [{"rank": i, "config": r["config"], "trades": r["trades"], "win_rate": r["win_rate"],
                               "total_r": r["total_r"], "profit_factor": r["profit_factor"], "max_dd": r["max_dd"]}
                              for i, r in enumerate(o.get("ranked", [])[:5])]
                          for s, o in self.last_optimize.items() if not o.get("error")},
            "ai_views": {k: {kk: v[kk] for kk in ("time", "text", "busy")} for k, v in self.ai_views.items()},
            "settings": {"styles": self.cfg.styles, "min_confidence": self.cfg.min_confidence,
                         "min_conviction": self.cfg.min_conviction, "min_votes": self.cfg.min_agent_votes,
                         "language": self.cfg.explain_language},
            "chart_version": str(self.last_scan) if self.last_scan else "",
        }

    def live_price(self, key: str | None = None) -> dict | None:
        """Near real-time price for the website (called from the web thread, cached 5 s).

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
        """Candles and SMC overlays for the website's live chart."""
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
        vol = market[tf]["volume"]
        bubbles = [{"time": ts(b["time"]), "price": b["price"], "x": b["x_avg"], "dir": b["direction"]}
                   for b in vol.get("bubbles", []) if ts(b["time"]) >= first] if vol.get("available") else []
        return {
            "tf": tf,
            "candles": [{"time": ts(c["time"]), "open": c["open"], "high": c["high"], "low": c["low"],
                         "close": c["close"]} for c in candles],
            "markers": sorted(unique, key=lambda m: m["time"]),
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

    SIGNAL_FIELDS = ("id", "direction", "entry_type", "entry", "stop_loss", "tps", "rr", "status", "stage", "outcome",
                     "result_r", "created_at", "expires_at", "closed_at", "confidence", "conviction", "smc_grade",
                     "smc_checklist", "headline", "reason", "explain", "invalidation", "management", "risks",
                     "evidence_for", "evidence_against", "confluences", "pip", "timeframes", "per_desk", "board",
                     "engine_only")

    def _signals(self) -> dict:
        """Open signals (last price, floating pips, lot size for the saved account) and the latest finished ones."""
        acct = self.storage.account

        def card(t):
            c = {k: t.get(k) for k in self.SIGNAL_FIELDS}
            c.update(symbol=t.get("symbol_name", "XAU/USD"), instrument=t.get("instrument", "XAUUSD"),
                     style=label(t), confluences=[plain(x) for x in t.get("confluences") or []])
            market = self.markets.get(c["instrument"]) or {}
            price = (market.get("5min") or {}).get("price")
            if price is not None:
                c["price"] = round(price, 2)
                if t["status"] == "active":
                    diff = (price - t["entry"]) if t["direction"] == "BUY" else (t["entry"] - price)
                    risk = abs(t["entry"] - (t.get("initial_sl") or t["stop_loss"]))
                    c.update(floating_pips=tracker.pips(diff, t.get("pip", 0.1)),
                             floating_r=round(diff / risk, 2) if risk else None)
            if acct.get("balance") and t["status"] != "closed":
                lots, risk_usd = lot_size(acct["balance"], acct.get("risk", 1.0), abs(t["entry"] - t["stop_loss"]),
                                          t.get("contract", 100))
                c["lot"] = {"lots": max(lots, 0.01), "risk_usd": round(risk_usd, 2), "min_lot": lots < 0.01}
            return c
        closed = self.storage.closed_trades()[-20:][::-1]
        return {"open": [card(t) for t in self.storage.open_trades()][::-1], "closed": [card(t) for t in closed]}

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

    # ================= website buttons =================

    async def dashboard_scan(self):
        try:
            await self.scan(manual=True)
        except Exception:
            log.exception("Manual scan failed")

    async def dashboard_ping(self):
        try:
            await self.desk.ping()
            self.save_ai_state()
        except Exception:
            log.exception("Agent test failed")

    async def dashboard_practice(self, key: str | None = None):
        try:
            await self.practice_review(key)
        except Exception as e:
            log.exception("Practice review failed")
            self.desk.monitor.event(f"Practice review failed: {str(e)[:150]}", "error")

    # ================= AI desk helpers =================

    def save_ai_state(self):
        """Remember which model/key slots are out of daily quota across restarts."""
        self.storage.data["ai_pool"] = self.desk.pool.export()
        self.storage.save()

    def track_record(self, style: str, key: str | None = None) -> str:
        """Short summary of how this style's recent signals ended, given to the Head Trader."""
        closed = [t for t in self.storage.closed_trades() if t["style"] == style
                  and (key is None or t.get("instrument", "XAUUSD") == key)][-10:]
        if not closed:
            return "no closed trades yet"
        s = stats(closed)
        last = ", ".join(f"{t['direction']} {t['outcome']} {t.get('result_r') or 0:+g}R" for t in closed[-5:])
        return (f"last {s['trades']} finished {style_name(style)} trades: win rate {s['win_rate']}%, "
                f"total {s['total_r']:+g}R; most recent: {last}")

    def practice_setup(self, key: str | None = None) -> dict | None:
        """Best setup the engine can see right now (normal mode), or a probe trade in the higher-timeframe
        direction - only for practice reviews, never published as a signal."""
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
        sign = 1 if bull else -1
        want = "bullish" if bull else "bearish"
        # Probe like a real SMC trade: limit order at the nearest order block / FVG in the H4 direction
        # (below price for a buy, above for a sell), stop beyond the zone. Market entry only if there is none.
        zones = [dict(z, kind=k) for k, field in (("Order Block", "order_blocks"), ("Fair Value Gap", "fvgs"))
                 for z in m["smc"][field] if z["direction"] == want]
        zones = [z for z in zones if (z["top"] <= price if bull else z["bottom"] >= price)
                 and abs(price - (z["top"] if bull else z["bottom"])) <= 4 * a]
        zone = min(zones, key=lambda z: abs(price - (z["top"] if bull else z["bottom"])), default=None)
        if zone:
            entry = zone["top"] if bull else zone["bottom"]
            stop = (zone["bottom"] - 0.3 * a) if bull else (zone["top"] + 0.3 * a)
            etype, poi = "LIMIT", {"kind": zone["kind"], "top": zone["top"], "bottom": zone["bottom"],
                                   "time": zone.get("time", "")}
            why = f"M15 {want} {zone['kind'].lower()} {zone['bottom']:.2f}-{zone['top']:.2f}, limit entry"
        else:
            entry, stop, etype = price, price - sign * 1.5 * a, "MARKET"
            poi, why = {"kind": "none", "top": price, "bottom": price, "time": ""}, "no zone nearby, market entry"
        risk = abs(entry - stop) or a
        return {"style": "intraday", "style_label": STYLES["intraday"]["label"], "direction": "BUY" if bull else "SELL",
                "entry_type": etype, "entry": round(entry, 2), "stop_loss": round(stop, 2),
                "tps": [{"price": round(entry + sign * r * risk, 2), "rr": r, "source": "R-multiple"} for r in (1.5, 2.5, 4)],
                "price": round(price, 2), "atr": round(a, 2), "score": 0, "probe": True,
                "confluences": [f"Practice probe (no real engine setup right now): H4 {trend} bias, {why}"],
                "poi": poi, "key": "practice",
                "expiry_min": 240, "timeframes": {"entry": "15min", "confirm": "1h", "bias": "4h"}, **extra}

    def intermarket(self, key: str) -> dict | None:
        """The other instruments' trend, 24h move and H1 correlation with `key` (for the intermarket analyst)."""
        def returns(candles):
            return {b["time"]: (b["close"] - a["close"]) / a["close"] for a, b in zip(candles, candles[1:]) if a["close"]}
        mine = (self.candles_by.get(key) or {}).get("1h") or []
        out = {}
        for inst in self.instruments:
            other, market = inst["key"], self.markets.get(inst["key"])
            if other == key or not market:
                continue
            h1 = (self.candles_by.get(other) or {}).get("1h") or []
            info = {"price": round(market["5min"]["price"], 2) if "5min" in market else None,
                    "trend": {TF_LABEL[tf]: market[tf]["smc"]["trend"] for tf in ("1day", "4h", "1h", "15min")
                              if tf in market}}
            if len(h1) >= 25:
                info["change_24h_pct"] = round(100 * (h1[-1]["close"] / h1[-25]["close"] - 1), 2)
            ra, rb = returns(mine[-121:]), returns(h1[-121:])
            common = [t for t in ra if t in rb]
            if len(common) >= 30:
                xs, ys = [ra[t] for t in common], [rb[t] for t in common]
                mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
                cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
                sx = sum((x - mx) ** 2 for x in xs) ** 0.5
                sy = sum((y - my) ** 2 for y in ys) ** 0.5
                info["h1_correlation"] = round(cov / (sx * sy), 2) if sx and sy else None
            out[inst["name"]] = info
        return out or None

    async def practice_review(self, key: str | None = None) -> dict | None:
        """Run the whole AI desk on live data right now so the owner can watch it work (nothing is published)."""
        mon = self.desk.monitor
        if self.scan_lock.locked():
            mon.event("Practice review skipped - a scan is running", "skip")
            return None
        inst = self.inst(key or self.primary)
        if inst["key"] not in self.markets:
            await self.refresh_market([inst["key"]])
        async with self.scan_lock:
            setup = self.practice_setup(inst["key"])
            session = {**sessions.current(), "news": self.news.brief(), "headlines": self.headlines.brief(),
                       "headlines_by_cat": self.headlines.by_category(), "intermarket": self.intermarket(inst["key"])}
            self.desk.records = agent_records(self.storage.closed_trades())
            try:
                return await self.desk.review(setup, self.markets[inst["key"]], session,
                                              self.track_record(setup["style"], inst["key"]), practice=True,
                                              instrument=inst["ai_name"])
            finally:
                mon.set_phase("idle")
                self.save_ai_state()

    # ================= start-up =================

    def start_website(self):
        from dashboard import Dashboard
        self.dashboard = Dashboard(self, asyncio.get_running_loop(), self.cfg.dashboard_host, self.cfg.dashboard_port)
        self.dashboard.start()
        self.desk.monitor.event(f"Website ready at {self.dashboard.url}")
        print(f"\n  >>> Open the website: {self.dashboard.url}  (signals, agents, charts)\n", flush=True)
        if self.cfg.dashboard_open:
            import webbrowser
            webbrowser.open(self.dashboard.url)


async def run(cfg: Config, cycles: int | None = None) -> GoldBot:
    """Website + a scan every SCAN_INTERVAL_MINUTES, forever (or `cycles` times in tests)."""
    bot = GoldBot(cfg)
    bot.start_website()
    bot.desk.monitor.set_phase("idle")
    bot.desk.monitor.event("Desk online - first scan in a few seconds")
    log.info("Gold & Bitcoin AI desk started: styles=%s, scan every %s min", ",".join(cfg.styles),
             cfg.scan_interval_minutes)
    await asyncio.sleep(10 if cycles is None else 0)
    n = 0
    while cycles is None or n < cycles:
        await bot.scheduled_scan()
        n += 1
        if cycles is None or n < cycles:
            await asyncio.sleep(cfg.scan_interval_minutes * 60)
    return bot


def main():
    cfg = load_config()
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass
    except OSError as e:
        raise SystemExit(f"The website could not start on port {cfg.dashboard_port}: {e}\n"
                         "Another copy may already be running - close it, or set DASHBOARD_PORT=8081 in .env")


if __name__ == "__main__":
    main()
