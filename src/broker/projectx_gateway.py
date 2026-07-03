from __future__ import annotations

"""TopstepX / ProjectX Gateway API broker -- SKELETON, not wired up yet.

Confirmed from public docs (https://gateway.docs.projectx.com/) at the time
this was written:
  - Auth: POST https://api.topstepx.com/api/Auth/loginKey
          body: {"userName": <str>, "apiKey": <str>}
          response includes a session token used to authorize further REST
          calls and to open the real-time WebSocket connection.
  - Real-time market data / order updates are delivered over an
    authenticated WebSocket once you hold a valid session token.

NOT yet confirmed (docs portal returned 403 to an unauthenticated fetch, so
these need to be filled in tomorrow once you're logged into
https://gateway.docs.projectx.com/ with your API subscription):
  - Exact bar/quote subscription payloads and hub/channel names.
  - Exact order placement endpoint, request schema (order type, bracket/OCO
    support or whether stop+target must be submitted as separate linked
    orders), and fill/position callback shape.
  - Exact account id plumbing (which id must accompany order requests).

Do not point this at a live account until every TODO below is replaced with
verified behavior and you've dry-run it against a paper/sim account if one
is available on your plan.
"""

import os
from typing import Callable

import requests

from src.broker.base import Broker
from src.models import Bar, Direction

AUTH_URL = "https://api.topstepx.com/api/Auth/loginKey"


class ProjectXGatewayBroker(Broker):
    def __init__(self, base_url: str, username: str | None = None, api_key: str | None = None, account_id: str | None = None):
        self.base_url = base_url
        self.username = username or os.environ.get("PROJECTX_USERNAME")
        self.api_key = api_key or os.environ.get("PROJECTX_API_KEY")
        self.account_id = account_id or os.environ.get("PROJECTX_ACCOUNT_ID")
        self._session_token: str | None = None

    def connect(self) -> None:
        if not self.username or not self.api_key:
            raise RuntimeError(
                "PROJECTX_USERNAME / PROJECTX_API_KEY not set -- copy .env.example to "
                ".env and fill in your ProjectX Gateway credentials."
            )
        response = requests.post(
            AUTH_URL,
            json={"userName": self.username, "apiKey": self.api_key},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        # TODO: confirm the exact field name for the session token in the
        # response body against the live docs -- assumed here based on the
        # public "authenticate with API key" guide.
        self._session_token = payload.get("token") or payload.get("sessionToken")
        if not self._session_token:
            raise RuntimeError(f"Auth succeeded but no session token found in response: {payload}")

    def subscribe_bars(self, symbol: str, timeframe_minutes: int, on_bar: Callable[[Bar], None]) -> None:
        # TODO: open the authenticated WebSocket (using self._session_token
        # as a bearer/JWT) and subscribe to real-time bars/quotes for
        # `symbol`, converting each incoming message into a Bar and calling
        # on_bar(bar). Confirm hub name / subscription message shape in the
        # docs portal.
        raise NotImplementedError(
            "ProjectX Gateway market data subscription is not wired up yet -- "
            "see TODOs in this file."
        )

    def place_bracket_order(
        self,
        symbol: str,
        direction: Direction,
        contracts: int,
        entry_price: float,
        stop_price: float,
        target_price: float,
    ) -> str:
        # TODO: confirm the order placement endpoint and whether stop/target
        # are submitted as a single bracket/OCO order or as separate linked
        # orders after the entry fills. Must include self.account_id.
        raise NotImplementedError(
            "ProjectX Gateway order placement is not wired up yet -- "
            "see TODOs in this file."
        )

    def poll_order_status(self, order_id: str) -> str:
        # TODO: confirm the order/position status endpoint and map its
        # states onto 'open' | 'filled_target' | 'filled_stop' | 'cancelled'.
        raise NotImplementedError(
            "ProjectX Gateway order status polling is not wired up yet -- "
            "see TODOs in this file."
        )

    def flatten_all(self, symbol: str) -> None:
        # TODO: confirm the flatten/close-position endpoint.
        raise NotImplementedError(
            "ProjectX Gateway flatten is not wired up yet -- see TODOs in this file."
        )
