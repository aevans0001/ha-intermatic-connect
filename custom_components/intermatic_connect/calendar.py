"""Combined calendar view of native Intermatic timer schedules."""

from __future__ import annotations

from collections import defaultdict

from homeassistant.helpers.sun import get_astral_event_date
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

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

    # Migrate the original generic entity ID (calendar.schedule) to a
    # timer-specific ID such as calendar.pool_schedule.
    for thing_id, thing in coordinator.data.items():
        existing_entity_id = registry.async_get_entity_id(
            "calendar", DOMAIN, f"{thing_id}_schedule"
        )
        timer_name = str(
            pick(thing, "FriendlyName", default="Intermatic") or "Intermatic"
        )
        desired_entity_id = f"calendar.{slugify(f'{timer_name}_schedule')}"
        if existing_entity_id and existing_entity_id != desired_entity_id:
            registry.async_update_entity(
                existing_entity_id,
                new_entity_id=desired_entity_id,
            )

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

    def __init__(self, coordinator: IntermaticCoordinator, thing_id: str) -> None:
        super().__init__(coordinator)
        self.thing_id = thing_id
        self._attr_unique_id = f"{thing_id}_schedule"
        timer_name = str(
            pick(coordinator.data[thing_id], "FriendlyName", default="Intermatic")
            or "Intermatic"
        )
        self._attr_name = f"{timer_name} Schedule"

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

    def _occurs_at(
        self,
        day: datetime.date,
        schedule_event: dict[str, Any],
        tzinfo: Any,
    ) -> datetime:
        """Return the local instant for a clock or astronomic timer event."""
        if schedule_event["is_astronomic"]:
            event_name = "sunrise" if schedule_event["is_dawn"] else "sunset"
            event_time = get_astral_event_date(self.hass, event_name, day)
            if event_time is None:
                raise ValueError(f"Unable to calculate {event_name}")
            return event_time + timedelta(minutes=schedule_event["astro_offset"])
        return datetime.combine(
            day,
            datetime.min.time().replace(
                hour=schedule_event["hour"], minute=schedule_event["minute"]
            ),
            tzinfo=tzinfo,
        )

    def _events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        raw = pick(self.thing, "Schedule", default={}) or {}
        circuits = self._circuits()
        occurrences: dict[int, list[tuple[datetime, dict[str, Any]]]] = defaultdict(list)

        # When an on and off slot have the same time, the timer means the
        # circuit stays on through the next day (for example, midnight to
        # midnight). Move that off occurrence to the following day.
        on_times: dict[int, set[tuple[int, int]]] = defaultdict(set)
        decoded_events: list[dict[str, Any]] = []
        for value in raw.values():
            schedule_event = decode_event(str(value))
            if schedule_event and schedule_event["circuit_mask"] in circuits:
                decoded_events.append(schedule_event)
                if schedule_event["turn_on"]:
                    on_times[schedule_event["circuit_mask"]].add(
                        (schedule_event["hour"], schedule_event["minute"])
                    )

        cursor = start.date() - timedelta(days=1)
        while cursor <= end.date() + timedelta(days=1):
            for schedule_event in decoded_events:
                if cursor.weekday() not in _WEEK_CODE_DAYS.get(
                    schedule_event["week_code"], set()
                ):
                    continue
                occurs_at = self._occurs_at(cursor, schedule_event, start.tzinfo)
                if (
                    not schedule_event["turn_on"]
                    and not schedule_event["is_astronomic"]
                    and (schedule_event["hour"], schedule_event["minute"])
                    in on_times[schedule_event["circuit_mask"]]
                ):
                    occurs_at += timedelta(days=1)
                occurrences[schedule_event["circuit_mask"]].append(
                    (occurs_at, schedule_event)
                )
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
                    end_time = occurs_at.strftime("%I:%M %p").lstrip("0").lower()
                    result.append(
                        CalendarEvent(
                            start=pending_on[0],
                            end=occurs_at,
                            # Home Assistant supplies the start time in the
                            # calendar grid, so this displays as:
                            # "10:00 am - 9:00 pm Pump".
                            summary=f"- {end_time} {circuits[circuit_mask]}",
                            uid=(
                                f"{self.thing_id}-{circuit_mask}-"
                                f"{pending_on[1]['uid']}-{schedule_event['uid']}-"
                                f"{pending_on[0].date().isoformat()}"
                            ),
                        )
                    )
                pending_on = None
        return sorted(result, key=lambda item: item.start)


