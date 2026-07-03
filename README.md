# Trading Bot -- NQ/MNQ NY-Open Opening Range Breakout

Automates a specific morning strategy on the Micro Nasdaq (MNQ) using the
TopstepX ProjectX Gateway API: mark prior-session levels, wait for the 9:30-
9:45 ET opening range to break and retest, enter on a strong 1-minute fair
value gap, and manage the trade with a 2:1 reward:risk bracket.

**Read [STRATEGY.md](STRATEGY.md) first.** It documents exactly what's
implemented and flags every place an assumption was made instead of an
explicit rule, so you can correct anything that doesn't match how you
actually trade before running this live.

This bot needs to run continuously, so it belongs on an always-on server,
not your laptop or this chat. See **[DEPLOY.md](DEPLOY.md)** for step-by-step
Hetzner VPS setup (systemd service, firewall, log rotation).

## Status

- Strategy logic, risk/stop-target calculation, and a mock broker for
  testing are implemented and covered by unit tests.
- The real broker (`src/broker/projectx_gateway.py`) is implemented against
  the ProjectX Gateway API (auth, contract lookup, real-time bars via
  SignalR, bracket order placement, fill/flatten). It defaults to
  `dry_run: true` (`config.yaml: broker`) -- it logs what it would do
  instead of sending real orders. See STRATEGY.md for exactly what's
  confirmed vs. still unverified.
- Nothing in this repo places a real order until you set your credentials
  in `.env` *and* flip `dry_run: false` after verifying the unverified
  pieces.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in PROJECTX_USERNAME / PROJECTX_API_KEY / PROJECTX_ACCOUNT_ID once you have them
```

## Run the tests

```bash
pytest tests/ -v
```

## Project layout

```
config.yaml               all tunable parameters (session times, risk, sizing)
src/
  config.py               loads config.yaml into typed dataclasses
  models.py                Bar / Trade / Direction
  session_levels.py        previous day / Asia / London high-low tracking
  opening_range.py         9:30-9:45 ET box tracker
  fvg.py                    1-minute fair value gap detector
  strategy.py               the breakout/retest/FVG state machine
  risk.py                   stop/target calculation, daily risk limits
  logger.py                 CSV trade log (for judging win rate before scaling up)
  runner.py                 wires broker -> strategy -> risk -> broker -> logger
  broker/
    base.py                 abstract broker interface
    mock_broker.py           simulated broker, used by tests
    projectx_gateway.py      real broker -- implemented, dry-run by default
tests/                      unit + integration tests, all using the mock broker
```

## Next steps (once you have API access)

1. Fill in `.env` (`PROJECTX_USERNAME`, `PROJECTX_API_KEY`, `PROJECTX_ACCOUNT_ID`).
2. Log into https://gateway.docs.projectx.com/ and check the "still
   unverified" list in STRATEGY.md / the docstring at the top of
   `src/broker/projectx_gateway.py` -- mainly the `GatewayTrade` payload
   field names and the `/Order/searchOpen` response envelope key. Adjust
   the code if they differ from what's assumed.
3. Run `python -m src.runner` with `dry_run: true` (the default) first --
   it will log every order it *would* place without sending anything, so
   you can sanity-check entries/stops/targets against what you'd expect.
4. Once that looks right, ideally verify against a paper/sim account if
   your plan has one, then flip `broker.dry_run: false` in `config.yaml`.
5. Watch `trades/trades.csv` build up at `contract_size: 1`
   (`config.yaml: position_sizing`) before manually bumping size -- scaling
   is manual-only by design.

## Safety notes

- `config.yaml: risk_limits` adds a daily trade cap, a daily loss cap, and a
  kill switch -- not part of your original rules, added because this trades
  a TopStep funded/evaluation account where breaching a drawdown rule can
  end the account. Adjust or disable as you see fit.
- Never commit `.env` or paste API keys into chat/commits -- it's already
  gitignored.
