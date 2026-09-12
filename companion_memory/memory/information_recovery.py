"""Validate every owned information leaf before memory recovery completes.

Composite cursors include all identity columns so equal database/object prefixes
cannot hide later evidence rows. Recovery checks facts and never repairs them.
"""
from __future__ import annotations
from typing import TYPE_CHECKING
from companion_memory.persistence import Value
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.information.records import Record, integer, text, decode
from .information_repository import LAYOUTS
from .formats import TOMBSTONE_SCHEMA
if TYPE_CHECKING:
    from .information_tracking import MemoryInformation


def require(value: bool) -> None:
    if not value: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')


async def verify_information(owner: MemoryInformation, sequence: Record) -> None:
    records = owner._records
    require(sequence['instance_id'] == owner.instance_id)
    require(0 <= integer(sequence['published_seq']) <= integer(sequence['last_seq']))
    require(sequence['published_generation_id'] is not None or sequence['published_seq'] == 0)
    # The exact revision equation is checked against retained publication receipts
    # after both data owners have finished their local record validation.
    for root in ('information_format', 'change_sequence'):
        require((await records.rows.read(root + '_count', {}))[0]['count'] == 1)
    for layout in LAYOUTS:
        after: dict[str, Value] = {'after_' + key: '' for key in layout.keys}
        acknowledgment_object: Value = None
        acknowledgment_count = 0
        while page := await records.rows.read(layout.name + '_recovery_page', after):
            current_objects: dict[str, Record] = {}
            dirty_objects: dict[str, Record] = {}
            if layout.name == 'index_gap':
                selected = tuple(text(raw['object_id']) for raw in page)
                current_objects = {text(item['object_id']): item for item in await owner.index_current_objects(selected)}
                dirty_objects = {text(item['object_id']): item for item in await records.rows.read('information_selected_dirty', {'object_ids': encode_content(selected, 4096).decode()})}
            for raw in page:
                value = records.unpack(layout.name, raw)
                if layout.name == 'index_gap':
                    require(1 <= integer(value['first_uncovered_seq']) <= integer(value['latest_change_seq']) <= integer(sequence['last_seq']))
                    current = current_objects.get(text(value['object_id']))
                    dirty = dirty_objects.get(text(value['object_id']))
                    # The retained legacy acceleration excludes forgotten
                    # objects; the local deep index still stores their terms.
                    legacy_action = 'REMOVE' if current is None or current['lifecycle'] == 'FORGOTTEN' else 'UPSERT'
                    require(dirty is not None and all(dirty[key] == value[key] for key in ('object_id', 'revision'))
                        and dirty['action'] == legacy_action)
                    if value['action'] == 'UPSERT': require(current is not None and current['revision'] == value['revision'])
                    else:
                        tombstones = await owner._objects.rows.read('tombstones_get', {'object_id': value['object_id']})
                        require(current is None and len(tombstones) == 1)
                        tombstone = decode(TOMBSTONE_SCHEMA, text(tombstones[0]['body']), 1024)
                        require(tombstone['object_id'] == value['object_id'] and integer(tombstone['last_revision']) + 1 == value['revision'])
                elif layout.name == 'generation_ack':
                    require(1 <= integer(value['applied_seq']) <= integer(sequence['last_seq']))
                    acknowledgment_count = acknowledgment_count + 1 if acknowledgment_object == value['object_id'] else 1
                    acknowledgment_object = value['object_id']
                    require(acknowledgment_count <= 2)
                elif layout.name == 'usage_receipt':
                    require(value['database_id'] == owner.configuration.database_id)
                    require(integer(value['result_revision']) == integer(value['returned_revision']) + (0 if value['effect'] == 'CONSUMED' else 1))
                after = {'after_' + key: raw[key] for key in layout.keys}
