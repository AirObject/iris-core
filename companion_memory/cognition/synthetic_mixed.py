"""Explicit finite creations and existing-object mutations in one original candidate."""
from dataclasses import dataclass
import hashlib
from types import MappingProxyType
from companion_memory.persistence.content_codec import encode_content
from .synthetic_graph import SyntheticGraphInput
from .synthetic_mutations import SyntheticMutationInput
from .candidates import isolate_candidate, manifest_digest


@dataclass(frozen=True, slots=True)
class SyntheticMixedInput:
    """Trusted proposal input; actual owners alone validate and apply its whole set."""
    graph: SyntheticGraphInput
    mutations: SyntheticMutationInput

    def __post_init__(self):
        if (type(self.graph) is not SyntheticGraphInput or type(self.mutations) is not SyntheticMutationInput
                or self.graph.transform_version != self.mutations.transform_version
                or len(self.graph.proposals) + len(self.mutations.changes) > 8):
            raise ValueError('One bounded original transform must own all mixed proposals.')

    @property
    def transform_version(self): return self.graph.transform_version

    @property
    def authority(self): return MappingProxyType({**self.mutations.authority, 'allow_creations': True})

    @property
    def fingerprint(self):
        return hashlib.sha256(encode_content((self.graph.fingerprint, self.mutations.fingerprint), 1024)).hexdigest()

    def build(self, configuration, instance_id, source, generation, terminal):
        created = self.graph.build(configuration, instance_id, source, generation, terminal)
        if created.manifest['terminal_proposal'] != 'SUCCEEDED': return created
        leaves = created.leaves + self.mutations.changes
        manifest = {**created.manifest, 'ordered_change_refs': tuple(MappingProxyType({'ordinal': ordinal, 'target_id': leaf['target_id'],
            'action': leaf['action'], 'digest': hashlib.sha256(encode_content(leaf, 8192)).hexdigest()}) for ordinal, leaf in enumerate(leaves))}
        manifest['manifest_digest'] = manifest_digest(MappingProxyType(manifest))
        settings = configuration.candidate.content
        return isolate_candidate(manifest, leaves, item_limit=settings.integer('cognition.candidate_item_limit'),
            item_bytes=settings.integer('cognition.candidate_item_max_bytes'), total_bytes=settings.integer('cognition.candidate_max_bytes'))
