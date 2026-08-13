"""Switch entities for Intermatic Connect timer outputs."""

from __future__ import annotations

from datetime import time
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.components.switch import SwitchEntity

from .api import IntermaticError, pick, relay_is_on
from .const import DOMAIN, OUTPUT_COMBINED
from .coordinator import IntermaticCoordinator

SERVICE_SET_WEEKLY_SCHEDULE = "set_weekly_schedule"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IntermaticCoordinator = entry.runtime_data
    entities: list[IntermaticSwitch] = []
    for thing_id, thing in coordinator.data.items():
        output_cfg = int(pick(thing, "OutputCfg", default=0) or 0)
        if output_cfg & OUTPUT_COMBINED:
            entities.append(IntermaticSwitch(coordinator, thing_id, 1, 3, "Circuit 1 & 2"))
        else:
            if output_cfg & 0x01:
                entities.append(IntermaticSwitch(coordinator, thing_id, 1, 1, "Circuit 1"))
            if output_cfg & 0x04:
                entities.append(IntermaticSwitch(coordinator, thing_id, 2, 2, "Circuit 2"))
        if output_cfg & 0x10:
            entities.append(IntermaticSwitch(coordinator, thing_id, 3, 4, "Circuit 3"))
    async_add_entities(entities)

class IntermaticSwitch(CoordinatorEntity[IntermaticCoordinator], SwitchEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: IntermaticCoordinator,
        thing_id: str,
        relay: int,
        circuit_mask: int,
        fallback_name: str,
    ) -> None:
        super().__init__(coordinator)
        self.thing_id = thing_id
        self.relay = relay
        self.circuit_mask = circuit_mask
        self.fallback_name = fallback_name
        self._attr_unique_id = f"{thing_id}_relay_{relay}"

    @property
    def thing(self) -> dict[str, Any]:
        return self.coordinator.data[self.thing_id]

    @property
    def name(self) -> str:
        if self.circuit_mask == 3:
            return str(pick(self.thing, "FriendlyNameRelay1", default="") or self.fallback_name)
        return str(
            pick(self.thing, f"FriendlyNameRelay{self.relay}", default="")
            or self.fallback_name
        )

    @property
    def is_on(self) -> bool:
        return relay_is_on(self.thing, self.relay)

    @property
    def available(self) -> bool:
        return super().available and int(pick(self.thing, "Connected", default=1) or 0) != 0

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.thing_id)},
            name=str(pick(self.thing, "FriendlyName", default="Intermatic Timer")),
            manufacturer="Intermatic",
            model=str(pick(self.thing, "ProductModel", default="PE/ETW Wi-Fi Timer")),
            sw_version=str(pick(self.thing, "FWVersion", default="")),
            hw_version=str(pick(self.thing, "HWVersion", default="")),
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_relay(self.thing_id, self.relay, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_relay(self.thing_id, self.relay, False)
    async def async_set_weekly_schedule(
        self, on_time: time, off_time: time, days: list[str], replace: bool = True
    ) -> None:
        try:
            await self.coordinator.api.set_weekly_schedule(
                self.thing,
                self.circuit_mask,
                on_time,
                off_time,
                days,
                replace,
            )
        except IntermaticError:
            raise
        await self.coordinator.async_request_refresh()
