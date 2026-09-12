"""Original Provider learning association, deterministic candidate and finalization.

Full frozen material is reconstructed from actual owners outside the transaction.
The original model key and candidate transform binding are committed first. Later
calls recover that exact request and candidate, including zero-result terminals.
"""
from __future__ import annotations
import time
from types import MappingProxyType
from typing import TYPE_CHECKING, cast
from companion_memory.buffers.content_material import SourceMember, build_content_material
from companion_memory.cognition.candidates import MANIFEST
from companion_memory.memory.formats import isolate, record, sequence
from companion_memory.persistence import Committed, Found, Value
from companion_memory.persistence.content_codec import decode_content, encode_content
from companion_memory.provider import WorkGrant, ResultGrant, CancellationSource, Completed, Pending, Found as ProviderFound
from companion_memory.provider.terminal_evidence import TerminalVerified
from companion_memory.provider.values import as_record
from .content_assembly import stable
from .results import Rejected, RuntimeError
if TYPE_CHECKING:
    from .content_service import ContentRuntimeService


async def material(runtime: ContentRuntimeService, source: MappingProxyType[str, Value]):
    """Build complete Base64 frozen materials without changing any owner state."""
    assembly = runtime.assembly; members = []
    for raw in sequence(source['ordered_members']):
        member = record(raw)
        payload = (await assembly.ingress.rows.read('payload', {'message_id': member['message_id']}))[0]
        versions = []
        if sequence(member['media']):
            from companion_memory.media.service import MediaService
            media = cast(MediaService, assembly.media)
            for selected in sequence(member['media']):
                row = (await media.rows.read('interpretations_get', {'interpretation_id': record(selected)['interpretation_id']}))[0]
                versions.append(cast(str, row['body']).encode())
        members.append(SourceMember(cast(str, member['role']), MappingProxyType({k: member[k] for k in
            ('message_id', 'entry_seq', 'received_at_us', 'transferred_at_us', 'payload_digest')} |
            {'interpretation_ids': tuple(record(s)['interpretation_id'] for s in sequence(member['media']))}), cast(str, payload['body']).encode(), tuple(versions)))
    return build_content_material(tuple(cast(str, v) for v in (assembly.instance_id, source['host_id'], source['platform_id'], source['entry_id'],
        source['batch_id'], source['run_id'], source['config_snapshot_id'])), tuple(members), event_limit=runtime.settings.integer('ingress.event_max_bytes'),
        interpretation_limit=assembly.configuration.candidate.content.integer('media.interpretation_record_max_bytes'), material_limit=runtime.settings.integer('learning.material_max_bytes'))


async def learn_batch(runtime: ContentRuntimeService, source: MappingProxyType[str, Value], *, fresh: bool, admission_event: str | None = None, observe: bool = True) -> object:
    """Advance original learning without treating a missed read as replay permission."""
    try:
        result = await _learn_batch(runtime, source, fresh=fresh, admission_event=admission_event)
        if type(result) is Committed:
            from .content_transfer import transfer_entry
            from companion_memory.persistence.owned_statements import OwnerFailure
            try: await transfer_entry(runtime, cast(str, source['entry_id']))
            except OwnerFailure:
                # The separately recoverable FIFO remains pending. Its read
                # failure cannot withdraw the already confirmed learning receipt.
                pass
        if observe: runtime.observations.record_work(cast(str, source['entry_id']), result)
        return result
    finally: runtime.release_ended_capabilities()


async def _learn_batch(runtime: ContentRuntimeService, source: MappingProxyType[str, Value], *, fresh: bool, admission_event: str | None = None) -> object:
    r = runtime; assembly = r.assembly; bid = cast(str, source['batch_id'])
    work = (await assembly.rows.read('work_get', {'batch_id': bid}))[0]
    integrity_revision = r.gate.protection_revision
    has_media = any(sequence(record(m)['media']) for m in sequence(source['ordered_members']))
    if has_media and work['phase'] != 'TERMINAL' and r.media is not None:
        for member in sequence(source['ordered_members']):
            for selected in sequence(record(member)['media']):
                blobs = await r.media.media.rows.read('blobs_get', {'blob_id': record(selected)['blob_id']})
                if blobs: await r.media.media.integrity.verify_metadata(blobs[0])
    if work['phase'] in ('CANDIDATE_STORED', 'TERMINAL'):
        header = (await assembly.cognition._rows.read('get', {'candidate_id': work['candidate_id']}))[0]
        manifest = assembly.cognition.manifest(decode_content(cast(str, header['manifest']).encode(), 4096))
        if any(record(ref)['action'] in ('REPLACE_CURRENT', 'SET_SCORES', 'DELETE_OBJECT') for ref in sequence(manifest['ordered_change_refs'])):
            from .candidate_application import apply_candidate
            return await apply_candidate(r, bid, work['candidate_id'], allow_replan=fresh)
        terminal = manifest['terminal_proposal']
        branch = 'published' if sequence(manifest['ordered_change_refs']) else 'without_objects'
        kind = 'commit_content_' + branch + ('_with_media' if has_media else '')
        # A persisted terminal in the information assembly must confirm its
        # original result. Absence cannot authorize a second application.
        finish = r.confirm_command if assembly.information_format and work['phase'] == 'TERMINAL' else r.execute
        return await finish(kind, stable('finish', bid), {'batch_id': bid, 'candidate_id': work['candidate_id'],
            'expected_revision': cast(int, work['revision']) - (1 if work['phase'] == 'TERMINAL' else 0),
            'generation': work['generation'], 'readable_objects': [], 'readable_subjects': []})
    if work['phase'] == 'WAITING_ADMISSION':
        if not fresh or admission_event is None or r.gate.state not in ('NORMAL', 'DRAINING'):
            return Found(MappingProxyType({'state': 'WAITING_ADMISSION', 'remote_result': 'NOT_SENT'}))
        prior = (await assembly.rows.read('learning_admissions_get', {'admission_id': stable('learning_admission', bid, work['admission_generation'])}))[0]
        if admission_event == work['admission_trigger'] and r.gate.epoch <= cast(int, prior['mode_epoch']):
            return Found(MappingProxyType({'state': 'WAITING_ADMISSION', 'remote_result': 'NOT_SENT'}))
        reopened = await r.execute('reopen_learning_admission', stable('readmit_learning', bid, admission_event, r.gate.epoch),
            {'batch_id': bid, 'generation': work['generation'], 'expected_revision': work['revision'], 'trigger_key': admission_event, 'expected_epoch': r.gate.epoch})
        if type(reopened) is not Committed: return reopened
        work = (await assembly.rows.read('work_get', {'batch_id': bid}))[0]
    admission_generation = cast(int, work['admission_generation'])
    model_key = stable('learning_request', bid) if admission_generation == 1 else stable('learning_request', bid, admission_generation)
    messages = await material(r, source)
    request = {'operation_key': model_key, 'run_id': source['run_id'], 'profile_id': r.learning_profile,
        'deadline': time.monotonic() + r.remaining_request(), 'cancellation': CancellationSource().token,
        'batch_id': bid, 'entry_ids': [source['entry_id']], 'prompt_revision': 'target_source_records:1',
        'payload': {'messages': [dict(m) for m in messages], 'input_units_limit': r.settings.integer('learning.input_units_limit'),
            'output_units_limit': r.settings.integer('learning.output_units_limit')}}
    from .learning_admissions import descriptor_digest, conclude_unsent
    binding = MappingProxyType({'profile_id': r.learning_profile, 'prompt_revision': 'target_source_records:1',
        'transform_version': r.candidates.transform_version, 'candidate_source_fingerprint': r.candidates.fingerprint, 'request_digest': descriptor_digest(request)})
    from companion_memory.cognition.synthetic_mutations import SyntheticMutationInput
    from companion_memory.cognition.synthetic_mixed import SyntheticMixedInput
    from companion_memory.cognition.goal_proposals import SyntheticGoalInput
    if type(r.candidates) is SyntheticMutationInput or type(r.candidates) is SyntheticMixedInput or type(r.candidates) is SyntheticGoalInput: binding = MappingProxyType({**binding, 'mutation_authority': r.candidates.authority})
    if type(r.candidates) is SyntheticGoalInput: binding = MappingProxyType({**binding, 'goal_route_ids': r.candidates.route_ids})
    if fresh and not r.remaining_request(): return Found(MappingProxyType({'state': 'WAITING_ADMISSION'}))
    if work['phase'] not in ('FROZEN', 'PARKED'): fresh = False
    if work['phase'] in ('FROZEN', 'PARKED'):
        if not fresh:
            if work['phase'] == 'FROZEN':
                parked = await r.execute('park_content_work', stable('park_learning', bid), {'batch_id': bid, 'expected_revision': work['revision'], 'generation': work['generation']})
                if type(parked) is not Committed: return parked
            return Found(MappingProxyType({'state': 'PARKED', 'reason': 'RECOVERY_NO_DISPATCH'}))
        associated = await r.execute('associate_content_request', stable('associate_learning', bid, admission_generation), {'batch_id': bid,
            'expected_revision': work['revision'], 'generation': work['generation'], 'provider_operation_key': model_key,
            'model_binding': encode_content(binding, 8192).decode()})
        if type(associated) is not Committed: return associated
        fresh = associated.source == 'NEW'
        work = (await assembly.rows.read('work_get', {'batch_id': bid}))[0]
    elif work['phase'] != 'REQUEST_ASSOCIATED':
        return Rejected(RuntimeError('PRECONDITION_FAILED', 'run_learning', 'state', 'WORK_FENCED'))
    if work['model_binding'] != encode_content(binding, 8192).decode():
        return Rejected(RuntimeError('CONFIGURATION_UNSUPPORTED', 'run_learning', 'configuration', 'BINDING_MISMATCH'))
    grant = WorkGrant('cognition', assembly.instance_id, None, 'LEARNING', (r.learning_profile,), ('GENERATION',), 'cognition', 'content_scheduler',
        (cast(str, source['run_id']),), (cast(str, source['entry_id']),), (bid,), prompt_revisions=('target_source_records:1',))
    held = r._learning_held.get(bid)
    if held:
        grant, port = held
    else:
        admitted = r.gate.admit(grant, r.gate.epoch, expected_protection_revision=integrity_revision) if fresh else r.gate.admit_recovery(grant)
        if not admitted and fresh:
            fresh = False; admitted = r.gate.admit_recovery(grant)
        if not admitted: return Rejected(RuntimeError('MODE_BLOCKED', 'run_learning', 'state', 'DREAMING'))
        port = r.provider.bind_work(grant); r._learning_held[bid] = grant, port

    request['deadline'] = time.monotonic() + r.remaining_request()
    result = await port.generate(request) if fresh else await port.lookup_request('generate', request)
    rid = result.record['object_id'] if type(result) is Completed else result.reference['request_id'] if type(result) is Pending else as_record(as_record(result.value)['request'])['object_id'] if type(result) is ProviderFound else None
    if rid is None:
        closed = await conclude_unsent(r, work, port, request)
        return closed if closed is not None else provider_observation(r, result, 'ORIGINAL_REQUEST_UNCONFIRMED')
    if work['provider_request_id'] is None:
        associated = await r.execute('confirm_content_request', stable('confirm_learning', bid, cast(str, rid)), {'batch_id': bid,
            'expected_revision': work['revision'], 'generation': work['generation'], 'request_id': rid})
        if type(associated) is not Committed: return associated
        work = (await assembly.rows.read('work_get', {'batch_id': bid}))[0]
    elif work['provider_request_id'] != rid:
        return Rejected(RuntimeError('STORAGE_FAILED', 'run_learning', 'storage', 'INTEGRITY_FAILURE'))
    owner = r.provider.bind_result_owner(ResultGrant('cognition', (cast(str, rid),)))
    try: terminal = await owner.verify_terminal(rid)
    finally: r.provider.revoke(owner)
    if type(terminal) is not TerminalVerified: return provider_observation(r, terminal, 'REMOTE_UNKNOWN')
    if not terminal.value.confirmed_sent or terminal.value.request['outcome'] == 'MODE_BLOCKED':
        closed = await conclude_unsent(r, work, port, request)
        return closed if closed is not None else Found(MappingProxyType({'state': 'WAITING_ADMISSION'}))
    candidate = r.candidates.build(assembly.configuration, assembly.instance_id, source, cast(int, work['generation']), terminal.value)
    assembly.retain_learning_terminal(terminal.value)
    staged = await r.execute('store_content_candidate' + ('_with_media' if has_media else ''), stable('candidate_stage', bid),
        {'batch_id': bid, 'expected_revision': work['revision'], 'generation': work['generation'],
            'manifest': encode_content(candidate.manifest, 4096).decode(), 'leaves': [encode_content(leaf, 8192).decode() for leaf in candidate.leaves]})
    if type(staged) is not Committed: return staged
    if port.consumers_ended():
        r.provider.revoke(port); r.gate.revoke(grant); r._learning_held.pop(bid, None)
    return await learn_batch(r, source, fresh=False)


def provider_observation(runtime, result, pending_state):
    """Keep a queryable original outcome separate from local storage availability."""
    from companion_memory.provider import NotFound as ProviderNotFound
    pending = bool(runtime.provider.get_health().cleanup_pending or runtime._commands)
    if type(result) in (Pending, ProviderNotFound):
        return Found(MappingProxyType({'state': pending_state, 'remote_result': 'UNKNOWN', 'cleanup_pending': pending}))
    cause = getattr(result, 'error', None)
    if getattr(cause, 'code', None) in ('MODE_BLOCKED', 'RESOURCE_BUSY', 'BUDGET_EXHAUSTED'):
        return Found(MappingProxyType({'state': 'WAITING_ADMISSION', 'cleanup_pending': pending}))
    return Rejected(RuntimeError('STORAGE_FAILED', 'run_learning', 'storage', 'SYSTEM_BLOCKED', pending))
