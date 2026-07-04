# Strategy: NQ/MNQ NY-Open Opening Range Breakout + Key-Level Approach + 15m FVG

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
4. After the breakout, price must **approach** one of the marked key
   levels (previous day high/low, Asia high/low, or London high/low --
   any of them) -- i.e. come within `key_level_approach_points`
   (`config.yaml`, default 5 points) of that price. An exact touch still
   counts (it's 0 points away), it's just no longer required.
5. Only *after* that approach has happened does the bot start watching for
   the entry trigger: a strong 15-minute FVG in the breakout direction that
   has **not been mitigated** (price has not yet traded clean through its
   far side) **and** sits **in the way of the move** -- between current
   price and the level that was approached, not behind price or past the
   level already. This FVG does **not** need to form at or near the level
   itself (the approach and the FVG are two separate, sequential
   conditions, not one combined condition), and it does **not** need to
   have formed *after* the approach either -- any still-unmitigated 15m
   FVG already sitting in the way, from earlier in the session, qualifies
   the moment the approach completes. The bot keeps a running pool of
   every 15m FVG detected in the session, continuously drops any that get
   mitigated, and once the approach happens, picks whichever in-the-way
   candidate is **nearest to current price**, breaking ties by the
   **larger gap** (nearest-first, size-as-tiebreak was a judgment call on
   an ambiguous request -- flip the sort key in `strategy.py: WAIT_FVG` if
   you actually meant strongest-first, nearest-as-tiebreak instead).
6. Entry is a **limit order at the midpoint of that FVG's gap**
   (`(gap_low + gap_high) / 2`), not a market order at whatever price the
   confirming candle closed at. The trade only starts once price actually
   trades back to that midpoint -- if it never comes back, there's no
   entry that setup. If price instead blows straight through the gap's
   **far** edge (below `gap_low` for a bullish/LONG gap, above `gap_high`
   for a bearish/SHORT gap) before ever retracing to the midpoint, the FVG
   is **mitigated** -- it's been fully traded through, not just tapped --
   and is abandoned rather than filled. The bot drops the mitigated gap
   from its pool and, on the next bar, immediately picks another
   still-active unmitigated FVG in the same direction if one exists (this
   is the same pool described in rule 5, not a separate concept) rather
   than entering off a level that no longer means anything.

   (Revision history: v1 entered at the confirming candle's close, which
   put entries well outside the FVG zone entirely -- caught by inspecting
   the backtest charts. v2 required the FVG's gap to overlap the key
   level's zone, as a single combined condition -- too strict in practice
   (most setups were being filtered at that step, per the funnel
   diagnostics), and not actually what was meant. Corrected 2026-07-04 to
   the two-step retest-then-FVG design. Also corrected 2026-07-04 to add
   mitigation: a chart inspection showed the bot entering short off a FVG
   that price had already broken clean through on the way down -- the old
   fill check only asked "did price reach the midpoint," which is also
   trivially true when price breaks clean through the entire gap.
   Corrected again 2026-07-04 to move FVG detection from 1-minute to
   15-minute candles, and from "only the single most-recently-detected
   FVG" to a running pool of every unmitigated 15m FVG in the session --
   requiring the FVG to form fresh *after* the retest was discarding
   perfectly valid, still-untouched gaps that had simply formed earlier,
   and cutting down on the number of setups found. Corrected again
   2026-07-04, a third time the same day: a full week of real history only
   produced a single setup, and it lost -- loosened the retest to an
   "approach" (within `key_level_approach_points`, not an exact touch),
   and replaced "most-recently-formed unmitigated FVG" with "nearest
   unmitigated FVG actually in the way of the move toward the approached
   level" so a FVG sitting behind price, already passed, can't be
   selected just because it's the newest one in the pool.)
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
    reforms later in the session (new breakout/approach/FVG sequence).

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
- **"Strong" FVG** (`src/fvg.py`): a 3-candle fair value gap on the
  `timeframe_minutes` chart (15-minute by default) where (a) the gap size is
  >= `min_gap_points` and (b) the middle (displacement) candle's body is >=
  `displacement_multiplier` times the recent average candle range (over
  `lookback_bars` candles on that same timeframe). All three are
  configurable. `FvgDetector` builds these candles internally from whatever
  bars it's fed (1-minute bars from the broker/backtest) and keeps a
  running pool of every gap detected in the session, dropping ones the
  moment they're mitigated (checked against every incoming 1-minute bar,
  not just at each 15m close, so mitigation is caught as soon as it
  actually happens).
- **"Approach"** (`src/strategy.py: _nearest_key_level_within_approach`): a
  bar's range (low-to-high) coming within `key_level_approach_points`
  (`config.yaml`, default 5 points -- ASSUMPTION, originally an exact
  touch was required, loosened 2026-07-04 because requiring an exact
  touch was producing too few setups) of any of the 6 marked levels
  (previous day high/low, Asia high/low, London high/low) counts as an
  approach. This is an OR across all 6: any single one being close enough
  is sufficient, they are not required together. When more than one level
  is within range on the same bar, the nearest one is recorded as "the"
  approached level, which then anchors the in-the-way check for FVG
  selection (rule 5). The opening range box itself is not part of this
  check (it's used for the breakout and as a stop-loss candidate).
- **"In the way of the move"** (`src/strategy.py: _is_in_the_way`): a FVG
  qualifies only if its midpoint sits between current price and the
  approached level (inclusive either direction) -- i.e. price still has
  to travel through it to reach that level. A FVG behind current price
  (already passed) or beyond the level (overshooting it) doesn't count,
  even if it's a perfectly valid, unmitigated gap in the right direction.
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
