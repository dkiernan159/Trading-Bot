# Strategy: NQ/MNQ NY-Open Opening Range Breakout + Retest + 1m FVG

This document is the source of truth for what the bot implements. Anything
marked **ASSUMPTION** was not fully specified and was filled in with a
reasonable default so the bot could be built before API access is ready --
review these and adjust `config.yaml` before running live.

## Rules as given

1. Mark previous day's high/low, plus Asia session and London session high/low.
2. At 9:30 ET the NY session opens. Watch the first 15-minute candle; it
   closes at 9:45 ET. Mark its high/low as "the box".
3. Wait for price to break the box high (bullish) or box low (bearish).
4. Wait for price to come back and test the broken level from the other side
   (retest).
5. Enter on a strong 1-minute fair value gap (FVG) that forms during/after the
   retest, in the direction of the breakout.
6. Reward:risk is 2:1.
7. Stop-loss: **either** the 2:1 ratio itself, **or** placed at a large
   support/resistance level whose break would imply a large move -- but never
   larger than the distance implied by the 2:1 ratio against the reference
   target.
8. Reference size is 5 MNQ contracts, targeting ~$300/trade. The bot starts
   at a smaller size (`config.yaml: position_sizing.contract_size`) until a
   consistent win rate is shown; scaling back up to 5 is **manual only** --
   the bot never changes its own size.
9. If stopped out, the bot re-arms and can take another trade if the setup
   reforms later in the session (new breakout/retest/FVG sequence).

## How ambiguous points were resolved (ASSUMPTIONS)

- **Stop-loss placement** (`src/risk.py`): the "large formed area of
  resistance/support" is interpreted as the nearest already-marked level
  beyond entry in the stop direction -- previous day high/low, Asia
  high/low, London high/low, or the opening range box edge. The bot finds
  the nearest such level beyond entry and uses it as the stop, **but caps**
  the distance at `max_stop_points` (derived below). If the nearest
  structural level is farther than the cap, the cap is used instead so the
  stop never exceeds what the 2:1 ratio implies.
  - `max_stop_points = target_dollars_at_reference_size / reference_contracts
    / point_value / reward_risk_ratio = 300 / 5 / 2.0 / 2.0 = 15 points.`
  - Take-profit is always `2 x actual_stop_distance` (so smaller structural
    stops give a smaller, still-2:1, target).
- **"Strong" FVG** (`src/fvg.py`): a 3-candle fair value gap on the 1-minute
  chart where (a) the gap size is >= `min_gap_points` and (b) the middle
  (displacement) candle's body is >= `displacement_multiplier` times the
  recent average candle range. Both thresholds are configurable.
- **Asia / London session windows** (`config.yaml: session`): set to common
  ICT-style approximations (Asia 19:00-23:59 ET prior evening, London
  02:00-05:00 ET). Adjust to your exact definition.
- **End of session for new entries**: no new setups after 11:30 ET, hard
  flatten by 11:45 ET if still in a trade. You described this as a morning
  strategy but didn't give an exact cutoff.
- **After a winning trade**: bot stands down for the rest of the day by
  default (`allow_new_setup_after_win: false`). You only specified re-entry
  behavior after a *loss*; flip this flag if you also want multiple winners
  per day.
- **Daily safety limits** (`max_trades_per_day`, `max_daily_loss_dollars`,
  `kill_switch`): not requested, added because this trades a TopStep
  funded/evaluation account where breaching a drawdown rule can end the
  account. Disable/adjust freely in `config.yaml`.

## Architecture

```
broker (data + orders)  --->  strategy state machine  --->  risk (stop/target/size)  --->  broker (place bracket order)
                                       |
                                 trade logger (CSV) -- used to judge win rate before you manually scale up
```

- `src/broker/base.py` -- abstract interface any broker must implement.
- `src/broker/mock_broker.py` -- deterministic/simulated bars, for testing
  the whole pipeline with no live account.
- `src/broker/projectx_gateway.py` -- **stub** for the real TopstepX
  ProjectX Gateway API. Auth flow (`POST /api/Auth/loginKey` with
  `userName` + `apiKey` -> session token, then a JWT-authenticated
  WebSocket for market data) is sketched from public docs at
  https://gateway.docs.projectx.com/, but the market-data and
  order-placement endpoint details are left as TODOs -- fill these in
  tomorrow once you're logged into the docs portal with API access, since
  the exact request/response schemas need to be confirmed there.
- `src/strategy.py` -- the state machine implementing steps 1-9 above.
- `src/risk.py` -- stop/target/size calculation described above.
- `src/session_levels.py`, `src/opening_range.py`, `src/fvg.py` -- level
  marking, box tracking, and FVG detection respectively.
- `src/runner.py` -- wires it all together into a run loop.

## Before going live

1. Fill in `.env` (copy from `.env.example`) with your ProjectX Gateway
   credentials once you have them.
2. Implement the TODOs in `src/broker/projectx_gateway.py` against the real
   docs (market data subscription + order placement + position/fill
   tracking).
3. Paper trade / dry-run first. `position_sizing.contract_size` should stay
   at `1` until you've reviewed enough trades in the CSV log
   (`trades/trades.csv`) to be comfortable.
4. Re-read the assumptions above against your actual rules and correct
   `config.yaml` (and `src/risk.py` / `src/fvg.py` if the assumption is
   structural, not just a number).
