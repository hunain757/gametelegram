# 🏆 Gold SMC AI Signal Bot (Telegram)

A Telegram bot that scans **gold (XAU/USD)** around the clock with a **Smart Money Concepts engine**,
**volume analysis** and a **desk of 9 Gemini AI agents**, sends **scalping / intraday / swing** signals
with entry, SL and 3 TPs, and **tracks every trade live** (entry fill, TP1/TP2/TP3, SL, expiry).

## How it works

```
every 5 min ─► Twelve Data: XAU/USD M5 · M15 · H1 · H4 · D1   (+ Binance PAXG/USDT volume)
                │
                ▼
        SMC ENGINE (per timeframe)
        • swings, BOS / CHoCH, trend, displacement strength
        • candlestick patterns (engulfing, pin bar, stars, inside bar), double tops/bottoms, ADR
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
        AI DESK (Gemini, each agent on its own model, automatic fallback when one is busy)
        🏗 Structure  💧 Liquidity/OB  📊 Volume  🕯 Price Action  ⚙️ Momentum
        🕐 Session/ADR/News  🛡 Risk  😈 Devil's Advocate                 → vote TAKE/SKIP
        👑 Head Trader reads all 8 reports → final TAKE/SKIP, confidence, fine-tuned levels
                │
                ▼ (Head Trader TAKE + confidence ≥ 70 + ≥ 5/8 agents agree)
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
and prints win rate, total/average R, max drawdown and profit factor. The AI desk and news filter are not
replayed. Use it to tune `MIN_ENGINE_SCORE`, `MIN_RISK_REWARD` and `STYLES` before trusting a style.

## Keys you need

| Key | Where to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram → @BotFather → `/newbot` |
| `GEMINI_API_KEY` | https://aistudio.google.com/apikey |
| `TWELVEDATA_API_KEY` | https://twelvedata.com (the free plan is enough) |

Volume comes from Binance's public PAXG/USDT market data, which needs no key.

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
