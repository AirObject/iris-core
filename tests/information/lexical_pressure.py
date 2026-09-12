"""Actual formal objects, lexical index publication and cold-owner validation.

The candidate is explicitly synthetic; every object and index write uses the
ordinary owners. The expanded witness stays valid memory but exceeds the index
material byte limit. Neither witness is described as a global term maximum.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import unicodedata
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.information.errors import InformationRejected
from companion_memory.information.index_worker import LocalIndexWorker
from companion_memory.information.management import HostIdentity
from companion_memory.information.records import record, text
from companion_memory.persistence import Committed, Found
from companion_memory.persistence.content_codec import encode_content
from companion_memory.retrieval.lexical import normalize_material
from tests.information.host_support import host, learn_objects
from tests.information.lexical_bounds import pressure_body
from tests.information.local_profile import inspect_database
from tests.information.publication_support import build, index_port, publish
from tests.information.test_queries import query


async def pressure(root: Path, action: str, *, expanded: bool = False) -> dict[str, object]:
    """Create or reopen one isolated corpus and assert all measured owner facts."""
    if action not in ('create', 'reopen'): raise ValueError('Expected a fixed verification action.')
    body = '\ufdfa' * 682 + 'aa' if expanded else pressure_body()
    h = host(root); h.candidates = SyntheticCandidateInput('lexical_pressure', (body,), 50)
    try:
        initialized = await h.initialize('CREATE_NEW' if action == 'create' else 'OPEN_EXISTING')
        if type(initialized) is not Found: raise RuntimeError('Actual initialization failed: ' + repr(initialized))
        if action == 'create': await learn_objects(h)
        if h.retrieval is None or h.runtime is None: raise RuntimeError('Expected actual owners.')
        current = await h.retrieval.memory.current_page()
        if len(current) != 1 or record(current[0]['content'])['body'] != body:
            raise RuntimeError('Actual formal body differs from the witness.')
        encoded = encode_content(current[0], 4096)
        oid = text(current[0]['object_id'])
        port = await h.bind_query(HostIdentity('pressure-' + action, 'principal', 'host', 'entry',
            frozenset(('search_memory',)), (), time.monotonic() + 300))
        native_read = await port._authority.memory_port.get_current(oid)
        if type(native_read) is not Found: raise RuntimeError('Actual authorized point read failed.')
        calls = h.adapter.calls
        before = await port.search_memory(query(action + '-before', query_text='aa' if expanded else '0',
            include_state=False, include_goals=False))
        if type(before) is not Found: raise RuntimeError('Expected an explicit query result.')
        before_value = record(before.value)
        if expanded:
            reasons = record(before_value['truncation'])['reasons']
            if type(reasons) is not tuple or 'INDEX_FORMAT_LIMIT' not in reasons:
                raise RuntimeError('An oversized normalized copy was not reported as a coverage gap.')
            complete = await port.search_memory(query(action + '-complete', query_text='aa', require_complete=True))
            if type(complete) is not InformationRejected or complete.error.reason != 'INDEX_NOT_READY':
                raise RuntimeError('An index gap incorrectly became complete retrieval.')
            if action == 'create':
                port_index = await index_port(h)
                started = await port_index.execute('index_begin', 'oversized-copy', {'expected_generation': None})
                if type(started) is not Committed: raise RuntimeError('Expected actual build registration.')
                gid = text(record(record(record(started.receipt.result)['facts'])['retrieval'])['object_id'])
                result = await LocalIndexWorker(h.runtime, h.retrieval, port_index, 'publication').run(gid)
                if type(result) is not InformationRejected or result.error.reason != 'LIMIT_EXCEEDED':
                    raise RuntimeError('Oversized material was silently indexed or truncated.')
            normalized = unicodedata.normalize('NFKC', body).casefold()
            metrics: dict[str, object] = {'normalized_bytes': len(normalized.encode()), 'index_complete': False,
                'reported_gap': 'INDEX_FORMAT_LIMIT'}
        else:
            material = normalize_material(body, byte_limit=8192, term_limit=4096)
            if (len(body.encode()), len(material.text.encode()), len(material.singles), len(material.pairs)) != (2048, 2705, 964, 1273):
                raise RuntimeError('The recorded lower-bound witness changed.')
            if action == 'create':
                port_index = await index_port(h)
                _, generation = await build(h, port_index, 'pressure-index')
                await publish(port_index, 'pressure-publication', generation)
            complete = await port.search_memory(query(action + '-complete', query_text='0', require_complete=True,
                include_state=False, include_goals=False))
            if type(complete) is not Found: raise RuntimeError('Actual complete retrieval failed: ' + repr(complete))
            value = record(complete.value); memories = record(value['sections'])['memories']
            if type(memories) is not tuple or len(memories) != 1 or record(memories[0])['object_id'] != oid:
                raise RuntimeError('The actual indexed witness was not delivered.')
            if record(record(memories[0])['content'])['body'] != body or value['availability'] != 'COMPLETE':
                raise RuntimeError('The authority body or complete availability changed.')
            with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                postings = connection.execute('SELECT token,revision,ordinal,body FROM retrieval_posting ORDER BY ordinal').fetchall()
                if tuple(row[0] for row in postings) != material.terms or tuple(row[2] for row in postings) != tuple(range(2237)):
                    raise RuntimeError('Actual persistent postings differ from the complete lexical material.')
                if any(row[1] != current[0]['revision'] for row in postings): raise RuntimeError('Posting revisions changed.')
                posting_sizes = [len(row[3].encode()) if type(row[3]) is str else len(row[3]) for row in postings]
                if max(posting_sizes) > 256: raise RuntimeError('A posting exceeds its complete carrier limit.')
                bound = [json.loads(row[0])['term_count'] for row in connection.execute('SELECT body FROM retrieval_index_object')]
                if bound != [2237]: raise RuntimeError('Persistent index object does not bind the full term set.')
            metrics = {'normalized_bytes': len(material.text.encode()), 'single_count': len(material.singles),
                'pair_count': len(material.pairs), 'term_count': len(material.terms), 'index_complete': True,
                'posting_max_bytes': max(posting_sizes), 'posting_total_bytes': sum(posting_sizes),
                'query_bytes': len(encode_content(value, 131072)), 'snapshot_bytes': len(encode_content(memories[0], 8192))}
            (root / (action + '-response.json')).write_bytes(encode_content(value, 131072))
        if h.adapter.calls != calls: raise RuntimeError('Indexing or queries called the model adapter.')
        if action == 'reopen' and calls: raise RuntimeError('Cold recovery called the model adapter.')
        digest = hashlib.sha256(encoded).hexdigest()
        if action == 'create': (root / 'formal-object.json').write_bytes(encoded)
        elif hashlib.sha256((root / 'formal-object.json').read_bytes()).hexdigest() != digest:
            raise RuntimeError('Cold recovery altered the formal object.')
        metrics.update(action=action, body_bytes=len(body.encode()), object_id=oid,
            formal_object_bytes=len(encoded), formal_object_sha256=digest, model_calls_during_query_or_index=0,
            candidate_origin='SYNTHETIC', model_adapter='SIMULATED', storage_execution='ACTUAL')
    finally:
        if not await h.close(): raise RuntimeError('Actual resource ownership did not finish closing.')
    metrics['database'] = inspect_database(root)
    return metrics
