"""Persistent occurrence scheduling and original-key local media recovery.

A new request is sent only in the invocation that confirmed original work
registration. All later invocations use lookup/recover on that exact descriptor.
The actual media owner applies results; this coordinator only joins its commands
with runtime membership, gate and ingress recovery protections.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import time
from types import MappingProxyType
from companion_memory.provider.ports import WorkPort
from typing import TYPE_CHECKING, cast
from companion_memory.media.service import MediaService, MediaError, identity
from companion_memory.media.interpretations import decode_interpretation
from companion_memory.persistence import Committed, Found, Value
from companion_memory.persistence.content_codec import decode_content, encode_content
from companion_memory.memory.formats import record, sequence
from companion_memory.provider import WorkGrant, ResultGrant, CancellationSource, Completed, Pending, Found as ProviderFound
from companion_memory.provider.values import as_record, freeze
from companion_memory.provider.stored_media import StoredMediaAuthorized
from companion_memory.provider.terminal_evidence import TerminalVerified
from .preparation_format import decode_preparation
if TYPE_CHECKING:
    from .content_service import ContentRuntimeService


@dataclass(frozen=True, slots=True)
class MediaPolicy:
    """Explicit trusted interpretation domain/profile/prompt, not event metadata."""
    authorization_domain_id: str
    profile_id: str
    prompt_revision: str


class ContentMedia:
    """One runtime's finite original media consumers and byte authorizations."""
    def __init__(self, runtime: ContentRuntimeService, policy: MediaPolicy):
        self.runtime = runtime; self.policy = policy
        media = runtime.assembly.media
        if type(media) is not MediaService: raise ValueError('Actual media owner required.')
        self.media = cast(MediaService, media)
        self.authority = runtime.provider.bind_stored_media_authority(media, runtime.assembly.instance_id, 'media')
        media.integrity.notify = runtime.gate.invalidate_current
        runtime.gate.integrity_pending = lambda: bool(media.integrity.pending or media._physical_fault)
        self._held: dict[str, tuple[object, WorkGrant, object]] = {}

    def fingerprint(self) -> str:
        provider = self.runtime.provider
        # The trusted Provider's own immutable profile is used; no profile is
        # inferred from the event, supplied body or a freely chosen model string.
        profile = provider._profiles.get(self.policy.profile_id)
        if profile is None: raise ValueError('Configured media profile is unavailable.')
        from companion_memory.provider.values import fingerprint
        return fingerprint(MappingProxyType({'profile': profile, 'prompt_revision': self.policy.prompt_revision, 'scope_kind': 'CONTENT'}))

    async def _observed(self, occurrence_id: str, domain: str | None = None):
        rows = await self.media.rows.read('occurrences_get', {'occurrence_id': occurrence_id})
        if not rows: return None
        occurrence = rows[0]
        blobs = await self.media.rows.read('blobs_get', {'blob_id': occurrence['blob_id']})
        if not blobs: return None
        blob = blobs[0]
        await self.media.integrity.verify_metadata(blob)
        task = 'TRANSCRIBE' if occurrence['modality'] == 'AUDIO' else 'DESCRIBE'
        guard = identity('media_guard', self.media.instance_id, domain or self.policy.authorization_domain_id, blob['sha256'], blob['byte_count'], occurrence['modality'], task)
        return occurrence, blob, guard

    def release_ended_capabilities(self) -> None:
        """Drop only process-local bytes whose actual Provider consumers ended."""
        for wid, (media, grant, port) in tuple(self._held.items()):
            if self.authority.release_media_authorization(media):
                self.runtime.provider.revoke(port); self.runtime.gate.revoke(grant); self._held.pop(wid)

    async def prepare(self, preparation_id: str):
        self.release_ended_capabilities()
        r = self.runtime; rows = await r.assembly.rows.read('preparations_get', {'preparation_id': preparation_id})
        if not rows: return MediaError('PRECONDITION_FAILED', 'prepare_media', 'state', 'WORK_FENCED')
        prep = rows[0]; manifest = decode_preparation(cast(str, prep['manifest'])); fingerprint = self.fingerprint()
        for member in sequence(manifest['ordered_members']):
            for item in sequence(record(member)['media']):
                oid = cast(str, record(item)['occurrence_id']); observed = await self._observed(oid)
                if observed is None: return MediaError('STORAGE_FAILED', 'prepare_media', 'storage', 'INTEGRITY_FAILURE')
                occurrence, _, guard = observed
                external = False
                if occurrence['interpretation_id'] is not None:
                    value = decode_interpretation(cast(str, (await self.media.rows.read('interpretations_get', {'interpretation_id': occurrence['interpretation_id']}))[0]['body']).encode())
                    external = value['origin'] == 'EXTERNAL'
                if external: continue
                guards = await self.media.rows.read('guards_get', {'guard_key': guard})
                cache_key = identity('interpretation_cache', guard, fingerprint, self.policy.prompt_revision, 'CONTENT')
                cache = await self.media.rows.read('content_cache_get', {'cache_key': cache_key})
                if guards or occurrence['interpretation_id'] is not None or cache:
                    target = guards[0]['interpretation_id'] if guards else occurrence['interpretation_id'] if occurrence['interpretation_id'] is not None else cache[0]['interpretation_id']
                    if target != occurrence['interpretation_id']:
                        result = await r.execute('reuse_occurrence_interpretation', identity('reuse', preparation_id, oid, target), {
                            'preparation_id': preparation_id, 'owner_generation': prep['owner_generation'], 'occurrence_id': oid,
                            'authorization_domain_id': self.policy.authorization_domain_id, 'prompt_revision': self.policy.prompt_revision, 'interpretation_fingerprint': fingerprint})
                        if type(result) is not Committed: return result
                    continue
                task = 'TRANSCRIBE' if occurrence['modality'] == 'AUDIO' else 'DESCRIBE'
                wid = identity('occurrence_work', self.media.configuration.database_id, oid, task)
                work = await self.media.rows.read('work_get', {'work_id': wid}); fresh = not work
                if fresh:
                    if not r.remaining_request(): return MediaError('TIMEOUT', 'prepare_media', 'state', 'DEADLINE_EXCEEDED')
                    result = await r.execute('register_occurrence_work', identity('register_work', wid), {'preparation_id': preparation_id,
                        'owner_generation': prep['owner_generation'], 'occurrence_id': oid, 'authorization_domain_id': self.policy.authorization_domain_id,
                        'profile_id': self.policy.profile_id, 'prompt_revision': self.policy.prompt_revision, 'interpretation_fingerprint': fingerprint})
                    if type(result) is not Committed: return result
                    fresh = result.source == 'NEW'
                    work = await self.media.rows.read('work_get', {'work_id': wid})
                if work[0]['phase'] == 'WAITING_ADMISSION' and work[0]['admission_preparation_id'] != preparation_id:
                    admitted = await r.execute('change_occurrence_admission', identity('readmit_media', wid, preparation_id), {
                        'work_id': wid, 'expected_revision': work[0]['revision'], 'action': 'REOPEN', 'preparation_id': preparation_id})
                    if type(admitted) is not Committed: return admitted
                    fresh = admitted.source == 'NEW'
                    work = await self.media.rows.read('work_get', {'work_id': wid})
                result = await self.drive(work[0], fresh=fresh)
                if type(result) is not Found or record(result.value)['state'] != 'RESULT_STORED': return result
        return Found(MappingProxyType({'state': 'MEDIA_READY'}))

    async def drive(self, work: MappingProxyType[str, Value], *, fresh: bool):
        try: return await self._drive(work, fresh=fresh)
        finally: self.release_ended_capabilities()

    async def _drive(self, work: MappingProxyType[str, Value], *, fresh: bool):
        r = self.runtime; wid = cast(str, work['work_id']); oid = cast(str, work['occurrence_id'])
        if work['phase'] == 'RESULT_STORED': return await self.cleanup(work)
        if work['phase'] == 'WAITING_ADMISSION': return Found(MappingProxyType({'state': 'WAITING_ADMISSION'}))
        descriptor = as_record(freeze(decode_content(cast(str, work['original_request_descriptor']).encode(), 8192), 8192))
        revision = r.gate.protection_revision
        observed = await self._observed(oid, cast(str, work['authorization_domain_id']))
        if observed is None: return MediaError('STORAGE_FAILED', 'prepare_media', 'storage', 'INTEGRITY_FAILURE')
        _, _, guard_key = observed
        guard = await self.media.rows.read('guards_get', {'guard_key': guard_key})
        if fresh and guard: fresh = False
        now = r.assembly.utc_now_us()
        if fresh and (now >= cast(int, work['deadline_at_us']) or now < cast(int, work['last_observed_at_us'])):
            return MediaError('TIMEOUT', 'prepare_media', 'state', 'DEADLINE_EXCEEDED')
        grant = WorkGrant('media', self.media.instance_id, None, 'MEDIA', (cast(str, work['profile_id']),), ('MEDIA_UNDERSTANDING',), 'media',
            'content_scheduler', (cast(str, descriptor['run_id']),), (cast(str, work['entry_id']),), prompt_revisions=(cast(str, work['prompt_revision']),))
        held = self._held.get(wid)
        if held:
            handle, grant, port = held
            from companion_memory.provider.resources import AuthorizedMedia
            from companion_memory.provider.ports import WorkPort
            handle = cast(AuthorizedMedia, handle); port = cast(WorkPort, port)
            authorized = StoredMediaAuthorized(handle, handle._artifact)
        else:
            admitted = r.gate.admit(grant, r.gate.epoch, guard_key, revision) if fresh else r.gate.admit_recovery(grant)
            if not admitted and fresh:
                fresh = False
                admitted = r.gate.admit_recovery(grant)
            if not admitted: return MediaError('MODE_BLOCKED', 'prepare_media', 'state', 'DREAMING')
            port = r.provider.bind_work(grant)
            authorized = await self.authority.authorize_stored_media(wid, oid)
            if type(authorized) is not StoredMediaAuthorized:
                r.provider.revoke(port); r.gate.revoke(grant); return authorized
            self._held[wid] = authorized.media, grant, port
        remaining = min(r.remaining_request(), max(0.001, (cast(int, work['deadline_at_us']) - now) / 1000000)) if fresh else self.media.settings.integer('media.operation_timeout_ms') / 1000
        raw = {**descriptor, 'entry_ids': list(cast(tuple, descriptor['entry_ids'])), 'deadline': time.monotonic() + remaining,
            'cancellation': CancellationSource().token, 'payload': {'media': authorized.media, 'task': work['task'], 'modality': work['modality']}}
        if fresh:
            result = await port.understand_media(raw)
            rid = result.record['object_id'] if type(result) is Completed else result.reference['request_id'] if type(result) is Pending else None
        else:
            result = await port.lookup_request('understand_media', raw)
            rid = as_record(as_record(result.value)['request'])['object_id'] if type(result) is ProviderFound else None
        if rid is None:
            parked = await self.close_if_unsent(work, port, raw)
            return parked if parked is not None else self.provider_observation(result, 'ORIGINAL_REQUEST_UNCONFIRMED', port)
        if work['provider_request_id'] is None:
            associated = await r.execute('associate_occurrence_request', identity('associate_media', wid, cast(str, rid)),
                {'work_id': wid, 'request_id': rid, 'expected_revision': work['revision']})
            if type(associated) is not Committed: return associated
            work = (await self.media.rows.read('work_get', {'work_id': wid}))[0]
        elif work['provider_request_id'] != rid:
            return MediaError('STORAGE_FAILED', 'prepare_media', 'storage', 'INTEGRITY_FAILURE')
        owner = r.provider.bind_result_owner(ResultGrant('media', (cast(str, rid),)))
        try: terminal = await owner.verify_terminal(rid, descriptor)
        finally: r.provider.revoke(owner)
        if type(terminal) is not TerminalVerified: return self.provider_observation(terminal, 'REMOTE_UNKNOWN', port)
        if not terminal.value.confirmed_sent or terminal.value.request['outcome'] == 'MODE_BLOCKED':
            parked = await self.close_if_unsent(work, port, raw)
            return parked if parked is not None else Found(MappingProxyType({'state': 'WAITING_ADMISSION'}))
        try: self.media.work.retain_terminal(terminal.value)
        except ValueError:
            return MediaError('RESOURCE_BUSY', 'prepare_media', 'state', 'ADMISSION_FULL', True)
        sensitive = terminal.value.terminal_reason == 'SENSITIVE_INFORMATION'
        if sensitive:
            try: r.gate.close_guard(guard_key)
            except ValueError:
                if ('terminal', cast(str, rid)) not in r._evidence_jobs.values(): self.media.work.evidence.pop(cast(str, rid), None)
                return MediaError('RESOURCE_BUSY', 'prepare_media', 'state', 'ADMISSION_FULL', True)
        saved = await r.execute('store_occurrence_result', identity('store_media', wid, cast(str, rid)),
            {'work_id': wid, 'request_id': rid, 'expected_revision': work['revision']})
        if type(saved) is not Committed: return saved
        if sensitive: r.gate.guard_committed(guard_key)
        return await self.cleanup((await self.media.rows.read('work_get', {'work_id': wid}))[0])

    async def close_if_unsent(self, work, port, original):
        """Keep unknown work protected; only the Provider can attest no dispatch."""
        from companion_memory.provider.unsent_evidence import UnsentVerified
        verified = await port.verify_unsent('understand_media', original)
        if type(verified) is not UnsentVerified: return None
        self.release_ended_capabilities()
        if work['work_id'] in self._held: return None
        self.media.work.unsent_evidence[work['work_id']] = verified.value
        closed = await self.runtime.execute('change_occurrence_admission', identity('close_media_admission', work['work_id'], work['admission_generation']),
            {'work_id': work['work_id'], 'expected_revision': work['revision'], 'action': 'CLOSE', 'preparation_id': work['admission_preparation_id']})
        if type(closed) is not Committed: return closed
        return Found(MappingProxyType({'state': 'WAITING_ADMISSION', 'remote_result': 'NOT_SENT', 'cleanup_pending': False}))

    def provider_observation(self, result: object, pending_state: str, port: WorkPort) -> Found | MediaError:
        """Translate Provider evidence without exposing its error object to media callers."""
        from companion_memory.provider import Pending, NotFound as ProviderNotFound
        pending = not port.consumers_ended() or bool(getattr(getattr(result, 'error', None), 'cleanup_pending', False))
        if type(result) in (Pending, ProviderNotFound):
            return Found(MappingProxyType({'state': pending_state, 'remote_result': 'UNKNOWN', 'cleanup_pending': pending}))
        error = getattr(result, 'error', None)
        code = getattr(error, 'code', None)
        if code in ('MODE_BLOCKED', 'RESOURCE_BUSY', 'BUDGET_EXHAUSTED'):
            return Found(MappingProxyType({'state': 'WAITING_ADMISSION', 'cleanup_pending': pending}))
        return MediaError('STORAGE_FAILED', 'prepare_media', 'storage', 'SYSTEM_BLOCKED', pending)

    async def cleanup(self, work: MappingProxyType[str, Value]):
        r = self.runtime; wid = cast(str, work['work_id'])
        held = self._held.get(wid)
        if held:
            media, grant, port = held
            if not self.authority.release_media_authorization(media):
                return MediaError('RESOURCE_BUSY', 'prepare_media', 'state', 'OWNER_ACTIVE', True)
            r.provider.revoke(port); r.gate.revoke(grant); self._held.pop(wid)
        rid = identity('media_ref', 'PROCESSING', wid, work['blob_id'], work['generation'], work['occurrence_id'])
        if await self.media.rows.read('references_get', {'reference_id': rid}):
            released = await r.execute('release_occurrence_processing', identity('release_processing', wid), {'work_id': wid})
            if type(released) is not Committed: return released
        return Found(MappingProxyType({'state': 'RESULT_STORED', 'interpretation_id': work['interpretation_id']}))


async def complete_preparation(assembly, execute, preparation_id):
    """Choose the fixed pinning branch before requesting the durable READY snapshot."""
    prep = (await assembly.rows.read('preparations_get', {'preparation_id': preparation_id}))[0]
    changed = False
    if assembly.media is not None:
        source = decode_preparation(prep['manifest'])
        for member in sequence(source['ordered_members']):
            for item in sequence(record(member)['media']):
                selected = record(item)
                occurrence = (await assembly.media.rows.read('occurrences_get', {'occurrence_id': selected['occurrence_id']}))[0]
                if occurrence['interpretation_id'] is None: return MediaError('PRECONDITION_FAILED', 'prepare_media', 'state', 'INTERPRETATION_MISSING')
                if occurrence['interpretation_id'] != selected['interpretation_id']: changed = True
    if prep['phase'] == 'MEDIA_READY' and not changed: return Found(MappingProxyType({'state': 'MEDIA_READY'}))
    from .content_assembly import stable
    return await execute('complete_content_preparation' + ('_with_media' if changed else ''), stable('media_ready', preparation_id, prep['revision']),
        {'preparation_id': preparation_id, 'expected_revision': prep['revision'], 'owner_generation': prep['owner_generation']})
