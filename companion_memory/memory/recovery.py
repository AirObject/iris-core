"""Read-only integrity checks before admission of a newly isolated content owner.

Legacy metadata pages use complete point checks. Information owners expose
finite combined reads of current bodies, links and immutable source members;
all identities, canonical bodies and protections remain checked without writes.
"""
import hashlib
import time
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import Found
from companion_memory.persistence.content_codec import decode_content, encode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.ingress.content_storage import ContentIngressTransactions
from companion_memory.ingress.media_events import decode_media_event
from companion_memory.media.interpretations import decode_interpretation
from .formats import isolate_subject, isolate_links, record, sequence
from .sources import decode_source


class MemoryRecovery:
    """A continuation belongs only to this current isolated module binding."""
    def __init__(self, memory, media):
        self.memory = memory; self.media = media
        self.phase = 'objects'; self.after = ''
        self.source_identity = None; self.holder_after = ''; self.holders_complete = False; self.member_ordinal = 0

    async def advance(self):
        memory = self.memory
        deadline = time.monotonic() + memory.configuration.candidate.runtime.integer('runtime.recovery_timeout_ms') / 1000
        limit = memory.configuration.candidate.content.integer('memory.read_page_size')
        for phase, key in (('objects', 'object_id'), ('subjects', 'subject_id'), ('sources', 'source_id')):
            if phase != self.phase: continue
            while True:
                if time.monotonic() >= deadline: return Found(MappingProxyType({'state': 'RECOVERY_PENDING', 'owner': 'memory'}))
                if phase == 'objects' and memory.information is not None:
                    objects = await memory.information.recovery_objects(self.after)
                    if not objects: break
                    self.after = objects[-1]['object_id']
                    continue
                rows = await memory.rows.read(phase + '_recovery_page', {'after': self.after, 'limit': limit})
                if not rows: break
                for metadata in rows:
                    if time.monotonic() >= deadline: return Found(MappingProxyType({'state': 'RECOVERY_PENDING', 'owner': 'memory'}))
                    identity = cast(str, metadata[key])
                    value = (await memory.rows.read(phase + '_get', {key: identity}))[0]
                    if phase == 'objects':
                        current = memory.decode_current(value)
                        links = (await memory.rows.read('links_get', {'object_id': identity}))[0]
                        structured = isolate_links(decode_content(cast(str, links['body']).encode(), 2048), identity, cast(int, current['revision']))
                        if encode_content(structured, 2048).decode() != links['body']: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                        for source in sequence(structured['sources']):
                            linked = record(source)
                            holder = await memory.rows.read('source_holders_get', {'source_id': linked['source_id'], 'owner_kind': 'OBJECT', 'owner_id': identity})
                            if not holder: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                    elif phase == 'subjects':
                        subject = isolate_subject(decode_content(cast(str, value['body']).encode(), 1024))
                        if (encode_content(subject, 1024).decode() != value['body'] or subject['instance_id'] != memory.instance_id
                                or any(subject[k] != value[k] for k in ('subject_id', 'kind', 'platform_id', 'external_subject_id', 'revision'))):
                            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                    elif not await self.source(value, limit, deadline):
                        return Found(MappingProxyType({'state': 'RECOVERY_PENDING', 'owner': 'memory'}))
                    self.after = identity
            self.phase = {'objects': 'subjects', 'subjects': 'sources', 'sources': 'complete'}[phase]; self.after = ''
        return Found(MappingProxyType({'state': 'MEMORY_VERIFIED'}))

    async def source(self, value, limit, deadline):
        memory = self.memory; sid = value['source_id']
        identity = (sid, value['references_revision'])
        if self.source_identity is not None and self.source_identity != identity:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        self.source_identity = identity
        member_count = (await memory.rows.read('source_member_count', {'source_id': sid}))[0]['count']
        count = (await memory.rows.read('source_holder_count', {'source_id': sid}))[0]['count']
        if count != value['holder_count'] or (value['state'] == 'RETAINED') != bool(count):
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        if value['state'] == 'RELEASED':
            if value['body'] is not None or member_count: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            self.source_identity = None
            return True
        from .fixed_source import is_fixed_source,decode_fixed_source,fixed_digest
        fixed=memory.semantic_format and is_fixed_source(value['body'])
        source=decode_fixed_source(value['body']) if fixed else decode_source(value['body'])
        if fixed and (value['batch_id'] is not None or fixed_digest(source)!=value['digest'] or member_count):
            raise OwnerFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
        if any(source[k] != value[k] for k in (('source_id','entry_id') if fixed else ('source_id', 'entry_id', 'batch_id', 'digest'))):
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        if member_count != len(sequence(source.get('ordered_members',()))):
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        from .source_records import split_manifest
        stored_header = split_manifest(value['body'],semantic_format=memory.semantic_format)[0] if memory.information is not None else None
        while not self.holders_complete:
            if time.monotonic() >= deadline: return False
            holders = await memory.information.recovery_source_holders(sid, self.holder_after) if memory.information is not None else await memory.rows.read('source_holders_page', {'source_id': sid, 'after': self.holder_after, 'limit': limit})
            if not holders:
                self.holders_complete = True
                break
            for holder in holders:
                if time.monotonic() >= deadline: return False
                if holder['owner_kind'] != 'OBJECT': raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                if memory.information is not None:
                    if holder['source_body'] != stored_header: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                else:
                    observed = await memory.rows.read('review_manifest', {'object_id': holder['owner_id'], 'source_id': sid})
                    if not observed or observed[0]['body'] != value['body']: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                self.holder_after = holder['owner_id']
        if fixed:
            self.source_identity=None;self.holder_after='';self.holders_complete=False;self.member_ordinal=0
            return True
        ingress = memory.sources
        if type(ingress) is not ContentIngressTransactions: raise OwnerFailure('CAPABILITY_UNAVAILABLE', 'source', 'OWNER_MISSING')
        stored_members = {item['ordinal']: item for item in await memory.information.recovery_source_members(sid)} if memory.information is not None else None
        payloads = {item['message_id']: item for item in await ingress.recovery_source_payloads(sid, tuple(cast(str, record(item)['message_id']) for item in sequence(source['ordered_members'])))} if memory.information is not None else None
        for ordinal, member in enumerate(sequence(source['ordered_members'])):
            if ordinal < self.member_ordinal: continue
            if time.monotonic() >= deadline: return False
            member = record(member)
            stored = (stored_members[ordinal],) if stored_members is not None and ordinal in stored_members else () if stored_members is not None else await memory.rows.read('source_members_get', {'source_id': sid, 'ordinal': ordinal})
            if not stored or stored[0]['body'] != encode_content(member, 2048).decode(): raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            if payloads is not None:
                selected_payload = payloads.get(cast(str, member['message_id']))
                payload = (selected_payload,) if selected_payload is not None else ()
                protected = selected_payload is not None and selected_payload['source_holder'] == sid
            else:
                payload = await ingress.rows.read('payload', {'message_id': member['message_id']})
                protected = await ingress.rows.read('holder', {'message_id': member['message_id'], 'owner_kind': 'SOURCE', 'owner_id': sid})
            if not payload or not protected: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            body = cast(str, payload[0]['body']).encode()
            decode_media_event(body, memory.configuration.candidate.runtime.integer('ingress.event_max_bytes'), occurrence_limit=memory.configuration.candidate.content.integer('media.event_occurrence_limit'),
                text_limit=memory.configuration.candidate.content.integer('media.interpretation_text_max_bytes'))
            if hashlib.sha256(body).hexdigest() != member['payload_digest']: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            for selected in sequence(member['media']):
                selected = record(selected)
                if self.media is None: raise OwnerFailure('CAPABILITY_UNAVAILABLE', 'media', 'OWNER_MISSING')
                stored = await self.media.rows.read('interpretations_get', {'interpretation_id': selected['interpretation_id']})
                if not stored: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                interpretation = decode_interpretation(cast(str, stored[0]['body']).encode())
                if (any(interpretation[k] != selected[k] for k in ('interpretation_id', 'blob_id'))
                        or cast(int, interpretation['generation']) > cast(int, selected['generation'])
                        or interpretation['generation'] != selected['generation'] and
                        (interpretation['origin'] != 'INTERNAL' or interpretation['status'] not in ('COMPLETE', 'EMPTY', 'REFUSED'))):
                    raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                from companion_memory.media.service import identity as media_identity
                protected_blob = await self.media.rows.read('references_get', {'reference_id': media_identity('media_ref',
                    'SOURCE', sid, selected['blob_id'], selected['generation'], selected['occurrence_id'])})
                protected_version = await self.media.rows.read('interpretation_holders_get', {'holder_id': media_identity('interpretation_ref', sid, selected['occurrence_id'])})
                selected_version = await self.media.rows.read('selections_get', {'occurrence_id': selected['occurrence_id'], 'selection_revision': selected['selection_revision']})
                if selected_version and interpretation['generation'] != selected['generation']:
                    expected_kind = 'PROTECTED_REFUSAL' if interpretation['status'] == 'REFUSED' else 'CONTENT_REUSE'
                    if (selected_version[0]['selection_kind'] != expected_kind
                            or interpretation['status'] != 'REFUSED' and interpretation['scope_kind'] != 'CONTENT'):
                        raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                if (not protected_blob or not protected_version or not selected_version
                        or protected_version[0]['interpretation_id'] != selected['interpretation_id']
                        or selected_version[0]['interpretation_id'] != selected['interpretation_id']
                        or selected_version[0]['generation'] != selected['generation']):
                    raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            self.member_ordinal = ordinal + 1
        self.source_identity = None; self.holder_after = ''; self.holders_complete = False; self.member_ordinal = 0
        return True
