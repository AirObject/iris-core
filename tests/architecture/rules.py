"""Explicit package collaboration and narrow lower-layer dependency rules.

Package collaboration does not authorize arbitrary database or model access.
The narrower rules protect shared primitives, provider content abstraction,
permission projection and the configuration snapshot/storage integration.
"""

COMMON = frozenset({'configuration', 'persistence', 'logging_service'})
ALLOWED = {
    'buffers': COMMON | {'ingress', 'media', 'memory', 'runtime'},
    'cognition': COMMON | {'dream', 'goals', 'ingress', 'media', 'memory', 'provider', 'retrieval', 'runtime', 'self_model'},
    'configuration': {'logging_service', 'memory', 'persistence', 'provider', 'retrieval', 'runtime'},
    'dream': COMMON | {'cognition', 'information', 'memory', 'runtime', 'self_model'},
    'goals': COMMON | {'cognition', 'information', 'ingress', 'provider', 'runtime'},
    'information': COMMON | {'cognition', 'goals', 'memory', 'provider', 'retrieval', 'runtime', 'state'},
    'ingress': COMMON | {'media', 'memory', 'runtime'},
    'logging_service': {'configuration', 'memory', 'persistence'},
    'management': COMMON | {'dream', 'goals', 'information', 'media', 'retrieval', 'runtime'},
    'media': COMMON | {'ingress', 'memory', 'provider', 'runtime'},
    'memory': COMMON | {'cognition', 'dream', 'goals', 'ingress', 'media', 'retrieval', 'runtime'},
    'persistence': {'cognition', 'configuration', 'ingress', 'logging_service', 'media', 'provider', 'retrieval', 'runtime'},
    'provider': COMMON | {'cognition', 'ingress', 'memory', 'retrieval', 'runtime'},
    'retrieval': COMMON | {'goals', 'information', 'memory', 'provider', 'runtime', 'self_model', 'state'},
    'runtime': COMMON | {'buffers', 'cognition', 'dream', 'goals', 'information', 'ingress', 'management', 'media', 'memory', 'provider', 'retrieval', 'self_model', 'state'},
    'self_model': COMMON | {'cognition', 'dream', 'ingress', 'memory', 'provider', 'runtime'},
    'state': COMMON,
}

# Storage validates immutable configuration and native format/capacity issuers.
# These are explicit integration exceptions, not a package-wide back edge.
CONFIGURATION_READS = {
    'companion_memory.persistence._settings': {'companion_memory.configuration'},
    'companion_memory.persistence.service': {
        'companion_memory.configuration',
        'companion_memory.configuration.daily_persistence',
        'companion_memory.configuration.dream_persistence',
        'companion_memory.configuration.managed_persistence',
        'companion_memory.configuration.execution_versions',
    },
    'companion_memory.persistence.command_capacity': {
        'companion_memory.configuration.text_persistence',
        'companion_memory.configuration.semantic_persistence',
        'companion_memory.configuration.daily_persistence',
        'companion_memory.configuration.dream_persistence',
        'companion_memory.configuration.managed_persistence',
        'companion_memory.configuration.text_codec',
        'companion_memory.configuration.semantic_codec',
        'companion_memory.configuration.semantic_schema',
        'companion_memory.configuration.content_codec',
    },
    'companion_memory.persistence.semantic_admission': {
        'companion_memory.configuration',
        'companion_memory.configuration.daily_resolution',
        'companion_memory.configuration.managed_resolution',
        'companion_memory.configuration.dream_resolution',
        'companion_memory.configuration.semantic_resolution',
    },
}
SNAPSHOT_NAMES = {'EffectiveSnapshot', 'PresentValue', 'persistence_snapshot_issue'}
PRIMITIVES = {
    'companion_memory.persistence.record_primitives',
    'companion_memory.persistence.record_repository',
}


def violation(source: str, target: str) -> str | None:
    parts = target.split('.')
    if len(parts) < 2 or parts[0] != 'companion_memory':
        return None
    owner = source.split('.')[1]
    dependency = parts[1]
    if dependency not in ALLOWED:
        return 'unregistered implementation package'
    if source == 'companion_memory.provider.media_input' and dependency != 'persistence':
        return 'Provider input ports may only depend on transaction/completion contracts'
    if source in PRIMITIVES:
        if dependency != owner and dependency != 'persistence':
            return 'shared contracts cannot depend on a business owner'
    if source.startswith('companion_memory.persistence.') and dependency == 'configuration':
        allowed = CONFIGURATION_READS.get(source, set())
        # ImportFrom emits the module and its imported symbols separately.
        base = target.rpartition('.')[0]
        if target in allowed:
            return None
        if base in allowed and (base != 'companion_memory.configuration' or parts[-1] in SNAPSHOT_NAMES):
            return None
        return 'storage may only read its declared configuration snapshot ports'
    if target.startswith(('companion_memory.information.records', 'companion_memory.information.repository')):
        return 'use persistence record contracts instead of compatibility imports'
    if dependency != owner and dependency not in ALLOWED.get(owner, set()):
        return f'{owner} cannot depend on {dependency}'
    return None
