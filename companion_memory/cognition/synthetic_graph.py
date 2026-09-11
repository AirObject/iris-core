"""Explicit finite subject, fact and relation proposals for the actual owners.

Aliases resolve only within this submitted graph. Stable identities derive from
the original handoff and ordinal, not labels, platform similarity or a new run.
This participant supplies candidates; it has no formal-memory write capability.
"""
from dataclasses import asdict, dataclass
import hashlib
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import Value
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.schema import InvalidValue, valid_identifier
from companion_memory.memory.changes import isolate_change
from companion_memory.memory.formats import record, sequence
from .candidates import Candidate, stable_identity, isolate_candidate, manifest_digest
from .synthetic_input import SyntheticCandidateInput


@dataclass(frozen=True, slots=True)
class SubjectProposal:
    alias: str
    kind: str
    label: str
    platform_id: str | None
    external_subject_id: str | None


@dataclass(frozen=True, slots=True)
class FactProposal:
    alias: str
    body: str
    subject_aliases: tuple[str, ...]
    world_kind: str
    context_alias: str | None


@dataclass(frozen=True, slots=True)
class RelationProposal:
    alias: str
    relation_type: str
    from_alias: str
    to_alias: str
    assertion: str
    world_kind: str
    context_alias: str | None


@dataclass(frozen=True, slots=True)
class SyntheticGraphInput:
    """Complete explicit proposal graph; it never infers a relation from a label."""
    transform_version: str
    proposals: tuple[SubjectProposal | FactProposal | RelationProposal, ...]
    belief: int

    def __post_init__(self):
        if (not valid_identifier(self.transform_version) or type(self.proposals) is not tuple or not 1 <= len(self.proposals) <= 8
                or any(type(p) not in (SubjectProposal, FactProposal, RelationProposal) or not valid_identifier(p.alias) for p in self.proposals)
                or len({p.alias for p in self.proposals}) != len(self.proposals) or type(self.belief) is not int or not 0 <= self.belief <= 100):
            raise ValueError('An explicit finite graph is required.')
        # Isolate the complete input before Provider admission. Nested mutable
        # carriers and unresolved aliases cannot alter a paid transformation.
        aliases = {p.alias for p in self.proposals}
        for p in self.proposals:
            if isinstance(p, FactProposal):
                if type(p.subject_aliases) is not tuple or any(type(a) is not str or a not in aliases for a in p.subject_aliases): raise ValueError('Invalid subject references.')
            if isinstance(p, RelationProposal) and (p.from_alias not in aliases or p.to_alias not in aliases): raise ValueError('Invalid relation references.')
            if isinstance(p, (FactProposal, RelationProposal)) and p.context_alias is not None and p.context_alias not in aliases: raise ValueError('Invalid scene reference.')
        encode_content(self.values(), 73728)

    def values(self):
        return MappingProxyType({'transform_version': self.transform_version, 'belief': self.belief,
            'proposals': tuple(MappingProxyType({'kind': type(p).__name__, 'value': MappingProxyType(asdict(p))}) for p in self.proposals)})

    @property
    def fingerprint(self): return hashlib.sha256(encode_content(self.values(), 73728)).hexdigest()

    def build(self, configuration, instance_id, source, generation, terminal) -> Candidate:
        # The existing native transform validates the same original terminal and
        # supplies the common immutable candidate/origin envelope, including zero.
        empty = SyntheticCandidateInput(self.transform_version, (), self.belief).build(configuration, instance_id, source, generation, terminal)
        if empty.manifest['terminal_proposal'] != 'SUCCEEDED': return empty
        manifest = dict(empty.manifest); handoff = cast(str, manifest['handoff_ref'] or manifest['provider_request_id'])
        ids = {p.alias: stable_identity('subject' if type(p) is SubjectProposal else 'object', configuration.database_id,
            source['batch_id'], handoff, self.transform_version, i, 'SUBJECT' if type(p) is SubjectProposal else 'MEMORY' if type(p) is FactProposal else 'RELATION')
            for i, p in enumerate(self.proposals)}
        types = {p.alias: 'SUBJECT' if type(p) is SubjectProposal else 'OBJECT' for p in self.proposals}
        target = next(record(m) for m in sequence(source['ordered_members']) if record(m)['role'] == 'T')
        settings = configuration.candidate.content
        leaves = []
        for p in self.proposals:
            oid = ids[p.alias]
            if isinstance(p, SubjectProposal):
                value = {'subject_version': 1, 'subject_id': oid, 'instance_id': instance_id, 'kind': p.kind,
                    'platform_id': p.platform_id, 'external_subject_id': p.external_subject_id, 'label': p.label, 'revision': 1}
                leaf = {'change_version': 1, 'action': 'REGISTER_SUBJECT', 'target_id': oid, 'expected_revision': None, 'proposed_value': value, 'links': None}
            else:
                context = None if p.context_alias is None else ids[p.context_alias]
                world = {'kind': p.world_kind, 'context_id': context}
                if isinstance(p, FactProposal):
                    content = {'category': 'FACT', 'body': p.body, 'subject_ids': [ids[a] for a in p.subject_aliases], 'speaker_subject_id': None,
                        'stance': 'UNCERTAIN', 'world_scope': world, 'occurred_range': None, 'applicable_range': None}
                else:
                    content = {'relation_type': p.relation_type, 'from_ref': {'type': types[p.from_alias], 'id': ids[p.from_alias], 'expected_revision': 1},
                        'to_ref': {'type': types[p.to_alias], 'id': ids[p.to_alias], 'expected_revision': 1}, 'assertion': p.assertion, 'world_scope': world}
                    if p.relation_type == 'SAME_SUBJECT' and ids[p.from_alias] > ids[p.to_alias]: content['from_ref'], content['to_ref'] = content['to_ref'], content['from_ref']
                value = {'object_version': 1, 'object_id': oid, 'instance_id': instance_id, 'kind': 'MEMORY' if type(p) is FactProposal else 'RELATION',
                    'revision': 1, 'created_at_us': source['frozen_at_us'], 'modified_at_us': source['frozen_at_us'], 'lifecycle': 'FORGOTTEN' if settings.integer('memory.initial_retention') < settings.integer('memory.forget_below') else 'ACTIVE',
                    'forgotten_since_us': source['frozen_at_us'] if settings.integer('memory.initial_retention') < settings.integer('memory.forget_below') else None, 'retention_policy_ref': 'configured_retention:1', 'content': content,
                    'scores': {'belief': self.belief, 'retention': settings.integer('memory.initial_retention'), 'scale_id': 'acceptance_100_v1',
                        'belief_reason': 'Explicit synthetic graph belief.', 'retention_reason': 'Configured initial retention.', 'score_basis': []},
                    'origin': {'kind': 'DIRECT_LEARNING', 'candidate_id': manifest['candidate_id'], 'batch_id': source['batch_id'], 'actor_ref': 'content_scheduler',
                        'model_origin': 'SIMULATED', 'candidate_origin': 'SYNTHETIC'}}
                links = {'sources': [{'object_id': oid, 'object_revision': 1, 'source_id': source['source_id'], 'link_role': 'DIRECT', 'target_anchors': [
                    {'message_id': target['message_id'], 'part': 'EVENT', 'item_index': None, 'start_utf8': None, 'end_utf8': None, 'occurrence_id': None, 'interpretation_id': None}],
                    'auxiliary_refs': []}], 'bases': []}
                leaf = {'change_version': 1, 'action': 'CREATE_MEMORY' if type(p) is FactProposal else 'CREATE_RELATION', 'target_id': oid,
                    'expected_revision': None, 'proposed_value': value, 'links': links}
            leaves.append(isolate_change(leaf, settings.integer('cognition.candidate_item_max_bytes')))
        manifest['ordered_change_refs'] = tuple(MappingProxyType({'ordinal': i, 'target_id': leaf['target_id'], 'action': leaf['action'],
            'digest': hashlib.sha256(encode_content(leaf, 8192)).hexdigest()}) for i, leaf in enumerate(leaves))
        manifest['manifest_digest'] = manifest_digest(MappingProxyType(manifest))
        return isolate_candidate(manifest, tuple(leaves), item_limit=settings.integer('cognition.candidate_item_limit'),
            item_bytes=settings.integer('cognition.candidate_item_max_bytes'), total_bytes=settings.integer('cognition.candidate_max_bytes'))
