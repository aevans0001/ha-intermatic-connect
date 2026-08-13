"""Cloud client and schedule codec for Intermatic Connect."""

from __future__ import annotations

import asyncio
import base64
import json
import hashlib
import hmac
from collections.abc import Awaitable, Callable, Iterable
from datetime import time
import logging
from typing import Any

import aiohttp
from pycognito import Cognito

from .const import (
    API_BASE,
    CLIENT_ID,
    CLIENT_SECRET,
    DELETE_EVENT,
    RELAY_OFF,
    RELAY_ON,
    USER_POOL_ID,
)

_LOGGER = logging.getLogger(__name__)


class IntermaticError(Exception):
    """Base Intermatic error."""


class IntermaticAuthError(IntermaticError):
    """Authentication failed."""


class IntermaticConnectionError(IntermaticError):
    """Cloud connection failed."""


class IntermaticApi:
    """Intermatic Connect cloud API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        run_sync: Callable[..., Awaitable[Any]],
        username: str,
        refresh_token: str | None = None,
        refresh_username: str | None = None,
    ) -> None:
        self._session = session
        self._run_sync = run_sync
        self.username = username
        self.refresh_token = refresh_token
        self.refresh_username = refresh_username
        self.id_token: str | None = None
        self.access_token: str | None = None
        self._auth_lock = asyncio.Lock()

    def _new_cognito(self) -> Cognito:
        return Cognito(
            USER_POOL_ID,
            CLIENT_ID,
            client_secret=CLIENT_SECRET,
            username=self.refresh_username or self.username,
            id_token=self.id_token,
            access_token=self.access_token,
            refresh_token=self.refresh_token,
        )

    async def authenticate(self, password: str) -> None:
        """Authenticate with the same Cognito SRP flow used by the app."""
        cognito = await self._run_sync(self._new_cognito)
        try:
            await self._run_sync(cognito.authenticate, password)
        except Exception as err:
            raise IntermaticAuthError(str(err)) from err
        self._take_tokens(cognito)

    async def _refresh(self) -> None:
        """Refresh tokens with Cognito's unsigned public API."""
        if not self.refresh_token:
            raise IntermaticAuthError("No refresh token is available")
        async with self._auth_lock:
            if self.id_token:
                return
            payload = {
                "ClientId": CLIENT_ID,
                "ClientSecret": CLIENT_SECRET,
                "RefreshToken": self.refresh_token,
            }
            try:
                async with self._session.post(
                    "https://cognito-idp.us-east-1.amazonaws.com/",
                    headers={
                        "Content-Type": "application/x-amz-json-1.1",
                        "X-Amz-Target": "AWSCognitoIdentityProviderService.GetTokensFromRefreshToken",
                    },
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as response:
                    data = await response.json(content_type=None)
                    if response.status >= 400:
                        message = data.get("message", "Cognito token refresh failed")
                        raise IntermaticAuthError(str(message))
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
                raise IntermaticConnectionError(str(err)) from err
            result = data.get("AuthenticationResult") or {}
            self.id_token = result.get("IdToken")
            self.access_token = result.get("AccessToken")
            if not self.id_token or not self.access_token:
                raise IntermaticAuthError("Cognito returned no refreshed access tokens")
    def _take_tokens(self, cognito: Cognito) -> None:
        self.id_token = cognito.id_token
        self.access_token = cognito.access_token
        self.refresh_token = cognito.refresh_token or self.refresh_token
        self.refresh_username = _jwt_claim(self.id_token, "sub") or self.refresh_username

    async def _request(
        self, method: str, path: str, *, json: dict[str, Any] | None = None
    ) -> Any:
        if not self.id_token:
            await self._refresh()
        for attempt in range(2):
            try:
                async with self._session.request(
                    method,
                    f"{API_BASE}{path}",
                    headers={
                        "Authorization": f"Bearer {self.id_token}",
                        "Cache-Control": "no-cache",
                    },
                    json=json,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as response:
                    if response.status == 401 and attempt == 0:
                        self.id_token = None
                        await self._refresh()
                        continue
                    body = await response.text()
                    if response.status >= 400:
                        if response.status in (401, 403):
                            raise IntermaticAuthError(
                                f"Intermatic rejected the credentials ({response.status})"
                            )
                        raise IntermaticConnectionError(
                            f"Intermatic returned {response.status}: {body[:300]}"
                        )
                    if not body:
                        return None
                    try:
                        return await response.json(content_type=None)
                    except ValueError:
                        return body
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise IntermaticConnectionError(str(err)) from err
        raise IntermaticAuthError("Authentication expired")

    async def get_things(self) -> list[dict[str, Any]]:
        """Return all timers, including their native schedules."""
        data = await self._request("GET", "/things?include=schedule")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("Things", "things", "Items", "items"):
                if isinstance(data.get(key), list):
                    return data[key]
        raise IntermaticConnectionError("Unexpected response from /things")

    async def patch_thing(self, thing_name: str, values: dict[str, Any]) -> None:
        await self._request("PATCH", f"/things/{thing_name}", json=values)

    async def set_relay(self, thing_name: str, relay: int, on: bool) -> None:
        await self.patch_thing(
            thing_name, {f"Relay{relay}": RELAY_ON if on else RELAY_OFF}
        )

    async def set_weekly_schedule(
        self,
        thing: dict[str, Any],
        circuit_mask: int,
        on_time: time,
        off_time: time,
        days: Iterable[str],
        replace: bool = True,
    ) -> None:
        """Set a native weekly on/off schedule for one circuit configuration."""
        raw_schedule = thing.get("Schedule") or thing.get("schedule") or {}
        schedule = {int(key): str(value) for key, value in raw_schedule.items()}
        if replace:
            for uid, payload in list(schedule.items()):
                decoded = decode_event(payload)
                if decoded and decoded["circuit_mask"] == circuit_mask:
                    schedule[uid] = DELETE_EVENT

        week_codes = compact_days(days)
        used = {uid for uid, value in schedule.items() if value != DELETE_EVENT}
        for week_code in week_codes:
            for event_time, turn_on in ((on_time, True), (off_time, False)):
                uid = next((candidate for candidate in range(1, 97) if candidate not in used), None)
                if uid is None:
                    raise IntermaticError("The timer's 96 schedule-event slots are full")
                used.add(uid)
                schedule[uid] = encode_weekly_event(
                    uid, week_code, event_time, turn_on, circuit_mask
                )
        await self.patch_thing(thing_name(thing), {"Schedule": schedule})


def _jwt_claim(token: str | None, claim: str) -> str | None:
    """Read a claim from a Cognito-issued JWT received over TLS."""
    if not token:
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload))[claim]
        return str(value)
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def pick(data: dict[str, Any], *names: str, default: Any = None) -> Any:
    """Read a field regardless of JSON casing."""
    for name in names:
        if name in data:
            return data[name]
    folded = {str(key).casefold(): value for key, value in data.items()}
    for name in names:
        if name.casefold() in folded:
            return folded[name.casefold()]
    return default


def thing_name(thing: dict[str, Any]) -> str:
    return str(pick(thing, "ThingName", "thingName", "thing_name", default=""))


def relay_is_on(thing: dict[str, Any], relay: int) -> bool:
    """Match the Intermatic Android app: the relay state is its low byte."""
    value = int(pick(thing, f"Relay{relay}", default=RELAY_OFF) or RELAY_OFF)
    return (value & 0xFF) == 100


_DAY_CODES = {
    "sunday": 4,
    "monday": 5,
    "tuesday": 6,
    "wednesday": 7,
    "thursday": 8,
    "friday": 9,
    "saturday": 10,
}


def compact_days(days: Iterable[str]) -> list[int]:
    normalized = {str(day).strip().casefold() for day in days}
    all_days = set(_DAY_CODES)
    if normalized == all_days:
        return [0]  # Day / every day
    if normalized == {"monday", "tuesday", "wednesday", "thursday", "friday"}:
        return [2]  # Weekday
    if normalized == {"saturday", "sunday"}:
        return [1]  # Weekend
    invalid = normalized - all_days
    if invalid or not normalized:
        raise IntermaticError(f"Invalid weekday selection: {sorted(invalid)}")
    return [_DAY_CODES[day] for day in _DAY_CODES if day in normalized]


def encode_weekly_event(
    uid: int, week_code: int, event_time: time, turn_on: bool, circuit_mask: int
) -> str:
    """Encode the app's 14-byte recurring event format."""
    payload = [
        uid & 0xFF,
        (uid >> 8) & 0xFF,
        0x02,  # relative behavior
        13,  # Year
        1,  # Each
        0,
        week_code,
        0,
        0,
        event_time.hour,
        event_time.minute,
        0,
        100 if turn_on else 0,
        circuit_mask,
    ]
    return ",".join(str(value) for value in payload)


def decode_event(raw: str) -> dict[str, Any] | None:
    """Decode the fields needed for calendars and safe schedule replacement."""
    try:
        values = [int(value) for value in raw.split(",")]
    except (AttributeError, TypeError, ValueError):
        return None
    if len(values) != 14 or all(value == 255 for value in values):
        return None
    return {
        "uid": values[0] | (values[1] << 8),
        "flags": values[2],
        "month": values[3],
        "day": values[4] | (values[5] << 8),
        "week_code": values[6],
        "hour": values[9],
        "minute": values[10],
        "turn_on": values[12] == 100,
        "circuit_mask": values[13],
    }
