from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.broker.projectx_gateway import MIN_ORPHAN_ORDER_AGE_SECONDS, ProjectXGatewayBroker
from src.models import Direction


def make_broker() -> ProjectXGatewayBroker:
    broker = ProjectXGatewayBroker(base_url="https://api.example.com", dry_run=True)
    broker._token = "test-token"
    return broker


def fake_response(status_code: int, json_body: dict, text: str | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = status_code < 400
    resp.text = text if text is not None else str(json_body)
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


def test_connect_converts_account_id_to_int_for_the_api(monkeypatch):
    """Confirmed live 2026-07-09: every single /Order/place call was
    failing with a 400 ("$.accountId: The JSON value could not be
    converted to System.Int32") -- os.environ values are always str, and
    that str was going straight into the JSON body, but the Gateway API
    requires accountId as a JSON integer, not a quoted string. No order
    had ever actually reached the exchange as a result -- every "filled"
    anchor was silently discarded there instead. connect() now converts
    it once, so every _post call sends an int."""
    monkeypatch.setenv("PROJECTX_USERNAME", "user")
    monkeypatch.setenv("PROJECTX_API_KEY", "key")
    monkeypatch.setenv("PROJECTX_ACCOUNT_ID", "12345")
    broker = ProjectXGatewayBroker(base_url="https://api.example.com", dry_run=True)

    with patch(
        "src.broker.projectx_gateway.requests.post",
        return_value=fake_response(200, {"token": "test-token"}),
    ):
        broker.connect()

    assert broker.account_id == 12345
    assert isinstance(broker.account_id, int)


def test_connect_raises_a_clear_error_when_account_id_cannot_be_resolved(monkeypatch):
    """A non-numeric account_id that also has no matching name in
    /Account/search's response must raise a clear, actionable error --
    not fail silently or crash with an unrelated traceback."""
    monkeypatch.setenv("PROJECTX_USERNAME", "user")
    monkeypatch.setenv("PROJECTX_API_KEY", "key")
    monkeypatch.setenv("PROJECTX_ACCOUNT_ID", "not-a-real-account")
    broker = ProjectXGatewayBroker(base_url="https://api.example.com", dry_run=True)

    responses = [
        fake_response(200, {"token": "test-token"}),
        fake_response(200, {"accounts": [{"id": 12345, "name": "some-other-account"}]}),
    ]
    with patch("src.broker.projectx_gateway.requests.post", side_effect=responses):
        with pytest.raises(RuntimeError, match="no matching account was found"):
            broker.connect()


def test_connect_resolves_account_id_via_account_search_when_not_numeric(monkeypatch, capsys):
    """Confirmed live 2026-07-10: PROJECTX_ACCOUNT_ID had been set to the
    account's display label ("50KTC-V2-69346-...."), not the Gateway's
    numeric account id -- the int() conversion added the day before then
    correctly rejected it on every single connect(), crashing the whole
    process at startup in an infinite restart loop (systemd's
    Restart=on-failure just kept restarting it into the same crash every
    10s, so it never got far enough to process a single bar -- this was
    the actual reason nothing had traded in days, discovered only once
    the user grepped bot.log for the traceback). connect() now falls back
    to /Account/search to resolve a non-numeric account_id automatically,
    matching it against the account's name."""
    monkeypatch.setenv("PROJECTX_USERNAME", "user")
    monkeypatch.setenv("PROJECTX_API_KEY", "key")
    monkeypatch.setenv("PROJECTX_ACCOUNT_ID", "50KTC-V2-69346-54207896")
    broker = ProjectXGatewayBroker(base_url="https://api.example.com", dry_run=True)

    responses = [
        fake_response(200, {"token": "test-token"}),
        fake_response(200, {"accounts": [{"id": 98765, "name": "50KTC-V2-69346-54207896"}]}),
    ]
    with patch("src.broker.projectx_gateway.requests.post", side_effect=responses):
        broker.connect()

    assert broker.account_id == 98765
    assert isinstance(broker.account_id, int)
    assert "Resolved PROJECTX_ACCOUNT_ID" in capsys.readouterr().out


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


def test_post_logs_the_response_body_before_raising_on_a_non_2xx(capsys):
    """Confirmed live 2026-07-08: /Order/place returned a real 400 Bad
    Request, but raise_for_status() alone throws away the response body --
    the API's own explanation of what was wrong with the request was never
    visible anywhere. Must be printed before the exception propagates."""
    broker = make_broker()
    bad_response = fake_response(400, {}, text='{"errorMessage": "invalid limitPrice precision"}')
    with patch("src.broker.projectx_gateway.requests.post", return_value=bad_response):
        with pytest.raises(requests.exceptions.HTTPError):
            broker._post("/Order/place", {})

    assert "invalid limitPrice precision" in capsys.readouterr().out


def test_place_bracket_order_flattens_if_stop_placement_fails_after_entry_fills():
    """Confirmed real risk: if the entry leg fills but placing the stop or
    target leg then fails, the account would be left with a naked, real
    position and no protective orders attached, with the runner unaware
    any trade was ever taken (place_bracket_order never returns, so
    current_trade never gets set). Must flatten immediately rather than
    leave that sitting open on the exchange."""
    broker = make_broker()
    broker.dry_run = False
    broker.account_id = "ACC1"
    broker._contract_id = "CON.F.US.MNQ.U26"

    responses = [{"orderId": "entry-1"}, RuntimeError("stop placement failed")]

    def fake_post(path, body, **kwargs):
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    with patch.object(broker, "_post", side_effect=fake_post), patch.object(
        broker, "_wait_for_fill", return_value=True
    ), patch.object(broker, "flatten_all") as mock_flatten:
        with pytest.raises(RuntimeError, match="stop placement failed"):
            broker.place_bracket_order(
                symbol="MNQ",
                direction=Direction.LONG,
                contracts=1,
                entry_price=100.0,
                stop_price=90.0,
                target_price=120.0,
            )

    mock_flatten.assert_called_once_with("MNQ")


def _place_order_and_capture_tags(broker) -> list[str]:
    tags = []

    def fake_post(path, body, **kwargs):
        tags.append(body["customTag"])
        return {"orderId": f"order-for-{body['customTag']}"}

    with patch.object(broker, "_post", side_effect=fake_post), patch.object(
        broker, "_wait_for_fill", return_value=True
    ):
        broker.place_bracket_order(
            symbol="MNQ", direction=Direction.LONG, contracts=1,
            entry_price=100.0, stop_price=90.0, target_price=120.0,
        )
    return tags


def test_bracket_id_customtag_survives_a_process_restart_without_colliding():
    """Confirmed live 2026-07-21: bracket_id used to be a plain in-memory
    counter starting at 1 in __init__, embedded directly into customTag
    ("entry-1", "entry-2", ...). The gateway enforces customTag uniqueness
    per *account*, not per process -- real bot.log tracebacks showed
    /Order/place failing with "Specified custom tag is already in use"
    over and over after every restart, because the new process's counter
    always starts back at 1 and collides with tags any earlier process
    already sent for this account. 10 of 12 real entry signals in one
    2-day window were silently lost this way. A fresh broker instance
    (standing in for "the process restarted") must not be able to produce
    a customTag any previous instance could plausibly have already used."""
    first_process = make_broker()
    first_process.dry_run = False
    first_process.account_id = "ACC1"
    first_process._contract_id = "CON.F.US.MNQ.U26"
    first_tags = _place_order_and_capture_tags(first_process)

    second_process = make_broker()  # simulates a restart: brand-new instance
    second_process.dry_run = False
    second_process.account_id = "ACC1"
    second_process._contract_id = "CON.F.US.MNQ.U26"
    second_tags = _place_order_and_capture_tags(second_process)

    assert set(first_tags).isdisjoint(second_tags)


def test_fetch_net_position_sums_signed_size_for_the_resolved_contract():
    """Added 2026-08-07 -- see Broker.fetch_net_position's own docstring:
    a real orphaned position went completely undetected on 2026-08-05
    because the bot only ever knew about an open trade through its own
    in-memory state. UNVERIFIED envelope/field names, same defensive
    pattern as _fetch_open_order_ids -- assumed "positions" envelope with
    signed "size" (positive long, negative short) filtered to the
    resolved contract."""
    broker = make_broker()
    broker.dry_run = False
    broker.account_id = "ACC1"
    broker._contract_id = "CON.F.US.MNQ.U26"

    response = {
        "positions": [
            {"contractId": "CON.F.US.MNQ.U26", "size": 3},
            {"contractId": "CON.F.US.MNQ.U26", "size": -1},
            {"contractId": "CON.F.US.ES.U26", "size": 5},  # a different contract -- must not count
        ]
    }
    with patch.object(broker, "_post", return_value=response) as mock_post:
        net = broker.fetch_net_position("MNQ")

    assert net == 2
    mock_post.assert_called_once_with("/Position/searchOpen", {"accountId": "ACC1"})


def test_fetch_net_position_returns_zero_when_flat():
    broker = make_broker()
    broker.dry_run = False
    broker.account_id = "ACC1"
    broker._contract_id = "CON.F.US.MNQ.U26"

    with patch.object(broker, "_post", return_value={"positions": []}):
        assert broker.fetch_net_position("MNQ") == 0


def test_fetch_net_position_short_circuits_in_dry_run():
    broker = make_broker()  # dry_run=True by default (see make_broker)
    with patch.object(broker, "_post") as mock_post:
        assert broker.fetch_net_position("MNQ") == 0
    mock_post.assert_not_called()


def _old_timestamp() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=MIN_ORPHAN_ORDER_AGE_SECONDS + 60)).isoformat()


def _fresh_timestamp() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()


def test_cancel_orphaned_orders_cancels_a_stale_untracked_order():
    """Added 2026-08-07 -- see Broker.cancel_orphaned_orders' own
    docstring: a resting order can outlive the process life that placed
    it if its own timeout-cancel silently failed, and sit unfilled and
    untracked for hours or days before finally executing on its own."""
    broker = make_broker()
    broker.dry_run = False
    broker.account_id = "ACC1"
    broker._contract_id = "CON.F.US.MNQ.U26"
    # This broker instance has never placed a bracket -- self._brackets is
    # empty, exactly like a freshly-started process that has no memory of
    # any order an earlier process life left resting.
    assert broker._brackets == {}

    orphan = {"id": "orphan-1", "contractId": "CON.F.US.MNQ.U26", "creationTimestamp": _old_timestamp()}
    response = {"orders": [orphan]}

    with patch.object(broker, "_post", return_value=response) as mock_post, patch.object(
        broker, "_cancel_order"
    ) as mock_cancel:
        cancelled = broker.cancel_orphaned_orders("MNQ")

    assert cancelled == 1
    mock_cancel.assert_called_once_with("orphan-1")
    mock_post.assert_called_once_with("/Order/searchOpen", {"accountId": "ACC1"})


def test_cancel_orphaned_orders_leaves_a_tracked_brackets_orders_alone():
    broker = make_broker()
    broker.dry_run = False
    broker.account_id = "ACC1"
    broker._contract_id = "CON.F.US.MNQ.U26"
    broker._brackets["bracket-1"] = {
        "status": "open",
        "stop_order_id": "tracked-stop",
        "target_order_id": "tracked-target",
    }

    response = {
        "orders": [
            {"id": "tracked-stop", "contractId": "CON.F.US.MNQ.U26", "creationTimestamp": _old_timestamp()},
            {"id": "tracked-target", "contractId": "CON.F.US.MNQ.U26", "creationTimestamp": _old_timestamp()},
        ]
    }

    with patch.object(broker, "_post", return_value=response), patch.object(broker, "_cancel_order") as mock_cancel:
        cancelled = broker.cancel_orphaned_orders("MNQ")

    assert cancelled == 0
    mock_cancel.assert_not_called()


def test_cancel_orphaned_orders_leaves_a_recently_created_order_alone():
    """Safety margin against ever touching an order this same process just
    placed a moment ago and is still legitimately waiting on -- the
    bracket dict entry for a live order is only written after the entire
    bracket succeeds, so a resting entry (or a stop/target leg placed but
    not yet recorded) would otherwise look indistinguishable from a
    genuine orphan for a brief window."""
    broker = make_broker()
    broker.dry_run = False
    broker.account_id = "ACC1"
    broker._contract_id = "CON.F.US.MNQ.U26"

    fresh = {"id": "fresh-1", "contractId": "CON.F.US.MNQ.U26", "creationTimestamp": _fresh_timestamp()}
    response = {"orders": [fresh]}

    with patch.object(broker, "_post", return_value=response), patch.object(broker, "_cancel_order") as mock_cancel:
        cancelled = broker.cancel_orphaned_orders("MNQ")

    assert cancelled == 0
    mock_cancel.assert_not_called()


def test_cancel_orphaned_orders_ignores_a_different_contract():
    broker = make_broker()
    broker.dry_run = False
    broker.account_id = "ACC1"
    broker._contract_id = "CON.F.US.MNQ.U26"

    other_contract = {"id": "other-1", "contractId": "CON.F.US.ES.U26", "creationTimestamp": _old_timestamp()}
    response = {"orders": [other_contract]}

    with patch.object(broker, "_post", return_value=response), patch.object(broker, "_cancel_order") as mock_cancel:
        cancelled = broker.cancel_orphaned_orders("MNQ")

    assert cancelled == 0
    mock_cancel.assert_not_called()


def test_cancel_orphaned_orders_short_circuits_in_dry_run():
    broker = make_broker()  # dry_run=True by default
    with patch.object(broker, "_post") as mock_post:
        assert broker.cancel_orphaned_orders("MNQ") == 0
    mock_post.assert_not_called()


def test_cancel_order_logs_loudly_instead_of_silently_swallowing_a_failure(capsys):
    """Confirmed live 2026-08-07: this used to silently pass on any
    exception, assuming "already filled or cancelled -- fine" -- but a
    genuine cancel failure looks identical and leaves a real order
    resting live on the exchange indefinitely."""
    broker = make_broker()
    broker.dry_run = False
    broker.account_id = "ACC1"

    with patch.object(broker, "_post", side_effect=RuntimeError("cancel failed")) as mock_post:
        broker._cancel_order("some-order-id")  # must not raise

    mock_post.assert_called_once_with("/Order/cancel", {"accountId": "ACC1", "orderId": "some-order-id"})
    assert "failed to cancel order some-order-id" in capsys.readouterr().out


def test_modify_stop_price_resolves_the_brackets_stop_leg_and_posts_the_new_price():
    """Added 2026-08-20 at the user's explicit request (breakeven-stop
    feature). order_id is the client-facing bracket id, not the
    broker-internal stop leg's own id -- must resolve via self._brackets
    rather than passing the bracket id straight through to the gateway."""
    broker = make_broker()
    broker.dry_run = False
    broker.account_id = "ACC1"
    broker._brackets["bracket-1"] = {
        "status": "open",
        "stop_order_id": "real-stop-order-id",
        "target_order_id": "real-target-order-id",
    }

    with patch.object(broker, "_post", return_value={"success": True}) as mock_post:
        broker.modify_stop_price("bracket-1", 110.0)

    mock_post.assert_called_once_with(
        "/Order/modify", {"accountId": "ACC1", "orderId": "real-stop-order-id", "stopPrice": 110.0}
    )


def test_modify_stop_price_short_circuits_in_dry_run():
    broker = make_broker()  # dry_run=True by default
    broker._brackets["bracket-1"] = {"status": "open", "stop_order_id": "x", "target_order_id": "y"}
    with patch.object(broker, "_post") as mock_post:
        broker.modify_stop_price("bracket-1", 110.0)
    mock_post.assert_not_called()


def test_hub_generation_guard_ignores_events_from_a_superseded_hub():
    """Confirmed live 2026-07-08: a "deque mutated during iteration" error
    in Runner meant more than one realtime hub was alive and delivering
    ticks concurrently -- most likely because _reconnect_hub's
    self._hub.stop() on the old connection doesn't reliably kill its
    receive thread. Each hub's handler must be tied to the generation it
    was built with, so a stale hub's events get dropped once superseded,
    regardless of whether its underlying connection actually dies."""
    broker = make_broker()
    broker._realtime_contract_id = "CON.F.US.MNQ.U26"
    received_bars = []
    broker._on_bar = lambda bar: received_bars.append(bar)
    builder_mock = _make_builder_mock()

    hub_mock = builder_mock.build.return_value
    with patch("src.broker.projectx_gateway.HubConnectionBuilder", return_value=builder_mock):
        broker._start_hub()  # generation 1
        first_generation_handler = hub_mock.on.call_args_list[-1][0][1]
        broker._start_hub()  # generation 2 -- supersedes the first

    real_shaped_args = ["CON.F.US.MNQ.U26", [{"price": 100.0, "timestamp": "2026-07-07T02:00:00+00:00", "volume": 1}]]
    first_generation_handler(real_shaped_args)  # a stale hub still delivering a tick

    assert broker._current_bar is None  # dropped, not processed
