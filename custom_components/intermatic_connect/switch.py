"""Switch entities for Intermatic Connect timer outputs."""

from __future__ import annotations

from datetime import time
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import IntermaticError, pick, relay_is_on
from .ascend import is_ascend
from .const import DOMAIN, OUTPUT_COMBINED
from .coordinator import IntermaticCoordinator

SERVICE_SET_WEEKLY_SCHEDULE = "set_weekly_schedule"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IntermaticCoordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def add_new_timers() -> None:
        entities: list[IntermaticSwitch] = []
        for thing_id, thing in coordinator.data.items():
            if thing_id in known:
                continue
            if is_ascend(thing):
                entities.append(IntermaticAscendSwitch(coordinator, thing_id))
            else:
                output_cfg = int(pick(thing, "OutputCfg", default=0) or 0)
                if output_cfg & OUTPUT_COMBINED:
                    entities.append(
                        IntermaticSwitch(coordinator, thing_id, 1, 3, "Circuit 1 & 2")
                    )
                else:
                    if output_cfg & 0x01:
                        entities.append(
                            IntermaticSwitch(coordinator, thing_id, 1, 1, "Circuit 1")
                        )
                    if output_cfg & 0x04:
                        entities.append(
                            IntermaticSwitch(coordinator, thing_id, 2, 2, "Circuit 2")
                        )
                if output_cfg & 0x10:
                    entities.append(
                        IntermaticSwitch(coordinator, thing_id, 3, 4, "Circuit 3")
                    )
            known.add(thing_id)
        if entities:
            async_add_entities(entities)

    add_new_timers()
    entry.async_on_unload(coordinator.async_add_listener(add_new_timers))


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
        return self.coordinator.data.get(self.thing_id, {})

    @property
    def name(self) -> str | None:
        if self.circuit_mask == 3:
            return str(pick(self.thing, "FriendlyNameRelay1", default="") or self.fallback_name)
        return str(
            pick(self.thing, f"FriendlyNameRelay{self.relay}", default="")
            or self.fallback_name
        )

    @property
    def is_on(self) -> bool | None:
        return relay_is_on(self.thing, self.relay)

    @property
    def available(self) -> bool:
        return (
            super().available
            and bool(self.thing)
            and int(pick(self.thing, "Connected", default=1) or 0) != 0
        )

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
        await self._async_set_relay(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set_relay(False)

    async def _async_set_relay(self, on: bool) -> None:
        try:
            await self.coordinator.async_set_relay(self.thing_id, self.relay, on)
        except IntermaticError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_set_weekly_schedule(
        self, on_time: time, off_time: time, days: list[str], replace: bool = True
    ) -> None:
        try:
            confirmed = await self.coordinator.api.set_weekly_schedule(
                self.thing,
                self.circuit_mask,
                on_time,
                off_time,
                days,
                replace,
            )
        except IntermaticError as err:
            raise HomeAssistantError(str(err)) from err
        if confirmed is not None:
            self.coordinator.async_set_updated_data(
                {**self.coordinator.data, self.thing_id: confirmed}
            )
        else:
            await self.coordinator.async_request_refresh()

    async def async_set_ascend_schedule(
        self, events: list[dict], replace: bool = True
    ) -> None:
        if not is_ascend(self.thing):
            raise HomeAssistantError("This action is only supported for ASCEND timers")
        try:
            confirmed = await self.coordinator.api.set_ascend_schedule(
                self.thing_id, events, replace
            )
        except IntermaticError as err:
            raise HomeAssistantError(str(err)) from err
        self.coordinator.async_set_updated_data(
            {**self.coordinator.data, self.thing_id: confirmed}
        )


class IntermaticAscendSwitch(IntermaticSwitch):
    """ASCEND's single output, using reported cloud state."""

    def __init__(self, coordinator: IntermaticCoordinator, thing_id: str) -> None:
        super().__init__(coordinator, thing_id, 1, 1, "")

    @property
    def name(self) -> None:
        return None

    @property
    def is_on(self) -> bool | None:
        value = self.thing.get("RelayOn")
        return bool(value) if value in (0, 1) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"relay_reported_at": self.thing.get("RelayReportedAt")}
