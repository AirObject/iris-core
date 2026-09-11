"""Explicit immutable mutation candidates with finite original read authority.

The input supplies complete revision-checked changes. It does not read or alter
formal objects, resolve conflicts, or regenerate proposed identities on recovery.
"""
from dataclasses import dataclass
import hashlib
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import Value, RecordSchema, Field, SequenceSchema
from companion_memory.memory.formats import ID
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.schema import valid_identifier, ScalarSchema
from companion_memory.memory.changes import isolate_change
from .candidates import Candidate, isolate_candidate, manifest_digest
from .synthetic_input import SyntheticCandidateInput


AUTHORITY = RecordSchema(tuple(Field(name, SequenceSchema(ID, 0, 16)) for name in ('readable_objects', 'readable_subjects', 'source_ids', 'writable_objects')) + (Field('allow_creations', ScalarSchema('boolean')),))

@dataclass(frozen=True, slots=True)
class SyntheticMutationInput:
    """Trusted finite mutation proposal and explicit scope persisted before dispatch."""
    transform_version: str
    changes: tuple[MappingProxyType[str, Value], ...]
    readable_objects: tuple[str, ...] = ()
    readable_subjects: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()

    def __post_init__(self):
        if not valid_identifier(self.transform_version) or type(self.changes) is not tuple or not 1 <= len(self.changes) <= 8:
            raise ValueError('Finite explicit mutation input required.')
        changes = tuple(isolate_change(change, 8192) for change in self.changes)
        if len({cast(str, c['target_id']) for c in changes}) != len(changes) or any(c['action'] not in ('REPLACE_CURRENT', 'SET_SCORES', 'DELETE_OBJECT') for c in changes):
            raise ValueError('Revision checked object mutations are required.')
        for scope in (self.readable_objects, self.readable_subjects, self.source_ids):
            if type(scope) is not tuple or len(scope) > 16 or len(set(scope)) != len(scope) or any(not valid_identifier(v) for v in scope):
                raise ValueError('Finite explicit object/source read authority required.')
        object.__setattr__(self, 'changes', changes)
        encode_content(changes, 73728)

    @property
    def authority(self):
        return MappingProxyType({'allow_creations': False, 'readable_objects': self.readable_objects, 'readable_subjects': self.readable_subjects,
            'source_ids': self.source_ids, 'writable_objects': tuple(c['target_id'] for c in self.changes)})

    @property
    def fingerprint(self):
        return hashlib.sha256(encode_content((self.transform_version, self.changes, self.authority), 81920)).hexdigest()

    def build(self, configuration, instance_id, source, generation, terminal) -> Candidate:
        base = SyntheticCandidateInput(self.transform_version, (), 50).build(configuration, instance_id, source, generation, terminal)
        if base.manifest['terminal_proposal'] != 'SUCCEEDED': return base
        manifest = {**base.manifest, 'ordered_change_refs': tuple(MappingProxyType({'ordinal': ordinal, 'target_id': change['target_id'],
            'action': change['action'], 'digest': hashlib.sha256(encode_content(change, 8192)).hexdigest()}) for ordinal, change in enumerate(self.changes))}
        manifest['manifest_digest'] = manifest_digest(MappingProxyType(manifest))
        settings = configuration.candidate.content
        return isolate_candidate(manifest, self.changes, item_limit=settings.integer('cognition.candidate_item_limit'),
            item_bytes=settings.integer('cognition.candidate_item_max_bytes'), total_bytes=settings.integer('cognition.candidate_max_bytes'))
