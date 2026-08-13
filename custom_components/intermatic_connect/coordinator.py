"""Data coordinator for Intermatic Connect."""

from __future__ import annotations

import asyncio

from datetime import timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import IntermaticApi, IntermaticError, relay_is_on, thing_name
from .const import DOMAIN, RELAY_OFF, RELAY_ON, SCAN_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)


class IntermaticCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    def __init__(self, hass: HomeAssistant, api: IntermaticApi) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=SCAN_INTERVAL_SECONDS),
        )
        self.api = api
        self._pending_relays: dict[tuple[str, int], bool] = {}

    async def async_set_relay(self, thing_id: str, relay: int, on: bool) -> None:
        """Send a command and retain its requested state until cloud confirms it."""
        await self.api.set_relay(thing_id, relay, on)
        self._pending_relays[(thing_id, relay)] = on
        optimistic = {key: dict(value) for key, value in self.data.items()}
        if thing := optimistic.get(thing_id):
            thing[f"Relay{relay}"] = RELAY_ON if on else RELAY_OFF
        self.async_set_updated_data(optimistic)
        self.hass.async_create_task(self._async_confirm_pending_relays())

    async def _async_confirm_pending_relays(self) -> None:
        """Poll briefly after a command; Intermatic cloud updates are eventual."""
        for delay in (2, 3, 5, 8, 12):
            await asyncio.sleep(delay)
            if not self._pending_relays:
                return
            await self.async_request_refresh()

    async def _async_update_data(self) -> dict[str, dict]:
        try:
            things = await self.api.get_things()
        except IntermaticError as err:
            raise UpdateFailed(str(err)) from err
        data = {thing_name(thing): thing for thing in things if thing_name(thing)}
        for key, requested_on in list(self._pending_relays.items()):
            thing_id, relay = key
            thing = data.get(thing_id)
            if thing is None:
                continue
            if relay_is_on(thing, relay) == requested_on:
                self._pending_relays.pop(key, None)
            else:
                thing[f"Relay{relay}"] = RELAY_ON if requested_on else RELAY_OFF
        return data
