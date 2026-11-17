"""Calendar of ASCEND's programmed on-periods."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .ascend_schedule import DAY_BITS, MODES, SOLAR_TIMES, AscendEvent, decode_schedule
from .const import DOMAIN
from .coordinator import IntermaticCoordinator
from .exceptions import IntermaticError


class AscendScheduleCalendar(CoordinatorEntity[IntermaticCoordinator], CalendarEntity):
    """Show programmed transitions, including schedules currently disabled by Manual mode."""

    _attr_has_entity_name = True
    _attr_name = "Schedule"

    def __init__(self, coordinator: IntermaticCoordinator, identifier: str) -> None:
        super().__init__(coordinator)
        self.identifier = identifier
        self._attr_unique_id = f"{identifier}_schedule"

    @property
    def thing(self) -> dict[str, Any]:
        return self.coordinator.data.get(self.identifier, {})

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.thing.get("Connected") == 1
            and self.thing.get("EventSets") is not None
        )

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self.identifier)})

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        try:
            events = [
                event.as_dict()
                for event in decode_schedule(self.thing.get("EventSets"))
            ]
        except IntermaticError:
            events = None
        return {
            "schedule_events": events,
            "operating_mode": next(
                (
                    name
                    for name, value in MODES.items()
                    if self.thing.get("Mode") == value
                ),
                None,
            ),
        }

    @property
    def event(self) -> CalendarEvent | None:
        now = dt_util.now()
        return next(iter(self._events(now, now + timedelta(days=8))), None)

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        return self._events(start_date, end_date)

    def _events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        try:
            events = decode_schedule(self.thing.get("EventSets"))
        except IntermaticError:
            return []
        local_start, local_end = dt_util.as_local(start), dt_util.as_local(end)
        # A weekly ON and its next OFF can lie on either side of the query.
        # Include a full week plus a day for solar offsets crossing midnight.
        day = local_start.date() - timedelta(days=8)
        occurrences: list[tuple[datetime, int, AscendEvent]] = []
        mode = self.thing.get("Mode")
        label = {0: "", 1: " (Manual — inactive)", 2: " (Random — nominal time)"}.get(
            mode, " (mode unknown)"
        )
        while day <= local_end.date() + timedelta(days=8):
            bit = list(DAY_BITS.values())[day.weekday()]
            for index, event in enumerate(events):
                if not event.days & bit:
                    continue
                solar = next(
                    (
                        name
                        for name, minute in SOLAR_TIMES.items()
                        if minute == event.minute
                    ),
                    None,
                )
                if solar:
                    occurs_at = get_astral_event_date(self.hass, solar, day)
                    if occurs_at is None:
                        continue
                    occurs_at += timedelta(minutes=event.offset)
                else:
                    occurs_at = datetime.combine(
                        day, datetime.min.time(), tzinfo=local_start.tzinfo
                    ).replace(hour=event.minute // 60, minute=event.minute % 60)
                occurrences.append((occurs_at, index, event))
            day += timedelta(days=1)

        result: list[CalendarEvent] = []
        pending_on: tuple[datetime, int, AscendEvent] | None = None
        for occurs_at, index, event in sorted(
            occurrences, key=lambda item: (item[0], item[1])
        ):
            if event.on:
                # Repeated ON commands do not shorten an already active period.
                if pending_on is None:
                    pending_on = (occurs_at, index, event)
                continue
            if pending_on is not None:
                on_at, on_index, on_event = pending_on
                if on_at < occurs_at and on_at < end and occurs_at > start:
                    result.append(
                        CalendarEvent(
                            start=on_at,
                            end=occurs_at,
                            summary=f"On: {self._time_label(on_event)} → {self._time_label(event)}{label}",
                            description=self._description(on_event, event),
                            uid=f"{self.identifier}-{on_index}-{index}-{on_at.date().isoformat()}",
                        )
                    )
                pending_on = None
            elif start - timedelta(minutes=1) < occurs_at < end:
                # OFF-only schedules still need a visible transition.
                result.append(self._transition(occurs_at, index, event, label))
        if pending_on is not None:
            on_at, on_index, on_event = pending_on
            if start - timedelta(minutes=1) < on_at < end:
                result.append(self._transition(on_at, on_index, on_event, label))
        if not any(not event.on for _, _, event in occurrences):
            # With no OFF event there is no finite on-period to project.
            result = [
                self._transition(at, index, event, label)
                for at, index, event in occurrences
                if start - timedelta(minutes=1) < at < end
            ]
        return sorted(result, key=lambda event: event.start)

    @staticmethod
    def _time_label(event: AscendEvent) -> str:
        value = event.as_dict()["time"]
        if value in SOLAR_TIMES:
            value = value.capitalize()
            if event.offset:
                value += f" {event.offset:+d} min"
        return value

    def _description(
        self, on_event: AscendEvent, off_event: AscendEvent | None = None
    ) -> str:
        text = (
            f"On at {self._time_label(on_event)}"
            if on_event.on
            else f"Off at {self._time_label(on_event)}"
        )
        if off_event is not None:
            text += f"; off at {self._time_label(off_event)}"
        return (
            text
            + ". Programmed schedule, not live relay state. Calendar uses Home Assistant's time zone and location; Random timing varies."
        )

    def _transition(
        self, at: datetime, index: int, event: AscendEvent, label: str
    ) -> CalendarEvent:
        return CalendarEvent(
            start=at,
            end=at + timedelta(minutes=1),
            summary=f"{'On' if event.on else 'Off'} at {self._time_label(event)}{label}",
            description=self._description(event),
            uid=f"{self.identifier}-{index}-{at.date().isoformat()}",
        )
