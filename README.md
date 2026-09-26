# Gold & Bitcoin SMC AI Signal Desk (website app)

A local website app that scans **gold (XAU/USD)** and **bitcoin (BTC/USD)** around the clock with a **Smart Money
Concepts engine**, **volume analysis**, a **regime-aware 17-strategy board** and a **26-agent AI desk (Mistral / Groq /
Gemini) that learns from its own track record**. It publishes **scalping / intraday / swing** signals with entry, SL and
3 TPs on the website and **tracks every trade live** (entry fill, TP1/TP2/TP3, SL, expiry). No Telegram, no bot
token, no server: everything runs on your PC.

## Where do signals appear?

**On the website** (http://localhost:8080, opens by itself on Windows). Every approved trade appears in the
**Signals** section with the levels, a price track (stop · entry · TP1-3 · current price), floating pips and R,
**your lot size**, confidence, conviction, SMC grade, the AI's explanation (in `EXPLAIN_LANGUAGE`, e.g. Roman
Urdu), invalidation, management plan and risks. Entry fills, TP hits and stops show up in the **Updates** feed.
Press **Enable alerts + sound** once to get a sound and a desktop notification for every new signal, trade
update, high-impact news warning and system alert.

## Markets

| Market | Candles | Volume | Hours |
|---|---|---|---|
| Gold `XAUUSD` | Twelve Data (weekend/closed candles removed) | Binance PAXG/USDT | Sun 22:00 → Fri 21:00 UTC |
| Bitcoin `BTCUSD` | Binance BTC/USDT (free, no key) | Binance (real) | 24/7 |

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
        • strict mode: trend + Supertrend + ADX + liquidity sweep + confirmation candle must all agree
        • order blocks, fair value gaps
        • liquidity pools, equal highs/lows, liquidity sweeps
        • key levels: PDH/PDL, previous week high/low, Asian range, daily/weekly open
        • premium / discount, sessions & killzones
        • volume: relative volume, delta (buy/sell pressure), POC & value area
                │
                ▼
        SETUP FINDER (per style)          bias TF → confirm TF → entry TF
        Scalping   H1 → M15 → M5         trigger: CHoCH/BOS, liquidity sweep or key-level sweep
        Intraday   H4 → H1  → M15        entry: order block / FVG retest
        Swing      D1 → H4  → H1         SL: beyond POI, sweep wick and nearby liquidity
                │                            TPs: next liquidity / key levels (min 1:1.5)
                │                            TP1 must have a clear path (no opposing OB/FVG)
                ▼
        NEWS FILTER – no new trades 30 min around high-impact USD news (ForexFactory calendar)
                │
                ▼ (only setups with confluence score ≥ MIN_ENGINE_SCORE)
        MARKET REGIME – every timeframe is classed trending / ranging / compressed / volatile / transition
                 (ADX, Bollinger width rank, TTM squeeze)
                │
        STRATEGY BOARD – 17 classic strategies vote AGREE / AGAINST / NEUTRAL on the setup; only the
                 strategies that fit the current regime are counted (no mean reversion in a trend, etc.):
                 trend (EMA pullback, Supertrend+MACD, Ichimoku, ADX/DMI, Heikin-Ashi, multi-timeframe),
                 breakout (Donchian, TTM squeeze), mean reversion (RSI+Bollinger, VWAP, RSI divergence),
                 ICT (OTE 62-79 %, premium/discount), momentum (Stochastic, CCI+Williams %R), volume (OBV, delta)
                │
        AI DESK – 26 Gemini agents in a 5-stage pipeline; each has ONE job and sees only its own data
        Stage 1  18 analysts in 3 desks
                 Technical: Structure · Liquidity · OB/Breaker · FVG · Volume · Price action
                              · Momentum · Trend indicators · Volatility
                 Strategy:  Multi-timeframe · ICT & Fibonacci · Levels & pivots · Trend-following
                              · Breakout & reversion
                 Macro:     Economic calendar · Geopolitics & world events · Central banks & dollar
                              · Intermarket (gold ↔ bitcoin correlation) & sentiment
        Stage 2  3 desk leads check their analysts' evidence; they challenge the doubtful ones, who re-check
                 their own data and answer (debate – they may change their vote)
        Stage 3  3 verifiers cross-check the desks: Confluence · Risk · Devil's Advocate
        Stage 4  Head Trader reads the desks, verifiers, strategy board, the style's track record and every
                 agent's own accuracy → TAKE/SKIP, levels
        Stage 5  Signal Auditor checks the final signal and can veto it
                │
                ▼ Decision: Head TAKE + confidence ≥ 70 + desk CONVICTION ≥ 58 (weighted average of every
                  agent's score: analysts 40 %, desk leads 25 %, verifiers 25 %, strategy board 10 %)
                  Hard vetoes only for concrete dangers: analysts split, too few analysts see an edge,
                  Risk Manager finds a broken stop / R:R / news, strategy board clearly against, auditor veto
        WEBSITE: signal card + your lot size + sound/notification ─► live tracking in the Updates feed:
        entry filled · TP1 (SL → breakeven) · TP2 · TP3 · SL · expired
```

## SMC grade

Every setup gets a 10-point SMC checklist and a grade (A+ / A / B / C): HTF bias, confirm TF, liquidity
sweep, BOS/CHoCH, displacement, quality POI (order block with an FVG inside, fresh zone, or nested inside a
higher-timeframe OB/FVG), discount/premium, OTE entry (62-79 %), killzone timing and a clear path to a liquidity
target. Judas swings (Asia range swept in a killzone) score extra. The grade and checklist are shown on the
signal and given to the agents. The engine runs in normal mode by default (`STRICT_MODE=off`) – the desk is the
quality filter; `MIN_CONVICTION` in `.env` sets how much agreement a signal needs.

## Strict agents

Every agent must finish its job before its vote counts: a valid vote, a score, a real summary and evidence
with exact numbers from its data (the macro/news desk needs at least one concrete item). An incomplete answer
is sent back once with exactly what is missing ("YOUR PREVIOUS ANSWER WAS REJECTED BECAUSE …"); the better of
the two answers is kept and the dashboard marks the agent "Re-asked once". NEUTRAL votes do not block a signal,
SKIP votes weigh against it, and the final decision uses the weighted **conviction** score.

**Honest results**: trades are booked as if 1/3 of the position is closed at each TP and the stop moves to
breakeven after TP1 – not the inflated "max TP reached" many signal channels show.

## Strategy lab (backtest & optimizer)

On the website: **Strategy lab → pick a style → Backtest** (or **Optimize settings**). It replays the selected
market's recent history candle by candle (no look-ahead) through the same engine and tracker and shows win
rate, total/average R, max drawdown and profit factor. **Optimize** tests 12 settings per style (engine score,
minimum R:R, TP1 cap) and ranks them; **Apply** uses one for live scans. **Signal styles** switches a style on or
off (or back to the `.env` defaults) without restarting. Settings are saved in `data.json`. The AI desk and news
filter are not replayed.

From the command line: `python backtest.py intraday` (or scalp / swing).

## The website (only on your PC)

When the app starts it opens **http://localhost:8080** (it listens on this computer only):

The site has a sidebar with seven pages – **Signals** (home), **Live chart**, **AI desk**, **Strategy lab**,
**Performance**, **Market & news** and **Settings** – with smooth page transitions, animated signal cards,
count-up KPIs, a price flash on every tick and a live-flowing agent network. It works on a phone too (bottom
navigation). Animations switch off automatically if your system asks for reduced motion.


- **Agent network**: the engine, 18 analysts grouped in their 3 desks, 3 desk leads, 3 verifiers, Head Trader,
  Signal Auditor and the website output as a live graph. Every time information is passed on, a glowing dot runs along
  the line. Nodes flash while an agent works and turn green/red with its vote; the coloured bar and `KEY 1`/`KEY 2`
  label show which API key the agent runs on. Click a node for its job, the data it receives, who it gets
  information from and sends it to, its evidence, risk, model and timing.
- **KPI strip**: session and killzone, H1 regime, open trades, win rate, total R, desk reviews, AI requests,
  model slots ready.
- **Agent directory**: a table of all 26 agents – only job, data it receives, who it sends to, key/model,
  **track record** (how often it was right, vote weight) and current vote.
- **Strategy board**: all 17 strategies with their verdict and the exact numbers behind it; groups that do not
  fit the current regime are greyed out and not counted.
- Clean design without emoji: every agent has a short code badge (MS, LQ, OB, FVG, … HT, AUD) coloured by desk.
- **Live chart** (M5 → D1, TradingView Lightweight Charts): candles with BOS/CHoCH arrows, liquidity sweeps,
  order blocks, breaker blocks, FVGs, key levels (PDH/PDL/PWH/PWL/Asia), buy/sell-side liquidity, volume
  bubbles, volume profile + POC and your open trades. Toggle each layer.
- **Live price** every 5 s (PAXG/USDT calibrated to the last XAU/USD candle between scans) and the time of the
  last real candle, so you can see how fresh the data is.
- **Agent conversation**: every message the agents send each other, with its text – the engine's setup to
  the analysts, each analyst's report to its desk lead, the leads' **challenges** back to their analysts and
  their **replies** (debate round), all reports to the Head Trader, the decision to the Signal Auditor and the
  final verdict. Replies/challenges run backwards along the lines in red.
- **Account & lot size**: enter your balance and risk % once – every open signal shows the exact lot.
- **AI market view**: a plain-language read of the current chart in your language.
- **Performance**: equity curve (R), win rate, profit factor, max drawdown, per market and per style.
- Live activity feed, last AI decisions, news calendar + headlines, market structure and system health
  (incl. each AI model/key and why it is resting).
- Buttons: **Scan now**, **Practice AI review** (runs the whole 26-agent desk on the live market right
  now so you can watch it – nothing is published) and **Test agents** (one tiny request per model).

Set `DASHBOARD_PORT=8081` if 8080 is taken, or `DASHBOARD_OPEN=off` to stop it opening the browser.
`static/lightweight-charts.js` is TradingView Lightweight Charts™ (Apache 2.0, see `static/LICENSE-lightweight-charts`).

## Keys you need

| Key | Where to get it |
|---|---|
| `GEMINI_API_KEY` (optional) | https://aistudio.google.com/apikey |
| `GEMINI_API_KEYS` (optional) | more Gemini keys, comma separated – each has its own free quota and the bot rotates across all of them |
| `MISTRAL_API_KEY` (recommended) | https://console.mistral.ai/api-keys – free "Experiment" plan, roughly 1 billion tokens a month, no card (phone verification) |
| `GROQ_API_KEY` (optional) | https://console.groq.com/keys – free, very fast, ~1,000 requests a day per model |
| `TWELVEDATA_API_KEY` | https://twelvedata.com (the free plan is enough) |

**Which AI does the desk use?** With a Mistral (or Groq) key the 18 analysts run on the small/medium models
and the desk leads, verifiers, Head Trader and Auditor on the largest model; Gemini becomes the automatic
fallback. Requests to one Mistral key are spaced about 1 per second, which is its free limit. Without those keys
everything runs on Gemini as before. Gemini is optional: leave `GEMINI_API_KEY` empty (or set
`USE_GEMINI=off`) to run only on Mistral / Groq. Limits are per account – a second key helps only if it comes from another
account.

Volume comes from Binance's public PAXG/USDT market data, which needs no key.

### How the agents are spread over your Gemini keys

Every *model × key* pair is its own "slot" with its own rate limit. The desk plans the work so no slot is
overloaded and the strongest models are kept for the decisions that matter:

| Agents | Models | Key 1 | Key 2 |
|---|---|---|---|
| 18 analysts (stage 1) | fast *lite* models (big free daily quota) | 9 | 9 |
| 3 desk leads (stage 2) | strong *flash* models | technical, macro | strategy |
| 3 verifiers (stage 3) | strong *flash* models | risk | confluence, devil |
| Head Trader (stage 4) | strongest model | | |
| Auditor (stage 5) | strongest model | | (independent second opinion) |
| **total** | | **13** | **13** |

With 3 keys the same plan is spread over all three.

If a slot hits a limit it rests (short back-off for "busy", until the daily reset for "quota used"), the
agent automatically moves to the next healthy slot, and resting slots are remembered across restarts.
Each agent gets only the data of its own job (about 1k characters on average), which keeps every call far
below the free tokens-per-minute limit.

**Why is key 2 "resting"?** Open the panel on the dashboard – the reason is written next to each slot.
The most common one: both keys were made in the **same Google account/project**. Keys of one project share
**one** quota, so when key 1 uses up a model's daily limit, key 2 is out too (the panel then says so). Create
the second key in a **different Google account** at https://aistudio.google.com/apikey.

**How many keys?** One review now uses ~26–32 calls. 2 keys (from 2 different Google accounts) cover gold
*or* bitcoin all day. For gold **and** bitcoin 24/7 plus practice reviews, add a 3rd key: `GEMINI_API_KEYS=key2,key3` in `.env`.
The dashboard's **API keys & Gemini models** panel shows every slot, its health, latency and which agents use it.

## Run it on Windows (easiest)

1. Install Python from https://www.python.org/downloads/ (tick **"Add python.exe to PATH"**).
2. Download this repo (green **Code** button → **Download ZIP**) and unzip it.
3. Double-click **`start.bat`**. The first time, it installs everything and asks you to paste
   your keys (Twelve Data + Mistral; Gemini is optional); it saves them in a local `.env` file. After that it
   just starts the app and opens the website.

To change a key later, delete `.env` and run `start.bat` again (or edit `.env` in Notepad).


## Run it manually

```bash
python -m venv venv
# Windows: venv\Scripts\activate    |  Linux/Mac: source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then paste your keys into .env
python bot.py
```

Keep the window open – the app scans every 5 minutes while it runs. Only run one copy at a time.
Gold is closed on weekends (Fri ~21:00 → Sun ~22:00 UTC); bitcoin keeps being scanned.

## Free-plan limits (handled by the bot)

- **Twelve Data**: 800 requests/day, 8/min. Higher timeframes are cached, so a 5-minute scan uses ~450/day.
- **Mistral** (free plan): about 1 request/second per key – requests are spaced automatically.
- **Gemini**: ~5 requests/minute *per model*. Each agent uses a different model, busy or overloaded models
  are skipped automatically, and the AI desk runs only when the engine has found a real setup.

## Settings

All optional settings (markets, styles, scan interval, `MIN_CONVICTION` – 62 for fewer, 55 for more signals –,
confidence / vote thresholds, R:R, explanation language, models) are documented in `.env.example`.

## Tests

```bash
python -m unittest discover -s tests -t .
```

---
These signals are not financial advice. No bot or AI can guarantee profits or catch every move.
Always use a stop loss and risk only a small part of your account per trade.
