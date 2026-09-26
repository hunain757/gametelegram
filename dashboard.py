"""The local website (http://localhost:8080): signals, the 26 AI agents at work, live charts, lab and settings.

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
                        q = parse_qs(u.query)
                        data = dash.gb.chart_data(q.get("tf", ["15min"])[0], q.get("sym", [None])[0])
                        self._send(200, json.dumps(data, default=str).encode(), "application/json")
                    elif u.path == "/api/live":
                        live = dash.gb.live_price(parse_qs(u.query).get("sym", [None])[0])
                        self._send(200, json.dumps(live, default=str).encode(), "application/json")
                    elif u.path == "/static/lightweight-charts.js":
                        self._send(200, STATIC_JS.read_bytes(), "application/javascript")
                    else:
                        self._send(404, b"not found", "text/plain")
                except Exception as e:
                    log.exception("Dashboard error")
                    self._send(500, str(e).encode(), "text/plain")

            def do_POST(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                try:
                    size = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(size) or b"{}") if size else {}
                except (ValueError, json.JSONDecodeError):
                    self._send(400, b'{"ok": false, "error": "bad json"}', "application/json")
                    return
                gb = dash.gb
                if u.path == "/api/settings":  # quick, applied on the bot's loop and answered directly
                    fut = asyncio.run_coroutine_threadsafe(_call(gb.apply_setting, body), dash.loop)
                    try:
                        result = fut.result(timeout=10)
                    except Exception as e:
                        result = {"ok": False, "error": str(e)[:200]}
                    self._send(200, json.dumps(result).encode(), "application/json")
                    return
                actions = {
                    "/api/scan": lambda: gb.dashboard_scan(),
                    "/api/ping": lambda: gb.dashboard_ping(),
                    "/api/practice": lambda: gb.dashboard_practice(q.get("sym")),
                    "/api/backtest": lambda: gb.run_lab("backtest", q.get("style", "intraday"), q.get("sym")),
                    "/api/optimize": lambda: gb.run_lab("optimize", q.get("style", "intraday"), q.get("sym")),
                    "/api/marketview": lambda: gb.ai_market_view(q.get("sym")),
                }
                if u.path not in actions:
                    self._send(404, b"not found", "text/plain")
                    return
                asyncio.run_coroutine_threadsafe(actions[u.path](), dash.loop)
                self._send(202, b'{"started": true}', "application/json")

        self.server = ThreadingHTTPServer((self.host, self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True, name="dashboard").start()
        log.info("Dashboard running at %s", self.url)

async def _call(fn, *args):
    """Run a plain function on the bot's event loop (keeps storage writes on one thread)."""
    return fn(*args)


PAGE = (Path(__file__).with_name("static") / "dashboard.html").read_text(encoding="utf-8")
