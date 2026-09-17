"""Synthetic native persona publication and controlled embedding resources.

The test participant explicitly approves generated fixture text. Production
initialization and all Provider bookkeeping remain unchanged.
"""
from dataclasses import replace
import time
from typing import cast

from companion_memory.configuration import PresentValue
from companion_memory.memory.formats import record
from companion_memory.persistence import Committed, Found
from companion_memory.provider.chat_transport import ChatTransport
from companion_memory.provider.credentials import CredentialResolver, CredentialLease, Available
from companion_memory.provider.values import Record, as_record
from .test_business import controlled_resources


def semantic_resources(port: int):
    generation = controlled_resources(port)
    def create(resources, candidate, key, name, authorize):
        original = generation(resources, candidate, key, name, authorize)
        profiles = next(entry.state.value for entry in candidate.foundation.list_entries()
            if entry.definition.key == 'provider.profiles' and type(entry.state) is PresentValue)
        account = next(as_record(profile)['account_id'] for profile in cast(tuple[Record, ...], profiles)
            if as_record(profile)['material_role'] == 'EMBEDDING_DOCUMENT')
        resolver = CredentialResolver(lambda *args: Available(CredentialLease(b'synthetic-semantic-only')))
        return replace(original, embedding_transport=ChatTransport.for_embedding(
            cast(Record, candidate.text.record('provider.embedding_transport')), resolver, cast(str, account),
            time.monotonic, loopback_port=port))
    return create


async def publish_fixture_persona(test, business):
    host = business.host
    assert host is not None and host.runtime is not None
    enabled = await business.set_dispatch('enable', None, True, business.dispatch_disclosure()['digest'])
    test.assertIs(type(enabled), Committed, enabled)
    prepared = await business.persona('prepare', {'key': 'prepare', 'self_revision': 1, 'epoch': host.runtime.gate.epoch})
    test.assertIs(type(prepared), Committed, prepared)
    pending = await business.persona('pending', {})
    assert type(pending) is Found
    run = record(pending.value['run'])
    await business.persona('generate', {'key': run['provider_operation_key'], 'generation': run['generation']})
    await host.combination.initial_persona.control.wait_actual()
    pending = await business.persona('pending', {})
    assert type(pending) is Found
    run, candidate = record(pending.value['run']), record(pending.value['candidate'])
    reviewed = await business.persona('review', {'key': 'test-participant-approval', 'run_revision': run['revision'],
        'candidate_id': candidate['object_id'], 'candidate_revision': candidate['revision'],
        'candidate_digest': pending.value['candidate_digest'], 'decision': 'APPROVE'})
    test.assertIs(type(reviewed), Committed, reviewed)
    pending = await business.persona('pending', {})
    assert type(pending) is Found
    run, candidate = record(pending.value['run']), record(pending.value['candidate'])
    published = await business.persona('publish', {'key': 'publish', 'run_revision': run['revision'],
        'candidate_id': candidate['object_id'], 'candidate_revision': candidate['revision'],
        'candidate_digest': pending.value['candidate_digest'], 'epoch': host.runtime.gate.epoch})
    test.assertIs(type(published), Committed, published)
    test.assertTrue(await business.business_ready())
