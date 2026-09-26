# 🏆 Gold & Bitcoin SMC AI Signal Bot (Telegram)

A Telegram bot that scans **gold (XAU/USD)** and **bitcoin (BTC/USD)** around the clock with a **Smart Money Concepts engine**,
**volume analysis** and a **13-agent Gemini AI desk**, sends **scalping / intraday / swing** signals
with entry, SL and 3 TPs, and **tracks every trade live** (entry fill, TP1/TP2/TP3, SL, expiry).

## Markets

| Market | Candles | Volume | Hours |
|---|---|---|---|
| 🥇 Gold `XAUUSD` | Twelve Data (weekend/closed candles removed) | Binance PAXG/USDT | Sun 22:00 → Fri 21:00 UTC |
| ₿ Bitcoin `BTCUSD` | Binance BTC/USDT (free, no key) | Binance (real) | 24/7 |

Choose with `MARKETS=XAUUSD,BTCUSD` in `.env`. Every signal, chart, lot size (gold 1 lot = 100 oz, BTC 1 lot = 1 BTC),
backtest and dashboard view is per market; the dashboard has Gold / Bitcoin tabs.

## How it works

```
every 5 min ─► Twelve Data: XAU/USD M5 · M15 · H1 · H4 · D1   (+ Binance PAXG/USDT volume)
                │
                ▼
        SMC ENGINE (per timeframe)
        • swings, BOS / CHoCH, trend, displacement strength
        • order blocks, breaker blocks, candlestick patterns, double tops/bottoms, ADR
        • indicators: EMA 20/50/200, RSI, MACD, ADX, Supertrend, Stochastic RSI, Bollinger, VWAP
        • 🛡 strict mode: trend + Supertrend + ADX + liquidity sweep + confirmation candle must all agree
        • order blocks, fair value gaps
        • liquidity pools, equal highs/lows, liquidity sweeps
        • key levels: PDH/PDL, previous week high/low, Asian range, daily/weekly open
        • premium / discount, sessions & killzones
        • volume: relative volume, delta (buy/sell pressure), POC & value area
                │
                ▼
        SETUP FINDER (per style)          bias TF → confirm TF → entry TF
        ⚡ Scalping   H1 → M15 → M5         trigger: CHoCH/BOS, liquidity sweep or key-level sweep
        📊 Intraday   H4 → H1  → M15        entry: order block / FVG retest
        🌊 Swing      D1 → H4  → H1         SL: beyond POI, sweep wick and nearby liquidity
                │                            TPs: next liquidity / key levels (min 1:1.5)
                │                            TP1 must have a clear path (no opposing OB/FVG)
                ▼
        📰 NEWS FILTER – no new trades 30 min around high-impact USD news (ForexFactory calendar)
                │
                ▼ (only setups with confluence score ≥ MIN_ENGINE_SCORE)
        AI DESK – 13 Gemini agents in a 4-stage pipeline (each on its own model, automatic fallback)
        Stage 1  8 analysts: 🏗 Structure · 💧 Liquidity · 🧱 Order/Breaker blocks · ⚡ FVG · 📊 Volume profile
                 · 🕯 Price action · ⚙️ Indicators (EMA/RSI/MACD/ADX/Supertrend/StochRSI/BB/VWAP) · 📰 News & macro
        Stage 2  3 verifiers read ALL analyst reports: 🔗 Confluence · 🛡 Risk · 😈 Devil's Advocate
                 Debate: when the verifiers disagree, their challenge goes back to the analysts, who re-check
                 their data and answer (they may change their vote)
        Stage 3  👑 Head Trader reads all 11 reports + the desk's recent track record → TAKE/SKIP, levels
        Stage 4  ✅ Signal Auditor checks the final signal and can veto it
                │
                ▼ (Head TAKE + confidence ≥ 70 + ≥ 5/8 analysts + ≥ 2/3 verifiers + auditor OK)
        TELEGRAM: 📈 chart + signal + 💰 your lot size ─► live tracking replies on the signal:
        ✅ entry filled · 🎯 TP1 (SL → breakeven) · 🎯 TP2 · 🏆 TP3 · 🛑 SL · ⌛ expired
```

## Telegram features

- **Signal with chart**: candles, order blocks, FVGs, key levels, entry / SL / TP1–TP3 lines and risk/reward boxes;
  caption with pips, R:R, AI confidence, each agent's vote, top confluences and **your personal lot size**
- Buttons on each signal: **📍 Live Status** · **🧠 AI Desk** (every agent's analysis) · **📋 Full Analysis**
- **Live tracking** replies on the signal: entry filled, TP1/TP2/TP3, stop loss, breakeven, expiry
- **Menu** (`/start`): 📡 Active Trades · 📜 History · 📊 Performance · 🌍 Market Now (+ 📈 M15/H1/H4 charts) ·
  🧠 AI Market View · 📰 News calendar · 💰 Risk & Lot · ⚙️ Settings (scalping / intraday / swing,
  briefings, news alerts) · 🔔 Alerts on/off
- **News alerts** 20 minutes before high-impact USD news, and a pause on new signals around it
- **Session briefings**: AI outlook + chart at the London and New York open
- **Daily report** (Mon–Fri) with win rate and total R
- **Owner tools**: ⚡ Scan now · 🩺 Status (API usage, errors, feeds) · 🧪 Backtest · automatic alerts when
  scans keep failing or the AI is down (the first person to `/start` the bot becomes the owner, or set `ADMIN_IDS`)
- Optional **channel** posting (`CHANNEL_ID`)

Commands: `/start` `/menu` `/trades` `/history` `/stats` `/market` `/news` `/balance 1000` `/risk 1` `/lot 50`
`/scan` `/status` `/backtest intraday` `/help` `/stop`

**Honest results**: trades are booked as if 1/3 of the position is closed at each TP and the stop moves to
breakeven after TP1 – not the inflated "max TP reached" many signal channels show.

## Backtest

```bash
python backtest.py intraday      # or scalp / swing   (also: /backtest in Telegram, owner only)
```

Replays the last weeks of gold data candle by candle (no look-ahead) through the same engine and tracker,
and prints win rate, total/average R, max drawdown and profit factor.

**Optimize** (Telegram → 🧪 Backtest → 🔧 Optimize, owner only) tests 12 settings per style (engine score,
minimum R:R, TP1 cap) on the same data, ranks them, and lets you **apply** the best one or **switch a style
off** with one tap. Applied settings are saved in `data.json` and used by live scans. The AI desk and news filter are not
replayed. Use it to tune `MIN_ENGINE_SCORE`, `MIN_RISK_REWARD` and `STYLES` before trusting a style.

## 🖥 Live dashboard (only on your PC)

When the bot starts it opens **http://localhost:8080** (it listens on this computer only):

- **Agent network**: the engine, 8 analysts, 3 verifiers, Head Trader, Signal Auditor and Telegram as a live
  graph. Every time information is passed on, a glowing dot runs along the line. Nodes flash while an agent
  thinks and turn green/red with its vote. Click a node for its full reasoning, model and timing.
- **Live chart** (M5 → D1, TradingView Lightweight Charts): candles with BOS/CHoCH arrows, liquidity sweeps,
  order blocks, breaker blocks, FVGs, key levels (PDH/PDL/PWH/PWL/Asia), buy/sell-side liquidity, volume
  bubbles, volume profile + POC and your open trades. Toggle each layer.
- **Live price** every 5 s (PAXG/USDT calibrated to the last XAU/USD candle between scans) and the time of the
  last real candle, so you can see how fresh the data is.
- **Agent conversation**: every message the agents send each other, with its text – the engine's setup to
  the analysts, each analyst's report to the verifiers, the verifiers' **challenges** back to the analysts and
  their **replies** (debate round), all reports to the Head Trader, the decision to the Signal Auditor and the
  final verdict. Replies/challenges run backwards along the lines in red.
- Live activity feed, last AI decisions, open trades, news calendar + headlines, market structure, system
  health (incl. each Gemini model/key and why it is resting) and performance.
- Buttons: **⚡ Scan now**, **🎓 Practice AI review** (runs the whole 13-agent desk on the live market right
  now so you can watch it – nothing is sent to Telegram) and **🩺 Test agents** (one tiny request per model).

Set `DASHBOARD_PORT=0` to turn it off, or `DASHBOARD_OPEN=off` to stop it opening the browser.
`static/lightweight-charts.js` is TradingView Lightweight Charts™ (Apache 2.0, see `static/LICENSE-lightweight-charts`).

## Keys you need

| Key | Where to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram → @BotFather → `/newbot` |
| `GEMINI_API_KEY` | https://aistudio.google.com/apikey |
| `GEMINI_API_KEYS` (optional) | more Gemini keys, comma separated – each has its own free quota and the bot rotates across all of them |
| `TWELVEDATA_API_KEY` | https://twelvedata.com (the free plan is enough) |

Volume comes from Binance's public PAXG/USDT market data, which needs no key.

### How the agents are spread over your Gemini keys

Every *model × key* pair is its own "slot" with its own rate limit. The desk plans the work so no slot is
overloaded and the strongest models are kept for the decisions that matter:

| Agents | Models | Keys |
|---|---|---|
| 8 analysts (stage 1) | fast *lite* models (big free daily quota) | alternate: key 1, key 2, key 1, … |
| 3 verifiers (stage 2) | strong *flash* models | spread over all keys |
| 👑 Head Trader (stage 3) | strongest model | key 1 first |
| 🛡 Auditor (stage 4) | strongest model | key 2 first (an independent second opinion) |

If a slot hits a limit it rests (short back-off for "busy", until the daily reset for "quota used"), the
agent automatically moves to the next healthy slot, and resting slots are remembered across restarts.
Each agent gets only the data of its own specialty (~1.5–2.5k characters instead of the full 12k brief),
which keeps every call far below the free tokens-per-minute limit.

**How many keys?** 1 key works for light use. 2 keys comfortably cover gold *or* bitcoin all day. For gold
**and** bitcoin 24/7 plus practice reviews, add a 3rd key: `GEMINI_API_KEYS=key2,key3` in `.env`.
The dashboard's **🔑 API keys & Gemini models** panel shows every slot, its health, latency and which agents use it.

## Run it on Windows (easiest)

1. Install Python from https://www.python.org/downloads/ (tick **"Add python.exe to PATH"**).
2. Download this repo (green **Code** button → **Download ZIP**) and unzip it.
3. Double-click **`start.bat`**. The first time, it installs everything and asks you to paste
   your 3 keys; it saves them in a local `.env` file. After that it just starts the bot.

To change a key later, delete `.env` and run `start.bat` again (or edit `.env` in Notepad).

> If Telegram is blocked on your internet, turn on a VPN (e.g. Cloudflare WARP) before starting the bot,
> or set `PROXY_URL` in `.env`.

## Run it manually

```bash
python -m venv venv
# Windows: venv\Scripts\activate    |  Linux/Mac: source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then paste your keys into .env
python bot.py
```

## Run it 24/7 on a server

Only run **one** copy of the bot per token (close `start.bat` on your PC once the server is running),
otherwise Telegram rejects the second copy. A server outside your country also means no VPN is needed.

### Option A – Railway (easiest, everything in the browser)

1. Merge the pull request so the code is on `main`.
2. Go to https://railway.com → **Login with GitHub**.
3. **New Project → Deploy from GitHub repo →** `hunain757/gametelegram`. Railway finds the `Dockerfile`
   and `railway.json` (auto-restart) by itself.
4. Open the service → **Variables** → add `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `TWELVEDATA_API_KEY`
   (and optionally `ADMIN_IDS`, `CHANNEL_ID`, any setting from `.env.example`).
5. **Settings → Volumes → Add volume**, mount path **`/data`** (keeps users, trades and stats across restarts).
6. **Settings → Region**: pick a Europe or Asia region (Binance volume data is not available from US servers).
7. **Deploy**. The **Logs** tab should show `Gold SMC AI bot started`.

Every push to `main` redeploys automatically. Railway's free allowance is small (a one-time trial credit,
then a small monthly credit); a bot that runs 24/7 may use more than that, so check your usage in the
Railway dashboard. For a truly free 24/7 server use Option B on Oracle Cloud's Always Free VM.

### Option B – Any Ubuntu / Debian VPS (cheapest long-term)

Rent a small Linux VPS (1 GB RAM is plenty; Hetzner, Contabo, DigitalOcean, Vultr, or Oracle Cloud's
free tier), open its console / SSH and run:

```bash
curl -fsSL https://raw.githubusercontent.com/hunain757/gametelegram/main/deploy/setup_vps.sh | sudo bash
```

It installs everything, asks for your keys once, and runs the bot as a `goldbot` service that restarts
on crashes and reboots. Run the same command again to update to the latest code.

```bash
sudo journalctl -u goldbot -f        # live logs
sudo systemctl restart goldbot       # restart
sudo nano /opt/goldbot/.env          # change keys/settings (then restart)
```

Gold is closed on weekends (Fri ~21:00 → Sun ~22:00 UTC), so the bot does not scan then.

## Free-plan limits (handled by the bot)

- **Twelve Data**: 800 requests/day, 8/min. Higher timeframes are cached, so a 5-minute scan uses ~450/day.
- **Gemini**: ~5 requests/minute *per model*. Each agent uses a different model, busy or overloaded models
  are skipped automatically, and the AI desk runs only when the engine has found a real setup.

## Settings

All optional settings (styles, scan interval, score / confidence / vote thresholds, R:R, models,
channel, admins, proxy) are documented in `.env.example`.

## Tests

```bash
python -m unittest discover -s tests -t .
```

---
⚠️ These signals are not financial advice. No bot or AI can guarantee profits or catch every move.
Always use a stop loss and risk only a small part of your account per trade.
