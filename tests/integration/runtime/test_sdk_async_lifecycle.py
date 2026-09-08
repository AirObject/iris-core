"""Real TCP slow/disconnected responses; cancellation closes actual HTTP work."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from iris_memory_sdk import AsyncIrisMemoryClient
from iris_memory_sdk.client import IrisMemoryTransportError


@asynccontextmanager
async def slow_server() -> AsyncIterator[tuple[str, dict[str, Any]]]:
    state: dict[str, Any] = {"active": 0, "peak": 0, "requests": 0, "disconnected": 0}
    handlers: set[asyncio.Task[None]] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        handlers.add(task)
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            content_length = next(
                (
                    int(line.split(b":")[1])
                    for line in headers.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                ),
                0,
            )
            if content_length:
                await reader.readexactly(content_length)
            state["requests"] += 1
            state["last_headers"] = headers.decode()
            if b"/disconnect" in headers:
                return
            if b"/fast" in headers:
                body = json.dumps(
                    {"api_version": "v1", "schema_version": 23, "capabilities": []}
                ).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\nConnection: close\r\n\r\n"
                    + body
                )
                await writer.drain()
            else:
                assert await reader.read() == b""
                state["disconnected"] += 1
        finally:
            state["active"] -= 1
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", state
    finally:
        server.close()
        await server.wait_closed()
        for task in tuple(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)


def test_real_http_deadline_and_unknown_write() -> None:
    async def run() -> None:
        async with slow_server() as (url, state):
            async with AsyncIrisMemoryClient(url, timeout_seconds=0.2) as client:
                with pytest.raises(IrisMemoryTransportError) as error:
                    await client.remember_claim({}, idempotency_key="same-key")
                assert error.value.code in {"deadline_exceeded", "socket_timeout"}
                assert error.value.result_unknown and error.value.request_id
                assert "Idempotency-Key: same-key" in state["last_headers"]
                await asyncio.sleep(0.02)
                assert state["active"] == 0 and state["disconnected"] == 1
            async with AsyncIrisMemoryClient(url + "/disconnect") as client:
                with pytest.raises(IrisMemoryTransportError) as error:
                    await client.capabilities()
                assert error.value.code == "transport_unavailable"
                assert not error.value.result_unknown

    asyncio.run(run())


def test_close_cancels_sockets_bounds_concurrency_and_borrows_client() -> None:
    async def run() -> None:
        async with slow_server() as (url, state), httpx.AsyncClient() as borrowed:
            client = AsyncIrisMemoryClient(
                url, max_in_flight=2, max_pending=4, http_client=borrowed
            )
            calls = [asyncio.create_task(client.capabilities()) for _ in range(4)]
            for _ in range(100):
                if state["requests"] == 2:
                    break
                await asyncio.sleep(0.005)
            assert state["requests"] == 2
            with pytest.raises(IrisMemoryTransportError) as full:
                await client.capabilities()
            assert full.value.code == "concurrency_exhausted" and not full.value.result_unknown
            await client.aclose()
            results = await asyncio.gather(*calls, return_exceptions=True)
            assert all(isinstance(result, asyncio.CancelledError) for result in results)
            await asyncio.sleep(0.02)
            assert state["active"] == 0 and state["peak"] == 2
            assert not borrowed.is_closed
            assert (await borrowed.get(url + "/fast")).status_code == 200
            await client.aclose()
            with pytest.raises(IrisMemoryTransportError) as closed:
                await client.capabilities()
            assert closed.value.code == "client_closed"

    asyncio.run(run())


def test_external_cancellation_stops_transport() -> None:
    async def run() -> None:
        async with slow_server() as (url, state), AsyncIrisMemoryClient(url) as client:
            call = asyncio.create_task(client.capabilities())
            while state["requests"] == 0:
                await asyncio.sleep(0.005)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
            await asyncio.sleep(0.02)
            assert state["active"] == 0

    asyncio.run(run())


def test_profile_and_required_capabilities_wire() -> None:
    async def run() -> None:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if "negotiation" in request.url.path:
                return httpx.Response(
                    200,
                    json={
                        "api_version": "v1",
                        "schema_version": 23,
                        "capabilities": ["profile.v1"],
                    },
                )
            return httpx.Response(200, json={"subject_id": "entity/id"})

        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(handle)) as transport,
            AsyncIrisMemoryClient("http://core", http_client=transport) as client,
        ):
            await client.negotiate(required_capabilities=("profile.v1",))
            profile = await client.get_entity_profile(
                "entity/id", agent_id="agent", space_id="space"
            )
            assert profile["subject_id"] == "entity/id"
            assert json.loads(requests[0].content)["required_capabilities"] == ["profile.v1"]
            assert requests[1].url.params["agent_id"] == "agent"
            assert b"entity%2Fid" in requests[1].url.raw_path

    asyncio.run(run())


def test_socket_timeout_is_distinct_and_retryable() -> None:
    async def run() -> None:
        def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("socket timed out", request=request)

        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as http,
            AsyncIrisMemoryClient("http://core", http_client=http) as client,
        ):
            with pytest.raises(IrisMemoryTransportError) as error:
                await client.capabilities()
            assert error.value.code == "socket_timeout" and error.value.retryable
            assert not error.value.result_unknown

    asyncio.run(run())
