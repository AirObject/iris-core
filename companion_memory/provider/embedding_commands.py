"""Provider-owned complete-result handoff and separately bounded retirement.

None of these payloads contains a response or vector. Only the retained original
execution owner can materialize terminal accounting and immutable result leaves.
"""
from companion_memory.persistence.semantic_records import ID,N,P,H,REQUEST,RECEIPT,record
PAYLOADS={
 'store_embedding_handoff':(record(request_ref=REQUEST,original_request_digest=H,terminal_evidence_ref=RECEIPT),),
 'confirm_embedding_handoff':(record(request_ref=REQUEST,receipt=RECEIPT,artifact_id=ID),),
 'retire_embedding_handoff':(record(request_ref=REQUEST,expected_handoff_revision=P,after_ordinal=(N,)),),
}
WRITERS={kind:('provider',) for kind in PAYLOADS}
READERS={kind:('retrieval',) if kind=='confirm_embedding_handoff' else () for kind in PAYLOADS}
