"""Intermatic Connect integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, service
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from .api import IntermaticApi
from .const import CONF_REFRESH_TOKEN, CONF_REFRESH_USERNAME, PLATFORMS
from .coordinator import IntermaticCoordinator

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register integration-level actions independently of loaded entries."""
    service.async_register_platform_entity_service(
        hass,
        "intermatic_connect",
        "set_weekly_schedule",
        entity_domain=SWITCH_DOMAIN,
        schema={
            vol.Required("on_time"): cv.time,
            vol.Required("off_time"): cv.time,
            vol.Required("days"): vol.All(cv.ensure_list, [cv.string]),
            vol.Optional("replace", default=True): cv.boolean,
        },
        func="async_set_weekly_schedule",
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    api = IntermaticApi(
        async_get_clientsession(hass),
        hass.async_add_executor_job,
        entry.data[CONF_USERNAME],
        entry.data[CONF_REFRESH_TOKEN],
        entry.data.get(CONF_REFRESH_USERNAME),
    )
    coordinator = IntermaticCoordinator(hass, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
