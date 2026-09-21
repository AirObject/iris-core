"""Bounded read-only validation of durable retrieval identities and ownership.

Recovery checks canonical records and actual local relationships. It neither
expires tickets nor promotes incomplete generations, and does not contact models.
"""
from __future__ import annotations
from typing import TYPE_CHECKING
from hashlib import sha256
import unicodedata
from companion_memory.persistence import Value
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.record_primitives import Record, integer, text
from companion_memory.memory.information_tracking import MemoryInformation
from .repository import LAYOUTS
from .lexical import PREPROCESS_ID, UNICODE_VERSION
if TYPE_CHECKING:
    from .initialization import RetrievalOwner


def require(condition: bool) -> None:
    if not condition: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')


async def verify_retrieval(owner: RetrievalOwner, memory: MemoryInformation) -> None:
    """Scan sixteen closed rows at a time under the inherited recovery deadline."""
    rows = owner._records
    coordinator = await rows.read('coordinator', {'instance_id': owner.instance_id})
    if coordinator is None: raise OwnerFailure('STORAGE_FAILED', 'index', 'INTEGRITY_FAILURE')
    require((await rows.rows.read('coordinator_count', {}))[0]['count'] == 1)
    require((await rows.rows.read('recovery_invalid_relations', {}))[0]['count'] == 0)
    require((await rows.rows.read('consumption_count', {}))[0]['count'] == await memory.usage_evidence_count())
    generations: dict[str, Record] = {}
    for layout in LAYOUTS:
        after: dict[str, Value] = {'after_' + key: '' for key in layout.keys}
        while page := await rows.rows.read(layout.name + '_recovery_page', after):
            current_objects: dict[str, Record] = {}
            coverage: dict[tuple[str, str], Record] = {}
            if layout.name == 'index_object':
                current_objects = {text(item['object_id']): item for item in await memory.index_current_objects(tuple(sorted({text(raw['object_id']) for raw in page})))}
                for generation_id in sorted({text(raw['generation_id']) for raw in page}):
                    selected = tuple(text(raw['object_id']) for raw in page if raw['generation_id'] == generation_id)
                    for item in await memory.index_acknowledgments(generation_id, selected):
                        coverage[(generation_id, text(item['object_id']))] = item
            for raw in page:
                value = rows.unpack(layout.name, raw)
                name = layout.name
                if name == 'ticket':
                    require(value['database_id'] == owner.configuration.database_id and value['instance_id'] == owner.instance_id
                        and value['config_snapshot_id'] == owner.configuration.snapshot_id
                        and integer(value['expires_at_us']) - integer(value['issued_at_us']) == 86400000000
                        and value['clock_observation'] == value['issued_at_us']
                        and integer(value['clock_observation']) <= integer(coordinator['clock_high_water']))
                elif name == 'member':
                    ticket = await rows.read('ticket', {'recall_id': value['recall_id']})
                    require(ticket is not None and (ticket['query_mode'] == 'DEEP' or value['returned_lifecycle'] == 'ACTIVE'))
                elif name == 'consumption':
                    require(value['database_id'] == owner.configuration.database_id)
                    mirror = await memory.read_usage_receipt({key: value[key] for key in ('database_id', 'principal_binding_id', 'recall_id', 'object_id')})
                    require(mirror == value)
                    ticket = await rows.read('ticket', {'recall_id': value['recall_id']})
                    disposed = await rows.read('disposition', {'recall_id': value['recall_id']})
                    original = ticket if ticket is not None else disposed
                    require(original is not None and original['principal_binding_id'] == value['principal_binding_id'])
                    require(integer(value['result_revision']) == integer(value['returned_revision']) + (0 if value['effect'] == 'CONSUMED' else 1))
                elif name == 'disposition':
                    require(value['database_id'] == owner.configuration.database_id and integer(value['disposed_at_us']) >= integer(value['expired_at_us']))
                elif name == 'index_generation':
                    require(value['config_snapshot_id'] == owner.configuration.snapshot_id and value['preprocess_id'] == PREPROCESS_ID
                        and value['unicode_version'] == UNICODE_VERSION and integer(value['contiguous_seq']) <= integer(value['captured_seq']))
                    physical = (await rows.rows.read('generation_object_count', {'generation_id': value['generation_id']}))[0]['count']
                    if value['status'] in ('ACTIVE', 'BUILDING') or physical:
                        generations[text(value['generation_id'])] = value
                        require(len(generations) <= 2)
                    lease = await rows.read('lease', {'work_id': value['generation_id']})
                    require(lease is not None and lease['instance_id'] == owner.instance_id)
                    require(value['status'] != 'FAILED' or lease is not None and lease['status'] == 'ENDED')
                elif name == 'posting':
                    token = text(value['token'])
                    require(1 <= len(token) <= 2 and token == unicodedata.normalize('NFKC', token).casefold()
                        and all(unicodedata.category(c)[0] in ('L', 'N') for c in token))
                elif name == 'index_object':
                    generation = generations.get(text(value['generation_id']))
                    if generation is not None and generation['status'] in ('ACTIVE', 'BUILDING'):
                        acknowledgment = coverage.get((text(value['generation_id']), text(value['object_id'])))
                        require(acknowledgment is not None and acknowledgment['revision'] == value['revision'] and acknowledgment['action'] == 'UPSERT')
                    current = current_objects.get(text(value['object_id']))
                    if current is not None and current['revision'] == value['revision']:
                        require(sha256(encode_content(current, 4096)).hexdigest() == value['body_digest'])
                elif name == 'lease':
                    require(value['instance_id'] == owner.instance_id and await rows.read('index_generation', {'generation_id': value['work_id']}) is not None)
                after = {'after_' + key: raw[key] for key in layout.keys}
    for field, status in (('active_generation', 'ACTIVE'), ('building_generation', 'BUILDING')):
        selected = coordinator[field]
        matching = tuple(g['generation_id'] for g in generations.values() if g['status'] == status)
        require(matching == (() if selected is None else (selected,)))
    require(coordinator['active_generation'] is None or coordinator['active_generation'] != coordinator['building_generation'])
    after_object = after_generation = ''
    while acknowledgments := await memory.index_acknowledgment_page(after_object, after_generation):
        indexed_objects: dict[tuple[str, str], Record] = {}
        for generation_id in sorted({text(item['generation_id']) for item in acknowledgments}):
            selected = tuple(text(item['object_id']) for item in acknowledgments if item['generation_id'] == generation_id)
            for raw in await rows.rows.read('recovery_selected_objects', {'generation_id': generation_id, 'object_ids': encode_content(selected, 4096).decode()}):
                indexed = rows.unpack('index_object', raw)
                indexed_objects[(generation_id, text(indexed['object_id']))] = indexed
        for acknowledgment in acknowledgments:
            generation = generations.get(text(acknowledgment['generation_id']))
            if generation is None: generation = await rows.read('index_generation', {'generation_id': acknowledgment['generation_id']})
            require(generation is not None)
            if generation is not None and generation['status'] in ('ACTIVE', 'BUILDING'):
                indexed = indexed_objects.get((text(acknowledgment['generation_id']), text(acknowledgment['object_id'])))
                require(indexed is None if acknowledgment['action'] == 'REMOVE' else indexed is not None and indexed['revision'] == acknowledgment['revision'])
            after_object, after_generation = text(acknowledgment['object_id']), text(acknowledgment['generation_id'])
