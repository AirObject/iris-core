"""Run reviewed material locally with no activation or model sends.

This executable fixture consumes an externally approved immutable material
package. Its synthetic account and loopback-only transport prove local behavior.
Create and reopen are separate invocations against actual SQLite and files.
"""
import asyncio
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sys
import time
from types import MappingProxyType

from companion_memory.cognition.fixed_memory import FixedReviewAuthority
from companion_memory.ingress.events import plain
from companion_memory.information.errors import InformationRejected
from companion_memory.information.management import HostIdentity
from companion_memory.memory.initial_self_storage import InitialSelfBinding
from companion_memory.persistence import Committed, Found
from companion_memory.runtime.semantic_host import SemanticHost
from tests.information.publication_support import index_identity
from tests.semantic.evaluation import QueryGold, evaluate
from tests.semantic.fault_support import lexical
from tests.semantic.test_semantic_host import make_host, opened


PROPOSAL_DIGEST = 'e93241a2235b56b99774cc4bd61d4cfa7948ac71649cd4648369d592a0a7df2f'
MATERIAL_DIGEST = '0ef0c4dbc7c8b8ffbc50fd9e2270f663138f674079046c2ff57f8476fd03796f'
DECISION_DIGEST = '2643f1a824b6454213cabdf5bcef34fea50b373998e5687dc5a6cfb2f4394a81'


def verified(path: Path, digest: str):
    raw = path.read_bytes()
    assert sha256(raw).hexdigest() == digest, path.name
    return json.loads(raw)


async def run(base: Path, mode: str) -> None:
    """Establish or recover the same reviewed objects, then save full responses."""
    assert mode in ('CREATE_NEW', 'OPEN_EXISTING')
    proposal = verified(base / 'proposal.canonical.json', PROPOSAL_DIGEST)
    package = verified(base / 'material-package.json', MATERIAL_DIGEST)
    decision = verified(base / 'material-review-decision.json', DECISION_DIGEST)
    assert decision['expected_amended_proposal_sha256'] == PROPOSAL_DIGEST
    assert decision['reviewed_by'] == 'codex_supervisor'
    claims = {'instance_id': proposal['instance_id'], 'set_id': proposal['set_id'], 'entry_id': proposal['entry_id'],
              'review_ref': decision['review_ref'], 'review_digest': PROPOSAL_DIGEST,
              'manifest_digest': package['manifest_digest'], 'reviewed_by': decision['reviewed_by']}
    review = FixedReviewAuthority(lambda received: dict(received) == claims).grant(claims)
    root = base / 'local-instance'
    if mode == 'CREATE_NEW': root.mkdir(exist_ok=False)
    initial = proposal['self_initialization']; template = make_host(root, 1)
    host = SemanticHost(template.configuration, replace(template.resources, instance_id=proposal['instance_id'],
                        initial_self=InitialSelfBinding(**initial['binding']), review=review))
    output = {'mode': mode, 'execution': 'LOCAL_ONLY_CONTROLLED_CONFIGURATION', 'material_digest': MATERIAL_DIGEST,
              'proposal_digest': PROPOSAL_DIGEST, 'material_decision_digest': DECISION_DIGEST,
              'real_activation': False, 'supplier_effect_qualification': 'NOT_RUN'}
    try:
        ready = await opened(host, mode); assert type(ready) is Found, ready
        assert host.embedding is not None and host.semantic is not None and host.fixed is not None
        assert host.initial is not None and host.stored is not None and host.queries is not None
        assert host.authorization is None
        assert host.embedding.executions == 0
        config = {'database_id': host.stored.database_id, 'instance_id': proposal['instance_id'],
                  'snapshot_id': host.stored.snapshot_id}
        fixed = host.fixed
        def env(kind, key, payload):
            return fixed.envelope(kind, key, MappingProxyType(payload), 1)
        if mode == 'CREATE_NEW':
            result = await host.initial.register_initial_self(initial['original_key'], initial['input_kind'], initial['body'], initial['input_origin'])
            assert type(result) is Committed, result
            result = await host.register_entry('reviewed-entry', proposal['entry_id'], 'host', 'sample_platform', 'reviewed-external-entry')
            assert type(result) is Committed, result
            result = await host.register_initial_subjects('reviewed-subjects', proposal['subjects'], 'SYNTHETIC_FIXTURE')
            assert type(result) is Committed, result
            result = await fixed.begin(env('fixed_begin', 'begin', {'set_id': proposal['set_id'], 'config': config,
                                       **{k: claims[k] for k in ('manifest_digest', 'review_ref', 'review_digest')}}), time.monotonic() + 5)
            assert type(result) is Committed, result
            for n, member in enumerate(proposal['memories']):
                result = await fixed.add_member(env('fixed_add_member', 'add:' + str(n), {'set_id': proposal['set_id'], 'expected_revision': n + 1,
                    **{k: member[k] for k in ('ordinal', 'member_id', 'event_json', 'memory_json', 'content_digest')}}), time.monotonic() + 5)
                assert type(result) is Committed, result
            result = await fixed.seal(env('fixed_seal', 'seal', {'set_id': proposal['set_id'], 'expected_revision': 13}), time.monotonic() + 5)
            assert type(result) is Committed, result
        for n in range(12):
            result = await fixed.establish_one(env('fixed_establish', 'establish:' + str(n), {'set_id': proposal['set_id'],
                'expected_revision': 14 + n, 'ordinal': n, 'expected_member_revision': 1}), time.monotonic() + 5)
            assert type(result) is Committed, result
        management = await host.bind_management(replace(index_identity(), entry_id=proposal['entry_id']))
        await lexical(host, 'reviewed-lexical', management)
        identity = HostIdentity('reviewed-query', 'principal', 'host', proposal['entry_id'],
                                frozenset(('search_memory', 'resolve_recall')), (), time.monotonic() + 300)
        port = await host.bind_query(identity)
        responses = {}; strict = {}; requests = {}
        for evaluation_mode in ('LEXICAL_BASELINE', 'LEXICAL_ONLY'):
            rows = {}; sent = {}
            for item in proposal['queries']:
                request = dict(item['query']); qid = item['query_id']
                request['request_key'] = mode + ':' + evaluation_mode + ':' + qid
                if evaluation_mode == 'LEXICAL_BASELINE': request['retrieval_mode'] = 'LOCAL_LEXICAL_V1'
                else: request.update(allow_partial=True, require_complete=False)
                result = await port.search_memory(request); assert type(result) is Found, result
                rows[qid] = plain(result.value); sent[qid] = request
            responses[evaluation_mode] = rows; requests[evaluation_mode] = sent
        for item in proposal['queries']:
            request = dict(item['query']); request['request_key'] = mode + ':strict:' + item['query_id']
            result = await port.search_memory(request)
            assert type(result) is InformationRejected, result
            strict[item['query_id']] = {'request': request, 'result': repr(result)}
        business = await host.bind_business(replace(identity, binding_id='reviewed-http'))
        assert host.http is not None
        token = host.http.issue_test_session(business, time.monotonic() + 300)
        address, port_number = await host.http.start()
        request = dict(proposal['queries'][4]['query'])
        request.update(request_key=mode + ':http-empty', allow_partial=True, require_complete=False)
        body = json.dumps(request, ensure_ascii=False).encode()
        reader, writer = await asyncio.open_connection(address, port_number)
        writer.write(('POST /api/host/memory/search HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer ' + token
                      + '\r\nContent-Length: ' + str(len(body)) + '\r\n\r\n').encode() + body)
        await writer.drain(); raw = await reader.read(); writer.close(); await writer.wait_closed()
        (base / (mode + '-http-response.bin')).write_bytes(raw)
        assert raw.startswith(b'HTTP/1.1 200'), raw
        value = json.loads(raw.split(b'\r\n\r\n', 1)[1])['value']
        assert value['response_version'] == 2 and value['decision'] == 'INCOMPLETE_EMPTY'
        assert value['recall_id'] is None and not value['sections']['memories']
        gold = tuple(QueryGold(q['query_id'], {label['object_id']: label['grade'] for label in q['labels']}) for q in proposal['queries'])
        metrics = {key: evaluate(gold, rows, key) for key, rows in responses.items()}
        assert metrics['LEXICAL_ONLY']['empty_rejection_passed'] is True
        assert host.embedding.executions == 0 and host.authorization is None
        assert await host.embedding.ledger.read('requests_page', {'after': '', 'limit': 8}) == ()
        output.update(configuration_identity=config, fixed_members=12, original_establish_keys_confirmed=12,
                      provider_executions=0, native_requests=[], real_slots_reserved=0,
                      responses=responses, requests=requests, strict_rejections=strict, metrics=metrics,
                      hybrid_metrics=None, http_response_version=value['response_version'])
        if mode == 'OPEN_EXISTING':
            previous = json.loads((base / 'CREATE_NEW.json').read_text())
            assert previous['configuration_identity'] == config
            assert previous['metrics'] == metrics
            output['recovery_same_rankings_and_scores'] = True
    finally:
        assert await host.close()
        assert host.state == 'CLOSED'
    output['closed'] = True
    (base / (mode + '.json')).write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in output.items() if k not in ('responses', 'requests', 'strict_rejections', 'metrics')}, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(run(Path(sys.argv[1]).resolve(), sys.argv[2]))
