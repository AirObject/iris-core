"""Provider-owned immutable handoff leaf declarations, tied to original roots.

Receipt confirmation cannot delete these leaves; only bounded retirement after
consumer release may remove them. The original Provider ledger remains owner.
"""
from companion_memory.persistence.semantic_catalog import SemanticTable,catalog,projection
from .embedding_schema import HANDOFF_LEAF,validate_leaf

TABLES=(SemanticTable('embedding_handoff_leaf',(HANDOFF_LEAF,),validate_leaf,
    (projection('handoff_id'),projection('request_id'),projection('attempt_id'),projection('ordinal',numeric=True)),
    ('UNIQUE(scope_id,handoff_id,ordinal)','CHECK(ordinal BETWEEN 0 AND 9)','CHECK(revision=1)',
     'FOREIGN KEY(scope_id,handoff_id) REFERENCES provider_handoffs(scope_id,object_id)',
     'FOREIGN KEY(scope_id,request_id) REFERENCES provider_requests(scope_id,object_id)',
     'FOREIGN KEY(scope_id,attempt_id) REFERENCES provider_attempts(scope_id,object_id)'),False,True),)


def embedding_handoff_catalog():
    return catalog('provider',TABLES)
