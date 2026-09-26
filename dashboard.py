"""Local live dashboard (http://localhost:8080): watch the scan, the 9 AI agents, the chart and trades.

Runs inside the bot process on a background thread and listens on 127.0.0.1 only, so it is
visible just on the computer that runs the bot.
"""

import asyncio
import json
import logging
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("goldbot.dashboard")

STATIC_JS = Path(__file__).with_name("static") / "lightweight-charts.js"


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
                    elif u.path == "/api/candles":
                        data = dash.gb.chart_data(parse_qs(u.query).get("tf", ["15min"])[0])
                        self._send(200, json.dumps(data, default=str).encode(), "application/json")
                    elif u.path == "/api/live":
                        self._send(200, json.dumps(dash.gb.live_price(), default=str).encode(), "application/json")
                    elif u.path == "/static/lightweight-charts.js":
                        self._send(200, STATIC_JS.read_bytes(), "application/javascript")
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
                actions = {"/api/scan": dash.gb.dashboard_scan, "/api/ping": dash.gb.dashboard_ping,
                           "/api/practice": dash.gb.dashboard_practice}
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


PAGE = (Path(__file__).with_name("static") / "dashboard.html").read_text(encoding="utf-8")
