from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time as dtime
from pathlib import Path

import yaml


def _parse_time(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(hour=int(h), minute=int(m))


@dataclass
class InstrumentConfig:
    symbol: str
    tick_size: float
    point_value: float


@dataclass
class SessionConfig:
    timezone: str
    asia_start: dtime
    asia_end: dtime
    london_start: dtime
    london_end: dtime
    ny_open: dtime
    opening_range_end: dtime
    no_new_entries_after: dtime
    flatten_by: dtime


@dataclass
class FvgConfig:
    min_gap_points: float
    displacement_multiplier: float
    lookback_bars: int
    timeframe_minutes: int


@dataclass
class ReentryConfig:
    allow_reentry_after_stop: bool
    allow_new_setup_after_win: bool


@dataclass
class OvernightConfig:
    enabled: bool
    max_trades_per_night: int


@dataclass
class StrategyConfig:
    reward_risk_ratio: float
    target_dollars_at_reference_size: float
    reference_contracts: int
    max_stop_dollars: float
    min_stop_dollars: float
    entry_retracement_pct: float
    fvg: FvgConfig
    entry_fvg: FvgConfig
    reentry: ReentryConfig
    overnight: OvernightConfig


@dataclass
class PositionSizingConfig:
    contract_size: int
    scaling: str


@dataclass
class RiskLimitsConfig:
    max_trades_per_day: int
    max_daily_loss_dollars: float
    kill_switch: bool


@dataclass
class BrokerConfig:
    provider: str
    base_url: str
    realtime_base_url: str
    dry_run: bool


@dataclass
class DashboardConfig:
    host: str
    port: int
    refresh_interval_seconds: int
    backtest_days: int


@dataclass
class BotConfig:
    instrument: InstrumentConfig
    session: SessionConfig
    strategy: StrategyConfig
    position_sizing: PositionSizingConfig
    risk_limits: RiskLimitsConfig
    broker: BrokerConfig
    dashboard: DashboardConfig


def load_config(path: str | Path = "config.yaml") -> BotConfig:
    with open(path, "r") as f:
        raw = yaml.safe_load(f)

    instrument = InstrumentConfig(**raw["instrument"])

    s = raw["session"]
    session = SessionConfig(
        timezone=s["timezone"],
        asia_start=_parse_time(s["asia_session"][0]),
        asia_end=_parse_time(s["asia_session"][1]),
        london_start=_parse_time(s["london_session"][0]),
        london_end=_parse_time(s["london_session"][1]),
        ny_open=_parse_time(s["ny_open"]),
        opening_range_end=_parse_time(s["opening_range_end"]),
        no_new_entries_after=_parse_time(s["no_new_entries_after"]),
        flatten_by=_parse_time(s["flatten_by"]),
    )

    strat = raw["strategy"]
    strategy = StrategyConfig(
        reward_risk_ratio=strat["reward_risk_ratio"],
        target_dollars_at_reference_size=strat["target_dollars_at_reference_size"],
        reference_contracts=strat["reference_contracts"],
        max_stop_dollars=strat["max_stop_dollars"],
        min_stop_dollars=strat["min_stop_dollars"],
        entry_retracement_pct=strat["entry_retracement_pct"],
        fvg=FvgConfig(**strat["fvg"]),
        entry_fvg=FvgConfig(**strat["entry_fvg"]),
        reentry=ReentryConfig(**strat["reentry"]),
        overnight=OvernightConfig(
            enabled=strat["overnight"]["enabled"],
            max_trades_per_night=strat["overnight"]["max_trades_per_night"],
        ),
    )

    position_sizing = PositionSizingConfig(**raw["position_sizing"])
    risk_limits = RiskLimitsConfig(**raw["risk_limits"])
    broker = BrokerConfig(**raw["broker"])
    dashboard = DashboardConfig(**raw["dashboard"])

    return BotConfig(
        instrument=instrument,
        session=session,
        strategy=strategy,
        position_sizing=position_sizing,
        risk_limits=risk_limits,
        broker=broker,
        dashboard=dashboard,
    )
