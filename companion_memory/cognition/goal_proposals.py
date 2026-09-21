"""Closed formed goal proposals alongside original synthetic memory mutations.

Proposal identities and source attribution derive from the verified candidate;
these leaves grant no write authority and cannot commit a standalone goal.
"""
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import Field, RecordSchema, ScalarSchema, Value
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.schema import InvalidValue, valid_identifier
from companion_memory.configuration.content_persistence import StoredContentConfiguration
from companion_memory.provider.terminal_evidence import VerifiedTerminal
from companion_memory.memory.formats import ID, VERSION, enum, isolate
from companion_memory.goals.inputs import INJECT
from companion_memory.persistence.record_primitives import Record, record
from .synthetic_mutations import SyntheticMutationInput
from .synthetic_mixed import SyntheticMixedInput
from .candidates import Candidate, isolate_candidate, manifest_digest, stable_identity

PROPOSAL = RecordSchema(tuple(f for f in INJECT.fields if f.name != 'source_id') + (Field('basis_id', ID),))
GOAL_CHANGE = RecordSchema((Field('change_version', VERSION), Field('action', enum('CREATE_GOAL')), Field('target_id', ID),
    Field('expected_revision', ScalarSchema('integer', 0, 0), nullable=True), Field('proposed_value', PROPOSAL), Field('links', ScalarSchema('integer', 0, 0), nullable=True)))


def isolate_goal_change(raw: object, limit: int) -> Record:
    value = isolate(GOAL_CHANGE, raw, limit)
    if value['expected_revision'] is not None or value['links'] is not None: raise InvalidValue()
    return value


@dataclass(frozen=True, slots=True)
class SyntheticGoalInput:
    """Explicit formed goals with real original mutation/history effects."""
    memory: SyntheticMutationInput | SyntheticMixedInput
    proposals: tuple[Record, ...]
    route_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.memory) not in (SyntheticMutationInput, SyntheticMixedInput) or type(self.proposals) is not tuple or not 1 <= len(self.proposals) <= 7:
            raise ValueError('Explicit memory mutations and bounded formed goals are required.')
        if type(self.route_ids) is not tuple or len(self.route_ids) > 16 or len(set(self.route_ids)) != len(self.route_ids):
            raise ValueError('Finite declared reminder routes are required.')
        if any(not valid_identifier(route) for route in self.route_ids): raise ValueError('Declared reminder route identities are required.')
        memory = self.memory
        if type(memory) is SyntheticMutationInput: count = len(memory.changes)
        elif type(memory) is SyntheticMixedInput: count = len(memory.mutations.changes) + len(memory.graph.proposals)
        else: raise ValueError('Explicit native memory input is required.')
        if count + len(self.proposals) > 8: raise ValueError('The complete candidate must fit its finite leaf limit.')
        values = tuple(isolate(PROPOSAL, p, 4096) for p in self.proposals)
        if any(p['route_id'] is not None and p['route_id'] not in self.route_ids for p in values):
            raise ValueError('A goal route must have explicit native authority.')
        object.__setattr__(self, 'proposals', values)

    @property
    def transform_version(self) -> str: return self.memory.transform_version

    @property
    def authority(self) -> Record: return self.memory.authority

    @property
    def fingerprint(self) -> str:
        return sha256(encode_content((self.memory.fingerprint, self.proposals, self.route_ids), 32768)).hexdigest()

    def build(self, configuration: StoredContentConfiguration, instance_id: str, source: Record, generation: int, terminal: VerifiedTerminal) -> Candidate:
        candidate = self.memory.build(configuration, instance_id, source, generation, terminal)
        if candidate.manifest['terminal_proposal'] != 'SUCCEEDED': return candidate
        m = candidate.manifest
        if len(candidate.leaves) + len(self.proposals) > 8: raise InvalidValue()
        goals = tuple(isolate_goal_change({'change_version': 1, 'action': 'CREATE_GOAL', 'target_id': stable_identity('goal',
            cast(str, record(m['origin'])['database_id']), cast(str, m['batch_id']), cast(str, m['handoff_ref']), self.transform_version,
            len(candidate.leaves) + ordinal, 'GOAL'), 'expected_revision': None, 'proposed_value': proposal, 'links': None}, 8192)
            for ordinal, proposal in enumerate(self.proposals))
        leaves = candidate.leaves + goals
        manifest: dict[str, Value] = dict(m)
        manifest.update(candidate_version=2, ordered_change_refs=tuple(MappingProxyType({'ordinal': ordinal, 'target_id': leaf['target_id'],
            'action': leaf['action'], 'digest': sha256(encode_content(leaf, 8192)).hexdigest()}) for ordinal, leaf in enumerate(leaves)))
        manifest['manifest_digest'] = manifest_digest(MappingProxyType(manifest))
        settings = configuration.candidate.content
        return isolate_candidate(manifest, leaves, item_limit=settings.integer('cognition.candidate_item_limit'),
            item_bytes=settings.integer('cognition.candidate_item_max_bytes'), total_bytes=settings.integer('cognition.candidate_max_bytes'), allow_goals=True)
