from __future__ import annotations

import csv
from pathlib import Path

from src.models import Trade

_HEADER = [
    "entry_time",
    "direction",
    "contracts",
    "entry_price",
    "stop_price",
    "target_price",
    "exit_price",
    "exit_time",
    "exit_reason",
    "pnl_points",
    "pnl_dollars",
    "strategy",
]


class TradeLogger:
    """Appends closed trades to a CSV so win rate can be reviewed before
    manually scaling contract size up (position_sizing.scaling: manual_only).
    """

    def __init__(self, path: str = "trades/trades.csv"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with open(self.path, "w", newline="") as f:
                csv.writer(f).writerow(_HEADER)
        else:
            self._migrate_header_if_needed()

    def _migrate_header_if_needed(self) -> None:
        """Files created before per-strategy tagging (2026-07-07, added so
        the dashboard can break out day vs overnight performance) have a
        header without "strategy" -- rewrite just that header line in
        place. Existing data rows are left untouched; DictReader fills
        their missing trailing "strategy" value with None, which
        src/dashboard.py's read_live_trades treats as "unknown"."""
        with open(self.path, newline="") as f:
            rows = list(csv.reader(f))
        if not rows or "strategy" in rows[0]:
            return
        rows[0] = list(_HEADER)
        with open(self.path, "w", newline="") as f:
            csv.writer(f).writerows(rows)

    def log_trade(self, trade: Trade, point_value: float, strategy: str) -> None:
        with open(self.path, "a", newline="") as f:
            csv.writer(f).writerow(
                [
                    trade.entry_time.isoformat(),
                    trade.direction.value,
                    trade.contracts,
                    trade.entry_price,
                    trade.stop_price,
                    trade.target_price,
                    trade.exit_price,
                    trade.exit_time.isoformat() if trade.exit_time else "",
                    trade.exit_reason,
                    trade.pnl_points(),
                    trade.pnl_dollars(point_value),
                    strategy,
                ]
            )
