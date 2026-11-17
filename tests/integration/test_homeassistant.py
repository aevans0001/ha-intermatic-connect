"""Exercise entity creation and unloading in a real Home Assistant instance."""

from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_USERNAME
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.intermatic_connect.const import DOMAIN
from custom_components.intermatic_connect.exceptions import (
    IntermaticConnectionError,
    IntermaticError,
)

pytestmark = pytest.mark.asyncio

ASCEND = {
    "ThingName": "ascend-one",
    "FriendlyName": "Front light",
    "_backend": "ascend",
    "RelayOn": 0,
    "Connected": 1,
    "ProductModel": "STW700W",
    "FWVersion": "2.05",
    "Mode": 1,
    "EventSets": ["1080,127,1,0", "1380,127,0,0"],
}
POOL = {
    "ThingName": "pool-one",
    "FriendlyName": "Pool",
    "OutputCfg": 80,
    "Relay1": 2304,
    "Relay3": 2404,
    "Connected": 1,
}


async def setup(hass, timers):
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_USERNAME: "test@example.com", "refresh_token": "fake"}
    )
    entry.add_to_hass(hass)
    api = AsyncMock()
    api.get_things.return_value = deepcopy(timers)
    with patch("custom_components.intermatic_connect.IntermaticApi", return_value=api):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, api


async def test_mixed_setup_removal_recovery_and_unload(
    hass, enable_custom_integrations
):
    entry, api = await setup(hass, [ASCEND, POOL])
    registry = er.async_get(hass)
    ids = {
        entity.unique_id: entity.entity_id
        for entity in registry.entities.values()
        if entity.config_entry_id == entry.entry_id
    }
    assert set(ids) == {
        "ascend-one_relay_1",
        "ascend-one_operating_mode",
        "ascend-one_schedule",
        "pool-one_relay_1",
        "pool-one_relay_3",
        "pool-one_schedule",
        "pool-one_freeze_protection",
    }
    ascend_id = ids["ascend-one_relay_1"]
    assert hass.states.get(ascend_id).state == "off"
    coordinator = entry.runtime_data
    coordinator.async_set_updated_data({"pool-one": POOL})
    await hass.async_block_till_done()
    assert hass.states.get(ascend_id).state == "unavailable"
    coordinator.async_set_updated_data(
        {"ascend-one": {**ASCEND, "RelayOn": 1}, "pool-one": POOL}
    )
    await hass.async_block_till_done()
    assert hass.states.get(ascend_id).state == "on"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not coordinator._listeners


async def test_offline_discovery_still_creates_one_switch(
    hass, enable_custom_integrations
):
    entry, api = await setup(hass, [{**ASCEND, "Connected": 0, "RelayOn": None}])
    states = [s for s in hass.states.async_all() if s.entity_id.startswith("switch.")]
    assert len(states) == 1
    assert states[0].state == "unavailable"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_delayed_rest_discovery_adds_freeze_sensor_once(
    hass, enable_custom_integrations
):
    entry, api = await setup(hass, [ASCEND])
    coordinator = entry.runtime_data
    pool = {**POOL, "FreezeState": 1}
    for _ in range(2):
        coordinator.async_set_updated_data({"ascend-one": ASCEND, "pool-one": pool})
        await hass.async_block_till_done()
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "binary_sensor", DOMAIN, "pool-one_freeze_protection"
    )
    assert entity_id is not None
    assert hass.states.get(entity_id).state == "on"
    sensors = hass.states.async_all("binary_sensor")
    assert len(sensors) == 1
    coordinator.async_set_updated_data({"ascend-one": ASCEND})
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unavailable"
    coordinator.async_set_updated_data({"ascend-one": ASCEND, "pool-one": pool})
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "on"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not coordinator._listeners


async def test_command_error_retains_reported_state_and_schedule_errors_surface(
    hass, enable_custom_integrations
):
    entry, api = await setup(hass, [ASCEND])
    entity_id = er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, "ascend-one_relay_1"
    )
    api.set_relay.side_effect = IntermaticConnectionError("not confirmed")
    with pytest.raises(HomeAssistantError, match="not confirmed"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": entity_id}, blocking=True
        )
    assert hass.states.get(entity_id).state == "off"
    assert entry.runtime_data._pending_relays == {}
    api.set_relay.side_effect = None
    api.set_relay.return_value = {**ASCEND, "RelayOn": 1}
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": entity_id}, blocking=True
    )
    assert hass.states.get(entity_id).state == "on"
    assert entry.runtime_data._pending_relays == {}
    api.set_weekly_schedule.side_effect = IntermaticError("Schedule was not confirmed")
    with pytest.raises(HomeAssistantError, match="not confirmed"):
        await hass.services.async_call(
            DOMAIN,
            "set_weekly_schedule",
            {
                "entity_id": entity_id,
                "on_time": "18:00:00",
                "off_time": "23:00:00",
                "days": ["mon"],
            },
            blocking=True,
        )
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_timer_discovered_after_setup_is_added_once(
    hass, enable_custom_integrations
):
    entry, api = await setup(hass, [])
    coordinator = entry.runtime_data
    coordinator.async_set_updated_data({"ascend-one": ASCEND})
    await hass.async_block_till_done()
    coordinator.async_set_updated_data({"ascend-one": ASCEND})
    await hass.async_block_till_done()
    states = [s for s in hass.states.async_all() if s.entity_id.startswith("switch.")]
    assert len(states) == 1
    assert states[0].state == "off"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_config_flow_counts_both_backends_without_mqtt(
    hass, enable_custom_integrations
):
    api = AsyncMock()
    api.get_things.return_value = [ASCEND, POOL]
    api.refresh_token = "fake-refresh"
    api.refresh_username = "fake-sub"
    with (
        patch(
            "custom_components.intermatic_connect.config_flow.IntermaticApi",
            return_value=api,
        ),
        patch(
            "custom_components.intermatic_connect.async_setup_entry", return_value=True
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "user"},
            data={"username": "TEST@example.com", "password": "private-password"},
        )
        await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert result["title"] == "Intermatic Connect (2 timers)"
    assert "password" not in result["data"]
    api.get_things.assert_awaited_once_with(include_states=False)
