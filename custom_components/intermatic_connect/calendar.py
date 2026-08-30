"""Combined calendar view of native Intermatic timer schedules."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import decode_event, pick
from .const import DOMAIN, OUTPUT_COMBINED
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
    """Set up one combined schedule calendar for every timer."""
    coordinator: IntermaticCoordinator = entry.runtime_data
    registry = er.async_get(hass)
    active_ids = {f"{thing_id}_schedule" for thing_id in coordinator.data}
    for entity in list(registry.entities.values()):
        if (
            entity.config_entry_id == entry.entry_id
            and entity.domain == "calendar"
            and entity.platform == DOMAIN
            and entity.unique_id not in active_ids
        ):
            registry.async_remove(entity.entity_id)
    async_add_entities(
        IntermaticScheduleCalendar(coordinator, thing_id)
        for thing_id in coordinator.data
    )


class IntermaticScheduleCalendar(
    CoordinatorEntity[IntermaticCoordinator], CalendarEntity
):
    """Expose on/off schedule pairs as ordinary duration events."""

    _attr_has_entity_name = True
    _attr_name = "Schedule"

    def __init__(self, coordinator: IntermaticCoordinator, thing_id: str) -> None:
        super().__init__(coordinator)
        self.thing_id = thing_id
        self._attr_unique_id = f"{thing_id}_schedule"

    @property
    def thing(self) -> dict[str, Any]:
        return self.coordinator.data[self.thing_id]

    @property
    def event(self) -> CalendarEvent | None:
        now = dt_util.now()
        events = self._events(now - timedelta(days=1), now + timedelta(days=8))
        return next((event for event in events if event.end >= now), None)

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        return self._events(start_date, end_date)

    def _circuits(self) -> dict[int, str]:
        output_cfg = int(pick(self.thing, "OutputCfg", default=0) or 0)
        circuits: dict[int, str] = {}
        if output_cfg & OUTPUT_COMBINED:
            circuits[3] = str(
                pick(self.thing, "FriendlyNameRelay1", default="") or "Pumps"
            )
        else:
            if output_cfg & 0x01:
                circuits[1] = str(
                    pick(self.thing, "FriendlyNameRelay1", default="") or "Circuit 1"
                )
            if output_cfg & 0x04:
                circuits[2] = str(
                    pick(self.thing, "FriendlyNameRelay2", default="") or "Circuit 2"
                )
        if output_cfg & 0x10:
            circuits[4] = str(
                pick(self.thing, "FriendlyNameRelay3", default="") or "Lights"
            )
        return circuits

    def _events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        raw = pick(self.thing, "Schedule", default={}) or {}
        circuits = self._circuits()
        occurrences: dict[int, list[tuple[datetime, dict[str, Any]]]] = defaultdict(list)
        cursor = start.date() - timedelta(days=1)
        while cursor <= end.date() + timedelta(days=1):
            for value in raw.values():
                schedule_event = decode_event(str(value))
                if (
                    not schedule_event
                    or schedule_event["circuit_mask"] not in circuits
                    or cursor.weekday() not in _WEEK_CODE_DAYS.get(schedule_event["week_code"], set())
                ):
                    continue
                occurs_at = datetime.combine(
                    cursor,
                    datetime.min.time().replace(
                        hour=schedule_event["hour"], minute=schedule_event["minute"]
                    ),
                    tzinfo=start.tzinfo,
                )
                occurrences[schedule_event["circuit_mask"]].append((occurs_at, schedule_event))
            cursor += timedelta(days=1)

        result: list[CalendarEvent] = []
        for circuit_mask, scheduled_events in occurrences.items():
            pending_on: tuple[datetime, dict[str, Any]] | None = None
            for occurs_at, schedule_event in sorted(scheduled_events, key=lambda item: item[0]):
                if schedule_event["turn_on"]:
                    pending_on = (occurs_at, schedule_event)
                    continue
                if pending_on is None or occurs_at <= pending_on[0]:
                    continue
                if pending_on[0] < end and occurs_at > start:
                    result.append(
                        CalendarEvent(
                            start=pending_on[0],
                            end=occurs_at,
                            summary=circuits[circuit_mask],
                            uid=(
                                f"{self.thing_id}-{circuit_mask}-"
                                f"{pending_on[1]['uid']}-{schedule_event['uid']}-"
                                f"{pending_on[0].date().isoformat()}"
                            ),
                        )
                    )
                pending_on = None
        return sorted(result, key=lambda item: item.start)
