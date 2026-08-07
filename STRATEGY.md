# Strategy: NQ/MNQ NY-Open Opening Range Breakout + 5m FVG Anchor Entry

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
4. After the breakout, the bot watches for a **large FVG** in the breakout
   direction to **anchor** the move -- this is the confirmation that a
   real move is underway. As of 2026-07-04, this can be **either** a
   large 5-minute FVG **or** a 1-minute FVG (`config.yaml:
   strategy.entry_fvg`) -- whichever qualifies, pooled together, not one
   nested inside the other (see "How ambiguous points were resolved"
   below for why this was added and how it differs from an earlier,
   removed nested design). It doesn't need to form right after the
   breakout; any currently-unmitigated FVG (of either timeframe) in the
   breakout direction, from anywhere in the session, qualifies. Whichever
   one is nearest to current price is picked, breaking ties by the larger
   gap -- and this selection is kept **live**: while waiting for price to
   retrace to it (rule 5), the bot keeps checking for a nearer/fresher
   unmitigated FVG (of either timeframe) and switches to it (moving the
   resting limit order with it) if one appears, so it's never stuck all
   session on the very first anchor it happened to find. This is **not**
   the same as "wait for it to be mitigated" -- the anchor is never
   invalidated or abandoned just because price trades through it;
   reaching its far edge and filling the entry are actually the same
   event (see rule 5), so there's no separate "abandoned because
   mitigated" outcome to have.
5. Entry is a **limit order at a retracement point inside that anchor
   FVG's own gap** (`entry_retracement_pct` of the way in from the near
   edge -- `0.5` is the exact midpoint, `(gap_low + gap_high) / 2`, and
   is what `config.yaml` currently uses, see revision history for a
   loosening attempt that was tried and reverted), not a market order at
   whatever price the
   confirming candle closed at, and not a further nested structure
   inside the anchor. The trade only starts once price actually trades
   back to that point, **no matter how much later in the session that
   happens or how far price has moved away from the gap in the
   meantime** -- there's no separate time or distance limit on the
   retest beyond the session cutoff itself (rule on end-of-session
   below). If it never comes back before the cutoff, there's no entry
   that setup. Since the entry point sits strictly between the gap's two
   edges (true for any retracement fraction strictly between 0 and 1,
   not just the midpoint), a bar can never break the gap's **far** edge
   without having *already* reached the entry point first -- so the
   resting limit order always fills; there's no such thing as this
   pending entry getting "mitigated before it could fill," regardless of
   which retracement fraction is configured. (Mitigation still matters
   earlier, when *choosing* a 5m FVG as
   the anchor in the first place -- an already-broken gap is never
   selected as the anchor to begin with.)

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
   thresholds it used.

   Corrected again 2026-07-04, based on a real annotated TopstepX chart
   the user shared of an actual trade they'd have taken by hand (MNQU26,
   5-minute candles, with the 15-minute ORB box, the 5-minute FVG, and
   the entry all marked). That example showed the real methodology never
   had a separate 15-minute FVG step at all -- "15 Minute ORB" in the
   rules only ever meant the opening-range box (rule 2); the FVG that
   anchors the move and supplies the entry price is itself 5-minute, with
   no higher-timeframe anchor above it. The same chart also showed the
   entry retest happening over an hour after the FVG formed, well after
   price had already rallied further away from it -- confirming there's
   no time or distance limit on the retest beyond the session cutoff
   (matching the "kept live" behavior in rule 4, just now with only one
   detector instead of two). `fvg_detector_15m` was renamed
   `fvg_detector_5m` and `config.yaml: strategy.fvg.timeframe_minutes`
   changed from 15 to 5; nothing else about rules 4-5's mechanics
   changed, since the entry-at-own-midpoint / kept-live / no-time-limit
   design was already correct -- only the timeframe it ran on was wrong.

   Loosened again 2026-07-04: a real 30-day `--near-miss` backtest showed
   "superseded" (a fresher/nearer anchor replacing one before it ever
   filled) was by far the largest outcome, 29 of 51 near-misses -- several
   anchors sat live for 1-2+ hours before being replaced, suggesting price
   was often approaching but not making it all the way to the exact
   midpoint before a fresher anchor took over. Entry is no longer
   hardcoded to the exact midpoint; `entry_retracement_pct`
   (`config.yaml`) sets how far into the gap price must retrace, as a
   fraction of that anchor's own width -- `0.5` reproduces the exact
   midpoint. Confirmed with the user beforehand (not guessed) that this
   should be a configurable retracement fraction scaling with each
   anchor's own size -- not a fixed point-distance tolerance, and not an
   entry aligned to wherever a marked structural level happens to sit
   inside the gap. The "always fills before it could be mitigated"
   guarantee (rule 5 above) needed no changes to hold for this: it was
   never actually specific to the midpoint -- any point strictly between
   the gap's two edges has the same property, since a single bar's
   low/high can't reach the far edge without having already reached
   anything closer to where price is coming from. No separate mitigation
   check was added as a result, since it would never fire.

   Loosened to `0.35` the same day to actually recover some of that
   "superseded" frequency; reverted back to `0.5` also the same day after
   a real `--verbose` run showed the 3 trades this recovered
   (2026-06-12 x2, 2026-06-29) all lost -- win rate dropped from 50% to
   33% and net from $249.75 to $159.28 despite the extra trades. This was
   the *second* consecutive loosening attempt (see rule 7's `min_stop_dollars`
   history) where the specific trades a loosened rule recovered were pure
   losers -- 0 of 8 combined across both -- a real pattern rather than
   noise: setups that only qualify once a rule is loosened tend to be
   lower quality by exactly the measure that rule was checking. Kept the
   mechanism (it's a legitimate, structure-scaled lever) but reset the
   value to the original midpoint until real data supports moving it.

   Added a second, 1-minute-timeframe anchor detector 2026-07-04 (rule 4)
   at the user's explicit request and specification: they can find at
   least 1 real trade per trading day manually after the ORB breakout,
   but a real 7-day backtest was only finding 2, and the funnel showed
   plenty of 5m anchors forming (23) versus few retracing all the way
   back to fill (3) -- i.e. anchors weren't the scarce resource, fills
   were. The user's diagnosis: the bot is "missing formations of 5 minute
   FVGs" and should also accept a 1-minute FVG once the ORB breakout
   confluence is satisfied. This is a different design from the nested
   "small FVG inside the big one" stage removed earlier in rule 5's own
   history (which required *both* a large 5m FVG *and* a smaller one
   nested inside it, in sequence, and was removed for being the tightest
   bottleneck at every timeframe tried) -- here, the 1m and 5m detectors
   are independent, pooled alternatives; either one qualifying is
   sufficient on its own, matching the user's "either... or" framing
   exactly.

   As predicted, this did initially follow the same pattern as every
   other loosening tried in this project: a real 30-day `--verbose`
   backtest (25 trades) came back at 44% win rate / $217 net -- a real
   frequency win (up from 2 trades in the same 7-day sub-window to 4),
   but a clean quality problem underneath it. Every one of the 7 trades
   whose anchor gap was under 14 points lost (0 of 7), while trades with
   a gap >=14 points won 11 of 18 (61%) -- true regardless of direction,
   stop distance, or which detector found the anchor (one of the 7 tiny
   losers was even a 5m anchor). At the original `min_gap_points: 0.5`,
   "any displacement counts" -- a 3.5-6.75 point 1-minute gap on MNQ is
   ordinary noise, not real structure, and `displacement_multiplier`
   alone doesn't catch this because a quiet stretch's recent average
   range is also small, so a tiny gap can look "relatively strong" while
   staying absolutely tiny. Raised `entry_fvg.min_gap_points` to `12`
   (config.yaml) as a result -- excluding just those 7 trades would have
   turned this same window into 61% win rate / $1,146.54 across the
   remaining 18, while still leaving ~14 of 19 trading days with a
   trade.)
6. Reward:risk is 2:1.
7. Stop-loss is placed intelligently at a real structural level -- the
   nearest marked previous-day/Asia/London high-low or opening-range box
   edge beyond entry, whichever makes sense on the chart -- as long as
   that distance falls between **$40 and $200 of risk per trade**
   (`config.yaml: strategy.min_stop_dollars` / `max_stop_dollars`) at the
   current `position_sizing.contract_size`. If no such level exists
   beyond entry, the nearest one is farther out than $200 allows, or it's
   closer than $40 (too tight to be a real invalidation point, not just a
   smaller structural stop), the trade is skipped entirely rather than
   taking a stop with no genuine structural backing (see "How ambiguous
   points were resolved" below for why the anchor's own boundary is
   deliberately *not* one of these candidates now that entry sits at its
   midpoint, why "nearest" rather than "farthest within budget" is what
   the real data actually supports, and why skipping replaced the
   original max-risk-cap fallback and was later extended to a floor too).
8. Reference size is 5 MNQ contracts, targeting ~$300/trade. The bot starts
   at a smaller size (`config.yaml: position_sizing.contract_size`) until a
   consistent win rate is shown; scaling back up to 5 is **manual only** --
   the bot never changes its own size.
9. If stopped out, the bot re-arms and can take another trade if the setup
   reforms later in the session (new breakout/anchor sequence).

## How ambiguous points were resolved (ASSUMPTIONS)

- **Stop-loss placement** (`src/risk.py`): **rewritten 2026-07-08** at the
  user's explicit correction to a two-tier rule, replacing the
  previous-day/Asia/London/box-level approach described in the revision
  history below entirely: primarily the outer edge of the nearest strong
  5m FVG sitting on the stop side of entry (below entry for a LONG, above
  for a SHORT -- the same "strong" 5m FVGs already used for anchor
  selection, `fvg_detector_5m.unmitigated_in_direction(direction)`, same
  direction as the trade, since a LONG-direction gap is a bullish/support
  gap, which is what should sit *below* a long entry), falling back to
  the most recent 1-minute break-of-structure swing point
  (`src/swing_points.py`'s `SwingPointTracker`: a standard N-bar fractal
  pivot, `PIVOT_WIDTH=2` -- ASSUMPTION, tune if real data suggests
  otherwise -- fed 1-minute bars directly, reset at day/night session
  boundaries same as the FVG detectors) only when no FVG qualified.
  Whichever of the two applies becomes the stop, as long as that distance
  falls between `min_stop_dollars` and `max_stop_dollars` (`config.yaml`,
  $40-$200) converted to points at signal time (unchanged from before --
  see `compute_stop_target`, whose own job shrank to just validating a
  given `stop_price` against this budget and computing the target, no
  longer searching a candidate list itself). If neither a qualifying FVG
  nor a swing point exists on the stop side at all, or the one that does
  is outside the $40-$200 band, the trade is skipped entirely
  (`find_structural_stop_price` returns `None`, or `compute_stop_target`
  does) rather than using the cap as a stop distance with no real level
  behind it, or taking a stop too tight to be a genuine invalidation
  point.
  - **Priority made per-strategy, 2026-07-08** (same day, twice more):
    first flipped to swing-first globally after a real 32-trade overnight
    backtest's `--verbose` detail (see the diagnostic addition below)
    showed swing-based stops winning 50% (net +$665 across 12 trades,
    +$55/trade) versus FVG-based stops winning only 35% (net +$248 across
    20 trades, +$12/trade) there -- and FVG size didn't predict the
    difference (the worst bucket was mid-sized 20-30pt gaps, not the
    smallest ones), so the *source* itself, not gap size, was the
    discriminator. But re-running the *day* (opening-range breakout)
    strategy's own 30-day backtest with the same global flip made it
    measurably worse (10->25 trades, 30%->24% win rate,
    -$199.75->-$424.75 net): the FVG-first rule had been usefully
    filtering day-strategy entries down to ones with a real
    support/resistance gap nearby, and swing-first let a lot of marginal
    setups through that used to get skipped as `no_valid_stop`. So
    `find_structural_stop_price` now takes a `prefer_swing: bool = True`
    parameter instead of a single global order: `strategy.py` (day) calls
    it with `prefer_swing=False` (FVG-first, its original rule),
    `overnight_strategy.py` calls it with `prefer_swing=True`
    (swing-first) -- each strategy keeps whichever order its own real
    backtest data showed winning, rather than sharing one priority.
  - Take-profit is always `2 x actual_stop_distance` (so smaller structural
    stops give a smaller, still-2:1, target -- this is why the target
    varies per trade rather than always chasing the reference $300).
  - **Diagnostic addition, 2026-07-08**: `find_structural_stop_price` now
    returns a `StopCandidate` (`price`, `source` -- `"fvg"` or `"swing"` --
    and, when `source=="fvg"`, `fvg_gap_low`/`fvg_gap_high`/`fvg_size`)
    instead of a bare float, purely so the question "why is the stop
    getting hit so much -- are the FVGs it's basing stops on not strong
    enough?" can be checked against real trade-by-trade data rather than
    guessed at. `EntrySignal` (`strategy.py`, `overnight_strategy.py`) now
    carries `stop_source`/`stop_fvg_size` alongside the resolved
    `stop_price`, and `backtest.py`'s `--verbose` trade detail prints
    `source=fvg (fvg size=N.NN)` or `source=swing` next to each trade's
    stop. No selection logic changed -- `compute_stop_target` still just
    validates a plain `stop_price` float against the budget; the callers
    now pass `stop_candidate.price` instead of the candidate itself.
  - **Cap fallback added 2026-07-10, reverted 2026-07-14:** the user
    noticed frequent `no_valid_stop` rejections live and asked that a
    trade "still occur as long as it meets all other qualifiers" even
    when no real structural level exists at all, using the chart-based
    rule above only when it's available. This was the exact thing this
    project tried once before (see `compute_stop_target`'s revision
    history, 2026-07-04) and reverted after real data showed both losing
    trades in that sample had defaulted to the full cap while real-level
    trades won small and consistently -- flagged to the user directly
    before making the change; they chose to proceed anyway to see if it
    held up under the newer (FVG/swing-based, not previous-day/Asia/
    London-level-based) anchor design, specifically with the full $200
    cap. It didn't: within days, a real live overnight trade whose stop
    landed on an exact round 100.00-point distance (precisely
    `max_stop_dollars/point_value/contracts` -- not a number any real
    structural level would coincidentally produce) lost $200, the same
    failure shape as before. On seeing that, the user reverted it
    immediately: "if the trade isn't strong and doesn't have a good stop
    loss point below a break of structure or resistance level then we
    shouldn't take it." `find_structural_stop_price` returns `None` again
    (skip the trade) when neither a qualifying FVG nor swing point exists
    at all -- this exact rule has now failed the same way under two
    different anchor-selection designs, so per the function's own
    docstring it shouldn't be tried a third time without a fundamentally
    different justification than "let more trades through."
  - **Open question, 2026-07-08** (not yet acted on -- watching for more
    data): with `prefer_swing=False` restored, the day strategy's own
    30-day backtest (10 trades, 30% win, -$199.75 net) split sharply by
    direction -- LONG breakouts won only 1 of 7 (14%), SHORT won 2 of 3
    (67%). Price rose from ~30,000 to a peak ~30,970 around 6/23 then
    fell back to ~30,000 by 7/6-7/7 over this window, and the LONG losses
    cluster in the declining second half -- consistent with (but not
    proof of) the day strategy having no higher-timeframe trend filter,
    so it fires counter-trend breakouts as readily as trend-following
    ones. 10 trades is too thin to separate a real edge issue from this
    particular month's regime; decided to keep the strategy as-is and
    revisit once a larger sample (or a wider backtest window) is
    available, rather than add a trend filter against this little data.
  - (Revision history: originally a fixed `max_stop_points: 15` derived
    from the reference $300 target at 5 contracts -- once entries moved
    to a smaller FVG nested inside the 15m anchor (a design later
    removed, see rule 5's revision history), the nearest structural level
    was often extremely close to entry, producing very shallow,
    easily-noise-triggered stops. Corrected 2026-07-04 to a
    dollar-denominated cap and added the 15m anchor's own boundary
    (`gap_low`/`gap_high`) as a stop candidate alongside the marked
    levels, so the bot could use a wider, more sensible structural stop
    -- the anchor's bottom/top, or the next break of structure beyond it
    -- as long as it stayed within $200. Removed the anchor's own
    boundary from the candidate list again 2026-07-04, the same day
    entry was changed to the anchor's own midpoint (rule 5): once entry
    sits exactly at the anchor's center, its near/far edges are always
    exactly *half the anchor's own gap width* from entry -- pure
    arithmetic, not a real break of structure -- and because that
    distance is essentially guaranteed to be small, it silently
    dominated the "nearest" comparison over the real, externally-marked
    levels every time. Confirmed against 3 real losing trades from a
    7-day backtest: stop distances of $55.75, $56.25, and $27.00 each
    matched exactly half of that trade's own anchor gap width, while the
    bot had up to $200 available and real structural levels sat farther
    out unused. The anchor's own boundary is no longer a stop candidate;
    only the marked previous-day/Asia/London/box levels are.

    Briefly changed the selection itself from nearest-level to
    farthest-level-within-budget the same day, on this reasoning: a real
    30-day backtest's `--verbose` detail across 7 trades showed 0 of 4
    trades whose stop landed on the nearest available level (in every
    case, the opening-range box edge, which is often close simply
    because that's literally where the breakout happened) won, while 2
    of 3 trades that instead fell back to the full $200 cap (because
    even the nearest real level was farther out than the cap) won.
    "Nearest" looked like it was consistently finding a minor speed bump
    rather than a real invalidation point. Reverted this the same day
    after re-running the identical 7 trades with the farthest-level
    change applied: it only actually changed the stop for 2 of them
    (2026-06-11, 2026-06-23) -- both were already losses, and the wider
    stop just made them lose *more* ($47.75->$93.24 and
    $115.25->$174.76) without turning either into a win. Win rate stayed
    at 2/7 (28.6%) but net P&L dropped from $354.25 to $249.26. Those 2
    trades weren't stopped out by noise a wider stop would have ridden
    through -- they kept moving against the position regardless -- so
    nearest, the more conservative choice when multiple levels are
    equally "real" structure, is what the evidence actually supports.
    Reverted back to nearest-level selection.

    Changed again 2026-07-04: previously, when no real level was within
    budget at all, the cap itself was used as the stop distance outright
    -- an arbitrary, structurally unjustified number. A real 7-day
    backtest's `--verbose` detail showed this was exactly what broke a
    50%-win-rate window: both losing trades (2026-06-29, 2026-06-30) had
    no real level within $200 of entry and defaulted straight to the full
    $200/100-point cap, while the two winning trades happened to have a
    real level only 8.25-31.13 points from entry, making their (correctly
    proportional) 2:1 targets tiny by comparison -- $16.50 and $124.50 of
    wins couldn't offset two $200 losses, for a net of -$242.50 despite
    2/4 trades winning. Rather than take a max-risk trade with no genuine
    invalidation point behind it, `compute_stop_target` now returns
    `None` in this case and the trade is skipped entirely --
    `strategy.py`'s `WAIT_FILL` handling records it as a new
    "no_valid_stop" anchor outcome and keeps hunting for a different
    anchor, excluding the rejected one by identity so it isn't
    immediately re-offered every subsequent bar just because it's still
    the nearest unmitigated gap.

    Added `min_stop_dollars` ($40, a 20-point floor at 1 contract) the
    same day, once fixing the max-side problem surfaced a different one:
    a fresh 7-day `--verbose` run (13 trades, no more max-cap losses)
    still only won 31% of the time for a thin $192.50 net. Splitting the
    13 trades by stop size showed why -- the 7 trades with a stop under
    ~20 points (almost always the opening-range box edge, close only
    because that's literally where the breakout happened, not real
    structure) won just once (14%), versus 3 of the remaining 6 (50%) for
    trades with a wider, more genuine stop. Removing the under-20-point
    group entirely would have turned this same window into a 50% win
    rate and $249.75 net across the remaining 6 trades. A stop that tight
    is inside ordinary MNQ chop, not a real invalidation level, so it's
    now rejected -- recorded as the same "no_valid_stop" outcome -- the
    same way an out-of-budget one is.

    Briefly changed the selection itself again 2026-07-04, chasing more
    trade frequency after a real 30-day `--near-miss` run showed 12 of 65
    near-miss anchors rejected as "no_valid_stop": previously, only the
    single nearest candidate beyond entry was ever checked against the
    $40-$200 band, so if *that one* happened to be too close, the trade
    was skipped even when a second, farther marked level existed that
    would have cleared the floor comfortably while staying well within
    the cap. Tried having `compute_stop_target` pick the nearest
    candidate that actually clears the band, rather than checking only
    the nearest candidate overall -- reasoned to be different from
    "farthest within budget" (reverted above) since it still prefers the
    nearest *usable* level, just without letting one unrealistically-close
    level block a perfectly good farther one. Reverted the same day: a
    real 30-day `--verbose` run showed this recovered exactly 5 trades
    (matching the drop in "no_valid_stop" near-misses) and all 5 lost
    (2026-06-11, -12, -17, -19, -29) -- every one landed on an Asia or
    London session level reached by skipping a tighter box-edge
    candidate, while the 6 trades that didn't need to skip anything held
    their existing 50% win rate. Despite the different reasoning, this
    turned out to be the same failure shape as farthest-within-budget:
    reaching past the nearest level for a "more valid-looking" one
    produced worse trades, not better ones. Reverted back to checking
    only the single nearest candidate against the band -- if it doesn't
    clear, the trade is skipped outright rather than substituting a
    farther level.

    That same `--near-miss` run also surfaced an unrelated bookkeeping
    bug: every "session_ended" anchor in the report appeared twice, once
    with a plausible same-day duration and once with an inflated (in a
    few cases multi-day) one. The `no_new_entries_after` cutoff branch in
    `strategy.py`'s `on_bar` recorded the anchor's outcome but never
    cleared `_anchor_fvg`/`_anchor_started_at`/`_pending_limit_price`
    afterward, unlike every other close site -- so the stale anchor got
    silently re-recorded by `_start_new_day`'s defensive close whenever
    the next bar happened to arrive, sometimes days later if the fed data
    had a gap. Fixed by clearing those fields at the cutoff close too;
    this only affects the near-miss diagnostic report, not any trading
    decision or past backtest P&L.

    **Replaced entirely 2026-07-08** at the user's explicit correction,
    prompted by them noticing a live overnight anchor go from `WAIT_FILL`
    back to `WAIT_FVG` and asking why: "the stop doesn't need to be a
    previous day high or low... the stop should be within $200 and at or
    above/below respectively the nearest break of structure on the 1 min
    timeframe." Followed up with the precise two-tier rule once asked to
    clarify the exact definition: "The stop should be set at either below
    or above respectively the nearest strong 5 minute FVG based on if the
    entry is long or short. Or if no strong 5 min fvg exists, the stop
    should be slightly above the nearest break of structure, which means
    the most recent high/low respectively for a long or short entry on
    the chart." Confirmed this fully replaces (not layers alongside) the
    previous-day/Asia/London/box-level approach above, and applies to
    both the day and overnight strategies. See `find_structural_stop_price`
    and `src/swing_points.py` for the implementation; every test in this
    file's revision history above that depended on previous-day/box
    levels for its stop was rewritten to construct a real 5m FVG or swing
    low instead (see `tests/test_strategy.py`'s `feed_premarket_swing_low`
    and `tests/test_overnight_strategy.py`'s
    `feed_swing_low_after_window_opens`/`feed_swing_high_after_window_opens`
    -- deliberately built from price action too small/short-lived to ever
    also register as a candidate *anchor*, which would otherwise
    contaminate those tests' anchor_history/state assertions with an
    extra premature pick-and-supersede cycle). Not yet validated against a
    real historical backtest as of this writing -- no network/broker
    credentials were available in the sandbox this change was made in;
    run a real backtest before trusting trade frequency/behavior at this
    new rule.)
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
  actually happens). The strategy runs **two** instances of this
  detector as of 2026-07-04: `fvg_detector_5m` (`config.yaml:
  strategy.fvg`, `timeframe_minutes: 5`) and `fvg_detector_1m`
  (`config.yaml: strategy.entry_fvg`, `timeframe_minutes: 1`) -- either
  one finding a large anchor FVG both confirms the move and supplies the
  entry price (a retracement point inside its own gap -- see rule 5),
  pooled together as alternatives (see rule 4's revision history for
  why). `fvg`'s thresholds were loosened 2026-07-04 (`min_gap_points`
  3.0->2.5->2.0, `displacement_multiplier` 1.5->1.3->1.1) while it still
  ran on 15-minute candles and a real week of history kept producing too
  few anchors, then again after the nested-entry stage was removed (see
  below) still only produced ~3 trades in a real 7-day backtest -- before
  `entry_fvg` was reintroduced, this had been the *only* remaining gate
  between a breakout and a trade. Switched from 15-minute to 5-minute
  candles 2026-07-04 (see rule 5's revision history for why) with
  `min_gap_points` retuned to 1.5 for the new timeframe; check `git log`
  / this file's revision history for the current values if config.yaml
  has moved past what's written here.

  (Revision history: a second detector instance -- also called
  `fvg_detector_1m` at the time, and using the same `entry_fvg` config
  key reused today -- used to run alongside the 15m anchor detector to
  find a smaller FVG *nested inside it* as the actual entry trigger,
  requiring both to exist in sequence; removed entirely 2026-07-04 (see
  rule 5's revision history) for being the tightest bottleneck at every
  timeframe tried. Reintroduced 2026-07-04, same config key and detector
  name, but as a genuinely different design: an independent, alternative
  anchor source pooled alongside `fvg_detector_5m`'s candidates rather
  than nested inside them -- either one qualifying is enough on its own.
  Added at the user's explicit request after a real 7-day backtest found
  only 2 trades against their >=1/trading-day requirement, with the
  funnel showing anchors weren't the scarce resource (23 5m ones formed)
  but fills were (3). Started at `min_gap_points: 0.5` /
  `displacement_multiplier: 1.0` / `lookback_bars: 8` as fresh
  ASSUMPTIONS for the 1-minute timeframe; `min_gap_points` raised to
  `12` the same day rule 5's revision history describes -- a real
  30-day backtest showed every trade with an anchor gap under 14 points
  losing (0 of 7), while `displacement_multiplier`/`lookback_bars`
  remain untested against real data as of this writing.)
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
- **End of session for new entries**: no new setups after 13:30 ET, hard
  flatten by 13:45 ET if still in a trade. You described this as a morning
  strategy but didn't give an exact cutoff. (Originally 11:30/11:45 --
  extended 2026-07-04 to 12:30/12:45: the two-stage 15m-anchor +
  fresh-1m-retest design needed more runway per anchor than the original
  2-hour window gave it; a real 30-day backtest was still only producing
  1 fill. Extended again 2026-07-04 to 13:30/13:45 as part of the push
  toward ~1 trade/day: a real 30-day backtest's funnel showed anchors
  forming fine (26 of 37 breakouts, 70%) but only 7 of those 26 (27%)
  ever retraced back to their own midpoint before running out of
  session -- price often needs more real time to come back to an
  anchor's center than the window was allowing, and that's now the
  actual bottleneck for trade frequency, not anchor strength.)
- **After a winning trade**: bot stood down for the rest of the day/night by
  default (`allow_new_setup_after_win: false`). You only specified re-entry
  behavior after a *loss* originally.
  - **Flipped to `true` 2026-07-31, your explicit instruction**, after a
    real overnight win (2026-07-31T02:00 UTC, +$149) stood the strategy
    down for the rest of that night and you asked why -- you'd initially
    guessed a trade-count cap, but both `max_trades_per_day` and
    `max_trades_per_night` were confirmed already `null` on the live VPS
    config, so this flag was the actual cause. You confirmed ("for testing
    lets just remove the limit entirely") you want the bot to keep hunting
    for new setups after a win too, same as it already does after a loss
    (`allow_reentry_after_stop`). Shared by both strategies (day and
    overnight read the same `strategy.reentry` config); `max_daily_loss_dollars`
    and `kill_switch` remain the real safety net regardless of trade count.
- **Daily safety limits** (`max_trades_per_day`, `max_daily_loss_dollars`,
  `kill_switch`): not requested, added because this trades a TopStep
  funded/evaluation account where breaching a drawdown rule can end the
  account. Disable/adjust freely in `config.yaml`.
  - **`max_trades_per_day` removed 2026-07-14, your explicit instruction**
    after reviewing a day where it (not the $ loss limit) was what
    actually stood the bot down: it was capping trade count regardless of
    quality, and you'd rather the FVG/swing-structure entry criteria (and
    the $ loss limit / kill switch below, which remain) be the only real
    gate. `RiskLimitsConfig.max_trades_per_day` is now `int | None`;
    `null` in `config.yaml` means no cap (`DailyRiskState.can_take_new_trade`
    skips the count check entirely when it's `None`).

## Overnight momentum strategy (Asia/London, added 2026-07-05)

A second, parallel strategy (`src/overnight_strategy.py`, `OvernightMomentumStrategy`)
runs during Asia (19:00-23:59 ET, the evening before the trading date) and
London (02:00-05:00 ET) hours, at your explicit request to let the bot trade
overnight while you sleep, on top of -- not instead of -- the 9:30 ORB
strategy above. The two run as independent instances fed the same bars;
neither knows the other exists.

Unlike the day strategy, there is no box or breakout here -- there's nothing
at 19:00 or 02:00 to form a box against. Instead, it reuses the day
strategy's own FVG-finding logic directly (same pooled 5m/1m detectors,
`strategy.fvg` / `strategy.entry_fvg` -- no separate config section of its
own), just without the box/breakout gate in front of it:

1. Whichever large, unmitigated fair value gap -- 5-minute or 1-minute,
   pooled, same "whichever detector qualifies" pattern as the day
   strategy's own 5m/1m pooling -- forms first, in *either* direction, both
   sets the trade direction and anchors the move on the spot. There's no
   separate confirmation step; the FVG itself plays the role the 9:30
   breakout plays in the day strategy.
2. That FVG's own midpoint (or a shallower retracement,
   `entry_retracement_pct`, same as the day strategy) is where a limit
   order rests -- the anchor *is* the entry, exactly as in the day
   strategy once its own breakout has set direction.
3. Same 2:1 reward:risk and the same $40-$200 stop band
   (`compute_stop_target`, shared code) -- and the same reasoning for
   *not* including the anchor's own gap edges as stop candidates: entry
   sits at a fixed fraction of the anchor's own width, so its edges are a
   deterministic, not-really-structural distance from entry.
4. The anchor is kept "live" while waiting to fill -- a nearer/fresher
   unmitigated FVG in the same direction supersedes it, mirroring the day
   strategy's WAIT_FILL behavior, rather than freezing on the first pick.
5. **Stale-anchor abandonment (added 2026-07-09), your explicit
   instruction:** a real overnight session sat in `WAIT_FILL` for hours
   while price ran ~52 points past a SHORT anchor's own entry and kept
   going, without any fresher FVG ever qualifying to supersede it (the
   move was a steady grind, not a sharp displacement, so nothing new
   registered as a "strong" FVG) -- "we shouldn't be hedging our entire
   night of trading on the first FVG we see that is strong." Once price
   moves more than half the `max_stop_dollars` budget (`config.yaml`,
   currently 50 points at $200/2.0-point-value/1-contract) past the
   pending entry without ever retracing to fill it, the anchor is dropped
   (`AnchorRecord.outcome == "stale"`, counted in `stats["stale_abandoned"]`,
   shown in `--overnight`'s funnel print) and the strategy goes back to
   `WAIT_FVG` to hunt a fresh setup, rather than waiting on a now-stale
   level for the rest of the window. Checked after the existing
   nearer/fresher-FVG supersede check each bar, so a fresh anchor just
   picked that same bar (distance ~0) never spuriously trips it. Half the
   stop budget, not the full amount, was chosen deliberately: dropping
   well before price reaches a distance a stop at this level would even
   be allowed to sit at. Requires a real strong FVG to re-anchor
   afterward, same bar as before abandonment -- not loosened, at your
   explicit choice, so a dropped anchor doesn't just get replaced by a
   weaker one right away. Overnight-strategy-only scope for now (the day
   strategy already has its own, different invalidation -- a full
   reversal through the box's opposite edge -- for the same "don't keep
   chasing a broken thesis" purpose); ask if you'd like the same
   distance-based check added there too.
   - **Bug fixed the next day, 2026-07-09:** the first cut of this
     abandonment didn't exclude the dropped anchor from re-selection --
     real `bot.log` from a deployed session showed the *same* gap
     (`29911.75-29931.25`) marked `stale` roughly 15 times in a row, each
     time "live for 0:01:00": since it was still the nearest unmitigated
     candidate in the pool, `WAIT_FVG` immediately re-picked the exact
     same anchor next bar, which immediately re-tripped the same distance
     check -- an infinite pick/abandon loop on one gap that defeated the
     entire point of the feature (it never actually moved on to hunt
     anything fresh; you noticed because 19 hours of uptime produced zero
     trades). Fixed by adding the abandoned anchor's id to
     `_rejected_anchor_ids` before clearing it -- the exact same exclusion
     mechanism the pre-existing `no_valid_stop` rejection path already
     used correctly, which this one should have matched from the start.
     A regression test reproduces the loop against the un-fixed code (confirmed
     failing before the fix, passing after) to make sure this exact
     failure mode can't silently return.

**Revision history:** originally built 2026-07-05 as a two-stage
large-anchor (15m/30m) + nested-entry (5m/1m) design, resurrecting a
nested-FVG design this project had already tried and removed once (see git
history, commits `94270f2`..`d1e1bd4`, removed there for being the tightest
bottleneck in the *day* funnel: box breakout + large 15m anchor + nested 5m
entry, stacked, left almost nothing surviving to a fill). A real 30-day
`--overnight` backtest of that two-stage version reproduced the exact same
failure shape: 110 anchors formed, but only 8 ever got a nested entry (the
same funnel-bottleneck pattern), and the 4 that did fill went 0-4
(-$448.25). Replaced the same day with this single-stage design instead, at
your direct instruction ("use the same strategy for finding strong FVGs as
the 15 minute ORB strat, just without the ORB confluence layer") -- it's
the day strategy's own already-proven-against-real-NY-data logic, just
without the box gate, rather than a second attempt at the design that had
already failed twice.

A real 30-day `--overnight` backtest of *this* single-stage design (38
trades, 37% WR, +$291.25 net) showed a much healthier funnel (149 FVGs
found, 39 filled -- 26%, versus the two-stage design's 7%), but a sharp
split by how many trades happened on the same night: nights with exactly 1
trade went 4-2 (67% WR), nights with 2+ trades (reentries after a stop,
since a win already stands down for the night --
`reentry.allow_new_setup_after_win`) went 10-24 (29% WR), with one
particularly bad night (2026-06-09, 4 trades, all losses, -$508.50) on an
unusually wide-range night. Added `strategy.overnight.max_trades_per_night`
(2) so the strategy stands down for the rest of the night once its own
trade count hits the cap, regardless of `reentry.allow_reentry_after_stop`
-- deliberately a separate cap from the day strategy's own reentry
settings (shared config, not touched), since that's tuned against real
NY-session data and this is a different, still-developing window. Retune
(or remove) once a fresh backtest with the cap in place shows whether it
actually helps or was just reacting to one bad night in a 38-trade sample.

**Status: LIVE as of 2026-07-07**, alongside the day strategy, at your
explicit instruction ("I want the overnight piece to go live now, capped at
4 trades per night with the same risk tolerance" / "this account is a
combine so it isn't real money -- this a perfect live testing ground" /
"I want to constantly iterate as we trade"). You can still backtest it in
isolation via:

```
python -m src.backtest --overnight --days 30 --verbose --near-miss
```

`config.yaml`'s `strategy.overnight.enabled` now gates **both** the
`--overnight` backtest CLI mode and the live overnight slot in
`src/runner.py` -- flip it to `false` to pull the overnight strategy out of
live trading without touching anything else.

**How it runs alongside the day strategy:** `Runner` now manages both
strategies in parallel, each in its own `_StrategySlot` (`src/runner.py`)
with independent in-flight-trade bookkeeping -- entering, checking, or
closing a trade on one slot never touches the other's. The day slot force-
flattens at `session.flatten_by` (13:45 ET) same as always; the overnight
slot has no flatten deadline (`flatten_by=None`) -- an overnight trade that
outlives its own hunting window is left open and simply monitored until it
hits its own stop/target, matching `OvernightMomentumStrategy`'s own
documented design (see its `on_bar`'s `IN_TRADE` handling).

`risk_limits` (`max_trades_per_day`, `max_daily_loss_dollars`, `kill_switch`)
is a **single account-wide cap shared across both strategies** via one
`DailyRiskState` instance, not a separate budget per strategy -- they trade
the same funded account, so a trade or a dollar of loss from either one
counts against the same daily limit. One known, accepted wrinkle: this
shared state resets at midnight ET (`local.date()`), which falls in the
*middle* of a single continuous overnight window (Asia, pre-midnight, into
London, post-midnight) -- so an overnight session's own trade/loss count,
from the account-wide risk state's perspective, can reset partway through
a night even though `OvernightMomentumStrategy`'s own `max_trades_per_night`
cap (see below) does not. Not fixed here since it wasn't the ask and the
existing day-strategy-tuned risk state is the real funded-account safety
net; revisit if real overnight data shows this actually matters.

`strategy.overnight.max_trades_per_night` was raised from 2 to 4 at your
explicit instruction when going live -- **untested at this value.** The
only real backtest evidence (uncapped single-stage design: 38 trades, 37%
WR, +$291.25 net) showed nights with 2+ trades performing far worse (29%
WR) than single-trade nights (67% WR), which is exactly why a cap of 2 was
chosen and tested in the first place -- a fresh backtest at 4 was never run
before this went live. Watch real results closely; per your own "constantly
iterate" instruction, be ready to lower this back down (or retune
fvg/entry_fvg for this window specifically) if multi-trade nights keep
underperforming in practice.

**Removed entirely 2026-07-16, your explicit instruction** ("the per-day
limit on trades is a bad idea given we're just testing, remove the limit
entirely"), given after reviewing the first 7 live trades (2W/5L). Flagged
the same contrary backtest evidence above before making the change --
this is the second time this exact cap has been loosened against that
evidence (first 2->4, now to no cap at all) -- and you chose to proceed
anyway, reasoning that during this testing phase you'd rather gather data
on more trades than have the bot stand down early. `OvernightConfig.max_trades_per_night`
is now `int | None`; `null` in `config.yaml` means `notify_trade_closed`
skips the count check entirely and always allows reentry (subject to the
existing `reentry.allow_reentry_after_stop`/`allow_new_setup_after_win`
flags, unchanged). `max_daily_loss_dollars` and `kill_switch` remain the
only account-wide backstop regardless of how many trades happen in a
night. Revisit if multi-trade nights keep showing the same underperformance
this evidence predicts.

`strategy.fvg` / `strategy.entry_fvg`'s thresholds are tuned against real NY
Opening Range data, not Asia/London -- Asia/London's own volatility/gap
profile may not match NY hours at all, so a real `--overnight` backtest
result should be judged (and these thresholds retuned) on its own terms,
not assumed to transfer just because the code is shared.

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
  - **Confirmed live against a real account (2026-07-07), via a temporary
    DEBUG-level `signalrcore` logging pass:** the realtime hub connects and
    receives `GatewayTrade` events continuously and healthily -- the field
    names guessed in `_on_trade_event` (`price`, `volume`, `timestamp`) are
    exactly right, no fallback keys ever needed. But the *outer shape* of
    the handler's `args` was wrong: it's `[contractId, [tick_dict, tick_dict,
    ...]]` -- signalrcore calls the handler with the raw hub message
    `arguments` list untouched, and for `GatewayTrade` that list is
    `[contractId, ticks]`, not a flat list of tick dicts. The previous
    "`isinstance(args, list)` else `[args]`" logic treated that whole
    2-element list as the events list, so every real tick was silently
    dropped forever: neither the contract-id string nor the nested ticks
    list itself is ever a `dict`, so both elements failed the `isinstance
    (event, dict)` check and were skipped. **No bars were ever built from
    live ticks as a result** -- despite the connection looking completely
    healthy, printing no errors or warnings, and (per the systemd `active
    (running)` status) never crashing. This is exactly the kind of bug the
    "unverified, watch the terminal" plan above was meant to catch, and it
    didn't: `_on_trade_event`'s own missing-price warning only checks
    individual *events* for a missing key, not whether the outer envelope
    was ever unpacked into individual events correctly in the first place.
    Fixed in `_on_trade_event` to unpack `args[1]` when the shape matches;
    regression test in `tests/test_projectx_gateway.py` using the real
    payload shape observed live, verified via revert-and-confirm.

  Still **unverified** -- the docs portal 403's an unauthenticated fetch,
  so these need a live check once you're logged in.
  - The exact response envelope key for `/Order/searchOpen` (assumed
    `"orders"`).
  - Whether `linkedOrderId` makes the gateway auto-cancel the sibling
    bracket leg. Not relied upon either way -- `poll_order_status()`
    explicitly cancels the sibling leg itself once one fills.

  **Entry order type (fixed 2026-07-04, before the first live session):**
  the entry leg was originally a MARKET order, placed once `Runner.on_bar`
  detected (from a closed bar) that price had touched `entry_price` --
  but every stop/target/R:R calculation in this bot assumes entry happens
  at that *exact* price, and a market order pays whatever price is
  current when it executes, which can be meaningfully different given
  the strategy only reacts once a full 1-minute bar has closed (up to
  ~60s after the actual touch). Found and fixed at the user's explicit
  direction before allowing real orders on their account -- the entry leg
  is now a real LIMIT order at `entry_price`. This can still simply fail
  to fill if price already moved on by the time the order reaches the
  exchange; `place_bracket_order` returns `None` in that case (not an
  exception) rather than chasing with a worse-priced market order, and
  `Runner._enter_trade` calls `strategy.notify_entry_not_filled()` so the
  state machine goes back to hunting instead of getting stuck believing
  it's in a trade that doesn't exist (see that method's docstring --
  `on_bar` moves to `IN_TRADE` the instant it returns a signal, since in
  backtest a signal always means a real fill; live, it doesn't
  necessarily). This is a stopgap for a real, known limitation, not a
  complete fix: a fully correct implementation would place the resting
  order the moment an anchor is picked (before any bar confirms a touch)
  and react to the exchange's own fill notification, removing the ~60s
  detection lag entirely -- not done here, a real follow-up once the
  current fix has been watched run for a while.

  **Realtime hub reconnect used a stale auth token (fixed 2026-07-07):**
  found while walking the user through checking `logs/bot.log` after the
  first live session -- a bot that had been running continuously for over
  a day (per `journalctl`'s own uptime accounting) had logged an unbroken
  stream of signalrcore's own reconnect-failure message ("Socket closed by
  the the server" -- a real typo baked into that library itself, not ours).
  Root cause: `subscribe_bars` built the SignalR hub URL with
  `access_token={self._token}` baked in once, then relied on
  `with_automatic_reconnect`, which retries against that *exact same URL*
  forever -- so once the auth token itself expires, every retry is
  rejected by the server and reconnect can never succeed again, no matter
  how long it keeps trying. The process itself never crashes (systemd
  reports it as `active (running)` throughout), so this fails silently:
  the bot goes permanently deaf to real-time market data mid-session with
  no visible error apart from a repeating log line easy to mistake for
  harmless reconnect chatter.

  First fix attempt (same day) removed `with_automatic_reconnect` and
  added an `on_close` handler (`_on_hub_closed`) that re-authenticates and
  rebuilds the hub -- redeployed, and the *exact same* endless "Socket
  closed" spam continued for 20+ minutes with zero corresponding output
  from that handler, meaning it was never actually being invoked for this
  failure. Traced into `signalrcore`'s own source: the observed failure
  path is a periodic keepalive ping's `send()` failing on an already-dead
  socket (`websocket_transport.py`'s `send()`), which -- when no
  reconnection handler is configured, exactly the state my first fix put
  it in -- logs the warning and `raise`s directly, never reaching the
  hub-level `on_close` callback this class hooks at all. So the very
  first attempt at "detect the failure and react to it" was defeated by
  signalrcore's own internal state machine taking a different path than
  expected, on a real, once-deployed, and re-tested basis.

  Given that a reactive, callback-based fix had already failed once in
  practice, the actual fix abandons trying to reliably detect *any*
  failure mode and instead runs a plain, unconditional background timer
  (`_start_realtime_refresh_thread`, `REALTIME_REFRESH_INTERVAL_SECONDS`,
  currently 1 hour -- an ASSUMPTION, since the real token lifetime is
  itself unverified) that re-authenticates and tears down/rebuilds the
  hub on a fixed schedule regardless of whether anything ever appears to
  go wrong. `with_automatic_reconnect` and the `on_close`/`on_error`
  handlers are still kept (transient blips should still recover quickly
  and their diagnostic prints are still useful), but the scheduled timer
  -- not any callback -- is the thing actually relied upon to guarantee
  the token never gets stale enough to cause a permanent failure.
  Verified via revert-and-confirm on both the reconnect-with-fresh-token
  logic and the thread actually being wired up from `subscribe_bars` (see
  `tests/test_projectx_gateway.py`) -- both reverts caused a real test
  failure.

  **Postscript, same day:** the "endless reconnect-failure spam" that
  motivated all of the above turned out to be a red herring for a
  different reason than any of it addressed -- repeated `tail`s of
  `logs/bot.log` kept showing the same spam even against a freshly
  restarted process with all these fixes in place, which pointed at
  something more fundamental. Enabling temporary DEBUG-level
  `signalrcore` logging (see the `GatewayTrade` note above) revealed the
  connection was actually healthy the whole time, receiving real ticks
  continuously -- what looked like fresh evidence of the bug across
  several exchanges was, each time, stale terminal scrollback from a much
  earlier command still visible above the actual (empty, or differently-
  problematic) output of whatever had just been run, compounded by a
  separate, unrelated permission issue (`bot.log` was owned by `root` from
  being run manually before the `tradingbot` systemd setup existed, so
  `tail`/`truncate` run as `tradingbot` silently failed or errored). None
  of the reconnect work above was wasted -- the token-refresh timer is
  still real, useful insurance for whenever the token does eventually
  expire, and the on_open/on_error/on_close diagnostics are what actually
  let this get untangled -- but the *original* symptom that kicked off
  this whole chain was never actually live evidence of anything currently
  wrong. The real, still-live bug this session was masking is the
  `_on_trade_event` payload-shape bug documented above, which silently
  dropped every real tick with no error at all.

  **A real live entry order was rejected, and the bot never traded again
  that day (found and fixed 2026-07-08):** the user noticed zero trades on
  a day the bot had clearly been running and receiving live bars. `grep`
  of `logs/bot.log` (not `journalctl` -- the app's own prints go to the
  file the systemd unit redirects to, not the journal, which cost one
  wasted round-trip before checking the right place) turned up two real
  entry attempts that both failed the same way:
  ```
  [LIVE] placing entry LIMIT long x1 @ 29707.375
  Receive error: 400 Client Error: Bad Request for url: https://api.topstepx.com/api/Order/place
  ```
  Root cause, found by tracing what happens after that: `_enter_trade`
  (in `src/runner.py`) called `place_bracket_order`, which raised
  `HTTPError` uncaught. By that point the strategy's own `on_bar` had
  already optimistically set `self.state = State.IN_TRADE` (same as the
  documented not-filled case), but the exception meant `current_trade`
  never got created and `notify_entry_not_filled()` never got called --
  so the strategy was stuck at `IN_TRADE` **forever**, with the dashboard
  showing `state: IN_TRADE, in_trade: false` for the rest of the process's
  life. This happened to both the day and overnight strategies
  independently over the course of the day. Fixes, all confirmed via
  revert-and-confirm:
  - `_post` now prints the response body before `raise_for_status()` --
    previously the API's own explanation of what was wrong with the
    request (a 400's body) was thrown away entirely, visible nowhere.
    Exact request-schema bug still not identified (needs the actual body
    text from a live 400, which nothing before this printed).
  - `_StrategySlot._enter_trade` now wraps the broker call in
    `try/except`, treating *any* failure -- not just a clean `None`
    return -- the same as "didn't fill": tell the strategy to keep
    hunting instead of getting stuck.
  - `place_bracket_order` now flattens immediately if placing the stop or
    target leg fails *after* the entry already filled -- previously that
    would leave a real, naked, unprotected position on the exchange while
    the runner believed no trade was ever taken.
  - `OvernightMomentumStrategy` was missing `notify_entry_not_filled`
    entirely (only the day strategy had it) -- any not-filled/failed entry
    there would have raised `AttributeError` instead of resetting state.
    Added, mirroring the day strategy's version.

  **A second, independent bug found while fixing the first:** reading
  signalrcore's own source (`base_socket_client.py`'s `run()`) while
  investigating why the 400 error printed but nothing after it did,
  turned up something more serious than the one bad order: an exception
  escaping the `on_message` callback chain (i.e. anything raised inside
  `Runner.on_bar`) makes that receive loop log the error, set
  `self.running = False`, and return -- **silently killing that hub
  connection's thread**, with no reconnect triggered via any of the normal
  paths, until the next scheduled hourly refresh (or forever, if the same
  bug fires again right after). This means *any* bug anywhere in the
  strategy pipeline didn't just fail one signal -- it could silently take
  the bot deaf to real-time data for up to an hour. Fixed by wrapping
  `Runner.on_bar`'s entire body in a top-level `try/except` that logs and
  swallows anything unexpected, so a trading-logic bug can never again
  take down the data feed itself.

  **A third bug, also found via the same investigation:** a
  `RuntimeError: deque mutated during iteration` also appeared in
  `logs/bot.log`, meaning `Runner._recent_bars` (the live chart snapshot
  buffer, added 2026-07-07) was being appended to and iterated from more
  than one thread at once -- i.e. more than one realtime hub connection
  was alive and delivering ticks concurrently, most likely because
  `_reconnect_hub`'s `self._hub.stop()` on the old connection doesn't
  reliably kill its underlying receive thread (unconfirmed which of
  signalrcore's several teardown paths is actually failing). Two fixes:
  - `Runner._recent_bars_lock` now guards every read and write of
    `_recent_bars`, so concurrent access from any number of threads can no
    longer corrupt it. Confirmed via a test that forces the exact same
    `RuntimeError` reliably without the lock (needs an artificially short
    `sys.setswitchinterval` to reproduce reliably in a short test run --
    without it the race is real but doesn't reliably manifest in time).
  - Each hub connection's `GatewayTrade` handler now captures the
    generation number (`self._hub_generation`, incremented in
    `_start_hub`) it was built with, and silently drops events once a
    newer hub has superseded it -- so even if an old connection's thread
    does survive a reconnect, it can no longer race a shared aggregator
    with whichever hub is actually current.

  **The exact request-schema bug flagged above, finally identified
  (2026-07-09):** the user noticed 19 hours of continuous uptime with
  zero trades, despite `bot.log`'s new anchor-outcome logging (see the
  stop-loss placement section) showing several `anchor ended (filled)`
  events for the day strategy that day -- meaning the strategy's own
  state machine believed real entries had happened, but
  `trades/trades.csv` had no rows at all beyond its header. Grepping
  `bot.log` for `WARNING`/`Order/place` (the response-body print added in
  the first bug above) finally surfaced the actual 400 body on every
  single order-placement attempt:
  ```
  {"errors":{"$.accountId":["The JSON value could not be converted to System.Int32. ..."]}}
  ```
  Root cause: `self.account_id = account_id or os.environ.get("PROJECTX_ACCOUNT_ID")`
  -- `os.environ` values are always `str`, and that string went straight
  into every order's JSON body (`{"accountId": self.account_id, ...}`),
  but the Gateway API requires `accountId` as a JSON integer, not a
  quoted string. This meant **every single order placement had been
  failing from the very first live session** -- not a timing/fill-window
  issue as first suspected, not the stale-anchor loop (a separate, real
  bug fixed the same day, see the overnight momentum strategy section),
  just a wrong JSON type on every request, silently caught by
  `_StrategySlot._enter_trade`'s existing try/except (from the first bug
  above) and treated as an ordinary "didn't fill," so nothing ever
  surfaced as an error to the dashboard or the user -- it looked
  identical to the strategy simply not finding a good setup. Fixed by
  converting `account_id` to `int` once in `connect()`, right after
  confirming it's set, so every `_post` call downstream sends a real
  integer; raises a clear `RuntimeError` if it isn't numeric at all
  rather than silently misbehaving. This is very likely the actual
  explanation for *why no trade had ever been taken live at all*, across
  every session up to this point -- everything before it (stop-rule
  tuning, the restart-storm fix, the anchor-outcome logging, the
  stale-anchor abandonment feature and its own infinite-loop bug) was
  real, necessary work, but none of it could have produced a trade while
  this sat underneath, unnoticed, the whole time.

  **Immediately caused a second, worse outage (found and fixed the next
  day, 2026-07-10):** the `int()` conversion above raises if
  `PROJECTX_ACCOUNT_ID` isn't a plain number -- and the value actually in
  `.env` was the account's *display label* (`"50KTC-V2-69346-54207896"`,
  the TopstepX-style account name), not the Gateway's internal numeric
  account ID. So `connect()` now raised on every single call, which
  propagates all the way up through `main()` uncaught -- crashing the
  whole process at startup, every time, before `Runner.start()` ever
  subscribed to a single bar. `Restart=on-failure` / `RestartSec=10` just
  restarted it into the identical crash every 10 seconds, forever. The
  user noticed via a plain `grep -iE "warning|error|traceback"
  logs/bot.log` turning up the raw `ValueError`/`RuntimeError` -- this had
  been silently crash-looping since the moment the previous fix deployed,
  meaning the bot did not process a single live bar in that entire
  window. Fixed by resolving a non-numeric `account_id` automatically:
  `connect()` now falls back to `POST /Account/search` (same naming
  convention as this file's other endpoints, `/Order/searchOpen` /
  `/History/retrieveBars` -- UNVERIFIED, so the raw response is printed
  the first time, same defensive pattern as those), matching the
  configured label against each returned account's name and using its
  numeric `id`. Raises a clear, actionable `RuntimeError` (not a bare
  traceback) if no match is found, rather than crash-looping again.
  **Confirmed working live the same day**: the real `/Account/search`
  response matched the assumed shape exactly (`{"accounts": [{"id":
  ..., "name": ..., ...}], "success": true, ...}`), resolving the
  configured label to its correct numeric id among two accounts
  returned. No field-name adjustment needed.

  **A third, quieter version of the same failure shape (found and fixed
  2026-07-21):** after the two bugs above were fixed, trades did start
  executing live -- but the user noticed `trades/trades.csv`'s row count
  stuck at 7 for five straight days despite the bot staying up the whole
  time (`status.json`'s `process_started_at` unchanged since 2026-07-16,
  ruling out the restart-storm bug recurring). `bot.log` told a different
  story than "no setups found": `anchor ended (filled)` fired 12 times for
  both strategies combined in just the 2026-07-19 to 2026-07-21 window
  alone, and `placing entry LIMIT ...` matched all 12 -- but only 2 ever
  got a real `entry order placed: id=...` confirmation back, and only 1
  ever hit the (legitimate, by-design) 20-second fill timeout. The other
  10 all raised inside `place_bracket_order`, caught by `_enter_trade`'s
  existing try/except and silently treated as an ordinary "not filled"
  case -- identical to a normal missed fill from the dashboard's or the
  logs' point of view. The actual traceback, once pulled via `sed` on the
  exact line number:
  ```
  RuntimeError: /Order/place failed: Specified custom tag is already in
  use: custom tag should be unique per account
  ```
  Root cause: `place_bracket_order`'s `bracket_id` was a plain in-memory
  counter (`self._next_bracket_id`, starting at `1` in `__init__`),
  embedded directly into each order's `customTag` (`"entry-1"`,
  `"stop-1"`, `"target-1"`, ...). The gateway enforces `customTag`
  uniqueness **per account, forever** -- not per process, per day, or per
  session. Since the counter resets to `1` on every single restart of the
  bot (and this account has been restarted many times across this
  project's history -- the restart-storm bug, the two `accountId` bugs
  above, ordinary deploys), a fresh process's low bracket-id values are
  near-guaranteed to collide with a tag some earlier process already sent
  to this same account, and it fails deterministically until the counter
  happens to climb past whatever the account's historical high-water mark
  is. This explains far more than the missing 5 days: it means the
  majority of every strategy's real entry signals, across every session
  since going live, have likely been silently eaten by this, not by
  "price moved on" as the surrounding code's own comments assumed. Fixed
  by generating `bracket_id` as `uuid.uuid4().hex[:16]` per order instead
  of an incrementing counter -- unique regardless of how many times the
  process has restarted, so it can never collide with the account's own
  tag history again. Regression test
  (`test_bracket_id_customtag_survives_a_process_restart_without_colliding`,
  `tests/test_projectx_gateway.py`) simulates a restart as a brand-new
  broker instance and asserts its customTags never overlap with an
  earlier instance's -- confirmed to fail against the old counter-based
  code and pass against the fix.

  **A fourth version of the same failure shape, unrelated cause this
  time (found and fixed 2026-08-07):** the user noticed no trades for 2
  days after scaling to 3 contracts and asked why. `systemctl status`
  confirmed both services healthy with no restart-storm recurrence.
  `bot.log`'s anchor-outcome breakdown (`grep -oE "anchor ended
  \([a-z_]+\)" | sort | uniq -c`) showed 14 `filled` outcomes but
  `trades.csv` had exactly 1 new row in that whole window -- the same
  "real signal, no logged trade" shape as the customTag bug, but that
  fix (uuid4 bracket_id) was still in place and unrelated. Lining up
  each `filled` anchor's timestamp against `grep -n "order placement
  raised\|did not fill within the timeout"` accounted for all but one:
  4 legitimate 20-second fill timeouts (price moved on, working as
  designed) and 8 exceptions inside `place_bracket_order` -- and
  crucially, **3 of those 8 predated the 2026-08-06 contract-size
  deploy** (as early as 2026-08-04), ruling out the 3-contract change as
  the cause before it was ever seriously considered. The actual
  traceback:
  ```
  RuntimeError: /Order/place failed: Invalid limit price. Price is not
  aligned to tick size.
  ```
  Root cause: neither `entry_price` (`_entry_price` in strategy.py/
  overnight_strategy.py -- `gap_high - pct*width`, a plain float
  retracement into a real FVG) nor `target_price`
  (`compute_stop_target` -- `entry_price +/- stop_points*reward_risk_ratio`)
  was ever rounded to the instrument's `tick_size` (0.25 for MNQ, already
  present in `config.yaml` but never once referenced anywhere in `src/`)
  before being sent to the gateway as a LIMIT price. A gap's exact
  midpoint is only tick-aligned when the gap's own width happens to be an
  even number of ticks -- roughly half the time by chance -- which
  matches the observed failure rate almost exactly (8 of 14, ~57%).
  Confirmed with the real numbers: gap `29568.75`-`29578.00`'s midpoint
  is `29573.375`, not a multiple of `0.25`; the one anchor that *did* log
  a trade (gap `28591.00`-`28613.00`) has a cleanly aligned midpoint of
  `28602.0`. This bug predates the contract-size scale-up entirely and
  had likely been silently costing roughly half of every real entry
  signal, on both strategies, since whenever the retracement/target math
  was first written -- not something introduced by any recent change.
  Fixed with a new `round_to_tick(price, tick_size)` helper in `risk.py`
  (`round(price / tick_size) * tick_size`), applied at both source
  points: `_entry_price` in both strategy files now rounds its result
  before returning, and `compute_stop_target` (now taking `tick_size` as
  a required parameter, threaded through all 5 call sites --
  `runner.py`, `backtest.py` x2, `strategy.py`, `overnight_strategy.py`)
  rounds `stop_price` before computing `stop_points`/`target_points` from
  it (so those stay internally consistent with what's actually sent to
  the broker) and rounds `target_price` itself before returning. Five new
  regression tests in `tests/test_risk.py` cover `round_to_tick` directly
  plus `compute_stop_target`'s rounding of both stop and target --
  confirmed to fail against a reverted (`return price`, unchanged)
  version of `round_to_tick` and pass against the fix. Existing tests in
  `test_strategy.py`/`test_backtest.py`/`test_overnight_strategy.py` use
  synthetic prices that aren't real-tick-aligned by coincidence (e.g.
  `107.2`) -- pinned `cfg.instrument.tick_size = 0.01` in each file's own
  `load_test_config()` (same isolation pattern already used there for
  `min_stop_dollars`/`contract_size`/etc.) so those files' own exact-value
  math stays isolated from the real `0.25` tick, which is covered
  specifically in `test_risk.py` instead.
- `src/strategy.py` -- the state machine implementing steps 1-10 above.
- Note: `src/session_levels.py` also computes a 15-minute-candle "zone"
  around each level (`previous_day_high_zone`, etc.) -- this is a leftover
  from the v2 overlap design above and is no longer used by any entry
  logic. It's kept only because the charts (backtest.py --chart-html and
  the dashboard) still shade it as a band for visual context, showing
  which candle actually set the previous day's high/low.
- `src/risk.py` -- stop/target/size calculation described above.
- `src/swing_points.py` -- `SwingPointTracker`, the 1-minute break-of-
  structure stop fallback (added 2026-07-08, see the stop-loss placement
  rule above): a standard N-bar fractal pivot over the raw 1-minute bar
  stream, tracking only the most recent confirmed swing high/low. Reset
  at day/night session boundaries by both strategies, same as their FVG
  detectors' `clear_active_gaps`.
- `src/session_levels.py`, `src/opening_range.py`, `src/fvg.py` -- level
  marking, box tracking, and FVG detection respectively.
- `src/runner.py` -- wires it all together into a run loop. Runs the day
  strategy and the Asia/London overnight strategy in parallel (see
  `_StrategySlot`), each with its own independent trade bookkeeping, sharing
  one broker connection and one account-wide `DailyRiskState`.
- `src/overnight_strategy.py` -- the Asia/London overnight momentum
  strategy described above. Live in `runner.py` alongside the day strategy
  as of 2026-07-07; also independently backtestable via `src/backtest.py
  --overnight`.
- `src/dashboard.py` / `src/dashboard_template.html` -- always-on local
  HTTP dashboard (binds `127.0.0.1` only, viewed via SSH port-forward, see
  DEPLOY.md), with three data sources:
  - Live trades: `trades/trades.csv` read fresh on every request (cheap,
    local, no API cost) -- closed trades only, since a trade is only ever
    logged once it exits.
  - Periodic backtest: calls the real TopstepX API, so it's only refreshed
    on a background timer (`dashboard.refresh_interval_seconds`), not per
    request.
  - **Bot activity (added 2026-07-07):** requested by the user after going
    live overnight, since the dashboard otherwise only shows *closed*
    trades and gives no sense of whether the bot is actually alive and
    hunting between them. `Runner._write_status` writes `trades/status.json`
    after every bar (wrapped in `try/except OSError` -- a failure to write
    this file is dashboard-only and must never take down live trading) with
    each strategy's `status_snapshot()` (state name, direction, anchor gap
    bounds, pending limit price -- a plain read-only view with no effect on
    trading decisions) plus whether that slot currently has an open trade.
    `src/dashboard.py`'s `read_status()` tolerates a missing file (bot
    hasn't processed its first bar yet) and a torn/mid-write read (the
    write isn't atomic) by returning `None` in both cases, same pattern as
    `read_live_trades()`. Served at `/api/status.json` and rendered as a
    "Bot activity" card per strategy, polled every 10s alongside live
    trades (both are free local file reads; only the backtest section is
    on the slower, API-cost-aware timer).
  - **Live P&L, performance stats, and a live chart snapshot (added
    2026-07-07):** the user asked for active P&L in the bot-activity
    boxes, a chart snapshot, and overall profitability markers, so the
    bot going live overnight wasn't just visible but actually legible day
    to day.
    - Trades are now tagged with which strategy took them:
      `TradeLogger.log_trade` takes a `strategy` ("day"/"overnight")
      argument (`_StrategySlot` knows its own name and passes it), and a
      new `_migrate_header_if_needed` rewrites just the header line of any
      `trades.csv` written before this change (including the live one
      already deployed) in place -- old data rows are left untouched and
      read back with `strategy=None` -> `"unknown"`, never a crash.
    - **`stop_source`/`stop_fvg_size` added 2026-07-14**: the user asked
      to analyze real losing trades for a pattern and iteratively correct
      -- but `trades.csv` had no way to tell which stop rule (fvg/swing/
      cap) produced any given trade's stop, the exact diagnostic
      `EntrySignal` had already been carrying since `risk.py`'s
      `StopCandidate` was added for backtest's `--verbose` output. `Trade`
      (`models.py`) gained the same two fields (defaulting to `"unknown"`/
      `None` for backward compatibility); `_StrategySlot._enter_trade`
      populates them from the signal; `_migrate_header_if_needed` now
      compares the whole header against the current one (not just
      checking for `"strategy"`) so it also migrates files stuck on the
      previous partial header. `read_live_trades` parses both, so the
      dashboard's `/api/live-trades.json` carries this too, not just
      `--verbose` backtests.
    - `Trade.unrealized_pnl_dollars(current_price, point_value)` mirrors
      `pnl_dollars` but marks to a live price instead of `exit_price`.
      `_StrategySlot.status_for_dashboard` uses it (mark-to-last-bar-close,
      not a broker-confirmed price) and also now exposes the open trade's
      real `entry_price`/`stop_price`/`target_price`, not just `in_trade`.
    - `Runner` keeps a rolling `deque` of the last `RECENT_CANDLES_MAXLEN`
      (180, ~3 hours) 1-minute bars and writes them into `status.json` as
      `recent_candles` -- a shared, top-level field (one price series for
      both strategies), not per-slot.
    - `src/dashboard.py`'s `compute_trade_stats` computes trade count, win
      rate, net P&L, avg win/loss, profit factor, and best/worst trade --
      overall, for "today" (in the bot's own session timezone, not the
      server's, so it lines up with when the strategies actually reset),
      and broken out per strategy -- fresh from `trades.csv` on every
      request, served at `/api/stats.json`, rendered as a new
      "Performance" section.
    - **"Since the customTag fix" slice (added 2026-07-22, your request):**
      after the customTag-collision bug was fixed (see the broker section
      above -- it had been silently eating roughly 10 of every 12 real
      entry signals), you said "I'll wait until we have a large enough
      sample to increase contract size" rather than scaling off stats
      still contaminated by that bug. `compute_trade_stats` now also
      returns a `since_fix` slice filtered to
      `entry_time >= BRACKET_ID_FIX_DEPLOYED_AT` (2026-07-22T01:18:37 UTC,
      the actual live deploy time confirmed via `systemctl status
      trading-bot`'s "Active" line, not just the commit timestamp), plus a
      `sample_target` (25, a placeholder -- adjust freely, it's not
      enforced anywhere, purely a visual marker) so the dashboard's new
      "Since the customTag fix" card shows a progress bar toward that
      target instead of you having to eyeball dates in `trades.csv`.
      `position_sizing.scaling: manual_only` still means nothing in code
      actually blocks scaling early -- this is a visibility aid for your
      own manual decision, not a guardrail.
    - **Scaled to `contract_size: 3` anyway, 2026-07-31, your explicit
      decision:** exactly the guardrail-not-a-block case above -- at the
      time you asked to scale, the `since_fix` slice was 16 of the 25-trade
      target, 37.5% WR, net **-$98** (running at a loss, not the proven
      win rate `position_sizing.scaling`'s comment originally expected
      before scaling). Flagged clearly before proceeding; you chose to
      scale anyway ("I'm happy with the testing so far... lets move from
      one micro contract to 3 micro contracts per trade").
      - Raising `contract_size` alone would **not** have actually 3x'd
        dollar exposure: `max_stop_dollars`/`min_stop_dollars` are fixed
        *total* dollar caps per trade, converted to points via
        `/ (point_value * contract_size)` (see risk.py's
        `compute_stop_target`) -- so at 3x the contracts with the caps
        left at $200/$40, the allowed stop distance in points would have
        shrunk to a third (100pt -> ~33pt), keeping total dollar risk per
        trade roughly where it already was at 1 contract, not 3x it.
        Flagged this before proceeding; you confirmed you wanted real 3x
        dollar exposure on the same real structural stops the strategy has
        been taking, which requires scaling the dollar caps too.
      - Your own numbers ("scale the TP up to $500 and the stop loss up to
        $300 as maximums for both") don't fit the existing fixed 2:1
        `reward_risk_ratio` exactly (a max-sized $300 stop at 2:1 gives a
        $600 target, not $500) -- flagged this too; you chose to change
        `reward_risk_ratio` to `1.67` (500/300) rather than keep 2:1, so a
        max-sized real stop now produces a target close to $500. Target is
        still always exactly `reward_risk_ratio` x whatever the real
        per-trade stop distance is (there's no independent target cap) --
        a tighter real stop still produces a proportionally smaller
        target, not a flat $500 regardless of the stop.
      - Net result: `max_stop_dollars: 200 -> 300` (a deliberate
        *tightening* in points at the new contract_size, not a
        proportional 3x -- 200/2.0/1=100pt before, 300/2.0/3=50pt now,
        half the old point-based cap, since you chose $300 over the
        proportional $600), `min_stop_dollars: 40 -> 120` (scaled a full
        3x, preserving the original 20-point floor:
        40/2.0/1=20pt, 120/2.0/3=20pt), `reward_risk_ratio: 2.0 -> 1.67`,
        `contract_size: 1 -> 3`. Structural stop *selection* itself
        (`find_structural_stop_price` -- nearest strong 5m FVG or 1m
        break-of-structure swing point) is completely unchanged; only the
        $ budget that filters which real stops qualify moved.
      - Several tests (`tests/test_strategy.py`, `tests/test_backtest.py`)
        had assertions written against the real config's old
        `contract_size=1`/`max_stop_dollars=200`/`reward_risk_ratio=2.0`
        without pinning them explicitly -- fixed by pinning those values
        in each file's own `load_test_config()`, the same isolation
        pattern already used there for `min_stop_dollars`/
        `entry_retracement_pct`, so this file's own test math stays
        correct regardless of further live tuning.
    - The template's "Bot activity" section gained a candlestick chart
      (`buildLiveChart`, a simpler sibling of the backtest's `buildChart`
      -- no anchor-zone/previous-day-zone overlays, since those are day-
      strategy-specific) built from `recent_candles`, overlaid with
      entry/stop/target lines for whichever strategy slot(s) currently
      have an open trade (labelled by strategy name, since both could be
      open at once).
  - **Process uptime indicator (added 2026-07-09):** the actual root cause
    of a "why didn't we take a real trade" report -- the live bot was
    being restarted (via `bot-pull`/`systemctl restart`) far more often
    than intended, most likely by earlier Claude Code sessions treating a
    full redeploy+restart as a routine status check rather than reserving
    it for an actual new commit. Every restart silently wipes all
    in-memory state (`Runner` keeps no historical backfill at all -- see
    `runner.py`'s `main()`/`start()`, which only subscribes to live bars),
    including the opening-range box, both FVG detector pools, and the
    swing-point tracker, none of which are persisted anywhere. With
    restarts happening every 10-20 minutes in the worst observed stretch
    (confirmed via `journalctl -u trading-bot`, cross-referenced against
    `/root/.bash_history` and `auth.log` login timestamps, all from the
    same source), the bot never accumulated enough uninterrupted live
    history to recognize a setup at all -- it looked like "the strategy
    isn't finding trades" when the real cause was infrastructure, not
    strategy logic, and there was nothing on the dashboard to reveal it.
    `Runner.__init__` now records `self.process_started_at` once at
    startup; `_write_status` includes it as `process_started_at` in
    `status.json`. The template shows it as a `bot up Xh Xm` badge next to
    the existing `last bar Xm ago` freshness indicator, styled red
    (`.freshness.warn`) whenever uptime is under `UPTIME_WARN_SECONDS`
    (20 minutes -- roughly how long the 5m FVG detector needs to bucket
    its first candles and the swing tracker needs to confirm its first
    pivot), with a tooltip explaining why a fresh restart means "give it
    time" rather than "something's broken." No trading logic changed --
    purely a visibility fix so a restart is obvious at a glance instead of
    requiring a `journalctl` investigation.
  - **Live anchor-outcome logging (added 2026-07-09):** a follow-up
    visibility gap found the same evening -- `WAIT_FILL` was observed
    reverting several times live with no way to tell why. Both strategy
    classes already record every anchor's fate
    (filled/superseded/invalidated/no_valid_stop/session_ended) in
    `anchor_history` purely for backtest's `--near-miss` reporting, but
    nothing printed that live, so there was no `bot.log` trail to check
    after the fact. `_StrategySlot.on_bar` (`runner.py`) now snapshots
    `len(strategy.anchor_history)` before calling the strategy's own
    `on_bar`, and prints every record appended since (direction, gap
    bounds, outcome, how long the anchor was live) -- the strategy classes
    themselves stay unaware of whether they're running live or backtest,
    same as before; this is purely an addition at the runner layer that
    diffs a list they were already maintaining.
    - **Timestamp added, 2026-07-10:** these lines had no timestamp at
      all, just a duration ("live for 0:01:00") -- when the user asked
      whether tonight specifically had missed any FVGs, there was no way
      to tell tonight's anchors apart from any other night's in the same
      `bot.log` (only rotated weekly, so it can hold days of mixed
      history). `AnchorRecord` already carries `ended_at`; it just wasn't
      being printed. Now prefixed to every line
      (`record.ended_at.isoformat()`), so a plain `grep`/`tail` can
      isolate one night's actual events instead of guessing from
      surrounding context (or, in the meantime, bounding by the line
      number of the process's own startup print, `"...strategy is LIVE
      alongside..."`, which appears once per restart).

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
