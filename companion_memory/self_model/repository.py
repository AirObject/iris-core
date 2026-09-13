"""Three fixed persona tables, bounded reads and genuine uniqueness constraints.

Historical candidates survive retry. The instance has one initial run and one
publication; only reviewed candidates may be published by the semantic owner.
"""
from companion_memory.persistence.text_records import RecordTable, IndexSpec, record_catalog


def persona_catalog():
    """Declare the closed initial-persona statement set before storage opens."""
    return record_catalog('self_model',1,(
        RecordTable('initial_persona_runs',4096,True,(
            IndexSpec('by_instance',('scope_id',)),IndexSpec('by_operation',('provider_operation_key',),point_read=False))),
        RecordTable('initial_persona_candidates',4096,True,(
            IndexSpec('by_run_generation',('run_id','generation')),
            IndexSpec('by_operation',('provider_operation_key',),point_read=False),
            IndexSpec('by_request',('provider_request_id',),nonnull='provider_request_id'))),
        RecordTable('persona_publications',4096,False,(
            IndexSpec('by_instance',('scope_id',)),IndexSpec('by_candidate',('candidate_id',),point_read=False),
            IndexSpec('by_run',('run_id',),point_read=False))),))
