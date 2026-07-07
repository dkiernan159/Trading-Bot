from unittest.mock import MagicMock, patch

import pytest
import requests

from src.broker.projectx_gateway import ProjectXGatewayBroker


def make_broker() -> ProjectXGatewayBroker:
    broker = ProjectXGatewayBroker(base_url="https://api.example.com", dry_run=True)
    broker._token = "test-token"
    return broker


def fake_response(status_code: int, json_body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(f"{status_code} error")
    else:
        resp.raise_for_status.side_effect = None
    return resp


def test_post_retries_on_429_then_succeeds():
    """A real 30-day dashboard backtest hit exactly this: widening
    dashboard.backtest_days from 7 to 30 (9 -> 32 /History/retrieveBars
    calls in a tight loop) tripped TopstepX's rate limit. A transient 429
    should be retried with backoff, not surfaced as a hard failure."""
    broker = make_broker()
    responses = [
        fake_response(429, {}),
        fake_response(429, {}),
        fake_response(200, {"success": True, "bars": []}),
    ]
    with patch("src.broker.projectx_gateway.requests.post", side_effect=responses) as mock_post, patch(
        "src.broker.projectx_gateway.time_module.sleep"
    ) as mock_sleep:
        result = broker._post("/History/retrieveBars", {})

    assert result == {"success": True, "bars": []}
    assert mock_post.call_count == 3
    assert mock_sleep.call_count == 2  # one sleep between each of the 2 retries


def test_post_gives_up_after_max_retries_and_raises():
    broker = make_broker()
    responses = [fake_response(429, {}) for _ in range(6)]  # more than max_retries + 1
    with patch("src.broker.projectx_gateway.requests.post", side_effect=responses), patch(
        "src.broker.projectx_gateway.time_module.sleep"
    ):
        with pytest.raises(requests.exceptions.HTTPError):
            broker._post("/History/retrieveBars", {}, max_retries=4)


def _make_builder_mock() -> MagicMock:
    """A HubConnectionBuilder mock whose chained with_url/with_automatic_reconnect
    calls all return itself, so .build() at the end of the chain is reachable --
    matches the real builder's fluent-interface shape."""
    builder_mock = MagicMock()
    builder_mock.with_url.return_value = builder_mock
    builder_mock.with_automatic_reconnect.return_value = builder_mock
    return builder_mock


def test_on_hub_closed_reconnects_with_a_fresh_token():
    """_on_hub_closed must call connect() (refreshing self._token) and
    rebuild the hub against a URL carrying the *new* token, not just
    retry the old one -- otherwise, once the token itself expires, no
    retry can ever succeed."""
    broker = make_broker()
    broker._realtime_contract_id = "CON.F.US.MNQ.U26"
    broker._on_bar = lambda bar: None

    hub_mocks = [MagicMock(), MagicMock()]
    builder_mock = _make_builder_mock()
    urls_used = []

    def fake_with_url(url, **kwargs):
        urls_used.append(url)
        return builder_mock

    builder_mock.with_url.side_effect = fake_with_url
    builder_mock.build.side_effect = hub_mocks

    def fake_connect():
        broker._token = "fresh-token"

    with patch("src.broker.projectx_gateway.HubConnectionBuilder", return_value=builder_mock), patch.object(
        broker, "connect", side_effect=fake_connect
    ) as mock_connect:
        broker._token = "stale-token"
        broker._start_hub()
        assert "stale-token" in urls_used[0]

        broker._on_hub_closed()

    mock_connect.assert_called_once()
    assert "fresh-token" in urls_used[1]
    assert hub_mocks[1].start.called


def test_on_hub_closed_gives_up_silently_on_reauth_failure():
    """Unlike an earlier version of this fix, _on_hub_closed is now a
    single best-effort attempt, not a blocking retry-until-success loop --
    confirmed live that this callback doesn't reliably fire for every real
    failure mode in the first place (see _start_realtime_refresh_thread's
    docstring), so a failed attempt here just waits for the next scheduled
    refresh rather than blocking whatever thread called this indefinitely."""
    broker = make_broker()
    broker._realtime_contract_id = "CON.F.US.MNQ.U26"
    broker._on_bar = lambda bar: None
    builder_mock = _make_builder_mock()

    with patch("src.broker.projectx_gateway.HubConnectionBuilder", return_value=builder_mock), patch.object(
        broker, "connect", side_effect=Exception("still down")
    ) as mock_connect:
        broker._on_hub_closed()

    mock_connect.assert_called_once()
    builder_mock.build.assert_not_called()  # no rebuild attempted after a failed reauth


def test_on_trade_event_unpacks_the_real_contract_id_plus_ticks_shape():
    """Confirmed live 2026-07-07 via DEBUG-level signalrcore logging: the
    real GatewayTrade invocation arguments are [contractId, [tick_dict,
    tick_dict, ...]], not a flat list of tick dicts. Before this fix, every
    real tick was silently dropped forever -- neither the contract-id
    string nor the nested ticks list itself is ever a dict -- despite the
    connection receiving GatewayTrade messages continuously and healthily.
    No bars were ever built from live ticks as a result."""
    broker = make_broker()
    received_bars = []
    broker._on_bar = lambda bar: received_bars.append(bar)

    real_shaped_args = [
        "CON.F.US.MNQ.U26",
        [
            {
                "symbolId": "F.US.MNQ",
                "price": 29748.50,
                "timestamp": "2026-07-07T02:33:48.604+00:00",
                "type": 0,
                "volume": 1,
                "contractId": "CON.F.US.MNQ.U26",
            },
            {
                "symbolId": "F.US.MNQ",
                "price": 29749.00,
                "timestamp": "2026-07-07T02:33:48.700+00:00",
                "type": 0,
                "volume": 2,
                "contractId": "CON.F.US.MNQ.U26",
            },
        ],
    ]

    broker._on_trade_event(real_shaped_args)

    assert broker._current_bar is not None
    bar = broker._current_bar["bar"]
    assert bar.close == pytest.approx(29749.00)
    assert bar.high == pytest.approx(29749.00)
    assert bar.low == pytest.approx(29748.50)
    assert bar.volume == pytest.approx(3)


def test_subscribe_bars_starts_the_realtime_refresh_thread():
    """subscribe_bars must actually wire up the scheduled refresh -- the
    periodic timer is the real fix for the stale-token bug (see
    _start_realtime_refresh_thread's docstring); on_close/on_error alone
    don't reliably catch every real failure mode."""
    broker = make_broker()
    broker._contract_id = "CON.F.US.MNQ.U26"
    builder_mock = _make_builder_mock()

    with patch("src.broker.projectx_gateway.HubConnectionBuilder", return_value=builder_mock), patch.object(
        broker, "_start_realtime_refresh_thread"
    ) as mock_start_thread:
        broker.subscribe_bars("MNQ", 1, lambda bar: None)

    mock_start_thread.assert_called_once()


def test_realtime_refresh_loop_reconnects_on_a_fixed_schedule():
    """The scheduled refresh is unconditional -- it doesn't wait for any
    close/error callback to fire, since confirmed live those don't always
    fire for every real failure mode."""
    broker = make_broker()

    class _StopLoop(Exception):
        pass

    with patch("src.broker.projectx_gateway.time_module.sleep") as mock_sleep, patch.object(
        broker, "_reconnect_hub", side_effect=[None, _StopLoop]
    ) as mock_reconnect:
        with pytest.raises(_StopLoop):
            broker._realtime_refresh_loop()

    assert mock_sleep.call_count == 2
    assert mock_reconnect.call_count == 2


def test_post_does_not_retry_on_other_error_statuses():
    """A 500 (or any non-429 error) should still fail immediately -- only
    429 (rate limited) is worth retrying."""
    broker = make_broker()
    with patch(
        "src.broker.projectx_gateway.requests.post", return_value=fake_response(500, {})
    ) as mock_post, patch("src.broker.projectx_gateway.time_module.sleep") as mock_sleep:
        with pytest.raises(requests.exceptions.HTTPError):
            broker._post("/History/retrieveBars", {})

    assert mock_post.call_count == 1
    mock_sleep.assert_not_called()
