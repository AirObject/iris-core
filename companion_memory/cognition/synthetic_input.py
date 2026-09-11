"""Explicit finite synthetic proposals transformed from an original handoff.

This input participant does not apply memory changes or substitute a data owner.
Its complete bounded input fingerprint is retained with original work so recovery
cannot silently use a different proposal source under the same transform version.
"""
from dataclasses import dataclass
import hashlib
from types import MappingProxyType
from typing import cast
from companion_memory.configuration.content_persistence import StoredContentConfiguration
from companion_memory.persistence import Value
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.schema import valid_identifier, InvalidValue
from companion_memory.memory.changes import isolate_change
from companion_memory.memory.formats import record, sequence
from companion_memory.provider.terminal_evidence import VerifiedTerminal, issued_terminal
from .candidates import Candidate, stable_identity, manifest_digest, isolate_candidate


@dataclass(frozen=True, slots=True)
class SyntheticCandidateInput:
    """Explicit candidate bodies and belief, with no implicit successful results."""
    transform_version: str
    bodies: tuple[str, ...]
    belief: int

    def __post_init__(self):
        if (not valid_identifier(self.transform_version) or type(self.bodies) is not tuple or len(self.bodies) > 8
                or any(type(body) is not str or not body.strip() or len(body.encode()) > 2048 for body in self.bodies)
                or type(self.belief) is not int or not 0 <= self.belief <= 100): raise ValueError('Explicit finite synthetic candidate input is required.')

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(encode_content((self.transform_version, self.bodies, self.belief), 20000)).hexdigest()

    def build(self, configuration: StoredContentConfiguration, instance_id: str, source: MappingProxyType[str, Value],
              generation: int, terminal: VerifiedTerminal) -> Candidate:
        """Deterministically recreate the same complete proposal from original work."""
        if not issued_terminal(terminal) or terminal.database_id != configuration.database_id: raise InvalidValue()
        request = terminal.request; rid = cast(str, request['object_id']); handoff = cast(str | None, request['handoff_id'])
        batch = cast(str, source['batch_id']); origin_ref = handoff or rid
        cid = stable_identity('candidate', configuration.database_id, batch, origin_ref, self.transform_version, 0, 'CANDIDATE')
        proposal = 'SUCCEEDED' if request['outcome'] == 'SUCCEEDED' else 'SENSITIVE_DROPPED' if request['outcome'] == 'SENSITIVE_REFUSAL' else 'FAILED_DROPPED'
        members = tuple(record(m) for m in sequence(source['ordered_members']))
        target = next(m for m in members if m['role'] == 'T')
        leaves = []
        settings = configuration.candidate.content
        for ordinal, body in enumerate(self.bodies if proposal == 'SUCCEEDED' else ()):
            oid = stable_identity('object', configuration.database_id, batch, origin_ref, self.transform_version, ordinal, 'MEMORY')
            value = {'object_version': 1, 'object_id': oid, 'instance_id': instance_id, 'kind': 'MEMORY', 'revision': 1,
                'created_at_us': source['frozen_at_us'], 'modified_at_us': source['frozen_at_us'], 'lifecycle': 'FORGOTTEN' if settings.integer('memory.initial_retention') < settings.integer('memory.forget_below') else 'ACTIVE',
                'forgotten_since_us': source['frozen_at_us'] if settings.integer('memory.initial_retention') < settings.integer('memory.forget_below') else None,
                'retention_policy_ref': 'configured_retention:1', 'content': {'category': 'FACT', 'body': body, 'subject_ids': [], 'speaker_subject_id': None,
                    'stance': 'UNCERTAIN', 'world_scope': {'kind': 'REAL', 'context_id': None}, 'occurred_range': None, 'applicable_range': None},
                'scores': {'belief': self.belief, 'retention': settings.integer('memory.initial_retention'), 'scale_id': 'acceptance_100_v1',
                    'belief_reason': 'Explicit synthetic proposal belief.', 'retention_reason': 'Configured initial retention.', 'score_basis': []},
                'origin': {'kind': 'DIRECT_LEARNING', 'candidate_id': cid, 'batch_id': batch, 'actor_ref': 'content_scheduler', 'model_origin': 'SIMULATED', 'candidate_origin': 'SYNTHETIC'}}
            links = {'sources': [{'object_id': oid, 'object_revision': 1, 'source_id': source['source_id'], 'link_role': 'DIRECT',
                'target_anchors': [{'message_id': target['message_id'], 'part': 'EVENT', 'item_index': None, 'start_utf8': None, 'end_utf8': None,
                    'occurrence_id': None, 'interpretation_id': None}], 'auxiliary_refs': []}], 'bases': []}
            leaves.append(isolate_change({'change_version': 1, 'action': 'CREATE_MEMORY', 'target_id': oid, 'expected_revision': None,
                'proposed_value': value, 'links': links}, settings.integer('cognition.candidate_item_max_bytes')))
        manifest: dict[str, Value] = {'candidate_version': 1, 'candidate_id': cid, 'batch_id': batch, 'run_id': source['run_id'],
            'work_generation': generation, 'config_snapshot_id': configuration.snapshot_id, 'provider_request_id': rid, 'handoff_ref': handoff,
            'transform_version': self.transform_version, 'source_id': source['source_id'], 'manifest_digest': 'pending', 'terminal_proposal': proposal,
            'ordered_change_refs': tuple(MappingProxyType({'ordinal': i, 'target_id': leaf['target_id'], 'action': leaf['action'],
                'digest': hashlib.sha256(encode_content(leaf, 8192)).hexdigest()}) for i, leaf in enumerate(leaves)),
            'origin': MappingProxyType({'storage_execution': 'ACTUAL', 'model_adapter': 'SIMULATED', 'candidate_origin': 'SYNTHETIC', 'database_id': configuration.database_id})}
        manifest['manifest_digest'] = manifest_digest(MappingProxyType(manifest))
        return isolate_candidate(manifest, tuple(leaves), item_limit=settings.integer('cognition.candidate_item_limit'),
            item_bytes=settings.integer('cognition.candidate_item_max_bytes'), total_bytes=settings.integer('cognition.candidate_max_bytes'))
