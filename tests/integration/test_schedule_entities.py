"""Exercise mode and schedule actions and calendar projections in Home Assistant."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from test_homeassistant import ASCEND, POOL, setup

from custom_components.intermatic_connect.ascend_calendar import AscendScheduleCalendar
from custom_components.intermatic_connect.const import DOMAIN
from custom_components.intermatic_connect.exceptions import IntermaticConnectionError

pytestmark = pytest.mark.asyncio


async def test_mode_select_tracks_only_confirmed_state(
    hass, enable_custom_integrations
):
    entry, api = await setup(hass, [ASCEND])
    entity_id = er.async_get(hass).async_get_entity_id(
        "select", DOMAIN, "ascend-one_operating_mode"
    )
    assert hass.states.get(entity_id).state == "Manual"
    api.set_mode.side_effect = IntermaticConnectionError("not confirmed")
    with pytest.raises(HomeAssistantError, match="not confirmed"):
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": entity_id, "option": "Automatic"},
            blocking=True,
        )
    assert hass.states.get(entity_id).state == "Manual"
    api.set_mode.side_effect = None
    api.set_mode.return_value = {**ASCEND, "Mode": 0}
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": entity_id, "option": "Automatic"},
        blocking=True,
    )
    assert hass.states.get(entity_id).state == "Automatic"
    api.set_mode.assert_awaited_with("ascend-one", "Automatic")
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_schedule_actions_update_calendar_and_preserve_mode(
    hass, enable_custom_integrations
):
    entry, api = await setup(hass, [ASCEND, POOL])
    registry = er.async_get(hass)
    switch_id = registry.async_get_entity_id("switch", DOMAIN, "ascend-one_relay_1")
    calendar_id = registry.async_get_entity_id(
        "calendar", DOMAIN, "ascend-one_schedule"
    )
    events = [{"time": "sunset", "state": "on", "days": ["monday"], "offset": 45}]
    api.set_ascend_schedule.return_value = {**ASCEND, "EventSets": ["32765,32,1,45"]}
    await hass.services.async_call(
        DOMAIN,
        "set_ascend_schedule",
        {"entity_id": switch_id, "events": events, "replace": False},
        blocking=True,
    )
    api.set_ascend_schedule.assert_awaited_once_with("ascend-one", events, False)
    assert hass.states.get(calendar_id).attributes["schedule_events"] == events
    assert hass.states.get(calendar_id).attributes["operating_mode"] == "Manual"
    api.set_weekly_schedule.return_value = ASCEND
    await hass.services.async_call(
        DOMAIN,
        "set_weekly_schedule",
        {
            "entity_id": switch_id,
            "on_time": "18:00:00",
            "off_time": "23:00:00",
            "days": ["monday"],
        },
        blocking=True,
    )
    assert len(hass.states.get(calendar_id).attributes["schedule_events"]) == 2
    pool_id = registry.async_get_entity_id("switch", DOMAIN, "pool-one_relay_1")
    with pytest.raises(HomeAssistantError, match="only supported for ASCEND"):
        await hass.services.async_call(
            DOMAIN,
            "set_ascend_schedule",
            {"entity_id": pool_id, "events": events},
            blocking=True,
        )
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_calendar_uses_local_weekday_and_signed_solar_offset(
    hass, enable_custom_integrations
):
    await hass.config.async_set_time_zone("America/New_York")
    entry, api = await setup(
        hass, [{**ASCEND, "EventSets": ["30,32,1,0", "32765,32,0,-45"]}]
    )
    calendar = AscendScheduleCalendar(entry.runtime_data, "ascend-one")
    calendar.hass = hass
    start = datetime(
        2026, 11, 16, 0, tzinfo=timezone.utc
    )  # Monday UTC, still Sunday locally.
    end = start + timedelta(days=2)

    def sunset(hass, name, day):
        assert name == "sunset"
        return datetime(day.year, day.month, day.day, 22, tzinfo=timezone.utc)

    with patch(
        "custom_components.intermatic_connect.ascend_calendar.get_astral_event_date",
        side_effect=sunset,
    ):
        events = await calendar.async_get_events(hass, start, end)
    assert len(events) == 1
    assert events[0].start.astimezone(timezone.utc) == start + timedelta(
        hours=5, minutes=30
    )
    assert events[0].end == start + timedelta(hours=21, minutes=15)
    assert all("Manual" in event.summary for event in events)
    assert "00:30 → Sunset -45 min" in events[0].summary
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_missing_schedule_unavailable_and_polar_sun_event_skipped(
    hass, enable_custom_integrations
):
    entry, api = await setup(hass, [{**ASCEND, "EventSets": None}])
    calendar_id = er.async_get(hass).async_get_entity_id(
        "calendar", DOMAIN, "ascend-one_schedule"
    )
    assert hass.states.get(calendar_id).state == "unavailable"
    entry.runtime_data.async_set_updated_data(
        {"ascend-one": {**ASCEND, "Mode": 2, "EventSets": ["32766,127,1,0"]}}
    )
    await hass.async_block_till_done()
    calendar = AscendScheduleCalendar(entry.runtime_data, "ascend-one")
    calendar.hass = hass
    with patch(
        "custom_components.intermatic_connect.ascend_calendar.get_astral_event_date",
        return_value=None,
    ):
        assert (
            await calendar.async_get_events(
                hass,
                datetime.now(timezone.utc),
                datetime.now(timezone.utc) + timedelta(days=2),
            )
            == []
        )
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_overnight_period_includes_previous_days_on_event(
    hass, enable_custom_integrations
):
    await hass.config.async_set_time_zone("UTC")
    entry, _ = await setup(
        hass, [{**ASCEND, "Mode": 0, "EventSets": ["32765,127,1,45", "120,127,0,0"]}]
    )
    calendar = AscendScheduleCalendar(entry.runtime_data, "ascend-one")
    calendar.hass = hass
    start = datetime(2026, 11, 17, 0, tzinfo=timezone.utc)

    def sunset(hass, name, day):
        return datetime(day.year, day.month, day.day, 16, 37, tzinfo=timezone.utc)

    with patch(
        "custom_components.intermatic_connect.ascend_calendar.get_astral_event_date",
        side_effect=sunset,
    ):
        events = await calendar.async_get_events(hass, start, start + timedelta(days=1))
    assert len(events) == 2
    assert events[0].start == start - timedelta(hours=6, minutes=38)
    assert events[0].end == start + timedelta(hours=2)
    assert events[1].start == start + timedelta(hours=17, minutes=22)
    assert events[1].end == start + timedelta(days=1, hours=2)
    assert all(event.summary == "On: Sunset +45 min → 02:00" for event in events)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_weekly_period_spanning_query_boundaries(
    hass, enable_custom_integrations
):
    await hass.config.async_set_time_zone("UTC")
    # Monday ON, Sunday OFF: both endpoints lie outside Friday's query.
    entry, _ = await setup(
        hass, [{**ASCEND, "EventSets": ["1380,32,1,0", "120,64,0,0"]}]
    )
    calendar = AscendScheduleCalendar(entry.runtime_data, "ascend-one")
    calendar.hass = hass
    start = datetime(2026, 11, 20, tzinfo=timezone.utc)
    events = await calendar.async_get_events(hass, start, start + timedelta(days=1))
    assert len(events) == 1
    assert events[0].start == datetime(2026, 11, 16, 23, tzinfo=timezone.utc)
    assert events[0].end == datetime(2026, 11, 22, 2, tzinfo=timezone.utc)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_redundant_on_does_not_shorten_period(hass, enable_custom_integrations):
    await hass.config.async_set_time_zone("UTC")
    entry, _ = await setup(
        hass,
        [{**ASCEND, "EventSets": ["1080,127,1,0", "1140,127,1,0", "1380,127,0,0"]}],
    )
    calendar = AscendScheduleCalendar(entry.runtime_data, "ascend-one")
    calendar.hass = hass
    start = datetime(2026, 11, 16, tzinfo=timezone.utc)
    events = await calendar.async_get_events(hass, start, start + timedelta(days=1))
    assert len(events) == 1
    assert events[0].start.hour == 18 and events[0].end.hour == 23
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("state", [0, 1])
async def test_single_direction_schedule_keeps_transition_markers(
    hass, enable_custom_integrations, state
):
    await hass.config.async_set_time_zone("UTC")
    entry, _ = await setup(hass, [{**ASCEND, "EventSets": [f"1080,127,{state},0"]}])
    calendar = AscendScheduleCalendar(entry.runtime_data, "ascend-one")
    calendar.hass = hass
    start = datetime(2026, 11, 16, tzinfo=timezone.utc)
    events = await calendar.async_get_events(hass, start, start + timedelta(days=2))
    assert len(events) == 2
    assert all(event.end - event.start == timedelta(minutes=1) for event in events)
    assert await hass.config_entries.async_unload(entry.entry_id)
