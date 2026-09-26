"""Local live dashboard (http://localhost:8080): watch the scan, the 9 AI agents, the chart and trades.

Runs inside the bot process on a background thread and listens on 127.0.0.1 only, so it is
visible just on the computer that runs the bot.
"""

import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("goldbot.dashboard")


class Dashboard:
    def __init__(self, gold_bot, loop: asyncio.AbstractEventLoop, host: str = "127.0.0.1", port: int = 8080):
        self.gb = gold_bot
        self.loop = loop
        self.host = host
        self.port = port
        self.server: ThreadingHTTPServer | None = None
        self._charts: dict[str, tuple[str, bytes]] = {}

    @property
    def url(self) -> str:
        return f"http://{'localhost' if self.host in ('127.0.0.1', '0.0.0.0') else self.host}:{self.port}"

    def start(self):
        dash = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep the console clean
                pass

            def _send(self, code: int, body: bytes, ctype: str):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urlparse(self.path)
                try:
                    if u.path == "/":
                        self._send(200, PAGE.encode(), "text/html; charset=utf-8")
                    elif u.path == "/api/state":
                        self._send(200, json.dumps(dash.gb.dashboard_state(), default=str).encode(), "application/json")
                    elif u.path == "/api/chart.png":
                        png = dash.chart(parse_qs(u.query).get("tf", ["15min"])[0])
                        if png:
                            self._send(200, png, "image/png")
                        else:
                            self._send(404, b"no data yet", "text/plain")
                    else:
                        self._send(404, b"not found", "text/plain")
                except Exception as e:
                    log.exception("Dashboard error")
                    self._send(500, str(e).encode(), "text/plain")

            def do_POST(self):
                u = urlparse(self.path)
                actions = {"/api/scan": dash.gb.dashboard_scan, "/api/ping": dash.gb.dashboard_ping}
                if u.path not in actions:
                    self._send(404, b"not found", "text/plain")
                    return
                asyncio.run_coroutine_threadsafe(actions[u.path](), dash.loop)
                self._send(202, b'{"started": true}', "application/json")

        self.server = ThreadingHTTPServer((self.host, self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True, name="dashboard").start()
        log.info("Dashboard running at %s", self.url)

    def chart(self, tf: str) -> bytes | None:
        import chart as chart_mod
        from setups import TF_LABEL

        gb = self.gb
        if not gb.candles or tf not in gb.candles or not gb.market or tf not in gb.market:
            return None
        version = str(gb.last_scan)
        cached = self._charts.get(tf)
        if cached and cached[0] == version:
            return cached[1]
        png = chart_mod.market_chart(gb.candles[tf], gb.market[tf]["smc"], gb.market.get("levels", {}), TF_LABEL[tf])
        self._charts[tf] = (version, png)
        return png


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gold AI Control Room</title>
<link rel="icon" href="data:,">
<style>
:root{--bg:#0b0e14;--panel:#121722;--panel2:#171d2b;--line:#232b3b;--text:#d7dce5;--mut:#8a93a6;
--gold:#f5c542;--green:#26c281;--red:#ef5350;--amber:#ffb020;--blue:#4aa3ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;padding:14px 20px;
border-bottom:1px solid var(--line);background:linear-gradient(90deg,#141a26,#0b0e14);position:sticky;top:0;z-index:5}
h1{margin:0;font-size:20px;color:var(--gold);letter-spacing:.5px}h1 small{color:var(--mut);font-weight:400;font-size:12px;margin-left:8px}
.pills{display:flex;flex-wrap:wrap;gap:8px;min-width:0}.pill{padding:4px 10px;border-radius:99px;background:var(--panel2);border:1px solid var(--line);font-size:12px;white-space:nowrap}
.ok{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--amber)}
button{background:var(--gold);color:#111;border:0;border-radius:8px;padding:8px 14px;font-weight:700;cursor:pointer}
button.alt{background:var(--panel2);color:var(--text);border:1px solid var(--line)}button:disabled{opacity:.5;cursor:wait}
main{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:16px;padding:16px 20px}
@media(max-width:1100px){main{grid-template-columns:minmax(0,1fr)}}
@media(max-width:600px){main{padding:12px}header{padding:12px}.pill{white-space:normal}}
section{overflow-x:auto;min-width:0;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:16px}
section h2{margin:0 0 10px;font-size:14px;text-transform:uppercase;letter-spacing:1px;color:var(--mut)}
.agents{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px}
.agent{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:11px;min-height:150px;transition:.3s}
.agent.head{grid-column:1/-1;border-color:#5a4a16;background:linear-gradient(90deg,#1d1a10,#171d2b)}
.agent .top{display:flex;align-items:center;gap:8px}.agent .ic{font-size:22px}.agent .nm{font-weight:700;flex:1}
.badge{font-size:11px;padding:2px 8px;border-radius:99px;background:#2a3142;color:var(--mut);text-transform:uppercase;font-weight:700}
.st-thinking{border-color:var(--amber);box-shadow:0 0 0 1px var(--amber) inset}.st-thinking .badge{background:var(--amber);color:#111;animation:pulse 1s infinite}
.st-waiting .badge{background:var(--blue);color:#fff}.st-error{border-color:var(--red)}.st-error .badge{background:var(--red);color:#fff}
.v-TAKE .badge{background:var(--green);color:#062}.v-SKIP .badge{background:#5a2330;color:#ffb3bd}
@keyframes pulse{50%{opacity:.45}}
.role{color:var(--mut);font-size:12px;margin:4px 0 6px}.sum{font-size:13px}.pts{margin:4px 0 0;padding-left:16px;color:var(--mut);font-size:12px}
.meta{color:var(--mut);font-size:11px;margin-top:6px}.bar{height:5px;background:#262d3d;border-radius:4px;margin-top:6px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--gold)}
.log{height:560px;overflow:auto;font:12px/1.5 ui-monospace,Consolas,monospace}.log div{padding:3px 0;border-bottom:1px dashed #1c2230}
.log .t{color:var(--mut);margin-right:6px}.k-take{color:var(--green)}.k-skip{color:#ff9aa8}.k-error{color:var(--red)}.k-signal{color:var(--gold);font-weight:700}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left}th{color:var(--mut);font-weight:600}
.tabs{display:flex;gap:6px;margin-bottom:8px}.tabs button{padding:5px 10px}
.chart{width:100%;border-radius:8px;border:1px solid var(--line);background:#0b0e14;min-height:200px}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:13px}.kv span:nth-child(odd){color:var(--mut)}
.empty{color:var(--mut);font-style:italic}
</style>
</head>
<body>
<header>
  <h1>🏆 Gold AI Control Room <small>XAU/USD · local only</small></h1>
  <div class="pills" id="pills"></div>
  <div style="display:flex;gap:8px">
    <button id="scanBtn" onclick="act('scan')">⚡ Scan now</button>
    <button class="alt" id="pingBtn" onclick="act('ping')">🩺 Test all 9 agents</button>
  </div>
</header>
<main>
  <div>
    <section><h2>🤖 AI Desk – 9 agents</h2><div class="agents" id="agents"></div></section>
    <section><h2>📈 Chart</h2>
      <div class="tabs" id="tabs"></div>
      <img class="chart" id="chart" alt="Chart appears after the first scan">
    </section>
    <section><h2>🌍 Market structure</h2><div id="market"></div></section>
    <section><h2>🧠 Last AI decisions</h2><div id="reviews"></div></section>
    <section><h2>📡 Open trades</h2><div id="trades"></div></section>
  </div>
  <div>
    <section><h2>⚡ Live activity</h2><div class="log" id="log"></div></section>
    <section><h2>🩺 System</h2><div class="kv" id="system"></div></section>
    <section><h2>📊 Performance</h2><div class="kv" id="perf"></div></section>
  </div>
</main>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let tf = "15min", chartVersion = "";
const TFS = [["5min","M5"],["15min","M15"],["1h","H1"],["4h","H4"],["1day","D1"]];
$("tabs").innerHTML = TFS.map(([k,l]) => `<button class="alt" data-tf="${k}">${l}</button>`).join("");
$("tabs").onclick = e => { if (e.target.dataset.tf) { tf = e.target.dataset.tf; chartVersion = ""; refresh(); } };

async function act(what){
  const b = $(what === "scan" ? "scanBtn" : "pingBtn"); b.disabled = true;
  try { await fetch("/api/" + what, {method: "POST"}); } finally { setTimeout(() => b.disabled = false, 4000); }
}

function agentCard(a){
  const cls = ["agent", a.key === "head" ? "head" : "", "st-" + a.status, a.status === "done" && a.vote ? "v-" + a.vote : ""].join(" ");
  const badge = a.status === "done" ? (a.vote || "done") : a.status;
  const score = a.score != null ? `<div class="bar"><i style="width:${Math.max(0, Math.min(100, a.score))}%"></i></div>` : "";
  const pts = (a.points || []).length ? `<ul class="pts">${a.points.map(p => `<li>${esc(p)}</li>`).join("")}</ul>` : "";
  const meta = [a.model, a.seconds != null ? a.seconds + "s" : "", a.at ? a.at + " UTC" : ""].filter(Boolean).join(" · ");
  return `<div class="${cls}"><div class="top"><span class="ic">${a.icon}</span><span class="nm">${esc(a.name)}</span>
    <span class="badge">${esc(badge)}${a.score != null && a.status === "done" ? " " + a.score : ""}</span></div>
    <div class="role">${esc(a.role)}</div><div class="sum">${esc(a.summary) || '<span class="empty">Idle – waits for the next setup</span>'}</div>
    ${pts}${score}<div class="meta">${esc(meta)}</div></div>`;
}

function pill(txt, cls){ return `<span class="pill ${cls||""}">${txt}</span>`; }

async function refresh(){
  let s;
  try { s = await (await fetch("/api/state")).json(); }
  catch (e) { $("pills").innerHTML = pill("❌ Bot not running – start it with start.bat", "bad"); return; }
  const st = s.status;
  const phase = {scanning:"🔎 Scanning market", ai_review:"🧠 AI desk thinking", idle:"💤 Waiting", starting:"⏳ Starting"}[s.phase] || s.phase;
  $("pills").innerHTML = [
    pill("🟢 Bot running", "ok"),
    pill(st.market_open ? "🟢 Market open" : "🔴 Market closed", st.market_open ? "ok" : "warn"),
    pill(phase, s.phase === "idle" ? "" : "warn"),
    pill("Last scan " + st.last_scan),
    s.next_scan_in != null ? pill("Next scan in " + s.next_scan_in + "s") : "",
    s.market ? pill("XAU/USD <b>" + s.market.price.toFixed(2) + "</b>") : "",
    st.fail_count ? pill("⚠️ " + st.fail_count + " failed scans", "bad") : "",
  ].join("");

  $("agents").innerHTML = s.agents.map(agentCard).join("");
  $("log").innerHTML = s.log.map(l => `<div class="k-${l.kind}"><span class="t">${l.t}</span>${esc(l.text)}</div>`).join("")
    || '<div class="empty">Waiting for the first scan…</div>';

  if (s.market) {
    $("market").innerHTML = `<table><tr><th>TF</th><th>Trend</th><th>Last event</th><th>Zone</th><th>RSI</th><th>ATR</th><th>Candles</th></tr>` +
      s.market.tfs.map(r => `<tr><td><b>${r.label}</b></td><td class="${r.trend==="bullish"?"ok":r.trend==="bearish"?"bad":""}">${r.trend||"ranging"}</td>
        <td>${esc(r.event||"–")}</td><td>${r.zone}</td><td>${r.rsi ?? "–"}</td><td>${r.atr}</td><td>${esc(r.patterns||"")}</td></tr>`).join("") +
      `</table><div class="meta" style="margin-top:8px">Key levels: ${Object.entries(s.market.levels).map(([k,v]) => k + " " + v).join(" · ") || "–"}` +
      (s.market.adr && s.market.adr.adr ? ` · ADR ${s.market.adr.adr} (${s.market.adr.used_pct}% used today)` : "") + `</div>`;
  } else $("market").innerHTML = '<div class="empty">No market data yet – press ⚡ Scan now.</div>';

  $("reviews").innerHTML = s.reviews.length ? `<table><tr><th>Time</th><th>Setup</th><th>Votes</th><th>Result</th><th>Why</th></tr>` +
    s.reviews.map(r => `<tr><td>${r.t}</td><td>${esc(r.style)} ${r.direction} @ ${r.entry}<br><span class="meta">engine ${r.score}</span></td>
      <td>${r.reports.map(x => x.icon + (x.vote==="TAKE"?"✅":x.vote==="SKIP"?"❌":"⚠️")).join(" ")}<br><span class="meta">${r.votes}/8</span></td>
      <td class="${r.approved?"ok":"bad"}">${r.approved?"✅ SENT":"❌ SKIPPED"}<br><span class="meta">conf ${r.confidence}%</span></td><td>${esc(r.reason)}</td></tr>`).join("") + "</table>"
    : '<div class="empty">No setup reviewed yet. The AI desk only runs when the engine finds a real setup.</div>';

  $("trades").innerHTML = s.trades.length ? `<table><tr><th>Trade</th><th>Entry</th><th>SL</th><th>TPs</th><th>Status</th></tr>` +
    s.trades.map(t => `<tr><td>${t.direction} · ${esc(t.style_label)}</td><td>${t.entry}</td><td>${t.stop_loss}</td>
      <td>${t.tps.join(" / ")}</td><td>${t.status} · TP ${t.stage}/3</td></tr>`).join("") + "</table>"
    : '<div class="empty">No open trades.</div>';

  const kv = o => Object.entries(o).map(([k,v]) => `<span>${k}</span><span>${v}</span>`).join("");
  $("system").innerHTML = kv({
    "Uptime": st.uptime, "Scans today": st.scans_today, "Twelve Data": st.td_requests + " / 800 requests",
    "Gemini": st.ai_calls + " calls (" + st.ai_failures + " retried)", "Volume feed": st.volume_ok ? "✅ OK" : "⚠️ unavailable",
    "News calendar": st.news_ok ? "✅ OK" : "⚠️ unavailable", "Telegram users": st.users,
    "Signal types": s.settings.styles.join(", "), "Rules": `confidence ≥ ${s.settings.min_confidence}%, ≥ ${s.settings.min_votes}/8 agents`,
    "Last error": st.last_error ? `<span class="bad">${esc(st.last_error.slice(0,160))}</span>` : "none",
  });
  const p = s.performance;
  $("perf").innerHTML = p.trades ? kv({"Finished trades": p.trades, "Win rate (TP1+)": p.win_rate + "%", "Total": p.total_r + "R",
    "Wins / losses": p.wins + " / " + p.losses, "Hit TP3": p.tp3}) : '<span class="empty">No finished trades yet</span>';

  document.querySelectorAll("#tabs button").forEach(b => b.className = b.dataset.tf === tf ? "" : "alt");
  if (s.chart_version && s.chart_version + tf !== chartVersion) {
    chartVersion = s.chart_version + tf;
    $("chart").src = "/api/chart.png?tf=" + tf + "&v=" + encodeURIComponent(s.chart_version);
  }
}
refresh(); setInterval(refresh, 2000);
</script>
</body>
</html>
"""
