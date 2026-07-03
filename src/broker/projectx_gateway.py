from __future__ import annotations

"""TopstepX / ProjectX Gateway API broker.

Endpoints, field names, and enum values below were confirmed against the
public ProjectX API docs (https://gateway.docs.projectx.com/) and the
open-source project-x-py SDK (https://github.com/TexasCoding/project-x-py),
which wraps this same API -- see STRATEGY.md for the full source list.
Still UNVERIFIED (the docs portal itself 403's an unauthenticated fetch, so
these need a live check tomorrow once you're logged in with API access):

  - The exact field names inside a `GatewayTrade` real-time event (price /
    volume / timestamp keys are guessed defensively in _on_trade_event).
  - The exact response envelope key for `/Order/searchOpen` (assumed here
    to be "orders").
  - Whether `linkedOrderId` alone makes the gateway auto-cancel the sibling
    bracket leg (OCO), or whether that's purely a client-side convention.
    Because this is unverified, poll_order_status() does NOT rely on it --
    it explicitly cancels the sibling leg itself once one leg disappears
    from the open-orders list.

`dry_run` defaults to True: orders are logged, never sent, until you flip
it off in config.yaml after verifying the above against a paper/sim
account. Do not flip it off against a live funded account without testing
end to end first.
"""

import os
import time as time_module
from datetime import datetime, timezone
from typing import Callable

import requests
from signalrcore.hub_connection_builder import HubConnectionBuilder

from src.broker.base import Broker
from src.models import Bar, Direction

API_PATH = "/api"
REALTIME_MARKET_HUB = "/hubs/market"

ORDER_TYPE_LIMIT = 1
ORDER_TYPE_MARKET = 2
ORDER_TYPE_STOP = 4

ORDER_SIDE_BUY = 0
ORDER_SIDE_SELL = 1


class ProjectXGatewayBroker(Broker):
    def __init__(
        self,
        base_url: str,
        realtime_base_url: str = "https://rtc.topstepx.com",
        username: str | None = None,
        api_key: str | None = None,
        account_id: str | None = None,
        dry_run: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.realtime_base_url = realtime_base_url.rstrip("/")
        self.username = username or os.environ.get("PROJECTX_USERNAME")
        self.api_key = api_key or os.environ.get("PROJECTX_API_KEY")
        self.account_id = account_id or os.environ.get("PROJECTX_ACCOUNT_ID")
        self.dry_run = dry_run

        self._token: str | None = None
        self._contract_id: str | None = None
        self._hub = None
        self._on_bar: Callable[[Bar], None] | None = None
        self._current_bar: dict | None = None
        self._brackets: dict[str, dict] = {}
        self._next_bracket_id = 1

    # -- auth / setup ------------------------------------------------------

    def connect(self) -> None:
        if not self.username or not self.api_key:
            raise RuntimeError(
                "PROJECTX_USERNAME / PROJECTX_API_KEY not set -- copy .env.example to "
                ".env and fill in your ProjectX Gateway credentials."
            )
        response = requests.post(
            f"{self.base_url}{API_PATH}/Auth/loginKey",
            json={"userName": self.username, "apiKey": self.api_key},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        self._token = payload.get("token")
        if not self._token:
            raise RuntimeError(f"Auth succeeded but no token found in response: {payload}")

        if not self.account_id:
            raise RuntimeError(
                "PROJECTX_ACCOUNT_ID not set -- required for order placement. "
                "Add it to .env once you know which account to trade."
            )

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    def _post(self, path: str, body: dict) -> dict:
        response = requests.post(
            f"{self.base_url}{API_PATH}{path}", json=body, headers=self._headers(), timeout=10
        )
        response.raise_for_status()
        data = response.json()
        if data.get("success") is False:
            raise RuntimeError(f"{path} failed: {data.get('errorMessage') or data}")
        return data

    def _resolve_contract(self, symbol: str) -> str:
        if self._contract_id is not None:
            return self._contract_id
        # `live: True` only returns contracts currently in an active trading
        # session -- confirmed empty while markets are closed. `live: False`
        # returns the full listed set regardless of market hours, which is
        # what contract resolution needs (it can run any time of day).
        data = self._post("/Contract/search", {"searchText": symbol, "live": False})
        contracts = data.get("contracts") or []
        if not contracts:
            raise RuntimeError(f"No contract found for symbol '{symbol}'")
        self._contract_id = contracts[0]["id"]
        return self._contract_id

    # -- market data ---------------------------------------------------------

    def subscribe_bars(self, symbol: str, timeframe_minutes: int, on_bar: Callable[[Bar], None]) -> None:
        if timeframe_minutes != 1:
            raise NotImplementedError("Only 1-minute bar aggregation is implemented")
        if self._token is None:
            raise RuntimeError("connect() must succeed before subscribing to market data")

        contract_id = self._resolve_contract(symbol)
        self._on_bar = on_bar

        hub_url = f"{self.realtime_base_url}{REALTIME_MARKET_HUB}?access_token={self._token}"
        self._hub = (
            HubConnectionBuilder()
            .with_url(hub_url, options={"verify_ssl": True})
            .with_automatic_reconnect(
                {"type": "raw", "keep_alive_interval": 10, "reconnect_interval": 5}
            )
            .build()
        )
        self._hub.on("GatewayTrade", self._on_trade_event)
        self._hub.on_open(lambda: self._hub.send("SubscribeContractTrades", [contract_id]))
        self._hub.start()

    def _on_trade_event(self, args) -> None:
        events = args if isinstance(args, list) else [args]
        for event in events:
            if not isinstance(event, dict):
                continue
            # TODO: verify these field names against a real GatewayTrade payload.
            price = event.get("price", event.get("lastPrice"))
            volume = event.get("volume", event.get("size", 0))
            ts_raw = event.get("timestamp", event.get("time"))
            if price is None:
                continue
            if isinstance(ts_raw, str):
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            else:
                ts = datetime.now(timezone.utc)
            self._update_bar(ts, float(price), float(volume or 0))

    def _update_bar(self, ts: datetime, price: float, volume: float) -> None:
        bucket = ts.replace(second=0, microsecond=0)
        if self._current_bar is None or self._current_bar["bucket"] != bucket:
            if self._current_bar is not None and self._on_bar is not None:
                self._on_bar(self._current_bar["bar"])
            self._current_bar = {
                "bucket": bucket,
                "bar": Bar(timestamp=bucket, open=price, high=price, low=price, close=price, volume=volume),
            }
            return

        bar = self._current_bar["bar"]
        self._current_bar["bar"] = Bar(
            timestamp=bar.timestamp,
            open=bar.open,
            high=max(bar.high, price),
            low=min(bar.low, price),
            close=price,
            volume=bar.volume + volume,
        )

    # -- orders --------------------------------------------------------------

    def place_bracket_order(
        self,
        symbol: str,
        direction: Direction,
        contracts: int,
        entry_price: float,
        stop_price: float,
        target_price: float,
    ) -> str:
        bracket_id = str(self._next_bracket_id)
        self._next_bracket_id += 1

        if self.dry_run:
            self._brackets[bracket_id] = {"status": "open", "dry_run": True}
            print(
                f"[DRY RUN] would enter {direction.value} x{contracts} @ market "
                f"(ref {entry_price}), stop={stop_price}, target={target_price}"
            )
            return bracket_id

        contract_id = self._resolve_contract(symbol)
        entry_side = ORDER_SIDE_BUY if direction is Direction.LONG else ORDER_SIDE_SELL
        protective_side = ORDER_SIDE_SELL if direction is Direction.LONG else ORDER_SIDE_BUY

        entry_resp = self._post(
            "/Order/place",
            {
                "accountId": self.account_id,
                "contractId": contract_id,
                "type": ORDER_TYPE_MARKET,
                "side": entry_side,
                "size": contracts,
                "customTag": f"entry-{bracket_id}",
            },
        )
        self._wait_for_fill(entry_resp["orderId"])

        stop_resp = self._post(
            "/Order/place",
            {
                "accountId": self.account_id,
                "contractId": contract_id,
                "type": ORDER_TYPE_STOP,
                "side": protective_side,
                "size": contracts,
                "stopPrice": stop_price,
                "customTag": f"stop-{bracket_id}",
            },
        )
        target_resp = self._post(
            "/Order/place",
            {
                "accountId": self.account_id,
                "contractId": contract_id,
                "type": ORDER_TYPE_LIMIT,
                "side": protective_side,
                "size": contracts,
                "limitPrice": target_price,
                "linkedOrderId": stop_resp["orderId"],
                "customTag": f"target-{bracket_id}",
            },
        )

        self._brackets[bracket_id] = {
            "status": "open",
            "dry_run": False,
            "stop_order_id": stop_resp["orderId"],
            "target_order_id": target_resp["orderId"],
        }
        return bracket_id

    def _wait_for_fill(self, order_id, timeout_seconds: float = 10.0, poll_interval: float = 0.5) -> None:
        deadline = time_module.monotonic() + timeout_seconds
        while time_module.monotonic() < deadline:
            if order_id not in self._fetch_open_order_ids():
                return
            time_module.sleep(poll_interval)
        raise RuntimeError(f"Entry order {order_id} did not fill within {timeout_seconds}s")

    def _fetch_open_order_ids(self) -> set:
        data = self._post("/Order/searchOpen", {"accountId": self.account_id})
        # TODO: confirm the response envelope key -- assumed "orders".
        return {o["id"] for o in data.get("orders", [])}

    def poll_order_status(self, order_id: str) -> str:
        bracket = self._brackets[order_id]
        if bracket["status"] != "open" or bracket.get("dry_run"):
            return bracket["status"]

        open_ids = self._fetch_open_order_ids()
        stop_open = bracket["stop_order_id"] in open_ids
        target_open = bracket["target_order_id"] in open_ids

        if stop_open and target_open:
            return "open"

        if not stop_open and target_open:
            bracket["status"] = "filled_stop"
            self._cancel_order(bracket["target_order_id"])
        elif stop_open and not target_open:
            bracket["status"] = "filled_target"
            self._cancel_order(bracket["stop_order_id"])
        else:
            # Both legs disappeared between polls -- can't tell which filled
            # first from this endpoint alone. Treated conservatively as a
            # stop-out; review trades/trades.csv against the account
            # statement if this ever fires live.
            bracket["status"] = "filled_stop"

        return bracket["status"]

    def _cancel_order(self, order_id) -> None:
        try:
            self._post("/Order/cancel", {"accountId": self.account_id, "orderId": order_id})
        except Exception:
            pass  # already filled or cancelled -- fine

    def flatten_all(self, symbol: str) -> None:
        if self.dry_run:
            print(f"[DRY RUN] would flatten all {symbol} positions")
            return
        contract_id = self._resolve_contract(symbol)
        self._post("/Position/closeContract", {"accountId": self.account_id, "contractId": contract_id})
