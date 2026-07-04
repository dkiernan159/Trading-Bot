# Strategy: NQ/MNQ NY-Open Opening Range Breakout + 15m FVG Anchor Entry

This document is the source of truth for what the bot implements. Anything
marked **ASSUMPTION** was not fully specified and was filled in with a
reasonable default so the bot could be built before API access is ready --
review these and adjust `config.yaml` before running live.

## Rules as given

1. Mark previous day's high/low, plus Asia session and London session high/low.
2. At 9:30 ET the NY session opens. Watch the first 15-minute candle; it
   closes at 9:45 ET. Mark its high/low as "the box".
3. Wait for price to break the box high (bullish) or box low (bearish) --
   this sets the trade direction/bias, it is not itself the entry. The
   breakout call itself can fail, though: if price later closes back
   through the box's **opposite** edge while still waiting on an anchor
   or entry (rules 4-6), the original direction call is invalidated and
   the bot resets to waiting for a fresh breakout, rather than continuing
   to hunt for a same-direction anchor/entry somewhere price has already
   reversed away from.

   (Added 2026-07-04: a real 30-day backtest's `--verbose` trade detail
   showed a LONG entry on 2026-06-22 filled at 133.5 points *below* the
   box's low -- meaning the breakout had fully round-tripped and reversed
   long before the bot's eventual entry, which was really a fresh
   downward move being mistaken for a continuation of a dead LONG thesis.
   Stop-distance was checked too and showed no correlation with win/loss
   across the 5 real trades sampled, ruling that out as the cause. Since
   nothing before this point invalidated `_breakout_direction` once set,
   the bot could keep chasing a same-direction anchor arbitrarily far from
   where the breakout actually happened. Fixed by resetting to
   `WAIT_BREAKOUT` the moment price closes back through the box's opposite
   edge while an anchor/entry is still pending.)
4. After the breakout, the bot watches for a **large 15-minute FVG** in the
   breakout direction to **anchor** the move -- this is the higher-
   timeframe confirmation that a real move is underway. It doesn't need to
   form right after the breakout; any currently-unmitigated 15m FVG in the
   breakout direction, from anywhere in the session, qualifies. Whichever
   one is nearest to current price is picked, breaking ties by the larger
   gap -- and this selection is kept **live**: while waiting for price to
   retrace to it (rule 5), the bot keeps checking for a nearer/fresher
   unmitigated 15m FVG and switches to it (moving the resting limit order
   with it) if one appears, so it's never stuck all session on the very
   first anchor it happened to find. This is **not** the same as "wait for
   it to be mitigated" -- the anchor is never invalidated or abandoned
   just because price trades through it; reaching its far edge and
   filling the entry are actually the same event (see rule 5), so there's
   no separate "abandoned because mitigated" outcome to have.
5. Entry is a **limit order at the midpoint of that anchor 15m FVG's own
   gap** (`(gap_low + gap_high) / 2`), not a market order at whatever
   price the confirming candle closed at, and not a further nested
   structure inside the anchor. The trade only starts once price actually
   trades back to that midpoint -- if it never comes back, there's no
   entry that setup. Since the midpoint sits strictly between the gap's
   two edges, a bar can never break the gap's **far** edge without having
   *already* reached the midpoint first -- so the resting limit order
   always fills; there's no such thing as this pending entry getting
   "mitigated before it could fill." (Mitigation still matters earlier,
   when *choosing* a 15m FVG as the anchor in the first place -- an
   already-broken gap is never selected as the anchor to begin with.)

   (Revision history: v1 entered at the confirming candle's close, which
   put entries well outside the FVG zone entirely -- caught by inspecting
   the backtest charts. v2 required the FVG's gap to overlap the key
   level's zone, as a single combined condition -- too strict in practice
   (most setups were being filtered at that step, per the funnel
   diagnostics), and not actually what was meant. Corrected 2026-07-04 to
   a two-step retest-then-FVG design, then to a running pool of every
   unmitigated 15m FVG in the session (rather than only the single most-
   recently-formed one), then to loosening the retest requirement to a
   5-point "approach" and picking whichever unmitigated FVG was nearest to
   price and in the way of the move -- each correction chasing more setups
   out of a real week of history that kept producing too few trades.
   Corrected again 2026-07-04: the entry was still priced off the
   15-minute FVG's midpoint, which spans several points on a fast-moving
   instrument like MNQ -- the resulting stop (sized off structural levels
   near that entry) was too wide for how this actually trades and got hit
   by ordinary noise. Replaced the key-level-approach step entirely with
   a two-stage design: the 15m FVG still confirms the move (the "large
   FVG" anchor), but the entry itself came from a smaller FVG nested
   inside that 15m gap (first 1-minute, then 5-minute -- see below), so
   the entry price -- and therefore the stop -- reflected finer-scale
   structure instead of 15-minute-scale noise. That nested stage went
   through several corrections of its own (requiring the nested gap to
   have formed strictly after the anchor locked in, so the bot couldn't
   claim a gap embedded in the anchor's own displacement leg as an
   instant "retest"; loosening containment to midpoint-only; removing an
   incorrectly-backwards mitigation check on the pending nested entry;
   switching the nested timeframe itself from 1-minute to 5-minute) but
   it was *always* the tightest bottleneck in the funnel, at every
   timeframe tried: a real 7-day backtest at 1-minute found only 1
   qualifying nested candidate across 8 anchors; a real 30-day backtest
   at 5-minute (after loosening the 5-minute thresholds too) still only
   found 1 across 25 anchors (breakouts=33, breakouts_invalidated=13,
   large_15m_fvgs=25, nested=1, fills=0) -- a *tighter* bottleneck,
   proportionally, than the 1-minute version had been. Removed the
   nested-entry stage entirely 2026-07-04 in favor of the current design:
   the anchor's own midpoint is the entry, directly. This keeps the exact
   same mathematical guarantee that made the nested design safe (the
   midpoint is always reached before the far edge can be broken, so
   "mitigated before it could fill" still can't happen) while removing
   the requirement that an *additional*, smaller structure also form and
   still be live inside the anchor -- which had been by far the rarest
   condition in the whole sequence regardless of what timeframe or
   thresholds it used.)
6. Reward:risk is 2:1.
7. Stop-loss is placed intelligently at a real structural level -- below
   the bottom of the 15m anchor FVG, or below the next break of structure
   beyond it, whichever makes sense on the chart (mirrored for shorts:
   above the top of the anchor / above the next break of structure) --
   **capped at $200 of risk per trade** (`config.yaml:
   strategy.max_stop_dollars`) at the current `position_sizing.contract_size`,
   so the stop is never wider than that regardless of how far away the
   nearest structural level is.
8. Reference size is 5 MNQ contracts, targeting ~$300/trade. The bot starts
   at a smaller size (`config.yaml: position_sizing.contract_size`) until a
   consistent win rate is shown; scaling back up to 5 is **manual only** --
   the bot never changes its own size.
9. If stopped out, the bot re-arms and can take another trade if the setup
   reforms later in the session (new breakout/anchor sequence).

## How ambiguous points were resolved (ASSUMPTIONS)

- **Stop-loss placement** (`src/risk.py`): the "large formed area of
  resistance/support" is interpreted as the nearest already-marked level
  beyond entry in the stop direction -- previous day high/low, Asia
  high/low, London high/low, the opening range box edge, **or either
  boundary of the 15m anchor FVG** (`strategy.py`'s `structural_levels`,
  built when the trade signal fires). The 15m anchor's far edge is a
  structural level in its own right -- a break of it invalidates the
  whole setup -- so it's included alongside the marked levels; whichever
  of all of these ends up nearest beyond entry becomes the stop
  ("whatever makes sense based on the chart": the anchor's own bottom/top
  if that's nearest, otherwise the next further-out break of structure).
  The distance to that nearest level is **capped** at `max_stop_dollars`
  (`config.yaml`, $200) converted to points at signal time
  (`max_stop_dollars / (instrument.point_value * position_sizing.contract_size)`)
  -- so the dollar risk per trade never exceeds $200 regardless of
  contract size, even if the nearest structural level is farther out.
  - Take-profit is always `2 x actual_stop_distance` (so smaller structural
    stops give a smaller, still-2:1, target -- this is why the target
    varies per trade rather than always chasing the reference $300).
  - (Revision history: originally a fixed `max_stop_points: 15` derived
    from the reference $300 target at 5 contracts -- once entries moved
    to a smaller FVG nested inside the 15m anchor (a design later
    removed, see rule 5's revision history), the nearest structural level
    was often extremely close to entry, producing very shallow,
    easily-noise-triggered stops. Corrected 2026-07-04 to a
    dollar-denominated cap and added the 15m anchor's own boundary as a
    stop candidate, so the bot can use a wider, more sensible structural
    stop -- the anchor's bottom/top, or the next break of structure
    beyond it -- as long as it stays within $200. This stop logic was
    unaffected by later removing the nested-entry stage: the anchor's own
    boundary was already a stop candidate regardless of where inside it
    the entry price landed.)
- **"Strong" FVG** (`src/fvg.py`): a 3-candle fair value gap on
  `FvgConfig.timeframe_minutes` where (a) the gap size is >=
  `min_gap_points` and (b) the middle (displacement) candle's body is >=
  `displacement_multiplier` times the recent average candle range (over
  `lookback_bars` candles on that same timeframe). All three are
  configurable. `FvgDetector` builds these candles internally from
  whatever bars it's fed (always 1-minute bars from the broker/backtest)
  and keeps a running pool of every gap detected, dropping ones the
  moment they're mitigated (checked against every incoming 1-minute bar,
  not just at each candle close, so mitigation is caught as soon as it
  actually happens). The strategy runs a single instance of this
  detector, `fvg_detector_15m` (`config.yaml: strategy.fvg`,
  `timeframe_minutes: 15`), which finds the large anchor FVG that both
  confirms the move and supplies the entry price (its own midpoint --
  see rule 5). Its thresholds were loosened 2026-07-04 (`min_gap_points`
  3.0->2.5, `displacement_multiplier` 1.5->1.3) after a real week of
  history kept producing too few anchors.

  (Revision history: a second detector instance, `fvg_detector_5m`
  (`config.yaml: strategy.entry_fvg`, previously `fvg_detector_1m`),
  used to run alongside this one to find a smaller FVG nested inside the
  15m anchor as the actual entry trigger -- see rule 5's revision
  history for why that stage was removed entirely 2026-07-04. Its
  config section, `min_gap_points`/`displacement_multiplier` tuning
  history, and the `entry_fvg` field on `StrategyConfig` were removed at
  the same time.)
- **Daily reset of the active-gap pool** (`src/fvg.py:
  FvgDetector.clear_active_gaps`, called from `strategy.py:
  _start_new_day`): a gap that simply never gets revisited stays
  "unmitigated" forever, so without this the pool could accumulate gaps
  from days or weeks earlier and offer them up as today's anchor --
  found by comparing a 7-day and a 30-day backtest that disagreed about
  what happened on the exact same calendar day (the 30-day run had a much
  larger backlog of old, technically-still-valid gaps available at that
  point). Every new trading day now clears the active-gap pool, so only
  gaps from *today's* session are ever candidates -- matching "the
  session" in rules 4-5. The candle history used for the average-range
  baseline is untouched by this, so it's still populated with real
  pre-market/overnight data from the moment 9:30 arrives.
- **Previous day / Asia / London levels**: no longer part of the entry
  sequence at all (an earlier revision briefly used them for a "retest"/
  "approach" step -- removed 2026-07-04 in favor of the 15m-anchor
  design above). They're still marked every day and still feed
  `src/risk.py`'s stop-loss placement (nearest structural level beyond
  entry, capped at `max_stop_dollars`) and the chart's shaded reference
  bands -- just not the entry trigger anymore.
- **Asia / London session windows** (`config.yaml: session`): set to common
  ICT-style approximations (Asia 19:00-23:59 ET prior evening, London
  02:00-05:00 ET). Adjust to your exact definition.
- **End of session for new entries**: no new setups after 12:30 ET, hard
  flatten by 12:45 ET if still in a trade. You described this as a morning
  strategy but didn't give an exact cutoff. (Originally 11:30/11:45 --
  extended 2026-07-04: the two-stage 15m-anchor + fresh-1m-retest design
  needs more runway per anchor than the original 2-hour window gave it; a
  real 30-day backtest was still only producing 1 fill.)
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
