"""Dependency-free asynchronous HTTP client skeleton."""

from __future__ import annotations

import asyncio
import json
from typing import cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from iris_memory_sdk.models import CapabilitiesEnvelope, ErrorEnvelope


class IrisMemoryApiError(RuntimeError):
    def __init__(self, envelope: ErrorEnvelope) -> None:
        self.envelope = envelope
        super().__init__(f"{envelope.code}: {envelope.message}")


class AsyncIrisMemoryClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def capabilities(self) -> CapabilitiesEnvelope:
        value = await asyncio.to_thread(self._request_json, "GET", "/v1/capabilities", None)
        return CapabilitiesEnvelope.from_value(value)

    async def negotiate(self, api_versions: tuple[str, ...] = ("v1",)) -> CapabilitiesEnvelope:
        value = await asyncio.to_thread(
            self._request_json,
            "POST",
            "/v1/negotiation",
            {"api_versions": list(api_versions)},
        )
        return CapabilitiesEnvelope.from_value(value)

    def _request_json(self, method: str, path: str, body: object | None) -> object:
        data = None if body is None else json.dumps(body).encode()
        request = Request(
            f"{self._base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                return cast(object, json.loads(response.read()))
        except HTTPError as error:
            value = cast(object, json.loads(error.read()))
            raise IrisMemoryApiError(ErrorEnvelope.from_value(value)) from error
