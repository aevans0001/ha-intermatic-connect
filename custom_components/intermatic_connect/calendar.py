"""Calendar views of native Intermatic timer schedules."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import decode_event, pick
from .const import OUTPUT_COMBINED
from .coordinator import IntermaticCoordinator

_WEEK_CODE_DAYS = {
    0: set(range(7)),
    1: {5, 6},
    2: {0, 1, 2, 3, 4},
    4: {6},
    5: {0},
    6: {1},
    7: {2},
    8: {3},
    9: {4},
    10: {5},
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IntermaticCoordinator = entry.runtime_data
    entities: list[IntermaticScheduleCalendar] = []
    for thing_id, thing in coordinator.data.items():
        output_cfg = int(pick(thing, "OutputCfg", default=0) or 0)
        if output_cfg & OUTPUT_COMBINED:
            entities.append(IntermaticScheduleCalendar(coordinator, thing_id, 3, "Circuit 1 & 2"))
        else:
            if output_cfg & 0x01:
                entities.append(IntermaticScheduleCalendar(coordinator, thing_id, 1, "Circuit 1"))
            if output_cfg & 0x04:
                entities.append(IntermaticScheduleCalendar(coordinator, thing_id, 2, "Circuit 2"))
        if output_cfg & 0x10:
            entities.append(IntermaticScheduleCalendar(coordinator, thing_id, 4, "Circuit 3"))
    async_add_entities(entities)


class IntermaticScheduleCalendar(
    CoordinatorEntity[IntermaticCoordinator], CalendarEntity
):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: IntermaticCoordinator,
        thing_id: str,
        circuit_mask: int,
        fallback_name: str,
    ) -> None:
        super().__init__(coordinator)
        self.thing_id = thing_id
        self.circuit_mask = circuit_mask
        self.fallback_name = fallback_name
        self._attr_unique_id = f"{thing_id}_schedule_{circuit_mask}"

    @property
    def thing(self) -> dict[str, Any]:
        return self.coordinator.data[self.thing_id]

    @property
    def name(self) -> str:
        relay = 3 if self.circuit_mask == 4 else 1
        circuit = str(
            pick(self.thing, f"FriendlyNameRelay{relay}", default="")
            or self.fallback_name
        )
        return f"{circuit} Schedule"

    @property
    def event(self) -> CalendarEvent | None:
        now = dt_util.now()
        events = self._events(now, now + timedelta(days=8))
        return events[0] if events else None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        return self._events(start_date, end_date)

    def _events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        raw = pick(self.thing, "Schedule", default={}) or {}
        result: list[CalendarEvent] = []
        cursor = start.date()
        while cursor <= end.date():
            for value in raw.values():
                event = decode_event(str(value))
                if not event or event["circuit_mask"] != self.circuit_mask:
                    continue
                weekdays = _WEEK_CODE_DAYS.get(event["week_code"], set())
                if cursor.weekday() not in weekdays:
                    continue
                begins = datetime.combine(
                    cursor,
                    datetime.min.time().replace(
                        hour=event["hour"], minute=event["minute"]
                    ),
                    tzinfo=start.tzinfo,
                )
                if start <= begins < end:
                    result.append(
                        CalendarEvent(
                            start=begins,
                            end=begins + timedelta(minutes=1),
                            summary="Turn on" if event["turn_on"] else "Turn off",
                            uid=f"{self.thing_id}-{event['uid']}-{cursor.isoformat()}",
                        )
                    )
            cursor += timedelta(days=1)
        return sorted(result, key=lambda item: item.start)
