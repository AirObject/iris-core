"""Closed reviewed-set transactions, retaining exact source and object bodies.

A payload never grants reviewer authority. Establishment declares the required
memory participant; each business writer supplies its own mandatory audit.
"""
from companion_memory.persistence.semantic_records import ID,P,H,CONFIG,record,integer
from companion_memory.persistence.schema import BoundedTextSchema
F={'set_id':ID,'expected_revision':P}
PAYLOADS={
 'fixed_begin':(record(set_id=ID,config=CONFIG,manifest_digest=H,review_ref=ID,review_digest=H),),
 'fixed_add_member':(record(**F,ordinal=integer(0,4095),member_id=ID,event_json=BoundedTextSchema(2048),
                          memory_json=BoundedTextSchema(4096),content_digest=H),),
 'fixed_seal':(record(**F),),
 'fixed_establish':(record(**F,ordinal=integer(0,4095),expected_member_revision=P),),
}
WRITERS={kind:('cognition','memory') if kind=='fixed_establish' else ('cognition',) for kind in PAYLOADS}
READERS={kind:('memory',) if kind=='fixed_add_member' else () for kind in PAYLOADS}
