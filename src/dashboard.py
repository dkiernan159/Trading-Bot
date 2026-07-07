from __future__ import annotations

"""Always-on dashboard: live trades (from trades/trades.csv, free to read,
served fresh on every request) plus a periodically-refreshed backtest
(costs real API calls, so throttled by config.yaml: dashboard.refresh_interval_seconds).

    python -m src.dashboard

Binds to 127.0.0.1 by default -- view it via an SSH local port-forward
(see DEPLOY.md), never expose this port publicly without adding auth.
"""

import csv
import json
import threading
import time as time_module
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from src.backtest import _build_chart_payload, fetch_recent_bars, run_backtest
from src.broker.projectx_gateway import ProjectXGatewayBroker
from src.config import BotConfig, load_config

TEMPLATE_PATH = Path(__file__).with_name("dashboard_template.html")
TRADES_CSV_PATH = "trades/trades.csv"
STATUS_JSON_PATH = "trades/status.json"


def read_live_trades(csv_path: str = TRADES_CSV_PATH) -> list[dict]:
    """Parses trades/trades.csv fresh -- cheap local file read, safe to call
    on every request. Only closed trades ever get logged, so every row here
    already has an exit_price/exit_time/exit_reason."""
    path = Path(csv_path)
    if not path.exists():
        return []

    rows = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            rows.append(
                {
                    "entry_time": row["entry_time"],
                    "direction": row["direction"],
                    "contracts": int(row["contracts"]),
                    "entry_price": float(row["entry_price"]),
                    "stop_price": float(row["stop_price"]),
                    "target_price": float(row["target_price"]),
                    "exit_price": float(row["exit_price"]),
                    "exit_time": row["exit_time"],
                    "exit_reason": row["exit_reason"],
                    "pnl_points": float(row["pnl_points"]),
                    "pnl_dollars": float(row["pnl_dollars"]),
                }
            )
    rows.sort(key=lambda r: r["entry_time"], reverse=True)
    return rows


def read_status(path: str = STATUS_JSON_PATH) -> dict | None:
    """Reads the live bot's current per-strategy status (src/runner.py
    writes this out after every bar) -- None if the bot hasn't written one
    yet (e.g. before its first bar), which the dashboard treats as "no
    activity to show" rather than an error."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None  # tolerate reading mid-write; the next poll gets a clean copy


class BacktestCache:
    """Holds the most recent backtest chart payload, refreshed on a
    background timer instead of on every request (it costs real API calls)."""

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._trades: list[dict] = []
        self._generated_at: str | None = None
        self._error: str | None = None

    def refresh(self) -> None:
        try:
            tz = ZoneInfo(self.cfg.session.timezone)
            broker = ProjectXGatewayBroker(
                base_url=self.cfg.broker.base_url,
                realtime_base_url=self.cfg.broker.realtime_base_url,
                dry_run=True,  # the dashboard only ever reads history, never trades
            )
            broker.connect()
            bars = fetch_recent_bars(broker, self.cfg.instrument.symbol, tz, self.cfg.dashboard.backtest_days)
            results = run_backtest(self.cfg, bars)
            payload = _build_chart_payload(self.cfg, results, bars)
        except Exception as exc:
            with self._lock:
                self._error = str(exc)
            return

        with self._lock:
            self._trades = payload
            self._generated_at = datetime.now(timezone.utc).isoformat()
            self._error = None

    def snapshot(self) -> dict:
        with self._lock:
            return {"trades": self._trades, "generated_at": self._generated_at, "error": self._error}


def _refresh_loop(cache: BacktestCache, interval_seconds: int) -> None:
    while True:
        cache.refresh()
        time_module.sleep(interval_seconds)


def make_handler(backtest_cache: BacktestCache) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path in ("/", "/index.html"):
                self._serve_html()
            elif self.path == "/api/live-trades.json":
                self._serve_json(read_live_trades())
            elif self.path == "/api/status.json":
                self._serve_json(read_status())
            elif self.path == "/api/backtest.json":
                self._serve_json(backtest_cache.snapshot())
            else:
                self.send_error(404)

        def _serve_html(self) -> None:
            body = TEMPLATE_PATH.read_text().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_json(self, data) -> None:
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:
            pass  # quiet -- systemd journal doesn't need per-request noise

    return Handler


def main() -> None:
    cfg = load_config("config.yaml")

    backtest_cache = BacktestCache(cfg)
    threading.Thread(
        target=_refresh_loop,
        args=(backtest_cache, cfg.dashboard.refresh_interval_seconds),
        daemon=True,
    ).start()

    server = ThreadingHTTPServer((cfg.dashboard.host, cfg.dashboard.port), make_handler(backtest_cache))
    print(
        f"Dashboard serving on http://{cfg.dashboard.host}:{cfg.dashboard.port} "
        f"(backtest refreshes every {cfg.dashboard.refresh_interval_seconds}s)"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
