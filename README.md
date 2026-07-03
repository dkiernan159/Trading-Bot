# Trading Bot -- NQ/MNQ NY-Open Opening Range Breakout

Automates a specific morning strategy on the Micro Nasdaq (MNQ) using the
TopstepX ProjectX Gateway API: mark prior-session levels, wait for the 9:30-
9:45 ET opening range to break and retest, enter on a strong 1-minute fair
value gap, and manage the trade with a 2:1 reward:risk bracket.

**Read [STRATEGY.md](STRATEGY.md) first.** It documents exactly what's
implemented and flags every place an assumption was made instead of an
explicit rule, so you can correct anything that doesn't match how you
actually trade before running this live.

## Status

- Strategy logic, risk/stop-target calculation, and a mock broker for
  testing are implemented and covered by unit tests.
- The real broker (`src/broker/projectx_gateway.py`) is a **stub**: auth is
  wired up, market data / order placement are `NotImplementedError` TODOs
  waiting on ProjectX Gateway API access and docs review.
- Nothing in this repo places a real order yet.

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
    projectx_gateway.py      real broker -- stub, see TODOs
tests/                      unit + integration tests, all using the mock broker
```

## Next steps (tomorrow, once you have API access)

1. Fill in `.env`.
2. Implement the TODOs in `src/broker/projectx_gateway.py` (market data
   subscription, order placement, position/fill polling, flatten) against
   the real docs at https://gateway.docs.projectx.com/.
3. Run `python -m src.runner` to start the bot once the broker is wired up
   and you've reviewed the assumptions in STRATEGY.md.
4. Watch `trades/trades.csv` build up at `contract_size: 1`
   (`config.yaml: position_sizing`) before manually bumping size -- scaling
   is manual-only by design.

## Safety notes

- `config.yaml: risk_limits` adds a daily trade cap, a daily loss cap, and a
  kill switch -- not part of your original rules, added because this trades
  a TopStep funded/evaluation account where breaching a drawdown rule can
  end the account. Adjust or disable as you see fit.
- Never commit `.env` or paste API keys into chat/commits -- it's already
  gitignored.
