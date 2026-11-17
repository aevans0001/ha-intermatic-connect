"""Native operating-mode selection for ASCEND timers."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .ascend import is_ascend
from .ascend_schedule import MODES
from .const import DOMAIN
from .coordinator import IntermaticCoordinator
from .exceptions import IntermaticError


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up operating-mode selectors for discovered ASCEND timers."""
    coordinator: IntermaticCoordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def add_new_timers() -> None:
        entities: list[AscendModeSelect] = []
        for identifier, thing in coordinator.data.items():
            if is_ascend(thing) and identifier not in known:
                entities.append(AscendModeSelect(coordinator, identifier))
                known.add(identifier)
        if entities:
            async_add_entities(entities)

    add_new_timers()
    entry.async_on_unload(coordinator.async_add_listener(add_new_timers))


class AscendModeSelect(CoordinatorEntity[IntermaticCoordinator], SelectEntity):
    """Select a timer's native operating mode using confirmed cloud state."""

    _attr_has_entity_name = True
    _attr_name = "Operating mode"
    _attr_options = list(MODES)
    _attr_icon = "mdi:timer-cog"

    def __init__(self, coordinator: IntermaticCoordinator, identifier: str) -> None:
        super().__init__(coordinator)
        self.identifier = identifier
        self._attr_unique_id = f"{identifier}_operating_mode"

    @property
    def thing(self) -> dict[str, Any]:
        return self.coordinator.data.get(self.identifier, {})

    @property
    def current_option(self) -> str | None:
        return next(
            (name for name, value in MODES.items() if self.thing.get("Mode") == value),
            None,
        )

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.thing.get("Connected") == 1
            and self.current_option is not None
        )

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self.identifier)})

    async def async_select_option(self, option: str) -> None:
        try:
            await self.coordinator.async_set_mode(self.identifier, option)
        except IntermaticError as err:
            raise HomeAssistantError(str(err)) from err
