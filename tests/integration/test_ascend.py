"""Offline tests for ASCEND discovery, credentials and reported-state control."""

import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import aiomqtt
import pytest

from custom_components.intermatic_connect.api import IntermaticApi
from custom_components.intermatic_connect.ascend import AscendClient, normalize_timer
from custom_components.intermatic_connect.ascend_schedule import decode_schedule
from custom_components.intermatic_connect.exceptions import (
    IntermaticAuthError,
    IntermaticConnectionError,
    IntermaticError,
)

pytestmark = pytest.mark.asyncio
RECORD = {"thingName": "test-timer", "name": "Test light"}
SHADOW = {
    "version": 10,
    "state": {
        "reported": {
            "RelayOn": 0,
            "isOnline": 1,
            "Model": "STW700W",
            "Version": "2.05",
            "Pin": "private-pin",
            "Name": "private-name",
        }
    },
    "metadata": {"reported": {"RelayOn": {"timestamp": 100}}},
}


def page(items, token=None):
    return {"data": {"getTimers": {"items": items, "nextToken": token}}}


def client():
    return AscendClient(AsyncMock(), asyncio.to_thread)


async def test_pagination_and_duplicate_ids():
    api = client()
    api._request.side_effect = [
        page([RECORD], "cursor"),
        page([RECORD, {"thingName": "second"}]),
    ]
    result = await api.discover()
    assert len(result) == 2
    assert (
        api._request.call_args.kwargs["json"]["variables"]["input"]["nextToken"]
        == "cursor"
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"errors": [{"message": "private-error"}]},
        {"data": {"getTimers": {"items": []}}},
        page([{"thingName": "unsafe/+/topic"}]),
        page([None]),
    ],
)
async def test_failed_discovery_preserves_previous_registry(bad):
    api = client()
    api.registry = {RECORD["thingName"]: RECORD}
    api._request.return_value = bad
    with pytest.raises(IntermaticConnectionError) as err:
        await api.discover()
    assert "private" not in str(err.value)
    assert list(api.registry.values()) == [RECORD]


async def test_repeated_cursor_fails():
    api = client()
    api._request.return_value = page([RECORD], "repeat")
    with pytest.raises(IntermaticConnectionError, match="pagination"):
        await api.discover()
    assert api._request.await_count == 2


async def test_normalization_omits_private_shadow_data_and_unknown_states():
    result = normalize_timer(RECORD, SHADOW)
    assert result["RelayOn"] == 0
    assert result["Connected"] == 1
    assert result["RelayReportedAt"] == 100
    assert "private-" not in json.dumps(result)
    assert normalize_timer(RECORD)["RelayOn"] is None
    assert normalize_timer(RECORD)["Connected"] == 0
    for value in ("1", 2, {}, None):
        shadow = {"state": {"reported": {"RelayOn": value, "isOnline": 1}}}
        assert normalize_timer(RECORD, shadow)["Connected"] == 0


async def test_credentials_cached_and_refreshed_before_expiration():
    api = client()
    api._request.return_value = {
        "Credentials": {
            "AccessKeyId": "test-access",
            "SecretKey": "test-secret",
            "SessionToken": "test+/token",
            "Expiration": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
    }
    path = await api._websocket_path()
    params = parse_qs(urlsplit(path).query)
    assert params["X-Amz-Security-Token"] == ["test+/token"]
    assert "/iotdevicegateway/aws4_request" in params["X-Amz-Credential"][0]
    await api._websocket_path()
    assert api._request.await_count == 1
    api._credentials_expire = datetime.now(timezone.utc) + timedelta(minutes=1)
    await api._websocket_path()
    assert api._request.await_count == 2


async def test_broker_failure_is_sanitized_and_invalidates_credentials():
    api = client()
    api._tls = object()
    api._credentials = {"secret": "private"}
    api._websocket_path = AsyncMock(return_value="/mqtt?secret=private")
    mqtt = AsyncMock()
    mqtt.__aenter__.side_effect = aiomqtt.MqttError("private-signed-url")
    with patch(
        "custom_components.intermatic_connect.ascend.aiomqtt.Client", return_value=mqtt
    ):
        with pytest.raises(IntermaticConnectionError) as err:
            async with api._connection():
                pass
    assert "private" not in str(err.value)
    assert api._credentials is None


async def test_connection_is_closed_on_cancellation():
    api = client()
    api._tls = object()
    api._websocket_path = AsyncMock(return_value="/mqtt")
    mqtt = AsyncMock()
    with patch(
        "custom_components.intermatic_connect.ascend.aiomqtt.Client", return_value=mqtt
    ):
        with pytest.raises(asyncio.CancelledError):
            async with api._connection():
                raise asyncio.CancelledError
    mqtt.__aexit__.assert_awaited_once()
    assert not api._lock.locked()


def mock_connection(api):
    mqtt = AsyncMock()

    @asynccontextmanager
    async def connection():
        yield mqtt

    api._connection = connection
    return mqtt


async def test_read_subscribes_before_empty_get_and_unsubscribes():
    api = client()
    mqtt = AsyncMock()
    api._receive = AsyncMock(return_value=("topic/accepted", SHADOW))
    assert await api._read_shadow(mqtt, "test-timer") == SHADOW
    assert [c[0] for c in mqtt.mock_calls] == ["subscribe", "publish", "unsubscribe"]
    mqtt.publish.assert_awaited_once_with(
        "$aws/things/test-timer/shadow/get", b"", qos=1, retain=False
    )


async def test_unreadable_shadow_remains_discoverable_but_unavailable():
    api = client()
    mock_connection(api)
    api._read_shadow = AsyncMock(side_effect=IntermaticConnectionError("unreachable"))
    result = await api.states([RECORD])
    assert len(result) == 1
    assert result[0]["Connected"] == 0
    assert result[0]["RelayOn"] is None


async def test_command_waits_for_partial_report_after_desired_ack():
    api = client()
    api.registry = {RECORD["thingName"]: RECORD}
    mqtt = mock_connection(api)
    api._read_shadow = AsyncMock(return_value=deepcopy(SHADOW))
    api._receive = AsyncMock(
        side_effect=[
            ("topic/accepted", {"version": 11, "state": {"desired": {"RelayOn": 1}}}),
            ("topic/accepted", {"version": 10, "state": {"reported": {"RelayOn": 1}}}),
            (
                "topic/accepted",
                {
                    "version": 12,
                    "state": {"reported": {"RelayOn": 1}},
                    "metadata": {"reported": {"RelayOn": {"timestamp": 200}}},
                },
            ),
        ]
    )
    confirmed = await api.set_relay("test-timer", True)
    assert api._receive.await_count == 3
    assert confirmed["RelayOn"] == 1
    assert confirmed["ProductModel"] == "STW700W"
    assert confirmed["RelayReportedAt"] == 200
    args = mqtt.publish.call_args
    assert args.args[0] == "$aws/things/test-timer/shadow/update"
    assert json.loads(args.args[1])["state"] == {"desired": {"RelayOn": 1}}
    assert args.kwargs["retain"] is False
    mqtt.unsubscribe.assert_awaited_once()


async def test_desired_ack_without_report_fails_without_optimistic_state():
    api = client()
    api.registry = {RECORD["thingName"]: RECORD}
    mqtt = mock_connection(api)
    api._read_shadow = AsyncMock(return_value=deepcopy(SHADOW))
    api._receive = AsyncMock(
        side_effect=[
            ("topic/accepted", {"version": 11, "state": {"desired": {"RelayOn": 1}}}),
            IntermaticConnectionError("timeout"),
        ]
    )
    with pytest.raises(IntermaticConnectionError, match="may still execute"):
        await api.set_relay("test-timer", True)
    mqtt.unsubscribe.assert_awaited_once()


async def test_known_offline_timer_receives_no_command():
    api = client()
    api.registry = {RECORD["thingName"]: RECORD}
    mqtt = mock_connection(api)
    shadow = deepcopy(SHADOW)
    shadow["state"]["reported"]["isOnline"] = 0
    api._read_shadow = AsyncMock(return_value=shadow)
    with pytest.raises(IntermaticConnectionError, match="unavailable"):
        await api.set_relay("test-timer", True)
    mqtt.publish.assert_not_called()


@pytest.mark.parametrize(
    ("field", "reported", "pending"),
    [
        ("RelayOn", 0, 1),
        ("Mode", 1, 0),
        ("EventSets", ["1080,127,1,0"], ["120,127,0,0"]),
    ],
)
@pytest.mark.parametrize("acknowledge", [True, False])
async def test_reverting_pending_change_requires_own_acknowledgment(
    field, reported, pending, acknowledge
):
    api = client()
    api.registry = {RECORD["thingName"]: RECORD}
    mqtt = mock_connection(api)
    shadow = deepcopy(SHADOW)
    shadow["state"]["reported"][field] = reported
    shadow["state"]["desired"] = {field: pending}
    api._read_shadow = AsyncMock(return_value=shadow)

    async def receive(*args):
        payload = json.loads(mqtt.publish.call_args.args[1])
        return "topic/accepted", {
            "version": 11,
            "clientToken": payload["clientToken"] if acknowledge else "another-client",
            "state": {"desired": {field: reported}},
        }

    api._receive = AsyncMock(side_effect=receive)
    if not acknowledge:

        async def unrelated_then_timeout(*args):
            if api._receive.await_count == 1:
                return await receive(*args)
            raise IntermaticConnectionError("timeout")

        api._receive.side_effect = unrelated_then_timeout

    async def revert():
        if field == "RelayOn":
            return await api.set_relay("test-timer", False)
        if field == "Mode":
            return await api.set_mode("test-timer", "Manual")
        events = [event.as_dict() for event in decode_schedule(reported)]
        return await api.set_schedule("test-timer", events)

    if acknowledge:
        result = await revert()
        assert result[field] == reported
        assert api._receive.await_count == 1
    else:
        with pytest.raises(IntermaticConnectionError, match="may still execute"):
            await revert()
    mqtt.publish.assert_awaited_once()
    payload = json.loads(mqtt.publish.call_args.args[1])
    assert payload["state"] == {"desired": {field: reported}}
    assert payload["version"] == 10
    mqtt.unsubscribe.assert_awaited_once()


@pytest.mark.parametrize("pending", [None, {}, {"RelayOn": 0}, {"Mode": 0}])
async def test_matching_report_without_conflicting_intent_is_noop(pending):
    api = client()
    api.registry = {RECORD["thingName"]: RECORD}
    mqtt = mock_connection(api)
    shadow = deepcopy(SHADOW)
    if pending is not None:
        shadow["state"]["desired"] = pending
    api._read_shadow = AsyncMock(return_value=shadow)
    assert (await api.set_relay("test-timer", False))["RelayOn"] == 0
    mqtt.publish.assert_not_awaited()


async def test_pending_command_reversal_rejects_version_conflicts():
    api = client()
    api.registry = {RECORD["thingName"]: RECORD}
    mqtt = mock_connection(api)
    shadow = deepcopy(SHADOW)
    shadow["state"]["desired"] = {"RelayOn": 1}
    api._read_shadow = AsyncMock(return_value=shadow)

    async def rejected(*args):
        payload = json.loads(mqtt.publish.call_args.args[1])
        return "topic/rejected", {"clientToken": payload["clientToken"], "code": 409}

    api._receive = AsyncMock(side_effect=rejected)
    with pytest.raises(IntermaticConnectionError, match="update rejected"):
        await api.set_relay("test-timer", False)
    mqtt.publish.assert_awaited_once()
    mqtt.unsubscribe.assert_awaited_once()


async def test_combined_discovery_deduplicates_and_routes_commands():
    api = IntermaticApi(AsyncMock(), asyncio.to_thread, "test@example.com")
    pe = {"ThingName": "pool", "OutputCfg": 80}
    api.get_rest_things = AsyncMock(return_value=[pe])
    api._ascend.discover = AsyncMock(return_value=[RECORD, {"thingName": "pool"}])
    api._ascend.registry = {RECORD["thingName"]: RECORD}
    api._ascend.states = AsyncMock(return_value=[normalize_timer(RECORD, SHADOW)])
    assert len(await api.get_things()) == 2
    api._ascend.states.assert_awaited_once_with([RECORD])
    api._ascend.set_relay = AsyncMock(return_value={"RelayOn": 1})
    api.patch_thing = AsyncMock()
    assert await api.set_relay("test-timer", 1, True) == {"RelayOn": 1}
    await api.set_relay("pool", 1, True)
    api.patch_thing.assert_awaited_once_with("pool", {"Relay1": 2404})


async def test_backend_failure_preserves_other_backend():
    api = IntermaticApi(AsyncMock(), asyncio.to_thread, "test@example.com")
    api.get_rest_things = AsyncMock(return_value=[{"ThingName": "pool"}])
    api._ascend.discover = AsyncMock(
        side_effect=IntermaticConnectionError("unreachable")
    )
    api._ascend.registry = {RECORD["thingName"]: RECORD}
    result = await api.get_things()
    assert result[0] == {"ThingName": "pool"}
    assert result[1]["Connected"] == 0
    api.get_rest_things.side_effect = IntermaticConnectionError("unreachable")
    result = await api.get_things()
    assert all(timer["Connected"] == 0 for timer in result)


@pytest.mark.parametrize("rest_available", [True, False])
async def test_ascend_credential_failure_preserves_healthy_rest_timers(rest_available):
    api = IntermaticApi(AsyncMock(), asyncio.to_thread, "test@example.com")
    pool = {"ThingName": "pool", "Connected": 1, "Relay1": 2304}
    api.get_rest_things = AsyncMock(return_value=[pool] if rest_available else [])
    api._ascend.discover = AsyncMock(return_value=[RECORD])
    api._ascend.registry = {RECORD["thingName"]: RECORD}
    api._ascend._request = AsyncMock(side_effect=IntermaticAuthError("STS HTTP 403"))
    if rest_available:
        result = await api.get_things()
        assert result[0] == pool
        assert result[1]["Connected"] == 0
        assert result[1]["RelayOn"] is None
        # The next successful poll restores ASCEND without replacing REST state.
        mock_connection(api._ascend)
        api._ascend._read_shadow = AsyncMock(return_value=deepcopy(SHADOW))
        result = await api.get_things()
        assert result[0] == pool
        assert result[1]["Connected"] == 1
    else:
        with pytest.raises(IntermaticAuthError):
            await api.get_things()


async def test_cached_rest_timers_do_not_hide_failure_of_both_backends():
    api = IntermaticApi(AsyncMock(), asyncio.to_thread, "test@example.com")
    api._rest_things = [{"ThingName": "pool", "Connected": 1}]
    api.get_rest_things = AsyncMock(side_effect=IntermaticConnectionError("offline"))
    api._ascend.discover = AsyncMock(return_value=[RECORD])
    api._ascend._request = AsyncMock(side_effect=IntermaticAuthError("STS HTTP 403"))
    with pytest.raises(IntermaticAuthError):
        await api.get_things()


async def test_discovery_only_does_not_open_broker():
    api = IntermaticApi(AsyncMock(), asyncio.to_thread, "test@example.com")
    api.get_rest_things = AsyncMock(return_value=[])
    api._ascend.discover = AsyncMock(return_value=[RECORD])
    api._ascend.states = AsyncMock()
    result = await api.get_things(include_states=False)
    assert len(result) == 1
    api._ascend.states.assert_not_awaited()


async def test_auth_scheme_and_refresh_retry():
    session = MagicMock()
    expired = AsyncMock(status=401)
    success = AsyncMock(status=200)
    success.text.return_value = "{}"
    success.json.return_value = {}
    contexts = []
    for response in (expired, success, success):
        context = AsyncMock()
        context.__aenter__.return_value = response
        contexts.append(context)
    session.request.side_effect = contexts
    api = IntermaticApi(session, asyncio.to_thread, "test@example.com")
    api.id_token = "expired-token"

    async def refresh():
        api.id_token = "new-token"

    api._refresh = AsyncMock(side_effect=refresh)
    await api._authorized_request("POST", "https://example.com/graphql")
    api._refresh.assert_awaited_once()
    await api._request("GET", "/things")
    calls = session.request.call_args_list
    assert [call.kwargs["headers"]["Authorization"] for call in calls] == [
        "expired-token",
        "new-token",
        "Bearer new-token",
    ]
    assert all(call.kwargs["allow_redirects"] is False for call in calls)


async def test_backend_errors_are_not_reported_as_empty_discovery():
    api = IntermaticApi(AsyncMock(), asyncio.to_thread, "test@example.com")
    api.get_rest_things = AsyncMock(return_value=[])
    api._ascend.discover = AsyncMock(
        side_effect=IntermaticConnectionError("unreachable")
    )
    with pytest.raises(IntermaticConnectionError):
        await api.get_things()


async def test_removed_timer_is_not_sent_to_rest():
    api = IntermaticApi(AsyncMock(), asyncio.to_thread, "test@example.com")
    api.patch_thing = AsyncMock()
    with pytest.raises(IntermaticError, match="no longer registered"):
        await api.set_relay("removed", 1, True)
    api.patch_thing.assert_not_awaited()
