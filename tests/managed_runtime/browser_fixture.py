"""Local browser engineering participant; no external model destination is used.

Runs the actual product entry point with only Provider transport and synthetic
input origin substituted. Identity, HTTP, wizard and all domain owners remain
native. The browser must explicitly initialize, permit, review and publish.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

from companion_memory.runtime.managed_main import serve
from tests.daily_cognition.test_initial_persona_host import persona
from tests.daily_cognition.test_reasoning import responses
from .test_business import controlled_resources, setup_draft
from .memory_support import shared_formal_response


def deny_external_connections(event: str, arguments: tuple[object, ...]) -> None:
    """Irrevocable process audit hook: simulated browser work cannot dial suppliers."""
    if event == 'socket.connect':
        address = arguments[1]
        if type(address) is not tuple or address[0] != '127.0.0.1':
            raise PermissionError('Synthetic browser fixture permits only literal loopback connections.')
    if event == 'socket.getaddrinfo' and arguments[0] != '127.0.0.1':
        raise PermissionError('Synthetic browser fixture does not resolve external destinations.')


def main() -> None:
    if sys.argv[1:] in (['configuration'], ['form']):
        from companion_memory.configuration.deployment import resolve_deployment
        from companion_memory.configuration.managed_registry import registries
        from companion_memory.configuration.managed_bootstrap import bootstrap_snapshot
        from companion_memory.configuration.managed_form import form_view
        settings = resolve_deployment({})
        directories = {'media': ('/data/blobs', '/data/upload_staging'),
            'database': ('/data/db',), 'audit': ('/data/db',), 'provider_usage': ('/data/db',),
            'backup': ('/data/backups', '/data/backup_staging')}
        view = form_view(registries(settings, directories, 'sample_platform'),
            bootstrap_snapshot(settings, directories), '/data')
        if sys.argv[1:] == ['form']:
            print(json.dumps(view, ensure_ascii=False))
            return
        configuration = setup_draft(Path('/data'))['configuration']
        complete = {domain: {**{entry['key']: entry['initial'] for entry in entries},
            **configuration[domain]} for domain, entries in view['domains'].items()}
        print(json.dumps(complete, ensure_ascii=False))
        return
    sys.addaudithook(deny_external_connections)
    import socket
    with socket.socket() as blocked:
        try:
            blocked.connect(('192.0.2.1', 443))
        except PermissionError:
            print('EXTERNAL_CONNECT_DENIED_BY_PROCESS_AUDIT_HOOK', flush=True)
        else:
            raise AssertionError('External network guard did not reject before connect.')
    with responses((persona, shared_formal_response)) as (port, requests, failures):
        try:
            result = asyncio.run(serve(resource_factory=controlled_resources(port)))
        finally:
            print(json.dumps({'participant': 'SYNTHETIC_BROWSER_REVIEWER',
                'transport': 'CONTROLLED_LOOPBACK', 'requests': len(requests),
                'failures': failures, 'external_requests': 0}))
    raise SystemExit(result)


if __name__ == '__main__':
    main()
