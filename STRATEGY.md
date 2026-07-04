# Strategy: NQ/MNQ NY-Open Opening Range Breakout + Retest + 1m FVG

This document is the source of truth for what the bot implements. Anything
marked **ASSUMPTION** was not fully specified and was filled in with a
reasonable default so the bot could be built before API access is ready --
review these and adjust `config.yaml` before running live.

## Rules as given

1. Mark previous day's high/low, plus Asia session and London session high/low.
2. At 9:30 ET the NY session opens. Watch the first 15-minute candle; it
   closes at 9:45 ET. Mark its high/low as "the box".
3. Wait for price to break the box high (bullish) or box low (bearish) --
   this sets the trade direction/bias, it is not itself the entry.
4. After the breakout, price must **retest** one of the marked key levels
   (previous day high/low, Asia high/low, or London high/low -- any of
   them) -- i.e. trade through that exact price at some point.
5. Only *after* that retest has happened does the bot start watching for
   the entry trigger: the next strong 1-minute FVG in the breakout
   direction. This FVG does **not** need to form at or near the key level
   that was retested -- the retest and the FVG are two separate,
   sequential steps, not one combined condition.
6. Entry is a **limit order at the midpoint of that FVG's gap**
   (`(gap_low + gap_high) / 2`), not a market order at whatever price the
   confirming candle closed at. The trade only starts once price actually
   trades back to that midpoint -- if it never comes back, there's no
   entry that setup. If price instead blows straight through the gap's
   **far** edge (below `gap_low` for a bullish/LONG gap, above `gap_high`
   for a bearish/SHORT gap) before ever retracing to the midpoint, the FVG
   is **mitigated** -- it's been fully traded through, not just tapped --
   and is abandoned rather than filled. The bot drops back to watching for
   a fresh, unmitigated FVG in the same breakout direction instead of
   entering off a level that no longer means anything. This only looks at
   the single most-recently-detected FVG (the one currently resting as the
   pending limit order); it does not track a history of older FVGs from
   earlier in the session as still-tradeable candidates.

   (Revision history: v1 entered at the confirming candle's close, which
   put entries well outside the FVG zone entirely -- caught by inspecting
   the backtest charts. v2 required the FVG's gap to overlap the key
   level's zone, as a single combined condition -- too strict in practice
   (most setups were being filtered at that step, per the funnel
   diagnostics), and not actually what was meant. Corrected 2026-07-04 to
   the current two-step retest-then-FVG design. Also corrected 2026-07-04
   to add mitigation: a chart inspection showed the bot entering short off
   a FVG that price had already broken clean through on the way down --
   the old fill check only asked "did price reach the midpoint," which is
   also trivially true when price breaks clean through the entire gap.)
7. Reward:risk is 2:1.
8. Stop-loss: **either** the 2:1 ratio itself, **or** placed at a large
   support/resistance level whose break would imply a large move -- but never
   larger than the distance implied by the 2:1 ratio against the reference
   target.
9. Reference size is 5 MNQ contracts, targeting ~$300/trade. The bot starts
   at a smaller size (`config.yaml: position_sizing.contract_size`) until a
   consistent win rate is shown; scaling back up to 5 is **manual only** --
   the bot never changes its own size.
10. If stopped out, the bot re-arms and can take another trade if the setup
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
- **"Retest"** (`src/strategy.py: _touches_any_key_level`): a bar's range
  (low-to-high) trading through the *exact* price of any of the 6 marked
  levels (previous day high/low, Asia high/low, London high/low) counts as
  a retest -- same check the box-retest logic always used, just against
  these levels instead of the box. This is an OR across all 6: any single
  one being touched is enough, they are not required together. Once any
  one has been touched, the bot moves on to watching for the FVG -- the
  FVG does not need to relate to whichever level was actually retested.
  The opening range box itself is not part of this check (it's used for
  the breakout and as a stop-loss candidate).
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
- `src/broker/projectx_gateway.py` -- the real TopstepX / ProjectX Gateway
  API broker. Implemented (auth, contract lookup, historical/real-time data,
  order placement, bracket stop/target, flatten), but **defaults to
  `dry_run: true`** in `config.yaml` -- it logs what it would do instead of
  sending real orders until you verify the unverified pieces below against
  a paper/sim account.

  Confirmed against the public docs (https://gateway.docs.projectx.com/)
  and the open-source project-x-py SDK
  (https://github.com/TexasCoding/project-x-py, which wraps this same
  API):
  - Auth: `POST {base}/api/Auth/loginKey` `{userName, apiKey}` -> `{token}`,
    used as `Authorization: Bearer {token}` on later REST calls.
  - Contract lookup: `POST /api/Contract/search` `{searchText, live}`.
  - Historical bars: `POST /api/History/retrieveBars`.
  - Orders: `POST /api/Order/place` / `/Order/cancel` / `/Order/modify` /
    `/Order/searchOpen`, with order type enum `1=Limit, 2=Market,
    3=StopLimit, 4=Stop, 5=TrailingStop` and side enum `0=Buy, 1=Sell`.
  - Positions: `POST /api/Position/searchOpen` / `/Position/closeContract`.
  - Real-time data: SignalR hubs at `rtc.topstepx.com/hubs/market` (and
    `/hubs/user` for account/order/position events, not yet used here),
    JWT passed as an `access_token` URL query param, subscribed via
    `hub.invoke("SubscribeContractTrades", [contractId])`, events arrive as
    `GatewayTrade` (ticks, aggregated here into 1-minute bars).
  - **Confirmed live against a real account (2026-07-03):** `/Contract/search`
    with `{"searchText": "MNQ", "live": false}` correctly returns the front-month
    contract (e.g. `CON.F.US.MNQ.U26`). `"live": true` returns an empty list
    whenever markets are closed -- it filters to contracts in an active
    trading session, not "all listed contracts" -- so contract resolution
    always uses `live: false`.

  Still **unverified** -- the docs portal 403's an unauthenticated fetch,
  so these need a live check once you're logged in:
  - Exact field names inside a `GatewayTrade` payload (guessed defensively).
  - The exact response envelope key for `/Order/searchOpen` (assumed
    `"orders"`).
  - Whether `linkedOrderId` makes the gateway auto-cancel the sibling
    bracket leg. Not relied upon either way -- `poll_order_status()`
    explicitly cancels the sibling leg itself once one fills.
- `src/strategy.py` -- the state machine implementing steps 1-10 above.
- Note: `src/session_levels.py` also computes a 15-minute-candle "zone"
  around each level (`previous_day_high_zone`, etc.) -- this is a leftover
  from the v2 overlap design above and is no longer used by any entry
  logic. It's kept only because the charts (backtest.py --chart-html and
  the dashboard) still shade it as a band for visual context, showing
  which candle actually set the previous day's high/low.
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
