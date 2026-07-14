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
    "stop_source",
    "stop_fvg_size",
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
        """Files created before a trailing column existed (per-strategy
        tagging, 2026-07-07; stop_source/stop_fvg_size, 2026-07-14, added
        so a live trade's losses can actually be analyzed for a pattern
        the same way backtest's --verbose already could) have a header
        missing one or more trailing columns -- rewrite just that header
        line in place. Existing data rows are left untouched; DictReader
        fills their missing trailing values with None, which
        src/dashboard.py's read_live_trades treats as "unknown"."""
        with open(self.path, newline="") as f:
            rows = list(csv.reader(f))
        if not rows or rows[0] == _HEADER:
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
                    trade.stop_source,
                    trade.stop_fvg_size,
                ]
            )
