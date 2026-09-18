"""Real managed host fixture retaining all normal background schedulers."""
from __future__ import annotations

from pathlib import Path
from companion_memory.persistence import Ready, Committed, Found
from companion_memory.memory.formats import record
from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from companion_memory.management.managed_application import ManagedApplication
from companion_memory.management.managed_http import ManagedHTTP
from .test_business import setup_draft, controlled_resources


async def ready_application(test, root: Path, provider_port: int, *, port: int = 18180, communication_format: bool = True, static_root: Path | None = None, origin: str | None = None, legacy_test_sink: bool = False):
    """Publish a synthetic persona through the same reviewed native protocol."""
    root.mkdir(mode=0o700, exist_ok=True)
    settings = resolve_deployment({'deployment.data_root': str(root), 'deployment.port': port,
        'deployment.origin': origin or f'http://127.0.0.1:{port}',
        'deployment.trusted_proxy_peers': '127.0.0.1' if origin and origin.startswith('https:') else ''})
    bootstrap = ManagedBootstrap(settings, communication_format=communication_format)
    test.assertIs(type(await bootstrap.open()), Ready)
    application = ManagedApplication(bootstrap, resource_factory=controlled_resources(provider_port))
    business, identity = application.business, application.identity
    draft = setup_draft(root)
    if legacy_test_sink:
        draft['configuration']['information']['goals.delivery']['sink_mode'] = 'TEST_HTTP'
    test.assertIs(type(await identity.save_draft('draft', None, draft)), Committed)
    await business.initialize('initialize', 1)
    host = business.host
    assert host is not None and host.runtime is not None
    test.assertIs(type(await business.persona('prepare', {'key': 'prepare', 'self_revision': 1, 'epoch': host.runtime.gate.epoch})), Committed)
    test.assertIs(type(await business.set_dispatch('synthetic-enable', None, True, str(business.dispatch_disclosure()['digest']))), Committed)
    pending = await business.persona('pending', {})
    assert type(pending) is Found
    run = record(pending.value['run'])
    await business.persona('generate', {'key': run['provider_operation_key'], 'generation': run['generation']})
    await host.combination.initial_persona.control.wait_actual()
    pending = await business.persona('pending', {})
    assert type(pending) is Found
    run, candidate = record(pending.value['run']), record(pending.value['candidate'])
    test.assertIs(type(await business.persona('review', {'key': 'approve', 'run_revision': run['revision'],
        'candidate_id': candidate['object_id'], 'candidate_revision': candidate['revision'],
        'candidate_digest': pending.value['candidate_digest'], 'decision': 'APPROVE'})), Committed)
    pending = await business.persona('pending', {})
    assert type(pending) is Found
    run, candidate = record(pending.value['run']), record(pending.value['candidate'])
    test.assertIs(type(await business.persona('publish', {'key': 'publish', 'run_revision': run['revision'],
        'candidate_id': candidate['object_id'], 'candidate_revision': candidate['revision'],
        'candidate_digest': pending.value['candidate_digest'], 'epoch': host.runtime.gate.epoch})), Committed)
    test.assertIs(type(await business.set_dispatch('synthetic-pause', 1, False, str(business.dispatch_disclosure()['digest']))), Committed)
    test.assertTrue(await business.business_ready())
    static = root.parent / 'communication-static'
    static.mkdir(exist_ok=True)
    for name in ('index.html', 'app.js', 'style.css'): (static / name).write_text('synthetic static fixture')
    http = ManagedHTTP(settings, identity, application.dispatch, application.health, static_root or static,
        communication_source=lambda: application.business.communication)
    await http.start()
    return application, http


async def enable_communication(test, application: ManagedApplication) -> None:
    """Use a real persisted candidate, activation decision and consumer ACKs."""
    from companion_memory.configuration.communication_schema import VALUES
    manager = application.business.configuration
    assert manager is not None
    status = await manager.status()
    values = {key: dict(value) for key, value in VALUES.items()}
    values['communication.ws']['enabled'] = True
    values['communication.delivery']['sink_mode'] = 'WS'
    patch: dict[str, object] = {'text': values}
    _, preview = await manager.preview(status['revision'], patch)
    saved = await manager.save('enable-communication', status['revision'], patch, preview['plan_digest'],
        actor='administrator', reason='Synthetic communication qualification')
    test.assertIs(type(saved), Committed, saved)
    assert type(saved) is Committed
    activation_id = record(saved.receipt.result)['activation_id']
    activated = await manager.activate(str(activation_id))
    test.assertEqual(activated['state'], 'APPLIED', activated)
    test.assertTrue(application.business.communication.available())


async def confirm_original(test, call):
    """Exercise finite same-key retries after explicit storage admission refusal.

    These refusals remain qualification evidence; a subsequent commit is not
    evidence that storage's deliberately single-writer admission was widened.
    """
    import asyncio
    import time
    from companion_memory.persistence import Rejected
    end = time.monotonic()+3
    while True:
        result = await call()
        if type(result) is Committed: return result
        if type(result) is not Rejected or result.error.reason != 'ADMISSION_BUSY' or time.monotonic()>=end:
            test.assertIs(type(result), Committed, result)
            return result
        print({'original_confirmation': 'SAME_KEY', 'admission_refusal': result.error.reason}, flush=True)
        await asyncio.sleep(.02)
