"""Binary sensors for Intermatic Connect timers."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import pick
from .const import DOMAIN
from .coordinator import IntermaticCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up freeze-protection sensors for each timer."""
    coordinator: IntermaticCoordinator = entry.runtime_data
    async_add_entities(
        IntermaticFreezeProtectionSensor(coordinator, thing_id)
        for thing_id in coordinator.data
    )


class IntermaticFreezeProtectionSensor(
    CoordinatorEntity[IntermaticCoordinator], BinarySensorEntity
):
    """Report whether the timer has activated freeze protection."""

    _attr_has_entity_name = True
    _attr_name = "Freeze protection"
    _attr_device_class = BinarySensorDeviceClass.COLD

    def __init__(self, coordinator: IntermaticCoordinator, thing_id: str) -> None:
        super().__init__(coordinator)
        self.thing_id = thing_id
        self._attr_unique_id = f"{thing_id}_freeze_protection"

    @property
    def thing(self) -> dict[str, Any]:
        """Return this timer's coordinator data."""
        return self.coordinator.data[self.thing_id]

    @property
    def is_on(self) -> bool:
        """Use the Android app's freeze-state bitfield decoder."""
        return (int(pick(self.thing, "FreezeState", default=0) or 0) & 0xFF) == 1

    @property
    def available(self) -> bool:
        return super().available and int(pick(self.thing, "Connected", default=1) or 0) != 0

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.thing_id)},
            name=str(pick(self.thing, "FriendlyName", default="Intermatic Timer")),
            manufacturer="Intermatic",
            model="PE733P",
        )
