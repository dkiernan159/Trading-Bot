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

    def log_trade(self, trade: Trade, point_value: float) -> None:
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
                ]
            )
