"""Native schedule encoding and version-guarded mode/schedule writes."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import time
from unittest.mock import AsyncMock

import pytest

from custom_components.intermatic_connect.api import IntermaticApi, decode_event
from custom_components.intermatic_connect.ascend import AscendClient
from custom_components.intermatic_connect.ascend_schedule import (
    decode_schedule,
    encode_events,
)
from custom_components.intermatic_connect.exceptions import (
    IntermaticConnectionError,
    IntermaticError,
)

pytestmark = pytest.mark.asyncio
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def event(when="18:00", state="on", days=None, offset=0):
    return {"time": when, "state": state, "days": days or DAYS, "offset": offset}


def transport(mode=1, schedule=None):
    api = AscendClient(AsyncMock(), asyncio.to_thread)
    api.registry = {"timer": {"thingName": "timer", "name": "Test timer"}}
    mqtt = AsyncMock()

    @asynccontextmanager
    async def connection():
        yield mqtt

    api._connection = connection
    api._read_shadow = AsyncMock(
        return_value={
            "version": 10,
            "state": {
                "reported": {
                    "RelayOn": 0,
                    "isOnline": 1,
                    "Mode": mode,
                    "EventSets": ["32765,127,1,30", "120,127,0,0"]
                    if schedule is None
                    else schedule,
                }
            },
        }
    )
    return api, mqtt


async def test_roundtrip_clock_solar_signed_offsets_and_day_bits():
    raw = encode_events(
        [
            event("sunset", offset=-45),
            event("02:00", "off"),
            event("sunrise", "off", ["sunday"]),
            event("12:00", days=["saturday"]),
        ]
    )
    assert raw == ["32765,127,1,-45", "120,127,0,0", "32766,64,0,0", "720,1,1,0"]
    assert encode_events([item.as_dict() for item in decode_schedule(raw)]) == raw


@pytest.mark.parametrize(
    "bad",
    [
        event("25:00"),
        event("18:00:30"),
        event("sunset", offset=121),
        event("12:00", offset=1),
        event(days=["funday"]),
        event(state="toggle"),
        event(offset=True),
        {**event(), "future_field": 1},
    ],
)
async def test_invalid_events_are_rejected(bad):
    with pytest.raises(IntermaticError):
        encode_events([bad])


async def test_capacity_counts_each_day_and_detects_conflicts():
    six = [event(f"{hour:02}:00") for hour in range(6)]
    assert len(encode_events(six)) == 6  # Six events daily = 42 slots.
    with pytest.raises(IntermaticError, match="42"):
        encode_events(six + [event("08:00", days=["monday"])])
    with pytest.raises(IntermaticError, match="overlapping"):
        encode_events([event("18:00"), event("18:00", "off", ["monday"])])


async def test_unknown_schedule_not_silently_discarded():
    for raw in (None, ["malformed"], ["50,0,1,0"], ["32767,127,1,0"]):
        with pytest.raises(IntermaticError):
            decode_schedule(raw)


async def test_mode_waits_for_fresh_report_and_keeps_relay_and_schedule():
    api, mqtt = transport()
    api._receive = AsyncMock(
        side_effect=[
            ("accepted", {"version": 11, "state": {"desired": {"Mode": 0}}}),
            ("accepted", {"version": 12, "state": {"reported": {"Mode": 0}}}),
        ]
    )
    result = await api.set_mode("timer", "Automatic")
    payload = json.loads(mqtt.publish.call_args.args[1])
    assert payload["state"] == {"desired": {"Mode": 0}}
    assert payload["version"] == 10
    assert result["Mode"] == 0 and result["RelayOn"] == 0
    assert result["EventSets"] == ["32765,127,1,30", "120,127,0,0"]
    assert api._receive.await_count == 2


@pytest.mark.parametrize("mode", ["Automatic", "Random"])
async def test_no_auto_or_random_without_a_schedule(mode):
    api, mqtt = transport(schedule=[])
    with pytest.raises(IntermaticError, match="Add a schedule"):
        await api.set_mode("timer", mode)
    mqtt.publish.assert_not_awaited()


async def test_append_reads_latest_and_confirms_reordered_report():
    api, mqtt = transport()
    expected = ["32765,127,1,30", "120,127,0,0", "720,32,1,0"]
    api._receive = AsyncMock(
        return_value=(
            "accepted",
            {
                "version": 12,
                "state": {"reported": {"EventSets": list(reversed(expected))}},
            },
        )
    )
    result = await api.set_schedule(
        "timer", [event("12:00", days=["monday"])], replace=False
    )
    payload = json.loads(mqtt.publish.call_args.args[1])
    assert payload["state"] == {"desired": {"EventSets": expected}}
    assert payload["version"] == 10
    assert result["Mode"] == 1 and result["RelayOn"] == 0


async def test_schedule_ack_without_report_does_not_succeed():
    api, mqtt = transport()
    api._receive = AsyncMock(
        side_effect=[
            (
                "accepted",
                {"version": 11, "state": {"desired": {"EventSets": ["1080,127,1,0"]}}},
            ),
            IntermaticConnectionError("timeout"),
        ]
    )
    with pytest.raises(IntermaticConnectionError, match="may still execute"):
        await api.set_schedule("timer", [event()])
    mqtt.unsubscribe.assert_awaited_once()


async def test_conflicting_version_rejection_is_not_retried():
    api, mqtt = transport()

    async def rejected(*args):
        token = json.loads(mqtt.publish.call_args.args[1])["clientToken"]
        return "topic/rejected", {"code": 409, "clientToken": token}

    api._receive = AsyncMock(side_effect=rejected)
    with pytest.raises(IntermaticConnectionError, match="refresh"):
        await api.set_mode("timer", "Automatic")
    assert mqtt.publish.await_count == 1


async def test_clear_requires_manual_and_does_not_change_mode():
    api, mqtt = transport(mode=0)
    with pytest.raises(IntermaticError, match="Manual"):
        await api.set_schedule("timer", [])
    mqtt.publish.assert_not_awaited()
    api, mqtt = transport(mode=1)
    api._receive = AsyncMock(
        return_value=(
            "accepted",
            {"version": 12, "state": {"reported": {"EventSets": []}}},
        )
    )
    result = await api.set_schedule("timer", [])
    assert result["EventSets"] == [] and result["Mode"] == 1


async def test_append_does_not_replace_unreadable_schedule():
    api, mqtt = transport(schedule=["future-format"])
    with pytest.raises(IntermaticError):
        await api.set_schedule("timer", [event()], replace=False)
    mqtt.publish.assert_not_awaited()


async def test_weekly_service_routes_ascend_and_preserves_pool_encoding():
    api = IntermaticApi(AsyncMock(), asyncio.to_thread, "test@example.com")
    api._ascend.set_schedule = AsyncMock(return_value={"Mode": 1})
    api.patch_thing = AsyncMock()
    ascend = {"ThingName": "timer", "_backend": "ascend"}
    assert await api.set_weekly_schedule(ascend, 1, time(18), time(23), ["monday"]) == {
        "Mode": 1
    }
    api._ascend.set_schedule.assert_awaited_once_with(
        "timer",
        [
            {"time": time(18), "days": ["monday"], "state": "on"},
            {"time": time(23), "days": ["monday"], "state": "off"},
        ],
        True,
    )
    api.patch_thing.assert_not_awaited()
    await api.set_weekly_schedule(
        {"ThingName": "pool", "Schedule": {}}, 3, time(18), time(23), ["monday"]
    )
    identifier, values = api.patch_thing.call_args.args
    assert identifier == "pool"
    decoded = [decode_event(value) for value in values["Schedule"].values()]
    assert [item["turn_on"] for item in decoded] == [True, False]
    assert all(item["circuit_mask"] == 3 and item["week_code"] == 5 for item in decoded)
