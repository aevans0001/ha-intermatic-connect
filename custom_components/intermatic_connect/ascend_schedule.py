"""ASCEND's native weekly event format, separate from the PE/ETW codec."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time
from typing import Any

from .exceptions import IntermaticError

MODES = {"Automatic": 0, "Manual": 1, "Random": 2}
DAY_BITS = {
    "monday": 32,
    "tuesday": 16,
    "wednesday": 8,
    "thursday": 4,
    "friday": 2,
    "saturday": 1,
    "sunday": 64,
}
SOLAR_TIMES = {"sunrise": 32766, "sunset": 32765}
MAX_EVENTS = 42  # Each selected weekday consumes one event slot.


@dataclass(frozen=True)
class AscendEvent:
    minute: int
    days: int
    on: bool
    offset: int = 0

    def encode(self) -> str:
        return f"{self.minute},{self.days},{int(self.on)},{self.offset}"

    def as_dict(self) -> dict[str, Any]:
        event_time = next(
            (key for key, value in SOLAR_TIMES.items() if value == self.minute), None
        )
        return {
            "time": event_time or f"{self.minute // 60:02d}:{self.minute % 60:02d}",
            "days": [day for day, bit in DAY_BITS.items() if self.days & bit],
            "state": "on" if self.on else "off",
            "offset": self.offset,
        }


def decode_schedule(raw: Any) -> list[AscendEvent]:
    """Reject incomplete/unknown data rather than silently dropping timer events."""
    if not isinstance(raw, list):
        raise IntermaticError("ASCEND schedule is unavailable")
    result = []
    for value in raw:
        if not isinstance(value, str) or not re.fullmatch(
            r"-?\d+,-?\d+,-?\d+,-?\d+", value
        ):
            raise IntermaticError("Unrecognized ASCEND schedule format")
        minute, days, on, offset = map(int, value.split(","))
        if (
            minute not in SOLAR_TIMES.values()
            and not 0 <= minute < 1440
            or not 1 <= days <= 127
            or on not in (0, 1)
            or not -120 <= offset <= 120
            or minute < 1440
            and offset != 0
        ):
            raise IntermaticError("Unrecognized ASCEND schedule values")
        result.append(AscendEvent(minute, days, bool(on), offset))
    return result


def encode_events(events: Any) -> list[str]:
    """Encode user-facing entries using weekday bits and signed solar offsets."""
    if not isinstance(events, list):
        raise IntermaticError("Schedule events must be a list")
    result = []
    for event in events:
        if not isinstance(event, dict) or set(event) - {
            "time",
            "days",
            "state",
            "offset",
        }:
            raise IntermaticError("Invalid schedule event fields")
        value = event.get("time")
        if isinstance(value, time):
            if value.second or value.microsecond or value.tzinfo:
                raise IntermaticError(
                    "Schedule clock times must be local whole minutes"
                )
            minute = value.hour * 60 + value.minute
        elif isinstance(value, str) and value in SOLAR_TIMES:
            minute = SOLAR_TIMES[value]
        elif isinstance(value, str) and re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d", value
        ):
            hour, minutes = map(int, value.split(":"))
            minute = hour * 60 + minutes
        else:
            raise IntermaticError("Use HH:MM, sunrise, or sunset for schedule times")
        days = event.get("days")
        if (
            not isinstance(days, list)
            or not days
            or any(
                not isinstance(day, str) or day.lower() not in DAY_BITS for day in days
            )
        ):
            raise IntermaticError("Select one or more valid weekdays")
        mask = sum(DAY_BITS[day] for day in {day.lower() for day in days})
        action = event.get("state")
        offset = event.get("offset", 0)
        if action not in ("on", "off"):
            raise IntermaticError("Schedule state must be on or off")
        if type(offset) is not int or not -120 <= offset <= 120:
            raise IntermaticError(
                "Solar offset must be an integer from -120 to 120 minutes"
            )
        if minute < 1440 and offset:
            raise IntermaticError("Offsets apply only to sunrise and sunset")
        result.append(AscendEvent(minute, mask, action == "on", offset).encode())
    validate_schedule(result)
    return result


def validate_schedule(raw: list[str]) -> None:
    events = decode_schedule(raw)
    if sum(event.days.bit_count() for event in events) > MAX_EVENTS:
        raise IntermaticError(
            "ASCEND supports 42 weekly event slots; each selected day uses a slot"
        )
    occupied = set()
    for event in events:
        for bit in DAY_BITS.values():
            if event.days & bit:
                key = (bit, event.minute, event.offset)
                if key in occupied:
                    raise IntermaticError(
                        "Schedule contains overlapping events at the same time"
                    )
                occupied.add(key)
