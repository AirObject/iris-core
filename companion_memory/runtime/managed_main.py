"""Single-process managed entry point; no import-time files, network or models."""
from __future__ import annotations

import asyncio
from pathlib import Path
import signal

from companion_memory.configuration.deployment import environment_settings
from companion_memory.management.managed_application import ManagedApplication
from companion_memory.management.managed_http import ManagedHTTP
from companion_memory.persistence import Ready
from .managed_bootstrap import ManagedBootstrap
from .managed_logging import ManagedLogging
from .managed_business import ResourceFactory
from .managed_host_resources import host_resources


async def serve(*, resource_factory: ResourceFactory = host_resources) -> int:
    """Open protected bootstrap, serve compiled assets, then drain actual owners."""
    settings = environment_settings()
    bootstrap = ManagedBootstrap(settings)
    http: ManagedHTTP | None = None
    application: ManagedApplication | None = None
    logging = ManagedLogging()
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.set)
    try:
        opened = await bootstrap.open()
        if type(opened) is not Ready or bootstrap.resources is None:
            return 2
        logging.open(bootstrap)
        application = ManagedApplication(bootstrap, resource_factory=resource_factory)
        application.logging = logging
        application.business.logging = logging
        await application.maintenance.recover()
        await application.business.recover()
        await logging.attach(application)
        http = ManagedHTTP(settings, application.identity, application.dispatch, application.health,
            Path(__file__).resolve().parents[2] / 'web/dist', identity_source=lambda: application.identity, audit_source=lambda: application.audit)
        await http.start()
        await shutdown.wait()
        return 0
    finally:
        # Do not release the volume lock while owned work continues. The
        # deployment grace period may end the process; original recovery facts
        # remain authoritative when the next process starts.
        while True:
            closed = http is None or await http.close()
            if application is not None:
                maintenance = application.maintenance.task
                if maintenance is not None and not maintenance.done():
                    closed = False
                else:
                    bootstrap = application.bootstrap
                    if closed:
                        closed = application.identity.close() and await application.business.close()
            if closed:
                closed = await logging.close()
            if closed and await bootstrap.close():
                break
            await asyncio.sleep(0.2)


def main() -> None:
    raise SystemExit(asyncio.run(serve()))


if __name__ == '__main__':
    main()
