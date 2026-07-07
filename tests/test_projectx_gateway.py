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


def test_on_hub_closed_reauthenticates_and_rebuilds_the_hub_with_a_fresh_token():
    """A real bot running over a day straight logged an unbroken stream of
    signalrcore's own reconnect-failure message -- automatic reconnect
    reuses the exact same URL (and therefore the same access_token) on
    every retry, so once the token itself expires, no retry can ever
    succeed. _on_hub_closed must call connect() (refreshing self._token)
    and rebuild the hub against a URL carrying the *new* token, not just
    retry the old one."""
    broker = make_broker()
    broker._realtime_contract_id = "CON.F.US.MNQ.U26"
    broker._on_bar = lambda bar: None

    hub_mocks = [MagicMock(), MagicMock()]
    builder_mock = MagicMock()
    builder_mock.with_url.return_value = builder_mock
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
    ) as mock_connect, patch("src.broker.projectx_gateway.time_module.sleep"):
        broker._token = "stale-token"
        broker._start_hub()
        assert "stale-token" in urls_used[0]

        broker._on_hub_closed()

    mock_connect.assert_called_once()
    assert "fresh-token" in urls_used[1]
    assert hub_mocks[1].start.called


def test_on_hub_closed_retries_reauth_on_failure_until_it_succeeds():
    broker = make_broker()
    broker._realtime_contract_id = "CON.F.US.MNQ.U26"
    broker._on_bar = lambda bar: None

    builder_mock = MagicMock()
    builder_mock.with_url.return_value = builder_mock

    connect_attempts = [Exception("still down"), Exception("still down"), None]

    def fake_connect():
        result = connect_attempts.pop(0)
        if isinstance(result, Exception):
            raise result

    with patch("src.broker.projectx_gateway.HubConnectionBuilder", return_value=builder_mock), patch.object(
        broker, "connect", side_effect=fake_connect
    ) as mock_connect, patch("src.broker.projectx_gateway.time_module.sleep") as mock_sleep:
        broker._on_hub_closed()

    assert mock_connect.call_count == 3
    assert mock_sleep.call_count == 3


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
