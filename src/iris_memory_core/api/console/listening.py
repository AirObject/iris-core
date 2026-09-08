"""Two listeners in one lifecycle; only the application listener serves /v1."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType
from typing import TYPE_CHECKING

import uvicorn
from fastapi import FastAPI

from iris_memory_core.api.console.app import create_console_app
from iris_memory_core.api.console.config import parse_bind

if TYPE_CHECKING:
    from iris_memory_core.runtime import ServiceConfig


class ManagedServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        # One outer handler owns both listeners, including startup failure.
        yield


async def run_pair(servers: tuple[ManagedServer, ManagedServer]) -> None:
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for server in servers:
            server.should_exit = True
        await asyncio.gather(*tasks)


def serve_separate(application: FastAPI, config: ServiceConfig) -> int:
    assert config.console_bind is not None
    host, port = parse_bind(config.console_bind)
    console = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    console.mount(
        "/console",
        create_console_app(
            store=application.state.runtime.uow,
            config=config.console_config(),
            archives=application.state.runtime.archives,
            embedding_runtime=application.state.runtime.projections.embedding_runtime,
            provider_generations=application.state.runtime.projections.provider_generations,
        ),
    )

    def server(app: FastAPI, host: str, port: int) -> ManagedServer:
        return ManagedServer(
            uvicorn.Config(
                app,
                host=host,
                port=port,
                timeout_graceful_shutdown=max(1, int(config.grace_seconds)),
                lifespan="on",
                proxy_headers=False,
                access_log=False,
            )
        )

    servers = (
        server(application, config.host, config.port),
        server(console, host, port),
    )

    def stop(signum: int, frame: FrameType | None) -> None:
        for server in servers:
            server.handle_exit(signum, frame)

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        asyncio.run(run_pair(servers))
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0 if all(server.started for server in servers) else 1
