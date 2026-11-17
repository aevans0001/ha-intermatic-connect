"""ASCEND registry and AWS IoT shadow transport."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import uuid4

import aiomqtt
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from .ascend_schedule import MODES, decode_schedule, encode_events, validate_schedule
from .exceptions import IntermaticAuthError, IntermaticConnectionError, IntermaticError

_LOGGER = logging.getLogger(__name__)
APPSYNC_URL = (
    "https://sa5dxythgvhd3hnncreklw34by.appsync-api.us-east-1.amazonaws.com/graphql"
)
STS_URL = "https://dbxobhqav6.execute-api.us-east-1.amazonaws.com/dev/token"
BROKER = "broker.iot.intermatic.io"
REGISTRY_QUERY = """
query GetTimers($input: QueryTimersInput) {
  getTimers(input: $input) { items { thingName name } nextToken }
}
"""
SHADOW_TIMEOUT = 15


def is_ascend(thing: dict[str, Any]) -> bool:
    """Identify normalized ASCEND records without guessing from relay fields."""
    return thing.get("_backend") == "ascend"


def _valid_id(identifier: Any) -> bool:
    return (
        isinstance(identifier, str)
        and re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", identifier) is not None
    )


def _binary(value: Any) -> int | None:
    return int(value) if type(value) in (int, bool) and value in (0, 1) else None


def normalize_timer(record: dict, shadow: dict | None = None) -> dict[str, Any]:
    """Keep entity metadata and relay state, excluding PINs and network settings."""
    state = (shadow or {}).get("state")
    reported = state.get("reported") if isinstance(state, dict) else None
    reported = reported if isinstance(reported, dict) else {}
    relay = _binary(reported.get("RelayOn"))
    result = {
        "_backend": "ascend",
        "ThingName": record["thingName"],
        "FriendlyName": record.get("name") or "ASCEND Timer",
        "ProductModel": reported.get("Model") or "ASCEND Timer",
        "FWVersion": reported.get("Version") or "",
        "RelayOn": relay,
        "Connected": int(_binary(reported.get("isOnline")) == 1 and relay is not None),
    }
    mode = reported.get("Mode")
    result["Mode"] = mode if type(mode) is int and mode in MODES.values() else None
    try:
        decode_schedule(reported.get("EventSets"))
    except IntermaticError:
        result["EventSets"] = None
    else:
        result["EventSets"] = list(reported["EventSets"])
    metadata = (shadow or {}).get("metadata")
    metadata = metadata.get("reported") if isinstance(metadata, dict) else None
    field = metadata.get("RelayOn") if isinstance(metadata, dict) else None
    timestamp = field.get("timestamp") if isinstance(field, dict) else None
    if type(timestamp) in (int, float):
        result["RelayReportedAt"] = timestamp
    return result


class AscendClient:
    """Use short-lived MQTT sessions so polling, cancellation and unload clean up."""

    def __init__(
        self,
        request: Callable[..., Awaitable[Any]],
        run_sync: Callable[..., Awaitable[Any]],
    ) -> None:
        self._request = request
        self._run_sync = run_sync
        self.registry: dict[str, dict] = {}
        self._credentials: dict | None = None
        self._credentials_expire = datetime.min.replace(tzinfo=timezone.utc)
        self._tls: ssl.SSLContext | None = None
        self._lock = asyncio.Lock()

    async def discover(self) -> list[dict]:
        """Replace the registry only after a complete paginated response."""
        registry = {}
        cursor = None
        cursors = set()
        for _ in range(100):
            query_input = {"pageSize": 100}
            if cursor is not None:
                query_input["nextToken"] = cursor
            body = await self._request(
                "POST",
                APPSYNC_URL,
                json={
                    "query": REGISTRY_QUERY,
                    "variables": {"input": query_input},
                },
            )
            if not isinstance(body, dict) or body.get("errors"):
                raise IntermaticConnectionError("ASCEND registry query failed")
            data = body.get("data")
            result = data.get("getTimers") if isinstance(data, dict) else None
            if not isinstance(result, dict) or not isinstance(
                result.get("items"), list
            ):
                raise IntermaticConnectionError("Invalid ASCEND registry response")
            for item in result["items"]:
                if not isinstance(item, dict) or not _valid_id(item.get("thingName")):
                    raise IntermaticConnectionError("Invalid ASCEND timer identifier")
                registry[item["thingName"]] = {
                    "thingName": item["thingName"],
                    "name": item.get("name"),
                }
            if "nextToken" not in result:
                raise IntermaticConnectionError("Missing ASCEND pagination token")
            cursor = result["nextToken"]
            if cursor is None or cursor == "":
                self.registry = registry
                return list(registry.values())
            if not isinstance(cursor, str) or cursor in cursors:
                raise IntermaticConnectionError("Invalid ASCEND pagination token")
            cursors.add(cursor)
        raise IntermaticConnectionError("ASCEND registry pagination limit exceeded")

    async def _websocket_path(self) -> str:
        now = datetime.now(timezone.utc)
        if (
            self._credentials is None
            or now + timedelta(minutes=5) >= self._credentials_expire
        ):
            body = await self._request("GET", STS_URL)
            credentials = body.get("Credentials") if isinstance(body, dict) else None
            if not isinstance(credentials, dict) or not all(
                isinstance(credentials.get(key), str) and credentials[key]
                for key in ("AccessKeyId", "SecretKey", "SessionToken", "Expiration")
            ):
                raise IntermaticConnectionError("ASCEND returned no broker credentials")
            try:
                expiration = datetime.fromisoformat(
                    credentials["Expiration"].replace("Z", "+00:00")
                )
                if expiration.tzinfo is None or expiration <= now:
                    raise ValueError
            except ValueError as err:
                raise IntermaticConnectionError(
                    "Invalid ASCEND credential expiration"
                ) from err
            self._credentials = credentials
            self._credentials_expire = expiration
        credentials = self._credentials
        request = AWSRequest(method="GET", url=f"https://{BROKER}/mqtt")
        SigV4QueryAuth(
            Credentials(credentials["AccessKeyId"], credentials["SecretKey"]),
            "iotdevicegateway",
            "us-east-1",
            expires=900,
        ).add_auth(request)
        signed = urlsplit(request.url)
        return (
            signed.path
            + "?"
            + signed.query
            + "&X-Amz-Security-Token="
            + quote(
                credentials["SessionToken"],
                safe="",
            )
        )

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiomqtt.Client]:
        """Serialize operations; never leave a persistent MQTT worker behind."""
        async with self._lock:
            if self._tls is None:
                self._tls = await self._run_sync(ssl.create_default_context)
            path = await self._websocket_path()
            try:
                async with aiomqtt.Client(
                    BROKER,
                    443,
                    identifier=uuid4().hex,
                    protocol=aiomqtt.ProtocolVersion.V311,
                    transport="websockets",
                    websocket_path=path,
                    tls_context=self._tls,
                    clean_session=True,
                    timeout=SHADOW_TIMEOUT,
                ) as mqtt:
                    yield mqtt
            except (aiomqtt.MqttError, OSError, asyncio.TimeoutError):
                # A signed WebSocket URL contains credentials. Never log the raw exception.
                self._credentials = None
                raise IntermaticConnectionError(
                    "ASCEND broker connection failed"
                ) from None

    async def _receive(
        self, mqtt: Any, topics: set[str], deadline: float
    ) -> tuple[str, dict]:
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise IntermaticConnectionError("ASCEND shadow response timed out")
            try:
                message = await asyncio.wait_for(anext(mqtt.messages), remaining)
            except asyncio.TimeoutError:
                raise IntermaticConnectionError(
                    "ASCEND shadow response timed out"
                ) from None
            topic = str(message.topic)
            if topic not in topics:
                continue
            try:
                body = json.loads(message.payload)
            except (TypeError, ValueError):
                raise IntermaticConnectionError(
                    "Invalid ASCEND shadow response"
                ) from None
            if not isinstance(body, dict):
                raise IntermaticConnectionError("Invalid ASCEND shadow response")
            return topic, body

    async def _read_shadow(self, mqtt: Any, identifier: str) -> dict:
        prefix = f"$aws/things/{identifier}/shadow/get"
        topics = {prefix + "/accepted", prefix + "/rejected"}
        await mqtt.subscribe([(topic, 1) for topic in topics])
        try:
            await mqtt.publish(prefix, b"", qos=1, retain=False)
            topic, body = await self._receive(
                mqtt,
                topics,
                asyncio.get_running_loop().time() + SHADOW_TIMEOUT,
            )
            if topic.endswith("/rejected"):
                raise IntermaticConnectionError("ASCEND shadow read rejected")
            return body
        finally:
            await mqtt.unsubscribe(list(topics))

    async def states(self, records: list[dict]) -> list[dict]:
        """Retain discoverable but unreachable timers as unavailable entities."""
        result = [normalize_timer(record) for record in records]
        if not records:
            return result
        try:
            async with self._connection() as mqtt:
                for index, record in enumerate(records):
                    try:
                        shadow = await self._read_shadow(mqtt, record["thingName"])
                        result[index] = normalize_timer(record, shadow)
                    except IntermaticConnectionError:
                        _LOGGER.debug(
                            "An ASCEND timer's cloud shadow could not be read"
                        )
        except IntermaticAuthError:
            raise
        except IntermaticConnectionError:
            # A failed connection invalidates every state from this polling session.
            result = [normalize_timer(record) for record in records]
            _LOGGER.warning(
                "ASCEND broker unavailable; its switches will be unavailable"
            )
        return result

    async def set_relay(self, identifier: str, on: bool) -> dict:
        return await self._update(identifier, lambda reported: {"RelayOn": int(on)})

    async def set_mode(self, identifier: str, option: str) -> dict:
        if option not in MODES:
            raise IntermaticError("Invalid ASCEND operating mode")

        def changes(reported: dict) -> dict:
            if option != "Manual" and not decode_schedule(reported.get("EventSets")):
                raise IntermaticError(
                    "Add a schedule before enabling Automatic or Random mode"
                )
            return {"Mode": MODES[option]}

        return await self._update(identifier, changes)

    async def set_schedule(
        self, identifier: str, events: list[dict], replace: bool = True
    ) -> dict:
        encoded = encode_events(events)

        def changes(reported: dict) -> dict:
            if replace:
                updated = list(encoded)
            else:
                existing = decode_schedule(reported.get("EventSets"))
                updated = [event.encode() for event in existing]
                updated.extend(event for event in encoded if event not in updated)
            validate_schedule(updated)
            if not updated and reported.get("Mode") != MODES["Manual"]:
                raise IntermaticError("Select Manual mode before clearing the schedule")
            return {"EventSets": updated}

        return await self._update(identifier, changes)

    @staticmethod
    def _matches(field: str, actual: Any, expected: Any) -> bool:
        if field == "EventSets":
            try:
                return sorted(
                    event.encode() for event in decode_schedule(actual)
                ) == sorted(event.encode() for event in decode_schedule(expected))
            except IntermaticError:
                return False
        return type(actual) in (int, bool) and actual == expected

    async def _update(self, identifier: str, changes: Callable[[dict], dict]) -> dict:
        """Read-modify-write one shadow with a version guard and reported confirmation."""
        record = self.registry.get(identifier)
        if record is None:
            raise IntermaticConnectionError("ASCEND timer is no longer registered")
        async with self._connection() as mqtt:
            initial = await self._read_shadow(mqtt, identifier)
            current = normalize_timer(record, initial)
            if not current["Connected"]:
                raise IntermaticConnectionError("ASCEND timer is unavailable")
            reported = initial["state"]["reported"]
            desired = changes(reported)
            pending = initial["state"].get("desired") or {}
            if not isinstance(pending, dict):
                raise IntermaticConnectionError("Invalid ASCEND desired state")
            reported_matches = all(
                self._matches(key, reported.get(key), value)
                for key, value in desired.items()
            )
            if reported_matches and all(
                key not in pending or self._matches(key, pending[key], value)
                for key, value in desired.items()
            ):
                return current
            version = initial.get("version")
            if type(version) is not int:
                raise IntermaticConnectionError("ASCEND shadow has no version")
            requested_version = version
            prefix = f"$aws/things/{identifier}/shadow/update"
            topics = {prefix + "/accepted", prefix + "/rejected"}
            token = uuid4().hex
            seen = set()
            await mqtt.subscribe([(topic, 1) for topic in topics])
            try:
                await mqtt.publish(
                    prefix,
                    json.dumps(
                        {
                            "state": {"desired": desired},
                            "clientToken": token,
                            "version": version,
                        }
                    ),
                    qos=1,
                    retain=False,
                )
                deadline = asyncio.get_running_loop().time() + SHADOW_TIMEOUT
                while True:
                    try:
                        topic, body = await self._receive(mqtt, topics, deadline)
                    except IntermaticConnectionError:
                        raise IntermaticConnectionError(
                            "ASCEND did not confirm the requested change; it may still execute"
                        ) from None
                    if topic.endswith("/rejected"):
                        if body.get("clientToken") == token:
                            raise IntermaticConnectionError(
                                "ASCEND update rejected; refresh and try again"
                            )
                        continue
                    state = body.get("state")
                    update = state.get("reported") if isinstance(state, dict) else None
                    if (
                        type(body.get("version")) is not int
                        or body["version"] <= version
                    ):
                        continue
                    version = body["version"]
                    # Replacing a pending command need not change the device's
                    # already matching report. Confirm our version-guarded write
                    # was accepted before returning that unchanged reported state.
                    acknowledged = (
                        state.get("desired") if isinstance(state, dict) else None
                    )
                    if (
                        reported_matches
                        and version == requested_version + 1
                        and body.get("clientToken") == token
                        and isinstance(acknowledged, dict)
                        and all(
                            self._matches(key, acknowledged.get(key), value)
                            for key, value in desired.items()
                        )
                    ):
                        return current
                    if not isinstance(update, dict):
                        continue
                    seen.update(update)
                    reported.update(update)
                    if isinstance(body.get("metadata"), dict):
                        fields = body["metadata"].get("reported")
                        if isinstance(fields, dict):
                            initial.setdefault("metadata", {}).setdefault(
                                "reported", {}
                            ).update(fields)
                    confirmed = normalize_timer(record, initial)
                    if (
                        seen.issuperset(desired)
                        and confirmed["Connected"]
                        and all(
                            self._matches(key, reported.get(key), value)
                            for key, value in desired.items()
                        )
                    ):
                        return confirmed
            finally:
                await mqtt.unsubscribe(list(topics))
